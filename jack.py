#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Antigravity 2.0 运行时汉化工具 V2.0 —— CDP 注入版（macOS / Windows 适配 + 自动化守护）

V2.0 相对 V1.0 的改进：
- 零第三方依赖：内置迷你 WebSocket 客户端（MiniWS），不再需要 pip install websockets，
  系统 python3 直接可跑——launchd 自启不依赖任何环境
- 常驻守护模式（--daemon）：每 N 秒巡检一次，发现 Antigravity 启动或翻译引擎丢失
  （页面重载被冲掉）自动注入，全程无人工干预
- launchd 集成（--install / --uninstall）：登录后自动启动守护进程，KeepAlive 保证
  守护进程崩溃自动重启
- 状态查询（--status）：进程 / CDP 端口 / 引擎 / launchd 一览

原理（同 V1.0）：不修改任何安装文件，通过 CDP 把 MutationObserver 翻译引擎注入
渲染页面。想恢复英文，关掉 Antigravity 正常重开即可。

平台适配要点：
- macOS 安装路径 /Applications/Antigravity.app，进程检测用 ps；
  Windows 安装路径 %LOCALAPPDATA%\Programs\antigravity，进程检测用 tasklist
- Antigravity 忽略 --remote-debugging-port 参数，实际 CDP 端口随机分配，
  脚本自动扫描进程监听端口发现真实 CDP（macOS 用 lsof，Windows 用 netstat）
- 清除 ELECTRON_RUN_AS_NODE 环境变量（从 Electron 宿主派生的 shell 会带它，
  导致 Antigravity 以纯 Node 模式启动、拒绝 Chromium 参数）

用法：
  python3 jack.py                  # 手动：启动 Antigravity + 注入 + 守护 30 秒
  python3 jack.py --no-launch      # 只注入已运行的实例
  python3 jack.py --daemon         # 常驻守护模式（launchd / 注册表自启或手动后台跑）
  python3 jack.py --install        # 安装登录自启守护（macOS launchd / Windows Run 注册表项）
  python3 jack.py --uninstall      # 卸载登录自启守护
  python3 jack.py --status         # 查看状态
  python3 jack.py --check-syntax   # 语法与完整性自检
"""
import io
import os
import sys
import json
import time
import re
import argparse
import base64
import struct
import socket
import shutil
import subprocess
import http.client
import urllib.parse

# ★★★ 配置 ★★★
DAEMON_INTERVAL = 5          # 守护巡检间隔（秒）
IS_WINDOWS = sys.platform == 'win32'
if IS_WINDOWS:
    _CACHE_BASE = os.path.join(
        os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
        'com.nick.jack-hanhua')
else:
    _CACHE_BASE = os.path.expanduser('~/Library/Caches/com.nick.jack-hanhua')
DAEMON_LOG = (os.path.join(_CACHE_BASE, 'antigravity-hanhua.log') if IS_WINDOWS
              else os.path.expanduser('~/Library/Logs/antigravity-hanhua.log'))
PLIST_PATH = os.path.expanduser('~/Library/LaunchAgents/com.nick.antigravity-hanhua.plist')
LAUNCHD_LABEL = 'com.nick.antigravity-hanhua'
# Windows 开机自启注册表项（HKCU\...\Run 下的值名）
RUN_KEY_NAME = 'AntigravityHanhuaDaemon'
RUN_KEY_PATH = r'Software\Microsoft\Windows\CurrentVersion\Run'

# 本工具只支持 macOS / Windows。早退比让后面的系统调用逐个诡异失败要好。
if sys.platform not in ('darwin', 'win32'):
    sys.exit(f"[错误] 本工具仅支持 macOS / Windows，当前平台: {sys.platform}")


# ============================================================
# 迷你 WebSocket 客户端（纯标准库，只实现 CDP 需要的能力）
# ============================================================

class MiniWS:
    """最小 WebSocket 客户端：握手 + 文本帧收发（含分片/ping-pong/close）。

    CDP 的 webSocketDebuggerUrl 是 ws://（本地明文），不涉及 TLS。
    客户端发送帧必须 mask（RFC 6455 要求），服务器帧不 mask。
    """

    def __init__(self, host, port, path, timeout=15):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET {path} HTTP/1.1\r\n"
            "Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).format(path=path, host=host, port=port, key=key)
        self.sock.sendall(req.encode())

        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手阶段连接被关闭")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0].decode("latin-1")
        if " 101 " not in status + " ":
            raise ConnectionError("WebSocket 握手失败: " + status)

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)          # FIN + opcode
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("连接被关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_text(self):
        parts = []
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:                 # close
                raise ConnectionError("对端关闭连接")
            if opcode == 0x9:                 # ping → 回 pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                 # pong：控制帧，不是消息内容
                # RFC 6455 §5.5.3 允许对端主动发 pong。把它并进 parts 会让
                # 载荷（如 "keepalive"）当成 CDP 响应返回，json.loads 直接抛异常。
                continue
            if opcode in (0x1, 0x0):          # text / continuation
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    """Chrome DevTools 协议客户端（同步，MiniWS 之上）"""

    def __init__(self, ws_url, timeout=15):
        u = urllib.parse.urlparse(ws_url)
        self.ws = MiniWS(u.hostname, u.port, u.path, timeout=timeout)
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps(
            {"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == mid:
                return msg

    def evaluate(self, expr):
        return self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def cdp_value(resp):
    """从 Runtime.evaluate 响应里取返回值，异常返回 None"""
    try:
        return resp['result']['result']['value']
    except Exception:
        return None


# ============================================================
# 安装路径与进程
# ============================================================

EXE_NAME = 'Antigravity'
EXE_NAME_WIN = 'Antigravity.exe'
if IS_WINDOWS:
    _la = os.environ.get('LOCALAPPDATA') or ''
    _pf = os.environ.get('ProgramFiles') or ''
    _pfx = os.environ.get('ProgramFiles(x86)') or ''
    CANDIDATE_PATHS = tuple(p for p in (
        os.path.join(_la, 'Programs', 'antigravity', EXE_NAME_WIN),
        os.path.join(_la, 'Programs', 'Antigravity', EXE_NAME_WIN),
        os.path.join(_pf, 'Antigravity', EXE_NAME_WIN),
        os.path.join(_pfx, 'Antigravity', EXE_NAME_WIN),
    ) if p and p[1:3] == ':\\')
else:
    CANDIDATE_PATHS = (
        '/Applications/Antigravity.app/Contents/MacOS/Antigravity',
        os.path.expanduser('~/Applications/Antigravity.app/Contents/MacOS/Antigravity'),
    )

# 只匹配主进程可执行文件本身，末尾必须是空白或行尾——否则会连
# "Antigravity Helper.app/.../Antigravity Helper" 一起匹配上。
# 在模块级预编译：守护模式每 5 秒调一次 antigravity_pids()，
# 放在函数里等于每次重新编译一遍。
_MAIN_PROC_RE = re.compile(
    r'(?:^|\s)'
    r'(?:/Applications|/Users/[^/]+/Applications)'
    r'/Antigravity\.app/Contents/MacOS/Antigravity'
    r'(?=\s|$)'
)
# Windows 主进程匹配：安装目录下的 Antigravity.exe，命令行不带 --type= 的即主进程。
# Helper（--type=renderer/gpu-process 等）不开 CDP 端口，扫它纯属浪费探测。
_MAIN_PROC_RE_WIN = re.compile(r'Antigravity\.exe(?=\s|$|")', re.IGNORECASE)

# CDP 端口探测用：Chromium 的 /json/version 里 Browser 字段形如 "Chrome/146.0..."
_CDP_BROWSER_HINT = 'Chrome'


def find_antigravity_exe(install_dir=None):
    """定位 Antigravity 可执行文件，返回路径或 None。

    install_dir 可以是 .app 本身、含 .app 的目录，或直接是可执行文件。
    """
    if install_dir:
        cand = os.path.expanduser(install_dir)
        exe_names = {EXE_NAME_WIN.lower()} if IS_WINDOWS else {EXE_NAME.lower()}
        if os.path.basename(cand).lower() in exe_names and os.path.isfile(cand):
            return cand
        if IS_WINDOWS:
            win_cands = (
                os.path.join(cand, EXE_NAME_WIN),
                os.path.join(cand, 'Antigravity', EXE_NAME_WIN),
            )
            for p in win_cands:
                if os.path.isfile(p):
                    return p
            return None
        for p in (
            os.path.join(cand, 'Contents', 'MacOS', EXE_NAME),
            os.path.join(cand, 'Antigravity.app', 'Contents', 'MacOS', EXE_NAME),
            os.path.join(cand, EXE_NAME),
        ):
            if os.path.isfile(p):
                return p
        return None
    for cand in CANDIDATE_PATHS:
        if os.path.isfile(cand):
            return cand
    return None


def antigravity_pids():
    """Antigravity 主进程 PID 列表（字符串）。

    只认主进程，不要 Helper：Helper 不开 CDP 端口，扫它纯属浪费端口探测。
    """
    try:
        my_pid = str(os.getpid())
        if IS_WINDOWS:
            # tasklist 拿不到命令行，无法区分主进程与 Helper；但 Helper 不监听
            # CDP，端口探测阶段会被 /json/version 自然过滤掉。
            out = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq Antigravity.exe',
                 '/FO', 'CSV', '/NH'],
                capture_output=True, text=True, timeout=8).stdout
            pids = []
            for line in out.splitlines():
                fields = [f.strip().strip('"') for f in line.split('","')]
                if (len(fields) >= 2 and fields[0].lower() == 'antigravity.exe'
                        and fields[1].isdigit() and fields[1] != my_pid):
                    pids.append(fields[1])
            return pids
        out = subprocess.run(['ps', '-eo', 'pid=,command='],
                             capture_output=True, text=True, timeout=5).stdout
        pids = []
        for line in out.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            pid, cmd = parts
            if pid == my_pid or not pid.isdigit():
                continue
            if _MAIN_PROC_RE.search(cmd):
                pids.append(pid)
        return pids
    except Exception:
        return []


def is_antigravity_running():
    return bool(antigravity_pids())


def launch_antigravity(exe, proxy=None):
    """启动 Antigravity，返回进程句柄（失败返回 None）。

    不传 --remote-debugging-port：macOS 上 Antigravity 无视这个参数，
    总是自己开随机端口的 CDP（靠 discover_cdp_port 扫出来）。传了只是噪音。

    必须清掉 ELECTRON_RUN_AS_NODE：从 Electron 宿主（如某些桌面端 App）派生的
    shell 会带着它，导致 Antigravity 以纯 Node 模式启动，Chromium 参数直接报
    "bad option"，进程秒退。
    """
    print(f"[启动] {exe}")
    env = dict(os.environ)
    env.pop('ELECTRON_RUN_AS_NODE', None)
    if proxy:
        print(f"[代理] {proxy}")
        for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            env[k] = proxy
    popen_kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=env)
    if IS_WINDOWS:
        # 脱离本进程的控制台/进程组，宿主退出不影响 Antigravity。
        popen_kwargs['creationflags'] = (
            getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)
            | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
            | getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
    else:
        popen_kwargs['start_new_session'] = True
    try:
        return subprocess.Popen([exe], **popen_kwargs)
    except Exception as e:
        print(f"[错误] 启动失败: {e}")
        return None


# ============================================================
# 字典与翻译引擎
# ============================================================

def normalize_text(text):
    """压缩空白 + 全角引号归一为半角（与引擎 JS 侧 norm() 必须逐条对应）"""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    return text


def load_dictionary():
    """合并 dicts/*.json 为一个 map"""
    total_map = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dicts_dir = os.path.join(script_dir, 'dicts')
    if os.path.exists(dicts_dir):
        for filename in sorted(os.listdir(dicts_dir)):
            if filename.endswith(".json"):
                try:
                    with open(os.path.join(dicts_dir, filename), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        for k, v in data.items():
                            norm_k = normalize_text(k)
                            if norm_k:
                                total_map[norm_k] = v
                except Exception as e:
                    print(f"  [跳过] {filename}: {e}")
    return total_map


_ENGINE_CACHE = {'js': None, 'built_at': 0.0, 'sig': None}
ENGINE_CACHE_TTL = 120          # 秒；到点就重编译，让新会话标题能进 CONV_TITLES


def _dicts_signature():
    """dicts/ 下所有 JSON 的 (文件名, mtime, 大小) 指纹，改了字典就变。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dicts_dir = os.path.join(script_dir, 'dicts')
    sig = []
    try:
        for fn in sorted(os.listdir(dicts_dir)):
            if fn.endswith('.json'):
                st = os.stat(os.path.join(dicts_dir, fn))
                sig.append((fn, st.st_mtime, st.st_size))
    except Exception:
        return None
    return tuple(sig)


def get_engine_js(force=False):
    """懒加载并缓存翻译引擎 JS（守护模式反复注入不用每次重编译）。

    缓存必须会失效，否则常驻守护（launchd 长期不重启）会一直用启动那一刻的引擎：
    改了 dicts/ 不生效，新建会话的标题也进不了 CONV_TITLES（它是编译期烘进去的）。
    两个失效条件：字典文件指纹变了，或超过 ENGINE_CACHE_TTL。
    """
    sig = _dicts_signature()
    stale = (
        force
        or _ENGINE_CACHE['js'] is None
        or _ENGINE_CACHE['sig'] != sig
        or (time.time() - _ENGINE_CACHE['built_at']) > ENGINE_CACHE_TTL
    )
    if stale:
        _ENGINE_CACHE['js'] = build_engine_js(load_dictionary())
        _ENGINE_CACHE['built_at'] = time.time()
        _ENGINE_CACHE['sig'] = sig
    return _ENGINE_CACHE['js']


def build_engine_js(dict_map):
    """构造注入的翻译引擎 IIFE（复用旧版 MutationObserver 思路，禁区按 2.0 调整）"""
    dict_json = json.dumps(dict_map, ensure_ascii=False, separators=(',', ':'))

    js_source = """\
(() => {
    // 汉化翻译引擎（CDP 注入版，基于容器回溯隔离）
    // 防重复注入：若已注入过（有全局标记），先断开旧 observer 再重建，避免叠加
    if (window.__ag_hanhua_engine__) {
        try { window.__ag_hanhua_engine__.disconnect(); } catch (e) {}
        delete window.__ag_hanhua_engine__;
    }
    const map = new Map(Object.entries(DICT_PLACEHOLDER));
    const lowerMap = new Map();
    for (const [k, v] of map.entries()) lowerMap.set(k.toLowerCase(), v);

    // 按 key 长度降序（长条目优先，避免短条目先匹配吃掉长条目的子串）
    const allEntries = [...map.entries()].sort((a, b) => b[0].length - a[0].length);
    // 预筛：只有长度 >= 30 的完整句子参与子串替换。
    // 门槛必须高：像 "safety barriers" 这种半截短语若参与子串替换，
    // 会把整句里的碎片换成中文、剩下英文，产生"中英混杂"。短条目只做整节点精确匹配。
    const phraseEntries = allEntries.filter(e => e[0].length >= 30);
    // 记录每个文本节点上一次处理过的值：值没变就跳过，避免重复开销
    const lastSeen = new WeakMap();
    // 属性侧的同款 memo：元素 → {属性名: 上次见过的值}。
    // 没有它，每 2 秒的定时器要把全部元素的 8 个属性重新归一化、查表、
    // 并可能过一遍 175 条正则——而文本节点早就有 lastSeen 跳过未变值了。
    const attrSeen = new WeakMap();

    // 词条漂移日志：记下「含英文、未进禁区、但整条没翻出来」的文本。
    // 官方更新后用 caiji.py --misses 拉取，即可看出哪些词条失效或缺失。
    const misses = new Set();

    // 保护名单：产品名/品牌，禁止子串替换（防止被部分匹配切碎）
    const PROTECTED = [
        'antigravity', 'jetski', 'gemini', 'google ai', 'best of n',
        'google chrome', 'marketplace', 'mcp', 'citc',
    ];
    function isProtected(s) {
        const low = s.toLowerCase();
        for (const p of PROTECTED) {
            if (low.includes(p)) return true;
        }
        return false;
    }

    // 智能子串替换：只有 phraseEntries（key 长度 >= 30 的完整句子）参与。
    function smartReplace(text) {
        let result = text;
        for (const [key, val] of phraseEntries) {
            // phraseEntries 按 key 长度降序。比文本还长的 key 不可能命中，
            // 用一次整数比较挡掉，省下昂贵的 indexOf 全串扫描。
            // 界面标签大多很短，这一条能挡掉 306 条里的绝大多数。
            if (key.length > result.length) continue;
            let idx = result.indexOf(key);
            if (idx === -1) continue;
            while (idx !== -1) {
                const before = idx > 0 ? result[idx - 1] : '';
                const afterIdx = idx + key.length;
                const after = afterIdx < result.length ? result[afterIdx] : '';
                const isWordBoundary =
                    (!/[一-鿿A-Za-z0-9]/.test(before)) &&
                    (!/[一-鿿A-Za-z0-9]/.test(after));
                if (isWordBoundary) {
                    result = result.slice(0, idx) + val + result.slice(afterIdx);
                    idx = result.indexOf(key, idx + val.length);
                } else {
                    idx = result.indexOf(key, idx + key.length);
                }
            }
        }
        return result;
    }

    // 禁区：代码/编辑器/输入/对话内容容器（防止翻译用户数据）
    const BLOCKED_CLASS_SUBSTR = [
        'code-view', 'editor-container', 'monaco-editor', 'suggest-widget',
        'output-view', 'debug-console', 'artifact-container',
        'code-block', 'diff-view', 'input-area', 'chat-input'
    ];
    const BLOCKED_CLASS_TOKEN = ['terminal', 'xterm', 'preview'];
    const BLOCKED_TAGS = ['SCRIPT', 'STYLE', 'CODE', 'PRE', 'INPUT', 'TEXTAREA',
                          'SVG', 'CANVAS', 'SYMBOL', 'PATH', 'MATH', 'KBD'];

    const TRANSLATABLE_ATTRS = ['placeholder', 'title', 'aria-label', 'alt',
                                'data-title', 'data-tooltip-content',
                                'aria-description', 'aria-placeholder'];

    // 必须与 Python 侧 normalize_text() 完全一致
    function norm(s) {
        if (!s) return '';
        return s.replace(/\\s+/g, ' ')
                .replace(/[’‘]/g, "'")
                .replace(/[“”]/g, '"')
                .trim();
    }

    // 会话/项目列表项：五个特征必须同时满足。只判 select-none + cursor-pointer
    // 会把菜单栏按钮（File/View/Window）也算进去，导致菜单栏整片不翻译。
    const ROW_CLASS_SIGNATURE = ['relative', 'w-full', 'select-none', 'cursor-pointer'];
    function isConversationRow(el) {
        if (!el || el.tagName !== 'DIV') return false;
        const className = el.className;
        if (typeof className !== 'string' || !className) return false;
        const tokens = className.split(/[ ]+/);
        for (const need of ROW_CLASS_SIGNATURE) {
            if (!tokens.includes(need)) return false;
        }
        return true;
    }

    // 行内只有**标题**是用户内容，其余（时间戳、计数、徽标）是 UI 装饰，仍要翻。
    // 标题是那个 truncate span（syncConversationTitles 也是按它定位的）；
    // 时间戳走的是 text-xs opacity-60，不带 truncate。
    // 整行拉黑会让 3h / 23h 这类时间戳跟着漏翻——实测踩过。
    function isRowTitle(el) {
        if (!el || typeof el.className !== 'string') return false;
        return el.className.split(/[ ]+/).includes('truncate');
    }

    function isInBlockedZone(node) {
        let curr = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
        let depth = 0;
        let sawRowTitle = false;
        while (curr && depth < 25) {
            if (curr.nodeType === Node.ELEMENT_NODE) {
                const tag = curr.tagName.toUpperCase();
                if (BLOCKED_TAGS.includes(tag)) return true;
                if (curr.getAttribute('contenteditable') === 'true') return true;
                if (isRowTitle(curr)) sawRowTitle = true;
                // 只有「truncate 标题 + 外层是会话行」两个条件同时成立才算用户内容
                if (sawRowTitle && isConversationRow(curr)) return true;
                const className = curr.className || '';
                if (typeof className === 'string') {
                    if (BLOCKED_CLASS_SUBSTR.some(cls => className.includes(cls))) return true;
                    const tokens = className.split(/[ ]+/);
                    if (tokens.some(t => BLOCKED_CLASS_TOKEN.includes(t))) return true;
                }
                const role = curr.getAttribute('role') || '';
                if (role === 'code' || role === 'img') return true;
            }
            curr = curr.parentElement || (curr.parentNode && curr.parentNode.host);
            depth++;
        }
        return false;
    }

    // 动态文案规则：含变量（时间、数字）的句子无法穷举，用正则处理。
    // 刻意不写反斜杠转义（Python 字符串层会把它吞掉），改用 [0-9] [.] [ ] 这类等价写法。
    function cnDuration(s) {
        return s
            .replace(/([0-9]+)[ ]*days?/gi, '$1 天')
            .replace(/([0-9]+)[ ]*hours?/gi, '$1 小时')
            .replace(/([0-9]+)[ ]*minutes?/gi, '$1 分钟')
            .replace(/([0-9]+)[ ]*seconds?/gi, '$1 秒')
            .replace(/([0-9]+)[ ]*d(?![A-Za-z0-9])/gi, '$1 天')
            .replace(/([0-9]+)[ ]*h(?![A-Za-z0-9])/gi, '$1 小时')
            .replace(/([0-9]+)[ ]*m(?![A-Za-z0-9])/gi, '$1 分钟')
            .replace(/([0-9]+)[ ]*s(?![A-Za-z0-9])/gi, '$1 秒')
            .replace(/,[ ]*/g, ' ')
            .trim();
    }
    const REGEX_RULES = [
        [/^You have used (some|all) of your (5-hour|5 hour|five hour) limit, it will fully refresh in (.+?)[.]?$/i,
         (m, q, l, a) => '你已使用' + (q.toLowerCase() === 'all' ? '全部' : '部分') + '五小时限额，将在 ' + cnDuration(a) + ' 后完全刷新。'],
        [/^You have used (some|all) of your weekly limit, it will fully refresh in (.+?)[.]?$/i,
         (m, q, a) => '你已使用' + (q.toLowerCase() === 'all' ? '全部' : '部分') + '每周限额，将在 ' + cnDuration(a) + ' 后完全刷新。'],
        [/^You have reached your (5-hour|5 hour|five hour) limit, it will fully refresh in (.+?)[.]?$/i,
         (m, l, a) => '你已达到五小时限额，将在 ' + cnDuration(a) + ' 后完全刷新。'],
        [/^You have reached your weekly limit, it will fully refresh in (.+?)[.]?$/i,
         (m, a) => '你已达到每周限额，将在 ' + cnDuration(a) + ' 后完全刷新。'],
        [/^You have used (some|all) of your (.+?) limit, it will fully refresh in (.+?)[.]?$/i,
         (m, q, l, a) => '你已使用' + (q.toLowerCase() === 'all' ? '全部' : '部分') + l + '限额，将在 ' + cnDuration(a) + ' 后完全刷新。'],
        [/^Will fully refresh in (.+?)[.]?$/i,
         (m, a) => '将在 ' + cnDuration(a) + ' 后完全刷新。'],
        [/^Fully refreshes in (.+?)[.]?$/i,
         (m, a) => '将在 ' + cnDuration(a) + ' 后完全刷新。'],
        [/^Resets in (.+?)[.]?$/i,
         (m, a) => '将在 ' + cnDuration(a) + ' 后重置。'],
        [/^Send feedback as (.+)$/, (m, a) => '以 ' + a + ' 身份发送反馈'],
        [/^Select model, current: (.+)$/, (m, a) => '选择模型，当前：' + a],
        [/^(.+?) Sends immediately$/, (m, a) => a + ' 立即发送'],
        [/^(.+?) Queues after the turn$/, (m, a) => a + ' 排队等待当前轮次结束后发送'],
        [/^Worked for (.+)$/, (m, a) => '已工作 ' + cnDuration(a)],
        [/^Thought for (.+)$/, (m, a) => '思考了 ' + cnDuration(a)],
        [/^Thinking for (.+)$/, (m, a) => '正在思考 (' + cnDuration(a) + ')'],
        [/^([0-9]+) files? changed$/, (m, a) => '已修改 ' + a + ' 个文件'],
        [/^Explored ([0-9]+) files?$/, (m, a) => '已浏览 ' + a + ' 个文件'],
        [/^Ran ([0-9]+) commands?$/, (m, a) => '已运行 ' + a + ' 条命令'],
        [/^([0-9]+) tasks? running$/, (m, a) => a + ' 个任务运行中'],
        [/^([0-9]+) files?, ([0-9]+) searches?$/, (m, a, b) => a + ' 个文件，' + b + ' 次搜索'],
        [/^([0-9]+) files?$/, (m, a) => a + ' 个文件'],
        [/^([0-9]+) commands?$/, (m, a) => a + ' 条命令'],
        [/^([0-9]+) searches?$/, (m, a) => a + ' 次搜索'],
        [/^([0-9]+) tools?$/, (m, a) => a + ' 个工具'],
        [/^(?:Load|Showing) older messages, showing ([0-9]+) of ([0-9]+)$/i, (m, a, b) => '加载更早的消息，显示 ' + a + ' / ' + b + ' 条'],
        [/^No more older messages, showing ([0-9]+) of ([0-9]+)$/i, (m, a, b) => '没有更早的消息了，显示 ' + a + ' / ' + b + ' 条'],
        [/^Showing ([0-9]+) of ([0-9]+) messages$/i, (m, a, b) => '显示 ' + a + ' / ' + b + ' 条消息'],
        [/^Showing ([0-9]+) of ([0-9]+)$/i, (m, a, b) => '显示 ' + a + ' / ' + b + ' 条'],
        [/^[(]([0-9,]+)[ ]*tokens?[)]$/i, (m, a) => '(' + a + ' Token)'],
        [/^[(]([0-9,]+)[ ]*tokens?[)][ ]*(.+)$/i, (m, a, rest) => '(' + a + ' Token) ' + rest],
        [/^([0-9,]+)[ ]*tokens?$/i, (m, a) => a + ' Token'],
        [/^([0-9,]+)[ ]*tokens?[ ]*(.+)$/i, (m, a, rest) => a + ' Token ' + rest],
        [/^(?:Show|View)[ ]+([0-9]+)[ ]+breakdowns?$/i, (m, a) => '显示 ' + a + ' 项明细'],
        [/^Hide[ ]+([0-9]+)[ ]+breakdowns?$/i, (m, a) => '隐藏 ' + a + ' 项明细'],
        [/^(?:Show|View)[ ]+breakdowns?$/i, () => '显示明细'],
        [/^Hide[ ]+breakdowns?$/i, () => '隐藏明细'],
        [/^Rules[ ]+([0-9]+)$/i, (m, a) => '规则 ' + a],
        [/^Skills[ ]+([0-9]+)$/i, (m, a) => '技能 ' + a],
        [/^MCP Servers[ ]+([0-9]+)$/i, (m, a) => 'MCP 服务器 ' + a],
        [/^Search MCP servers by name$/i, () => '按名称搜索 MCP 服务器'],
        [/^Search ([A-Za-z0-9 _-]+) by name(?: or description)?[.][.][.]?$/i, (m, target) => '按名称搜索 ' + (map.get(norm(target)) || lowerMap.get(norm(target).toLowerCase()) || target) + '...'],
        [/^Permanently delete (.+?)[.]?$/i, (m, name) => '永久删除 ' + name.replace(/[.]+$/, '') + '。'],
        [/^Plugins are packaged collections of skills and MCPs to help the Agent in$/i, () => '插件是技能和 MCP 的打包集合，可帮助 '],
        [/^work with Google developer products[.] You can always change your choices in Settings[.]?$/i, () => ' 中的智能体配合 Google 开发者产品工作。你可以随时在“设置”中更改你的选择。'],
        [/^work with Google developer products[.]?$/i, () => ' 中的智能体配合 Google 开发者产品工作。'],
        [/^Prototype, build & run modern apps users love with Firebase['’]s backend, AI, and operational infrastructure[.]?$/i, () => '借助 Firebase 的后端、AI 和运营基础设施，原型设计、构建并运行深受用户喜爱的现代应用。'],
        [/^Reliable automation, in-depth debugging, and performance analysis in Chrome using Chrome DevTools and Puppeteer[.]?$/i, () => '使用 Chrome DevTools 和 Puppeteer 在 Chrome 中进行可靠的自动化、深度调试和性能分析。'],
        [/^Skills providing tailored instructions for happy path Dart and Flutter development workflows[.]?$/i, () => '为 Dart 和 Flutter 开发顺畅工作流提供定制指令的技能。'],
        [/^Build and prototype location-aware applications with Google Maps Platform[.]?.*$/i, () => '使用 Google Maps Platform 构建并原型设计位置感知应用。集成交互式地图、搜索并查看地点详情、计算最佳路线。'],
        [/^Specialized suite of skills for data engineers and database practitioners on Google Cloud[.]?$/i, () => '面向 Google Cloud 上数据工程师和数据库从业者的专用技能套件。'],
        [/^Build applications with the Gemini Interactions API and Live API.*$/i, () => '使用 Gemini Interactions API 和 Live API 构建应用，涵盖文本生成、多轮对话、流式传输、函数调用与实时多模态交互。'],
        [/^Comprehensive guide and reference for the Antigravity Customization System.*$/i, () => 'Antigravity 自定义系统的全面指南与参考。用于解释自定义项的工作原理、加载优先级、发现机制，并指导技能、规则、插件、钩子及 MCP 服务器的创建。'],
        [/^Provides a comprehensive guide, quick reference, and sitemap for Google Antigravity.*$/i, () => '提供 Google Antigravity (AGY) 的全面指南、快速参考与站点地图，涵盖 Antigravity CLI (agy)、Antigravity 2.0、Antigravity IDE、Python SDK、斜杠命令、快捷键及自定义项。'],
        [/^Guidelines for interacting with GitHub and request permissions from the user.*$/i, () => '与 GitHub 交互的指南，并在命令因智能体环境限制而失败时向用户请求权限。'],
        [/^Investigate and fix software issues using AI-powered root cause analysis.*$/i, () => '使用 AI 驱动的根本原因分析调查并修复软件问题。此 MCP 服务器可连接到你的 Antimetal 账户以搜索问题并查看调查报告。'],
        [/^Query and act on your marketing, analytics, CRM, e-commerce, and warehouse data.*$/i, () => '跨 325+ 个连接器（Meta Ads、Google Ads、TikTok Ads、GA4、HubSpot 等）查询并处理营销、分析、CRM、电子商务和数据仓库数据。'],
        [/^Query your GitLab SDLC as a knowledge graph.*$/i, () => '将你的 GitLab 软件开发生命周期 (SDLC) 作为知识图谱进行查询。Orbit 将群组、项目、源代码、合并请求、流水线、工作项和安全发现编入知识图谱。'],
        [/^Enable Antigravity to deploy apps to Google Cloud Run[.]?$/i, () => '让 Antigravity 能够将应用部署到 Google Cloud Run。'],
        [/^Ask questions[.] Get answers[.] The MCP is a server your coding agent talks to.*$/i, () => '提出问题，获取解答。此 MCP 服务器供你的编码智能体进行通信，针对你的 PostHog 数据运行查询并返回结果。'],
        [/^Search and reference over 600,000 real-world app screens, user flows, and UI patterns from Mobbin.*$/i, () => '直接在 AI 工具中搜索并参考 Mobbin 上超过 600,000 个真实应用界面、用户流程和 UI 模式。'],
        [/^Build, edit, deploy, and manage full-stack web apps with Lovable.*$/i, () => '使用由 AI 驱动的应用构建器 Lovable，通过自然语言构建、编辑、部署和管理全栈 Web 应用。此 MCP 服务器将你的 AI 客户端连接至 Lovable。'],
        [/^The GKE remote MCP server provides read write access to your GKE Kubernetes resources.*$/i, () => 'GKE 远程 MCP 服务器提供对 GKE Kubernetes 资源的读写访问权限。它允许 AI 智能体检查并观察你的环境。'],
        [/^The Dart and Flutter MCP server exposes Dart [(]and Flutter[)] development tool actions.*$/i, () => 'Dart 和 Flutter MCP 服务器向兼容的 AI 助手客户端公开 Dart（及 Flutter）开发工具操作。'],
        [/^The Firebase Model Context Protocol [(]MCP[)] Server gives AI-powered development tools.*$/i, () => 'Firebase 模型上下文协议 (MCP) 服务器让 AI 开发工具能够与你的 Firebase 项目及应用代码库进行交互协作。'],
        [/^The Genkit Model Context Protocol [(]MCP[)] Server gives AI-powered development tools.*$/i, () => 'Genkit 模型上下文协议 (MCP) 服务器让 AI 开发工具能够构建、调试并检查你的 Genkit 应用。'],
        [/^The gopls Model Context Protocol [(]MCP[)] server provides tools for semantic code analysis.*$/i, () => 'gopls 模型上下文协议 (MCP) 服务器提供用于语义代码分析、实时诊断以及 Go 代码库转换的工具。'],
        [/^Interact with your BigQuery data using natural language.*$/i, () => '使用自然语言与 BigQuery 数据交互。此 MCP 服务器允许你安全连接到数据集以搜索数据集、检查表元数据。'],
        [/^The AlloyDB for PostgreSQL remote MCP server lets you access and run AlloyDB tools.*$/i, () => 'AlloyDB for PostgreSQL 远程 MCP 服务器允许你访问并运行 AlloyDB 工具来管理 AlloyDB 集群和实例、管理用户、创建和恢复备份。'],
        [/^The Bigtable Admin remote MCP server lets you manage Bigtable resources[.]?$/i, () => 'Bigtable Admin 远程 MCP 服务器允许你管理 Bigtable 资源。'],
        [/^Cloud CLI MCP Server provides tools to run gcloud and bq CLI ?commands.*$/i, () => 'Cloud CLI MCP 服务器提供在远程沙箱环境中运行 gcloud 和 bq CLI 命令的工具。'],
        [/^The Cloud SQL remote MCP server lets you access and run Cloud SQL tools.*$/i, () => 'Cloud SQL 远程 MCP 服务器允许你访问并运行 Cloud SQL 工具来管理 Cloud SQL 实例、管理用户、创建和恢复备份、进行管理操作。'],
        [/^The Spanner remote MCP server lets you access and run Spanner tools.*$/i, () => 'Spanner 远程 MCP 服务器允许你访问并运行 Spanner 工具，以便从支持 AI 的开发环境中创建、管理和查询 Spanner 资源。'],
        [/^The Apigee API hub remote MCP server lets you manage the APIs.*$/i, () => 'Apigee API hub 远程 MCP 服务器允许你管理在 Apigee 中注册的 API、版本、规范、操作、部署、属性、外部 API 和依赖项。'],
        [/^Connect your AI assistants to Looker business intelligence.*$/i, () => '将你的 AI 助手连接到 Looker 商业智能。此 MCP 服务器通过允许你执行自然语言查询来实现数据探索和内容管理。'],
        [/^Connect your AI assistants to the Knowledge Catalog [(]formerly known as Dataplex[)].*$/i, () => '将你的 AI 助手连接到 Knowledge Catalog（原名 Dataplex）。此 MCP 服务器通过允许你搜索资源来实现数据发现与数据治理。'],
        [/^The MCP Toolbox for Databases is an open-source MCP server designed to simplify and secure the development of tools for interacting with databases[.]?$/i, () => 'MCP Toolbox for Databases 是一个开源 MCP 服务器，旨在简化并保护用于与数据库交互的工具开发。'],
        [/^Interact with your Oracle Database data using natural language.*$/i, () => '使用自然语言与 Oracle 数据库数据交互。此 MCP 服务器允许你安全连接到数据库以执行 SQL 查询。'],
        [/^The Dev Mode MCP Server brings Figma directly into your workflow.*$/i, () => 'Dev Mode MCP 服务器通过向从 Figma 生成代码的 AI 智能体提供关键设计信息和上下文，将 Figma 直接引入你的工作流。'],
        [/^The GitHub MCP Server is a Model Context Protocol [(]MCP[)] server that provides seamless integration with GitHub APIs.*$/i, () => 'GitHub MCP 服务器是一个模型上下文协议 (MCP) 服务器，提供与 GitHub API 的无缝集成，以实现高级自动化和交互操作。'],
        [/^The Google Home Developer MCP server allows you to search through Google Home documentation.*$/i, () => 'Google Home Developer MCP 服务器允许你搜索 Google Home 文档、OpenThread 和 Matter 规范文档。'],
        [/^Neon MCP Server is an open-source tool that lets you interact with your Neon Postgres databases in natural language[.]?$/i, () => 'Neon MCP 服务器是一款开源工具，允许你使用自然语言与 Neon Postgres 数据库交互。'],
        [/^The Stripe Model Context Protocol server allows you to integrate with Stripe APIs.*$/i, () => 'Stripe 模型上下文协议服务器允许你通过函数调用与 Stripe API 集成。该协议支持多种工具与 Stripe 进行交互。'],
        [/^Interact with Redis key-value stores[.]?$/i, () => '与 Redis 键值存储进行交互'],
        [/^A Model Context Protocol server for interacting with MongoDB Atlas[.]?$/i, () => '用于与 MongoDB Atlas 交互的模型上下文协议 (MCP) 服务器。'],
        [/^Official Notion MCP Server that allows interaction with Notion workspaces, pages, databases, and comments via the Notion API[.]?$/i, () => '官方 Notion MCP 服务器，允许通过 Notion API 与 Notion 工作区、页面、数据库和评论进行交互。'],
        [/^Official Linear[.]app MCP Server for interacting with Linear projects, issues, and workflows[.]?$/i, () => '官方 Linear.app MCP 服务器，用于与 Linear 项目、议题和工作流进行交互。'],
        [/^An MCP server implementation that integrates the Perplexity Sonar API.*$/i, () => '集成 Perplexity Sonar API 的 MCP 服务器实现，提供实时的全网搜索与研究能力。'],
        [/^Official PayPal MCP Server that allows integration with PayPal APIs.*$/i, () => '官方 PayPal MCP 服务器，允许与 PayPal API 集成以进行支付处理、交易管理和账户操作。'],
        [/^The Heroku Platform MCP Server enables seamless interaction with Heroku Platform resources.*$/i, () => 'Heroku Platform MCP 服务器可实现与 Heroku 平台资源的无缝交互，允许大语言模型读取、管理和操作应用及插件等。'],
        [/^The Pinecone MCP Server enables AI tools to search Pinecone documentation.*$/i, () => 'Pinecone MCP 服务器使 AI 工具能够搜索 Pinecone 文档、配置索引、根据索引配置生成代码等。'],
        [/^Connect your Supabase projects to AI assistants.*$/i, () => '将你的 Supabase 项目连接到 AI 助手。此 MCP 服务器允许管理数据表、获取配置、执行 SQL 查询、管理 Edge 函数等。'],
        [/^The Prisma MCP Server enables AI tools to interact with Prisma for creating and managing Postgres databases easily[.]?$/i, () => 'Prisma MCP 服务器使 AI 工具能够与 Prisma 交互，从而轻松创建和管理 Postgres 数据库。'],
        [/^The Locofy MCP Server enables Locofy[.]ai code to be integrated and extended with your IDE[.]?$/i, () => 'Locofy MCP 服务器使 Locofy.ai 代码能够与你的 IDE 进行集成和扩展。'],
        [/^Airweave lets agents search any app[.]?$/i, () => 'Airweave 允许智能体搜索任何应用。'],
        [/^Atlassian MCP Server for interacting with Atlassian products[.]?$/i, () => '用于与 Atlassian 产品交互的 Atlassian MCP 服务器。'],
        [/^Interact with your Harness account using natural language.*$/i, () => '使用自然语言与你的 Harness 账户交互。此 MCP 服务器允许 AI 智能体检查并管理 CI/CD 流水线、执行、服务、环境等。'],
        [/^SonarQube MCP Server enables AI assistants to interact with SonarQube instances.*$/i, () => 'SonarQube MCP 服务器使 AI 助手能够与 SonarQube 实例交互，以进行代码质量分析、项目管理和质量阈操作。'],
        [/^Netlify MCP Server enables AI assistants to interact with Netlify['’]s platform.*$/i, () => 'Netlify MCP 服务器使 AI 助手能够与 Netlify 平台交互，以管理网站、部署、域名及其他 Web 开发工作流。'],
        [/^A Model Context Protocol server that provides structured thinking and reasoning capabilities.*$/i, () => '为大语言模型对话提供结构化思考与推理能力的模型上下文协议 (MCP) 服务器。'],
        [/^Sonatype MCP server for interacting with our dependency management and security intelligence platform[.]?$/i, () => '用于与我们的依赖项管理和安全情报平台交互的 Sonatype MCP 服务器。'],
        [/^The Google Maps Platform Code Assist MCP server provides.*$/i, () => 'Google Maps Platform Code Assist MCP 服务器为你常用的 AI 编码助手提供最新官方 Google Maps Platform 文档与代码示例。'],
        [/^This MCP server provides your LLM with docs and examples to instrument your AI apps with Arize AX.*$/i, () => '此 MCP 服务器为你的大模型提供文档和示例，以便使用 Arize AX 检测你的 AI 应用并获得支持。'],
        [/^The Postman MCP Server connects Postman to AI tools.*$/i, () => 'Postman MCP 服务器将 Postman 连接到 AI 工具，使 AI 智能体和助手能够访问工作区、管理集合和环境等。'],
        [/^The Stitch MCP server enables AI assistants to interact with Stitch for vibe design.*$/i, () => 'Stitch MCP 服务器使 AI 助手能够与 Stitch 交互以进行灵感设计：根据文本和图像生成 UI 设计，并访问项目和屏幕。'],
        [/^The Google Developer Knowledge MCP server gives AI-powered development tools.*$/i, () => 'Google Developer Knowledge MCP 服务器让 AI 开发工具能够搜索 Google 官方开发者文档并获取相关资料。'],
        [/^The ClickHouse MCP server enables agents to securely interact with ClickHouse databases.*$/i, () => 'ClickHouse MCP 服务器使智能体能够安全地与 ClickHouse 数据库交互，提供执行 SQL、探索数据和查看架构的通用接口。'],
        [/^Perform a range of infrastructure management tasks, including: manage virtual machine [(]VM[)] instances.*$/i, () => '执行一系列基础设施管理任务，包括：管理虚拟机 (VM) 实例、管理实例组管理器和实例模板等。'],
        [/^Access enterprise mobility data using natural language queries about device fleets.*$/i, () => '使用有关设备队列的自然语言查询访问企业移动数据、自动审计策略合规性，以及集成设备管理。'],
        [/^Search your Google Cloud projects using natural language[.]?$/i, () => '使用自然语言搜索你的 Google Cloud 项目。'],
        [/^Perform searches on ingested data in Google-owned data stores[.]?$/i, () => '在 Google 自有数据存储中的摄入数据上执行搜索。'],
        [/^Interact with documents stored in a Firestore database using natural language[.]?$/i, () => '使用自然语言与 Firestore 数据库中存储的文档交互。'],
        [/^Access resources in the Cloud Logging platform using natural language[.]?$/i, () => '使用自然语言访问 Cloud Logging 平台中的资源。'],
        [/^Manage clusters for Managed Service for Apache Kafka and Kafka Connect using natural language[.]?$/i, () => '使用自然语言管理 Managed Service for Apache Kafka 和 Kafka Connect 集群。'],
        [/^Access resources in the Cloud Monitoring platform using natural language[.]?$/i, () => '使用自然语言访问 Cloud Monitoring 平台中的资源。'],
        [/^Manage Pub[/]Sub resources and publish messages.*$/i, () => '管理 Pub/Sub 资源并发布消息。创建、列出、获取、更新和删除 Pub/Sub 主题、订阅与快照，以及发布消息。'],
        [/^The Cloud Quotas MCP server allows you to view quota allocations.*$/i, () => 'Cloud Quotas MCP 服务器允许你查看配额分配、申请增加配额以及管理配额调整器配置。'],
        [/^Enable Antigravity to control and inspect a live Chrome browser.*$/i, () => '让 Antigravity 能够控制并检查运行中的 Chrome 浏览器，利用 Chrome DevTools 的全部能力进行可靠自动化与深度调试。'],
        [/^The ([A-Za-z0-9 ._-]+) remote MCP server lets you access and run ([A-Za-z0-9 ._-]+) tools.*$/i, (m, name, tool) => name + ' 远程 MCP 服务器允许你访问并运行 ' + tool + ' 工具以进行管理和操作。'],
        [/^The ([A-Za-z0-9 ._-]+) remote MCP server lets you manage ([A-Za-z0-9 ._-]+) resources[.]?$/i, (m, name, res) => name + ' 远程 MCP 服务器允许你管理 ' + res + ' 资源。'],
        [/^The ([A-Za-z0-9 ._-]+) Model Context Protocol [(]MCP[)] [Ss]erver gives AI-powered development tools the ability to (.*)$/i, (m, name, rest) => name + ' 模型上下文协议 (MCP) 服务器让 AI 开发工具能够' + rest],
        [/^(?:All |全部|所有)?(?:定时任务|scheduled tasks?)[s]?[ ]+runs?[ ]+as[ ]+(.*)$/i, (m, model) => '所有定时任务均以 ' + model.replace(/[.]+$/, '').trim() + ' 模型运行。'],
        [/^Enter scheduled task name[.][.][.]$/i, () => '输入定时任务名称...'],
        [/^Enter a prompt for the agent to run[.][.][.]$/i, () => '输入供智能体执行的提示词...'],
        [/^to be installed[.][ ]*(.*)$/i, (m, rest) => '。' + (rest ? (rest.startsWith(' ') ? rest : ' ' + rest) : '')],
        [/^to be installed$/i, () => '已安装'],
        [/^Configure the browser subagent[.] It requires$/i, () => '配置浏览器子代理。需要先安装 '],
        [/^By using this app, you agree to its$/i, () => '使用此应用即表示你同意其'],
        [/^By using this app, you agree to its (?:the |its |our )?(.+)$/i, (m, a) => '使用此应用即表示你同意其' + (map.get(norm(a)) || lowerMap.get(norm(a).toLowerCase()) || a)],
        [/^By using (.+?), you agree to its$/i, (m, app) => '使用 ' + app + ' 即表示你同意其'],
        [/^By using (.+?), you agree to its (?:the |its |our )?(.+)$/i, (m, app, a) => '使用 ' + app + ' 即表示你同意其' + (map.get(norm(a)) || lowerMap.get(norm(a).toLowerCase()) || a)],
        [/^By continuing, you agree to (?:the |its |our )?(.+)$/i, (m, a) => '继续即表示你同意' + (map.get(norm(a)) || lowerMap.get(norm(a).toLowerCase()) || a)],
        // 'By clicking Continue, you agree to' 在字典里是半截短语（34 字符 >= 30），
        // 会参与子串替换，把整句译成「点击"继续"即表示你同意 the Privacy Policy」这种混杂。
        // 用正则接住完整句，把后半截回查字典。
        [/^By clicking ([A-Za-z ]+?), you agree to (?:the |its |our )?(.+)$/i,
         (m, btn, a) => '点击“' + (map.get(norm(btn)) || lowerMap.get(norm(btn).toLowerCase()) || btn)
                        + '”即表示你同意' + (map.get(norm(a)) || lowerMap.get(norm(a).toLowerCase()) || a)],
        [/^By signing in, you agree to (?:the |its |our )?(.+)$/i, (m, a) => '登录即表示你同意' + (map.get(norm(a)) || lowerMap.get(norm(a).toLowerCase()) || a)],
        [/^Available AI Credits:[ ]*(.*)$/i, (m, val) => '可用 AI 点数：' + val],
        [/^(?:否|无)?(?:项目|projects?)[ ]+found[.]?$/i, () => '未找到项目'],
        [/^No ([A-Za-z0-9 _-]+?)s?[ ]+found[.]?$/i, (m, what) => '未找到' + (map.get(norm(what)) || lowerMap.get(norm(what).toLowerCase()) || what)],
        [/^(Projects|Conversations|Workspaces|Tasks|Files)[ ]*[(](Status|Worktree|Workspace|Date|Time|Type|Category)[)][ ]*([>›»]?)$/i, (m, a, b, arrow) => (map.get(norm(a)) || a) + ' (' + (map.get(norm(b)) || b) + ')' + (arrow ? ' ' + arrow : '')],
        [/^Scan the (?:QR )?code to open this device in ([A-Za-z0-9 _-]+),?[ ]+or[ ]*$/i, (m, target) => '扫描二维码在“' + (map.get(norm(target)) || lowerMap.get(norm(target).toLowerCase()) || target) + '”中打开此设备，或 '],
        [/^Scan the (?:QR )?code to open this device in ([A-Za-z0-9 _-]+)[.]?$/i, (m, target) => '扫描二维码在“' + (map.get(norm(target)) || lowerMap.get(norm(target).toLowerCase()) || target) + '”中打开此设备。'],
        // 界面里有一批标题是 `Open ${config.title}` 模板拼出来的（如 Open Remote Control），
        // JS 里没有完整字面量，字典永远命不中，会掉到逐词翻译译出「打开远程 Control」
        // 这种中英混杂。这里把后半截回查字典；查不到就整体保留英文，
        // 也比半中半英好。已有精确条目（Open Changes → 查看变更）在第 1 级命中，走不到这。
        [/^Open ([A-Za-z0-9][A-Za-z0-9 ._-]{1,40})$/, (m, target) => {
            const hit = map.get(norm(target)) || lowerMap.get(norm(target).toLowerCase());
            return hit ? '打开' + hit : m;
        }],
        [/^Fold lines?[ ]+([0-9]+)(?:-([0-9]+))?$/i, (m, a, b) => '折叠第 ' + a + (b ? '-' + b : '') + ' 行'],
        [/^Unfold lines?[ ]+([0-9]+)(?:-([0-9]+))?$/i, (m, a, b) => '展开第 ' + a + (b ? '-' + b : '') + ' 行'],
        [/^when working in this project[.]?$/i, () => '（在此项目中工作时）。'],
        [/^when working in a project[.]?$/i, () => '（在项目中工作时）。'],
        [/^when not in a project[.]?$/i, () => '（在非项目状态下）。'],
        [/^User uploaded media ([0-9]+)$/i, (m, a) => '用户上传媒体 ' + a],
        [/^确定要删除 (?:this |the )?(?:conversation|对话)[?][ ]*This action cannot be undone[.] 吗[？?]$/i, () => '确定要删除此对话吗？此操作无法撤销。'],
        [/^Are you sure you want to delete (?:the |this )?(conversation|project|workspace|task|file|item)[?][ ]*This action cannot be undone[.]?$/i, (m, type) => '确定要删除此' + (map.get(norm(type)) || lowerMap.get(norm(type).toLowerCase()) || type) + '吗？此操作无法撤销。'],
        [/^Are you sure you want to delete (?:the |this )?([^?]+)[?][ ]*This action cannot be undone[.]?$/i, (m, target) => '确定要删除 ' + target + ' 吗？此操作无法撤销。'],
        [/^Are you sure you want to delete (?:the |this )?(?:项目|project|workspace)[ ]+([^?]+)[?]?$/i, (m, name) => '确定要删除项目 ' + name.replace(/[?]+$/, '').trim() + ' 吗？'],
        [/^Are you sure you want to delete (?:the |this )?(conversation|project|workspace|task|file|folder|message|item)[?]?$/i, (m, type) => '确定要删除此' + (map.get(norm(type)) || lowerMap.get(norm(type).toLowerCase()) || type) + '吗？'],
        [/^Are you sure you want to delete (?:the |this )?([^?]+)[?]?$/i, (m, target) => '确定要删除 ' + target.replace(/[?]+$/, '').trim() + ' 吗？'],
        [/^Are you sure you want to delete (?:the |this )?(?:项目|project|workspace)?$/i, () => '确定要删除项目 '],
        [/^Yes, and always allow (.+) in this conversation$/, (m, a) => '是，且在当前对话中始终允许 ' + a],
        [/^Yes, and always allow (.+) when not in a project$/, (m, a) => '是，且在非项目状态下始终允许 ' + a],
        [/^Working[.][.][.] Requesting permission to run (.+)$/, (m, a) => '运行中... 正在请求权限以运行 ' + a],
        [/^Ran ([0-9]+) commands? Explored ([0-9]+) tasks?$/, (m, a, b) => '已运行 ' + a + ' 条命令，已浏览 ' + b + ' 个任务'],
        [/^Ran ([0-9]+) commands? Working[.][.][.]$/, (m, a) => '已运行 ' + a + ' 条命令 运行中...'],
        [/^Explored ([0-9]+) files? Working[.][.][.]$/, (m, a) => '已浏览 ' + a + ' 个文件 运行中...'],
        [/^Updated[ ]+([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?:[ ]*[AaPp][Mm])?)$/i, (m, t) => '更新于 ' + t],
        [/^Updated[ ]+([0-9]+[ ]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ ]*[0-9]*)$/i, (m, d) => '更新于 ' + d],
        [/^Created[ ]+([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?:[ ]*[AaPp][Mm])?)$/i, (m, t) => '创建于 ' + t],
        [/^Created[ ]+([0-9]+[ ]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ ]*[0-9]*)$/i, (m, d) => '创建于 ' + d],
        [/^Mark ([0-9]+) conversations? as (read|unread)$/i, (m, count, state) => '将 ' + count + ' 个对话标记为' + (state.toLowerCase() === 'read' ? '已读' : '未读')],
        [/^Mark all(?: conversations)? as (read|unread)$/i, (m, state) => '将全部对话标记为' + (state.toLowerCase() === 'read' ? '已读' : '未读')],
        [/^Thought for ([0-9.]+[ ]*[smhd])$/i, (m, a) => '思考完成于 ' + a.replace('s','秒').replace('m','分').replace('h','小时').replace('d','天')],
        [/^Thinking for ([0-9.]+[ ]*[smhd])[.]*$/i, (m, a) => '正在思考（' + a.replace('s','秒').replace('m','分').replace('h','小时').replace('d','天') + '）'],
        [/^Exposes? ([0-9]+) tools?[,]?[ ]*and[ ]*([0-9]+) resources?[.]?$/i, (m, a, b) => '暴露 ' + a + ' 个工具和 ' + b + ' 个资源'],
        [/^Exposes? ([0-9]+) (tools?|resources?|prompts?)[.]?$/i, (m, count, type) => '暴露 ' + count + ' 个' + (type.toLowerCase().includes('tool') ? '工具' : (type.toLowerCase().includes('resource') ? '资源' : '提示词'))],
        [/^Command exited with code ([0-9]+)[.]?$/i, (m, code) => '命令已退出（返回码 ' + code + '）'],
        [/^([0-9,]+)[ ]+(input|output|cached)[ ]+tokens?$/i, (m, count, type) => count + ' ' + (type.toLowerCase() === 'input' ? '输入' : (type.toLowerCase() === 'output' ? '输出' : '缓存')) + ' Token'],
        [/^([0-9]+)d$/, (m, a) => a + ' 天前'],
        [/^([0-9]+)h$/, (m, a) => a + ' 小时前'],
        [/^([0-9]+)m$/, (m, a) => a + ' 分钟前'],
        [/^([0-9]+)s$/, (m, a) => a + ' 秒前'],
        [/^just now$/i, () => '刚刚'],
        [/^now$/i, () => '刚刚'],
    ];
    function applyRegexRules(text) {
        for (const [re, fn] of REGEX_RULES) {
            if (re.test(text)) return text.replace(re, fn);
        }
        return text;
    }

    const TITLE_WORD_MAP = {
        'implement': '实现', 'integrate': '集成', 'optimize': '优化', 'authenticate': '认证',
        'auth': '认证', 'login': '登录', 'logout': '登出', 'register': '注册', 'signup': '注册',
        'create': '创建', 'add': '添加', 'delete': '删除', 'remove': '移除', 'update': '更新',
        'upgrade': '升级', 'downgrade': '降级', 'install': '安装', 'uninstall': '卸载',
        'setup': '配置', 'config': '配置', 'configure': '配置', 'check': '检查', 'inspect': '检查',
        'audit': '审计', 'review': '审查', 'clean': '清理', 'clear': '清理', 'flush': '刷新',
        'fix': '修复', 'patch': '修补', 'repair': '修复', 'resolve': '解决', 'handle': '处理',
        'process': '处理', 'parse': '解析', 'format': '格式化', 'lint': '代码规范', 'test': '测试',
        'benchmark': '压测', 'debug': '调试', 'troubleshoot': '排查', 'trace': '追踪', 'track': '跟踪',
        'monitor': '监控', 'alert': '告警', 'notify': '通知', 'log': '日志', 'record': '记录',
        'analyze': '分析', 'measure': '度量', 'evaluate': '评估',
        'build': '构建', 'compile': '编译', 'bundle': '打包', 'deploy': '部署',
        'release': '发布', 'publish': '发布', 'distribute': '分发', 'export': '导出', 'import': '导入',
        'sync': '同步', 'backup': '备份', 'restore': '恢复', 'migrate': '迁移', 'transfer': '传输',
        'connect': '连接', 'disconnect': '断开', 'reconnect': '重连', 'search': '搜索', 'find': '查找',
        'query': '查询', 'fetch': '获取', 'get': '获取', 'pull': '拉取', 'push': '推送', 'commit': '提交',
        'merge': '合并', 'rebase': '变基', 'branch': '分支', 'clone': '克隆', 'fork': '派生',
        'generate': '生成', 'render': '渲染', 'display': '展示', 'show': '显示', 'hide': '隐藏',
        'start': '启动', 'init': '初始化', 'launch': '启动', 'boot': '引导', 'stop': '停止',
        'terminate': '终止', 'kill': '终止', 'restart': '重启', 'reload': '重载', 'reset': '重置',
        'load': '加载', 'unload': '卸载', 'mount': '挂载', 'unmount': '卸载', 'attach': '挂载',
        'detach': '分离', 'enable': '启用', 'disable': '禁用', 'activate': '激活', 'deactivate': '停用',
        'lock': '锁定', 'unlock': '解锁', 'encrypt': '加密', 'decrypt': '解密', 'sign': '签名',
        'verify': '校验', 'validate': '验证', 'encode': '编码', 'decode': '解码', 'compress': '压缩',
        'decompress': '解压', 'archive': '归档', 'extract': '提取', 'filter': '筛选', 'sort': '排序',
        'group': '分组', 'split': '拆分', 'combine': '合并', 'convert': '转换', 'transform': '转换',
        'user': '用户', 'admin': '管理员', 'role': '角色', 'permission': '权限', 'access': '访问',
        'account': '账户', 'profile': '个人资料', 'member': '成员', 'team': '团队',
        'project': '项目', 'workspace': '工作区', 'repo': '仓库', 'repository': '仓库', 'folder': '文件夹',
        'directory': '目录', 'file': '文件', 'doc': '文档', 'document': '文档', 'documentation': '文档',
        'code': '代码', 'codebase': '代码库', 'source': '源码', 'script': '脚本', 'style': '样式',
        'template': '模板', 'theme': '主题', 'layout': '布局', 'view': '视图', 'page': '页面',
        'screen': '屏幕', 'component': '组件', 'widget': '挂件', 'element': '元素', 'node': '节点',
        'dialog': '弹窗', 'modal': '弹窗', 'popup': '弹窗', 'tooltip': '提示气泡', 'banner': '横幅',
        'header': '顶部栏', 'footer': '底部栏', 'sidebar': '侧边栏', 'navbar': '导航栏', 'menu': '菜单',
        // image 在这里必须是图片义：界面里 Upload/Paste/Add image 都是图片。
        // Docker 的镜像义靠 dicts 里的整条短语在第 1 级命中，不走这里。
        'button': '按钮', 'icon': '图标', 'image': '图片', 'photo': '照片', 'video': '视频', 'audio': '音频',
        'data': '数据', 'dataset': '数据集', 'database': '数据库', 'db': '数据库', 'table': '数据表',
        'column': '列', 'row': '行', 'field': '字段', 'index': '索引', 'schema': '架构',
        'model': '模型', 'entity': '实体', 'relation': '关系', 'key': '键', 'value': '值', 'item': '项目',
        'list': '列表', 'array': '数组', 'object': '对象', 'string': '字符串', 'number': '数字',
        'class': '类', 'function': '函数', 'method': '方法', 'variable': '变量', 'constant': '常量',
        'parameter': '参数', 'argument': '参数', 'event': '事件', 'handler': '处理器', 'hook': '钩子',
        'plugin': '插件', 'extension': '扩展', 'module': '模块', 'package': '依赖包', 'library': '库',
        'framework': '框架', 'dependency': '依赖', 'sdk': 'SDK', 'api': 'API', 'endpoint': '端点',
        'route': '路由', 'router': '路由器', 'request': '请求', 'response': '响应', 'status': '状态',
        'error': '错误', 'warning': '警告', 'info': '信息', 'bug': '错误', 'issue': '问题',
        'performance': '性能', 'latency': '延迟', 'throughput': '吞吐量', 'memory': '内存',
        'cpu': 'CPU', 'disk': '磁盘', 'storage': '存储', 'io': 'IO', 'network': '网络',
        'traffic': '流量', 'port': '端口', 'socket': '套接字', 'protocol': '协议', 'proxy': '代理',
        'gateway': '网关', 'firewall': '防火墙', 'dns': 'DNS', 'domain': '域名', 'host': '主机',
        'server': '服务器', 'client': '客户端', 'agent': '智能体', 'bot': '机器人', 'crawler': '爬虫',
        'worker': '工作进程', 'job': '作业', 'task': '任务', 'cron': '定时任务', 'schedule': '计划',
        'queue': '队列', 'stack': '堆栈', 'cache': '缓存', 'buffer': '缓冲区', 'session': '会话',
        'cookie': 'Cookie', 'token': 'Token', 'secret': '密钥', 'password': '密码', 'security': '安全',
        'rule': '规则', 'policy': '策略', 'license': '许可证', 'version': '版本', 'vibe': '灵感',
        'chat': '对话', 'conversation': '对话', 'history': '历史', 'summary': '摘要', 'prompt': '提示词',
        'game': '游戏', 'snake': '贪吃蛇', 'tetris': '俄罗斯方块', 'calculator': '计算器', 'todo': '待办',
        'payment': '支付', 'order': '订单', 'cart': '购物车', 'price': '价格', 'product': '商品',
        'docker': 'Docker', 'container': '容器', 'compose': 'Compose', 'k8s': 'K8s',
        'kubernetes': 'Kubernetes', 'cluster': '集群', 'pod': 'Pod', 'cloud': '云', 'pipeline': '流水线',
        'ci': 'CI', 'cd': 'CD', 'workflow': '工作流', 'step': '步骤', 'git': 'Git', 'github': 'GitHub',
        'linux': 'Linux', 'macos': 'macOS', 'windows': 'Windows', 'unit': '单元', 'structure': '结构',
        'junk': '垃圾', 'temp': '临时', 'frontend': '前端', 'backend': '后端', 'fullstack': '全栈',
        'crash': '崩溃', 'redis': 'Redis', 'postgres': 'PostgreSQL',
        'postgresql': 'PostgreSQL', 'mysql': 'MySQL', 'mongo': 'MongoDB', 'mongodb': 'MongoDB',
        'sqlite': 'SQLite', 'kafka': 'Kafka', 'nginx': 'Nginx', 'aws': 'AWS', 'gcp': 'GCP', 'azure': 'Azure',
        'analysis': '分析', 'leak': '泄漏', 'inspection': '检查',
        'scan': '扫描', 'scanning': '扫描', 'scanned': '扫描'
    };

    const STEM_SUFFIXES = [
        [/ization$/, 'ize'],
        [/isation$/, 'ise'],
        [/uration$/, 'ure'],
        [/ication$/, 'y'],
        [/ation$/, 'ate'],
        [/ation$/, ''],
        [/tion$/, 't'],
        [/tion$/, ''],
        [/sion$/, ''],
        [/ment$/, ''],
        [/gging$/, 'g'],
        [/tting$/, 't'],
        [/pping$/, 'p'],
        [/nning$/, 'n'],
        [/mming$/, 'm'],
        [/rring$/, 'r'],
        [/dding$/, 'd'],
        [/bbing$/, 'b'],
        [/ing$/, ''],
        [/ing$/, 'e'],
        [/gged$/, 'g'],
        [/tted$/, 't'],
        [/pped$/, 'p'],
        [/nned$/, 'n'],
        [/mmed$/, 'm'],
        [/rred$/, 'r'],
        [/dded$/, 'd'],
        [/bbed$/, 'b'],
        [/ed$/, ''],
        [/ed$/, 'e'],
        [/ies$/, 'y'],
        [/es$/, ''],
        [/s$/, ''],
        [/able$/, ''],
        [/ive$/, '']
    ];

    // 两遍查询：先把 TITLE_WORD_MAP（原形 + 全部词干变体）穷尽，再回退字典。
    // 不能把两个来源交错在一个循环里——那样 'packages' 会先撞上字典的 '包'，
    // 而 'package' 直接命中词表的 '依赖包'，同一个词的单复数译出两个结果。
    // 逐词查询的 memo：界面上同一批单词反复出现（Settings/File/Open/Delete…），
    // 而每次未命中都要跑 33 条 STEM_SUFFIXES × 两轮 = 上百次正则+查表。
    // 实测未命中的短标签走完整链要 ~16 µs，其中绝大部分在这里。
    // 缓存单词级结果（含"查不到"这个结论，用 null 表示），命中后是一次 Map 查询。
    // 单词集合有限（界面词汇量），不会无界增长；上限兜底防异常输入刷爆。
    const wordCache = new Map();
    const WORD_CACHE_MAX = 4000;

    function lookupWord(clean) {
        if (wordCache.has(clean)) return wordCache.get(clean);
        const r = lookupWordUncached(clean);
        if (wordCache.size < WORD_CACHE_MAX) wordCache.set(clean, r);
        return r;
    }

    function lookupWordUncached(clean) {
        if (TITLE_WORD_MAP[clean]) return TITLE_WORD_MAP[clean];
        for (const [re, rep] of STEM_SUFFIXES) {
            const stem = clean.replace(re, rep);
            if (!stem) continue;
            if (TITLE_WORD_MAP[stem]) return TITLE_WORD_MAP[stem];
            if (TITLE_WORD_MAP[stem + 'e']) return TITLE_WORD_MAP[stem + 'e'];
        }
        if (lowerMap.has(clean) && lowerMap.get(clean).length <= 12) return lowerMap.get(clean);
        for (const [re, rep] of STEM_SUFFIXES) {
            const stem = clean.replace(re, rep);
            if (stem && lowerMap.has(stem) && lowerMap.get(stem).length <= 12) return lowerMap.get(stem);
        }
        return null;
    }

    const TECH_IDENTIFIERS = new Set([
        'claude', 'gemini', 'gpt', 'ui', 'api', 'sdk', 'ide', 'mcp', 'git', 'css', 'html', 'js', 'ts',
        'python', 'node', 'vue', 'react', 'docker', 'linux', 'macos', 'windows', 'redis', 'postgres',
        'mysql', 'mongodb', 'sqlite', 'kafka', 'nginx', 'aws', 'gcp', 'azure', 'kubernetes', 'k8s',
        'java', 'rust', 'go', 'golang', 'cpp', 'php', 'ruby', 'spring', 'fastapi', 'flask', 'django',
        'codex', 'vibebar'
    ]);

    const STOPWORDS = {
        'the': '', 'a': '', 'an': '', 'of': '', 'for': '', 'with': '与',
        'in': '', 'on': '', 'and': '与', 'to': '到', 'from': '来自', 'by': '由',
        'into': '入'
    };

    function translatePhraseTokens(text) {
        if (!text || text.length > 70) return text;
        const tokens = text.trim().split(/[ ]+/);
        if (tokens.length < 1 || tokens.length > 10) return text;

        if (tokens.length === 1) {
            const tok = tokens[0];
            const clean = tok.toLowerCase().replace(/^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$/g, '');
            if (!clean) return text;
            const looked = lookupWord(clean);
            return looked || text;
        }

        const translated = [];
        let hasTranslatedWord = false;
        for (const tok of tokens) {
            const clean = tok.toLowerCase().replace(/^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$/g, '');
            if (!clean) continue;
            if (STOPWORDS[clean] !== undefined) {
                if (STOPWORDS[clean]) translated.push(STOPWORDS[clean]);
            } else {
                const looked = lookupWord(clean);
                if (looked) {
                    translated.push(looked);
                    hasTranslatedWord = true;
                } else if (map.has(tok)) {
                    translated.push(map.get(tok));
                    hasTranslatedWord = true;
                } else if (/^[0-9.]+$/.test(tok)) {
                    translated.push(tok);
                } else if (tok === tok.toUpperCase() || /^[A-Z][A-Za-z0-9_.-]*$/.test(tok) || TECH_IDENTIFIERS.has(clean)) {
                    translated.push(tok);
                } else {
                    return text;
                }
            }
        }
        if (!hasTranslatedWord || translated.length === 0) return text;
        let res = '';
        for (let i = 0; i < translated.length; i++) {
            if (i > 0) {
                const prev = translated[i - 1];
                const cur = translated[i];
                if (/[A-Za-z0-9]$/.test(prev) || /^[A-Za-z0-9]/.test(cur)) {
                    res += ' ';
                }
            }
            res += translated[i];
        }
        return res;
    }

    function translateAttrValue(raw) {
        const t = norm(raw);
        if (!t) return null;
        if (map.has(t)) return map.get(t);
        const low = t.toLowerCase();
        if (lowerMap.has(low) && t.length < 30) return lowerMap.get(low);
        const byRegex = applyRegexRules(t);
        if (byRegex !== t) return byRegex;
        const phraseTrans = translatePhraseTokens(t);
        return phraseTrans !== t ? phraseTrans : null;
    }

    // ---- 原文暂存：让「上一版引擎译错的地方」能被新引擎救回 ----
    //
    // 文本节点挂不了属性，所以存在父元素上，按它在 childNodes 里的下标做键：
    //   data-ag-i18n = {"2": ["Open Remote Control", "打开远程 Control"]}
    // 同时存原文与当时写下的译文。还原前要求当前值仍等于那个译文——
    // 这样 React 重渲染换掉节点后，陈旧的暂存不会把无关文本改掉。
    // 这个属性不在 TRANSLATABLE_ATTRS 里，所以不会触发 observer 的 attributeFilter。
    const STASH_ATTR = 'data-ag-i18n';

    function stashOriginal(node, original, translated) {
        try {
            const el = node.parentElement;
            if (!el) return;
            const idx = [].indexOf.call(el.childNodes, node);
            if (idx < 0) return;
            let obj = {};
            const raw = el.getAttribute(STASH_ATTR);
            if (raw) { try { obj = JSON.parse(raw) || {}; } catch (e) { obj = {}; } }
            obj[idx] = [original, translated];
            el.setAttribute(STASH_ATTR, JSON.stringify(obj));
        } catch (e) {}
    }

    // 还原成功返回 true（调用方要重读 nodeValue），没还原返回 false。
    // 不在这里动 lastSeen：调用方只在 !lastSeen.has(node) 时才调它，
    // 动了反而会破坏早退阻尼。
    function restoreOriginal(node) {
        try {
            const el = node.parentElement;
            if (!el) return false;
            const raw = el.getAttribute(STASH_ATTR);
            if (!raw) return false;
            let obj;
            try { obj = JSON.parse(raw); } catch (e) { return false; }
            if (!obj) return false;
            const idx = [].indexOf.call(el.childNodes, node);
            const rec = obj[idx];
            if (!rec) return false;
            // 只有当前值仍是我们写下的那个译文时才还原，否则说明内容已被应用改过
            if (node.nodeValue === rec[1] && rec[0] !== rec[1]) {
                node.nodeValue = rec[0];
                return true;
            }
        } catch (e) {}
        return false;
    }

    // 单个元素的属性翻译，带 memo：值没变就整条跳过。
    // 属性不查禁区——它是 UI 元数据而非用户输入，查了会漏翻输入框提示。
    function translateAttrs(el) {
        let seen = attrSeen.get(el);
        for (const attr of TRANSLATABLE_ATTRS) {
            const v = el.getAttribute(attr);
            if (!v) continue;
            if (seen && seen[attr] === v) continue;   // 上一轮处理过且没变
            const t = translateAttrValue(v);
            const final = (t !== null && t !== v) ? t : v;
            if (final !== v) el.setAttribute(attr, final);
            if (!seen) { seen = {}; attrSeen.set(el, seen); }
            seen[attr] = final;
        }
    }

    function translateNode(node) {
        try {
            if (!node) return;

            if (node.nodeType === Node.ELEMENT_NODE) {
                const tag = node.tagName.toUpperCase();

                translateAttrs(node);

                if (BLOCKED_TAGS.includes(tag)) return;

                if (node.shadowRoot) translateNode(node.shadowRoot);
                for (const child of node.childNodes) translateNode(child);

            } else if (node.nodeType === Node.TEXT_NODE) {
                let originalVal = node.nodeValue;
                if (!originalVal || originalVal.trim().length < 1) return;

                // lastSeen 的早退必须在还原之前。
                // observer 听 characterData，每次写 nodeValue 都会回调进来；
                // 若在早退前还原，就成了「还原→译→还原→译」的死循环，把渲染进程挂死
                // （实测过：注入后 CDP 完全不响应）。lastSeen 就是那道阻尼。
                if (lastSeen.get(node) === originalVal) return;

                // 只在本引擎还没处理过这个节点时才尝试还原（新引擎的 lastSeen 是空的）。
                // 上一版引擎可能把它译错了（词条不全时逐词译出「打开远程 Control」这种
                // 中英混杂）；译文写回 DOM 后原文就没了，改坏的文本匹配不上任何词条，
                // 光重新注入救不回来。所以带原文一起存，新引擎还原后重译。
                if (!lastSeen.has(node) && restoreOriginal(node)) {
                    originalVal = node.nodeValue;
                }
                if (!/[A-Za-z]/.test(originalVal)) { lastSeen.set(node, originalVal); return; }
                if (isInBlockedZone(node)) { lastSeen.set(node, originalVal); return; }

                let newVal = originalVal;
                const valNorm = norm(originalVal);
                const valLower = valNorm.toLowerCase();

                if (map.has(valNorm)) {
                    newVal = map.get(valNorm);
                } else if (lowerMap.has(valLower) && valLower.length < 30) {
                    newVal = lowerMap.get(valLower);
                } else {
                    const byRegex = applyRegexRules(valNorm);
                    if (byRegex !== valNorm) {
                        newVal = byRegex;
                    } else if (!isProtected(valNorm)) {
                        const phraseTrans = translatePhraseTokens(valNorm);
                        if (phraseTrans !== valNorm) {
                            newVal = phraseTrans;
                        } else {
                            newVal = smartReplace(valNorm);
                        }
                    }
                }

                if (newVal !== originalVal && newVal !== valNorm) {
                    stashOriginal(node, originalVal, newVal);
                    node.nodeValue = newVal;
                    lastSeen.set(node, newVal);
                } else {
                    lastSeen.set(node, originalVal);
                    if (misses.size < 2000 && valNorm.length >= 3) misses.add(valNorm);
                }
            }
        } catch (e) {}
    }

    const observer = new MutationObserver(mutations => {
        for (const m of mutations) {
            if (m.type === 'childList') {
                for (const n of m.addedNodes) translateNode(n);
            } else if (m.type === 'characterData') {
                translateNode(m.target);
            } else if (m.type === 'attributes' && m.target) {
                translateNode(m.target);
            }
        }
    });

    const obsOpts = {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: TRANSLATABLE_ATTRS
    };

    // 补扫 translateNode 递归到不了的属性：BLOCKED_TAGS 的子树会被 return 掉，
    // 但里面的 INPUT placeholder 之类仍需翻译。走同一个 translateAttrs，
    // 因此享受同一份 memo——同一轮里已处理过的元素在这里是纯查表跳过。
    function rescanAttributes() {
        if (!document.body) return;
        const sel = TRANSLATABLE_ATTRS.map(a => '[' + a + ']').join(',');
        for (const el of document.body.querySelectorAll(sel)) {
            if (!el.isConnected) continue;
            translateAttrs(el);
        }
    }

    function updateTitle() {
        try {
            const cur = document.title;
            if (cur && /[A-Za-z]/.test(cur)) {
                const normCur = norm(cur);
                if (map.has(normCur)) {
                    document.title = map.get(normCur);
                } else {
                    const byRegex = applyRegexRules(normCur);
                    if (byRegex !== normCur) {
                        document.title = byRegex;
                    } else if (!isProtected(normCur)) {
                        const phraseTrans = translatePhraseTokens(normCur);
                        if (phraseTrans !== normCur) {
                            document.title = phraseTrans;
                        } else {
                            const rep = smartReplace(normCur);
                            if (rep !== normCur) document.title = rep;
                        }
                    }
                }
            }
        } catch (e) {}
    }

    const CONV_TITLES = CONV_TITLES_PLACEHOLDER;
    const dynamicTitles = Object.assign({}, CONV_TITLES);
    const CHINESE_CHAR_RE = new RegExp('[\\u4e00-\\u9fa5]');

    function distillTitle(raw) {
        if (!raw) return '';
        const lines = raw.split(/[\\r\\n]+/).map(s => s.trim()).filter(Boolean);
        if (lines.length === 0) return '';
        const firstLine = lines[0];
        const clauses = firstLine.split(/[，。！？\\n；,!?\\r\\t|/]/).map(s => s.trim()).filter(Boolean);
        if (clauses.length === 0) return '';
        let core = clauses[0];

        const prefixPatterns = [
            /^(请帮我|帮我|请你|请问|请|麻烦帮我|麻烦|我想|我需要|看一下|看下|查一下|查找|能否|能不能|如何|怎么|测试一下|写一个|做一个|实现一个|开发一个|搞一个|弄一个|快速|帮我分析一下|分析一下)+/,
            /^(做一下|做个|写个|弄个|查个)+/,
            /^(针对|关于|这个)+/
        ];

        let changed = true;
        while (changed) {
            changed = false;
            for (const pat of prefixPatterns) {
                const newCore = core.replace(pat, '').trim();
                if (newCore !== core && newCore.length >= 2) {
                    core = newCore;
                    changed = true;
                }
            }
        }

        core = core.replace(/[\\?？\\.\\!！,，_—\\-:：]+$/, '').trim();
        if (core.length > 15) {
            let trimmed = core.slice(0, 15);
            if (core[14] && /[A-Za-z0-9]/.test(core[14]) && /[A-Za-z0-9]/.test(core[15])) {
                const lastSpace = trimmed.lastIndexOf(' ');
                if (lastSpace >= 6) trimmed = trimmed.slice(0, lastSpace).trim();
            }
            core = trimmed;
        }
        return core;
    }

    // lastSeen 的键是「文本节点」，不是元素。这里写完标题后要标记 span 底下的
    // 文本节点，否则 translateNode 下一轮照样处理它——标记在 span 元素上永远读不到。
    function markHandled(el, value) {
        try {
            lastSeen.set(el, value);
            for (const child of el.childNodes) {
                if (child.nodeType === Node.TEXT_NODE) lastSeen.set(child, child.nodeValue);
            }
        } catch (e) {}
    }

    function syncConversationTitles() {
        try {
            const activeEl = document.activeElement;
            if (activeEl && (activeEl.tagName === 'INPUT' || activeEl.tagName === 'TEXTAREA' || activeEl.isContentEditable)) {
                return;
            }

            const path = window.location.pathname || '';
            const matchCid = path.match(/\\/c\\/([a-f0-9-]+)/);
            const curCid = matchCid ? matchCid[1] : null;
            if (curCid && !dynamicTitles[curCid]) {
                const firstUserMsg = document.querySelector('div[class*="group/user-input"], div[class*="user-input"]');
                if (firstUserMsg) {
                    const text = (firstUserMsg.innerText || '').trim();
                    if (CHINESE_CHAR_RE.test(text)) {
                        const distilled = distillTitle(text);
                        if (distilled) dynamicTitles[curCid] = distilled;
                    } else if (text.length >= 2) {
                        const trans = translatePhraseTokens(text.split(/[\\r\\n]+/)[0]);
                        if (trans && trans !== text) dynamicTitles[curCid] = trans.slice(0, 15);
                    }
                }
            }

            for (const a of document.querySelectorAll('a[href*="/c/"]')) {
                const href = a.getAttribute('href') || '';
                const match = href.match(/\\/c\\/([a-f0-9-]+)/);
                if (!match) continue;
                const cid = match[1];
                const row = a.closest('div.relative') || a.parentElement;
                if (!row) continue;
                const span = row.querySelector('span.truncate') || row.querySelector('span');
                if (!span) continue;

                const curSpanText = (span.innerText || '').trim();
                const ariaLabel = (a.getAttribute('aria-label') || '').trim();

                // 1. 最高优先级：用户手动重命名（aria-label 含中文或用户自定名称），绝不覆盖！
                if (ariaLabel && CHINESE_CHAR_RE.test(ariaLabel)) {
                    if (curSpanText !== ariaLabel) {
                        span.innerText = ariaLabel;
                        markHandled(span, ariaLabel);
                    }
                    continue;
                }

                // 2. 只有当前标题还是英文或未汉化时，才用提炼或词库标题替换
                const targetTitle = dynamicTitles[cid];
                if (targetTitle && curSpanText !== targetTitle && (!CHINESE_CHAR_RE.test(curSpanText) || /[A-Za-z]{4,}/.test(curSpanText))) {
                    span.innerText = targetTitle;
                    markHandled(span, targetTitle);
                }
            }

            if (curCid) {
                const activeA = document.querySelector('a[href*="/c/' + curCid + '"]');
                const aria = activeA ? (activeA.getAttribute('aria-label') || '').trim() : '';
                const titleToUse = (aria && CHINESE_CHAR_RE.test(aria)) ? aria : dynamicTitles[curCid];
                if (titleToUse && document.title !== titleToUse) {
                    document.title = titleToUse;
                }
            }
        } catch (e) {}
    }

    let started = false;
    let stopped = false;
    const startEngine = () => {
        if (started || stopped || !document.body) return;
        started = true;
        observer.observe(document.body, obsOpts);
        const titleEl = document.querySelector('title');
        if (titleEl) observer.observe(titleEl, { childList: true, characterData: true, subtree: true });
        translateNode(document.body);
        updateTitle();
        syncConversationTitles();
    };

    if (!Element.prototype.attachShadow.__ag_hooked__) {
        const origAttachShadow = Element.prototype.attachShadow;
        const hooked = function () {
            const sr = origAttachShadow.apply(this, arguments);
            const eng = window.__ag_hanhua_engine__;
            if (eng && eng.observe) { try { eng.observe(sr); } catch (e) {} }
            return sr;
        };
        hooked.__ag_hooked__ = true;
        Element.prototype.attachShadow = hooked;
    }

    startEngine();
    if (!started) {
        document.addEventListener('DOMContentLoaded', startEngine, { once: true });
    }
    setTimeout(startEngine, 300);
    // 全量重扫：一轮 = translateNode(整棵 body) + updateTitle + rescanAttributes
    // + syncConversationTitles。窗口不可见时这一轮纯属白烧 CPU——界面没人看，
    // DOM 也基本不动。document.hidden 时跳过，切回前台再补一轮。
    // MutationObserver 始终挂着，所以隐藏期间真有 DOM 变化仍会被实时翻译，
    // 跳过重扫不会漏翻。
    const fullSweep = () => {
        if (stopped) return;
        startEngine();
        if (document.body) translateNode(document.body);
        updateTitle();
        rescanAttributes();
        syncConversationTitles();
    };
    const timerId = setInterval(() => {
        if (document.hidden) return;
        fullSweep();
    }, 2000);
    // 从后台切回前台：立刻补一轮，不等下一个 tick
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) fullSweep();
    });
    // 冷启动补扫：SPA 挂载有先后，这两枪覆盖首屏渲染完成前后
    setTimeout(fullSweep, 2000);
    setTimeout(fullSweep, 5000);

    window.__ag_hanhua_engine__ = {
        observe: (root) => observer.observe(root, obsOpts),
        misses: misses,
        disconnect: () => {
            stopped = true;
            try { observer.disconnect(); } catch (e) {}
            try { clearInterval(timerId); } catch (e) {}
        }
    };
})();
"""
    db_titles = get_db_conversation_titles()
    db_titles_json = json.dumps(db_titles, ensure_ascii=False)
    return js_source.replace("DICT_PLACEHOLDER", dict_json).replace("CONV_TITLES_PLACEHOLDER", db_titles_json)


def distill_chinese_title(raw):
    """从中文原始 Prompt 提炼干净、精准、干练的会话标题"""
    if not raw:
        return ''
    lines = [l.strip() for l in raw.split('\n') if l.strip()]
    if not lines:
        return ''
    first_line = lines[0]
    clauses = [c.strip() for c in re.split(r'[，。！？\n；,!?\r\t|/]', first_line) if c.strip()]
    if not clauses:
        return ''
    core = clauses[0]

    prefix_patterns = [
        r'^(请帮我|帮我|请你|请问|请|麻烦帮我|麻烦|我想|我需要|看一下|看下|查一下|查找|能否|能不能|如何|怎么|测试一下|写一个|做一个|实现一个|开发一个|搞一个|弄一个|快速|帮我分析一下|分析一下)+',
        r'^(做一下|做个|写个|弄个|查个)+',
        r'^(针对|关于|这个)+'
    ]

    changed = True
    while changed:
        changed = False
        for pat in prefix_patterns:
            new_core = re.sub(pat, '', core).strip()
            if new_core != core and len(new_core) >= 2:
                core = new_core
                changed = True

    core = re.sub(r'[\?？\.\!！,，_—\-:：]+$', '', core).strip()
    if len(core) > 15:
        trimmed = core[:15]
        if len(core) > 15 and core[14].isalnum() and core[15].isalnum():
            last_space = trimmed.rfind(' ')
            if last_space >= 6:
                trimmed = trimmed[:last_space].strip()
        core = trimmed
    return core


def get_db_conversation_titles():
    """从本地 SQLite 对话数据库读取用户最初的中文 Prompt，作为会话的原生真实中文标题"""
    import glob, sqlite3
    conv_dir = os.path.expanduser('~/.gemini/antigravity/conversations')
    titles = {}
    if not os.path.exists(conv_dir):
        return titles
    # 按最后修改时间倒序，只扫描最近 50 个会话，毫秒级快速读取
    db_files = sorted(
        glob.glob(os.path.join(conv_dir, '*.db')),
        key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0,
        reverse=True
    )[:50]
    for db_file in db_files:
        cid = os.path.basename(db_file).replace('.db', '')
        try:
            conn = sqlite3.connect(f'file:{db_file}?mode=ro', uri=True, timeout=0.1)
            cur = conn.cursor()
            cur.execute('SELECT step_payload FROM steps WHERE idx=0;')
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                payload = row[0].decode('utf-8', errors='ignore')
                matches = re.findall(r'[\u4e00-\u9fa5A-Za-z0-9_，。！？、 ]{2,}', payload)
                chinese_matches = [m.strip() for m in matches if re.search(r'[\u4e00-\u9fa5]', m)]
                if chinese_matches:
                    raw_prompt = chinese_matches[0]
                    distilled = distill_chinese_title(raw_prompt)
                    if distilled:
                        titles[cid] = distilled
        except Exception:
            pass
    return titles


# ============================================================
# CDP 端口发现与页面定位
# ============================================================

def http_json(host, port, path, timeout=0.5):
    """GET http://host:port/path → dict/list，失败抛异常（针对 localhost 极速 0.5s 超时）"""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return json.loads(resp.read())
    finally:
        conn.close()


def _port_cache_file():
    """端口缓存放用户私有目录，不放共享临时目录。

    /tmp 是共享目录，固定名文件可被同机其他用户抢先建成符号链接，
    我们再以自己的权限写进去。改用用户私有缓存目录（macOS 0700 /
    Windows %LOCALAPPDATA% 本身即用户私有）就没有这个面。
    """
    try:
        os.makedirs(_CACHE_BASE, mode=0o700, exist_ok=True)
    except Exception:
        pass
    return os.path.join(_CACHE_BASE, 'last_cdp_port')


PORT_CACHE_FILE = _port_cache_file()


def discover_cdp_port(timeout=30):
    """macOS：Antigravity 忽略传入的 --remote-debugging-port，总是自己开随机端口的 CDP。
    扫描 Antigravity 主进程监听的 127.0.0.1 端口，找到响应 /json/version 的那个。
    自适应快速轮询（0.02s 起步），发现即返回。
    """
    deadline = time.time() + max(timeout, 0.01)
    interval = 0.02
    while True:
        port = _scan_ports_once()
        if port:
            return port
        if time.time() >= deadline or timeout == 0:
            return None
        time.sleep(interval)
        interval = min(interval * 1.4, 0.2)


def _scan_ports_once():
    # 1. 极速直达路径：读取 Chromium / Electron 原生 DevToolsActivePort（0.001s）
    if IS_WINDOWS:
        _appdata = os.environ.get('APPDATA') or ''
        devtools_candidates = tuple(p for p in (
            os.path.join(_appdata, 'Antigravity', 'DevToolsActivePort'),
            os.path.join(_appdata, 'Google', 'Antigravity', 'DevToolsActivePort'),
        ) if _appdata)
    else:
        devtools_candidates = (
            os.path.expanduser('~/Library/Application Support/Antigravity/DevToolsActivePort'),
            os.path.expanduser('~/Library/Application Support/Google/Antigravity/DevToolsActivePort'),
        )
    for devtools_cand in devtools_candidates:
        try:
            if os.path.exists(devtools_cand):
                with open(devtools_cand, 'r') as f:
                    port = int(f.readline().strip())
                data = http_json('127.0.0.1', port, '/json/version')
                if isinstance(data, dict) and _CDP_BROWSER_HINT in str(data.get('Browser', '')):
                    try:
                        with open(PORT_CACHE_FILE, 'w') as cf:
                            cf.write(str(port))
                    except Exception:
                        pass
                    return port
        except Exception:
            pass

    # 2. 优先探测上一轮成功过的缓存端口（0.05ms 超低延迟）
    try:
        if os.path.exists(PORT_CACHE_FILE):
            with open(PORT_CACHE_FILE, 'r') as f:
                cached_port = int(f.read().strip())
            data = http_json('127.0.0.1', cached_port, '/json/version')
            if isinstance(data, dict) and _CDP_BROWSER_HINT in str(data.get('Browser', '')):
                return cached_port
    except Exception:
        pass

    # 3. 扫描活跃进程端口（单次合并 lsof / netstat，避免循环内反复调用）
    pids = [pid for pid in antigravity_pids() if pid.isdigit()]
    if pids:
        listen_ports = []
        if IS_WINDOWS:
            try:
                ns = subprocess.run(['netstat', '-ano', '-p', 'tcp'],
                                    capture_output=True,
                                    timeout=5).stdout.decode(errors='replace')
                pid_set = set(pids)
                for line in ns.splitlines():
                    cols = line.split()
                    if (len(cols) >= 5 and cols[0].upper() == 'TCP'
                            and cols[3].upper() == 'LISTENING'
                            and cols[4] in pid_set):
                        m = re.match(r'127[.]0[.]0[.]1:(\d+)$', cols[1])
                        if m:
                            listen_ports.append(int(m.group(1)))
            except Exception:
                pass
        else:
            try:
                ls = subprocess.run(['lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-a', '-p', ','.join(pids)],
                                    capture_output=True, timeout=2).stdout.decode()
                for m in re.finditer(r'127[.]0[.]0[.]1:(\d+)', ls):
                    listen_ports.append(int(m.group(1)))
            except Exception:
                pass
        for p in listen_ports:
            try:
                data = http_json('127.0.0.1', p, '/json/version')
                if isinstance(data, dict) and _CDP_BROWSER_HINT in str(data.get('Browser', '')):
                    try:
                        with open(PORT_CACHE_FILE, 'w') as f:
                            f.write(str(p))
                    except Exception:
                        pass
                    return p
            except Exception:
                continue
    return None


def get_main_page(port):
    """取真正的主界面页（跳过加载页/空白页）"""
    pages = get_all_pages(port)
    return pages[0] if pages else None


def get_all_pages(port):
    """获取所有可用的界面页面（支持多窗口、多标签页全量注入）"""
    try:
        pages = http_json('127.0.0.1', port, '/json')
    except Exception:
        return []
    pages = pages if isinstance(pages, list) else []
    return [p for p in pages
            if isinstance(p, dict)
            and p.get('type') == 'page'
            and p.get('url', '').startswith('http')
            and not p.get('title', '').startswith('Loading')]


def wait_for_debug_port(port, timeout=60):
    """轮询指定 CDP 端口直到返回 page，返回 page 对象或 None"""
    deadline = time.time() + timeout
    interval = 0.02
    while time.time() < deadline:
        page = get_main_page(port)
        if page:
            return page
        time.sleep(interval)
        interval = min(interval * 1.3, 0.15)
    return None


def engine_alive(page):
    """页面上的翻译引擎是否在线（连接失败返回 False，由调用方决定重试）"""
    try:
        cdp = CDP(page['webSocketDebuggerUrl'], timeout=3)
        try:
            return cdp_value(cdp.evaluate("!!window.__ag_hanhua_engine__")) is True
        finally:
            cdp.close()
    except Exception:
        return False


# ============================================================
# 注入核心
# ============================================================

READY_EXPR = ("(() => { try { return !!document.body"
              " && (document.readyState === 'complete' || document.body.children.length > 0);"
              " } catch (e) { return false; } })()")


def inject_page(page, engine_js, watch_seconds=30, verbose=True):
    """通过 CDP 注入翻译引擎（同步 MiniWS 实现，超低延迟优化）。
    1. 立即注册 Page.addScriptToEvaluateOnNewDocument 保证全局生命周期自注入
    2. 极速等待 DOM 挂载（0.05s 自适应轮询）
    3. 立即注入当前视图 + 守护补注
    """
    cdp = CDP(page['webSocketDebuggerUrl'])
    try:
        # 立即注册页面生命周期挂钩（页面重载/导航自动携带）
        try:
            cdp.call("Page.enable")
            cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": engine_js})
        except Exception:
            pass

        if verbose:
            print("[等待] 等待界面挂载...")
        deadline = time.time() + 30
        interval = 0.05
        while time.time() < deadline:
            if cdp_value(cdp.evaluate(READY_EXPR)) is True:
                break
            time.sleep(interval)
            interval = min(interval * 1.5, 0.2)

        resp = cdp.evaluate(engine_js)
        if resp.get('error') or resp.get('result', {}).get('exceptionDetails'):
            return resp

        if watch_seconds > 0:
            if verbose:
                print(f"[守护] 监控 {watch_seconds} 秒，页面重载会自动补注...")
            reinjects = 0
            end = time.time() + watch_seconds
            while time.time() < end:
                time.sleep(1.5)
                alive = cdp_value(cdp.evaluate("!!window.__ag_hanhua_engine__"))
                if alive is not True:
                    r = cdp.evaluate(engine_js)
                    if r.get('error') or r.get('result', {}).get('exceptionDetails'):
                        if verbose:
                            print("[守护] 补注未成功，等待下轮重试")
                    else:
                        reinjects += 1
                        if verbose:
                            print(f"[守护] 检测到引擎丢失，已补注（第 {reinjects} 次）")
            if verbose:
                print(f"[守护] 结束，{'共补注 %d 次' % reinjects if reinjects else '引擎全程存活'}")
        return resp
    finally:
        cdp.close()


def inject_result(resp, err=None):
    """把注入结果判定成 (ok, 原因)。

    resp/err/exceptionDetails 这三层检查原先在 manual_main、daemon_step、
    click_main 里各抄了一遍，措辞还不一致。收成一处，改判定逻辑只需改这里。
    """
    if err:
        return False, err
    if resp is None:
        return False, "无响应"
    if resp.get('error'):
        return False, str(resp['error'])
    exc = (resp.get('result') or {}).get('exceptionDetails')
    if exc:
        detail = ((exc.get('exception') or {}).get('description')
                  or exc.get('text') or str(exc))
        return False, f"引擎脚本异常: {detail[:200]}"
    return True, None


def inject_with_retry(port, engine_js, watch_seconds, attempts=3, verbose=True):
    """带重试的全量注入：扫描所有窗口页面并全量注入"""
    for attempt in range(attempts):
        pages = get_all_pages(port)
        if not pages:
            page = wait_for_debug_port(port, timeout=30 if attempt else 5)
            if page:
                pages = [page]
        if not pages:
            return None, "页面未出现，Antigravity 可能已退出"
        try:
            last_resp = None
            for idx, p in enumerate(pages):
                last_resp = inject_page(p, engine_js, watch_seconds=watch_seconds if idx == 0 else 0,
                                        verbose=verbose and (idx == 0))
            return last_resp, None
        except Exception as e:
            if verbose:
                print(f"[重试] 连接中断（{type(e).__name__}），重新探测页面... ({attempt + 1}/{attempts})")
            time.sleep(1)
    return None, "多次重试后仍无法注入"


# ============================================================
# 常驻守护模式
# ============================================================

def daemon_loop(interval=DAEMON_INTERVAL, log_path=DAEMON_LOG):
    """常驻守护：每 interval 秒巡检一次。

    - Antigravity 未运行 → 跳过（本轮零开销）
    - CDP 端口未就绪（刚启动）→ 跳过，下轮再查
    - 引擎在线 → 跳过
    - 引擎离线（首次启动 / 页面重载被冲掉 / Antigravity 重启）→ 自动注入

    正常巡检不写日志（防止日志无限增长），只在注入和异常时追加。
    """
    def log(msg):
        line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg
        print(line, flush=True)
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(line + "\n")
        except Exception:
            pass

    log(f"[守护] 常驻模式启动（每 {interval} 秒巡检，日志: {log_path}）")
    while True:
        try:
            daemon_step(log)
        except Exception as e:
            log(f"[异常] {type(e).__name__}: {e}")
        time.sleep(interval)


def daemon_step(log):
    if not antigravity_pids():
        return
    port = discover_cdp_port(timeout=0)
    if not port:
        return
    pages = get_all_pages(port)
    if not pages:
        return
    # 必须查全部窗口，不能只看 pages[0]：多窗口时窗口 0 有引擎、新开的窗口没有，
    # 只判第一个就会直接 return，新窗口永远等不到补注。
    missing = [p for p in pages if not engine_alive(p)]
    if not missing:
        return
    if len(pages) > 1:
        log(f"[注入] {len(pages)} 个页面中 {len(missing)} 个未汉化（CDP 端口 {port}），自动注入...")
    else:
        log(f"[注入] 发现未汉化界面（CDP 端口 {port}），自动注入...")
    engine_js = get_engine_js()
    resp, err = inject_with_retry(port, engine_js, watch_seconds=0, attempts=2,
                                  verbose=False)
    ok, detail = inject_result(resp, err)
    if not ok:
        log(f"[注入] 失败: {detail}")
        return
    log("[注入] 完成，界面已汉化")


# ============================================================
# launchd 开机自启（macOS）
# ============================================================

def start_daemon_detached(interval=DAEMON_INTERVAL):
    """拉起脱离宿主的守护进程，宿主退出不影响。

    macOS 用 double-fork（脱离当前进程组，init 接管），用于 launchd 不可达的
    环境（如从 Electron 桌面端派生的终端——launchctl bootstrap/load 会报
    Input/output error 5）。Windows 用 DETACHED_PROCESS 等 creationflags。
    """
    if IS_WINDOWS:
        try:
            os.makedirs(os.path.dirname(DAEMON_LOG), exist_ok=True)
        except Exception:
            pass
        try:
            subprocess.Popen(
                [_bg_python(), os.path.abspath(__file__), '--daemon',
                 '--interval', str(interval)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, close_fds=True,
                **_detached_popen_kwargs())
            time.sleep(0.5)
            return True
        except Exception:
            return False
    try:
        pid = os.fork()
        if pid > 0:
            time.sleep(0.5)
            return True
        os.setsid()
        if os.fork() > 0:
            os._exit(0)
        log_f = open(DAEMON_LOG, 'a', encoding='utf-8')
        os.dup2(log_f.fileno(), 1)
        os.dup2(log_f.fileno(), 2)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.execv(sys.executable,
                 [sys.executable, os.path.abspath(__file__),
                  '--daemon', '--interval', str(interval)])
    except Exception:
        return False
    return False


def install_autostart_win():
    """Windows 登录自启：写 HKCU Run 注册表项，并立刻拉起守护进程。"""
    script = os.path.abspath(__file__)
    python = _bg_python()
    cmd = f'"{python}" "{script}" --daemon'
    r = subprocess.run(
        ['reg', 'add', 'HKCU\\' + RUN_KEY_PATH, '/v', RUN_KEY_NAME,
         '/t', 'REG_SZ', '/d', cmd, '/f'],
        capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[错误] 注册开机自启失败: {(r.stderr or r.stdout).strip()}")
        sys.exit(1)
    print("[成功] 已注册登录自启（注册表 Run 项）")
    print(f"       注册表: HKCU\\{RUN_KEY_PATH}\\{RUN_KEY_NAME}")
    print(f"       日志: {DAEMON_LOG}")
    if start_daemon_detached():
        print("[效果] 守护进程已启动；打开 Antigravity 后几秒内自动汉化，无需任何手动操作")
    else:
        print("[提示] 守护进程拉起失败，重启系统后会自动生效；或手动执行: "
              f"{python} {script} --daemon")


def _kill_daemon_win():
    """结束正在运行的汉化守护进程（pythonw/python + jack.py --daemon）。"""
    ps = ("Get-CimInstance Win32_Process | "
          "Where-Object { $_.Name -match '^pythonw?\\.exe$' "
          "-and $_.CommandLine -match 'jack\\.py' "
          "-and $_.CommandLine -match '--daemon' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
          "-ErrorAction SilentlyContinue }")
    try:
        subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def uninstall_autostart_win():
    r = subprocess.run(
        ['reg', 'delete', 'HKCU\\' + RUN_KEY_PATH, '/v', RUN_KEY_NAME, '/f'],
        capture_output=True, text=True)
    removed = r.returncode == 0
    _kill_daemon_win()
    if removed:
        print("[成功] 已停止守护并移除登录自启（注册表 Run 项）")
        print("[效果] 下次打开 Antigravity 将回到英文原版")
    else:
        print("[提示] 未安装（注册表 Run 项不存在）")


def autostart_status_win():
    r = subprocess.run(
        ['reg', 'query', 'HKCU\\' + RUN_KEY_PATH, '/v', RUN_KEY_NAME],
        capture_output=True, text=True)
    if r.returncode != 0:
        return "未安装"
    return "已安装（登录自启，注册表 Run 项）"


def install_launchd():
    if IS_WINDOWS:
        return install_autostart_win()
    script = os.path.abspath(__file__)
    python = sys.executable or 'python3'
    # 用 plistlib 而不是 f-string 拼 XML：路径里出现 & < > 时手拼会生成非法 plist，
    # launchd 静默拒绝加载，很难查。plistlib 负责转义。
    import plistlib
    plist_obj = {
        'Label': LAUNCHD_LABEL,
        'ProgramArguments': [python, script, '--daemon'],
        'RunAtLoad': True,
        'KeepAlive': True,
        'ProcessType': 'Background',
        'StandardOutPath': DAEMON_LOG,
        'StandardErrorPath': DAEMON_LOG,
    }
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(DAEMON_LOG), exist_ok=True)
    with open(PLIST_PATH, 'wb') as f:
        plistlib.dump(plist_obj, f)

    # launchctl load 失败时 exit code 仍可能为 0，必须看 stderr
    r = subprocess.run(['launchctl', 'load', '-w', PLIST_PATH], capture_output=True)
    err = (r.stderr or b'').decode(errors='replace')
    if 'failed' in err.lower() or 'error' in err.lower():
        # 从 Electron 桌面端（WorkBuddy 等）派生的终端与 launchd 通信会被拒，
        # 报 Input/output error 5。改为直接拉起守护（本次登录有效），
        # 并给出在系统终端执行的持久化命令。
        print("[提示] 当前环境无法注册 launchd 服务（桌面端 App 内嵌终端的常见限制）。")
        ok = start_daemon_detached()
        if ok:
            print("[成功] 守护进程已直接拉起（本次登录有效），打开 Antigravity 后自动汉化。")
        else:
            print("[错误] 守护进程拉起失败，请手动执行: "
                  f"{sys.executable} {script} --daemon")
        print()
        print("要开机自启（一劳永逸），请打开 系统终端（Terminal / Ghostty） 执行：")
        print(f"  launchctl load -w {PLIST_PATH}")
        return

    print(f"[成功] 守护进程已安装并启动（{LAUNCHD_LABEL}）")
    print(f"       配置: {PLIST_PATH}")
    print(f"       日志: {DAEMON_LOG}")
    print("[效果] 登录后自动守护；打开 Antigravity 后 5 秒内自动汉化，无需任何手动操作")


def uninstall_launchd():
    if IS_WINDOWS:
        return uninstall_autostart_win()
    # 杀掉所有守护进程（launchd 管理的或直接拉起的）
    subprocess.run(['pkill', '-f', 'jack.py --daemon'], capture_output=True)
    if os.path.exists(PLIST_PATH):
        subprocess.run(['launchctl', 'unload', PLIST_PATH], capture_output=True)
        os.remove(PLIST_PATH)
        print(f"[成功] 已停止守护并移除配置（{LAUNCHD_LABEL}）")
        print("[效果] 下次打开 Antigravity 将回到英文原版")
        # launchd 域里可能还挂着服务（本环境 launchctl 不可达时清不掉），
        # 给出系统终端的兜底命令
        print("[提示] 若提示仍在运行，在系统终端执行: "
              f"launchctl bootout gui/$(id -u)/{LAUNCHD_LABEL}")
    else:
        print(f"[提示] 未安装（{PLIST_PATH} 不存在）")


def launchd_status():
    if IS_WINDOWS:
        return autostart_status_win()
    if not os.path.exists(PLIST_PATH):
        return "未安装"
    r = subprocess.run(['launchctl', 'list'], capture_output=True, text=True)
    if LAUNCHD_LABEL in r.stdout:
        return "已安装（launchd 托管，运行中）"
    # launchctl 不可达的终端里 list 为空，退化用进程判断
    p = subprocess.run(['pgrep', '-f', 'jack.py --daemon'],
                       capture_output=True, text=True)
    return "已安装，守护运行中" if p.stdout.strip() else "已安装，守护未运行"


# ============================================================
# 状态查询
# ============================================================

def show_status():
    print("=== Antigravity 汉化状态 ===")
    running = is_antigravity_running()
    print(f"Antigravity 进程: {'运行中' if running else '未运行'}")
    print(f"{'登录自启守护' if IS_WINDOWS else 'launchd 守护'}:     {launchd_status()}")
    dict_map = load_dictionary()
    print(f"字典词条:         {len(dict_map)} 条")
    port = discover_cdp_port(timeout=3)
    if not port:
        print("CDP 端口:         未发现（Antigravity 未运行或未开放）")
        return
    print(f"CDP 端口:         {port}（随机分配）")
    page = get_main_page(port)
    if not page:
        print("主界面页面:       未就绪")
        return
    print(f"主界面页面:       {page.get('title', '')[:40] or '(加载中)'}")
    print(f"翻译引擎:         {'已注入（界面为中文）' if engine_alive(page) else '未注入（界面为英文）'}")


# 不变量清单：(源码里必须存在的片段, 说明)。
# 为什么需要它——2026-08 有一次编辑把 inject_page 里的守护补注 while 循环整段删掉了，
# 而 ast.parse 与 node --check 全部通过：语法完整、逻辑被掏空，这类损坏纯语法检查抓不到。
# 每条都是「删了就静默坏掉」的东西，改动结构后请同步维护这张表。
SOURCE_INVARIANTS = (
    # 用函数内独有的字符串做锚，别用 "while time.time() < end:" 这种
    # 在 guard_main 里也出现的片段——那样删掉 inject_page 的循环也检测不出来。
    ("reinjects += 1", "inject_page 的守护补注循环（补注计数）"),
    ("[守护] 检测到引擎丢失", "补注时的日志（循环体在位的证据）"),
    ("def inject_result(", "注入结果判定（三处调用共用）"),
    ("function stashOriginal", "原文暂存（旧引擎译坏后能救回）"),
    ("function restoreOriginal", "原文还原"),
    ("data-ag-i18n", "暂存所用的属性名"),
    ("function isConversationRow", "会话行识别（保护用户会话标题）"),
    ("function isRowTitle", "行内标题识别（只拦标题，不拦时间戳）"),
    ("const fullSweep", "全量重扫（可见性门共用）"),
    ("if (document.hidden) return;", "可见性门：隐藏时不烧 CPU"),
    ("visibilitychange", "切回前台补扫"),
    ("const attrSeen", "属性 memo"),
    ("function markHandled", "标题写回后标记文本节点"),
    ("_MAIN_PROC_RE", "只匹配主进程、排除 Helper"),
)


def check_source_invariants(src):
    """返回缺失的不变量列表（空列表 = 全部在位）。

    必须先把 SOURCE_INVARIANTS 这张表自身从源码里挖掉再搜——否则表里的字符串
    副本会让每一条都自动「命中」，即使真正的实现已经被删干净了。
    """
    head = "SOURCE_INVARIANTS = ("
    i = src.find(head)
    if i >= 0:
        j = src.find("\n)\n", i)
        if j > i:
            src = src[:i] + src[j:]
    return [(frag, why) for frag, why in SOURCE_INVARIANTS if frag not in src]


def check_syntax():
    """自动化语法与完整性自检。

    四层：Python AST → JSON 字典 → 源码不变量 → 引擎 JS（node --check + 控制字符）。
    第三、四层是关键：前两层只看语法，抓不到「结构完整但逻辑被删」和
    「正则合法但混入控制字符」这两类静默损坏。
    """
    import ast
    print("=== Antigravity 汉化工具：自动化语法与完整性校验 ===")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    py_files = [f for f in os.listdir(base_dir) if f.endswith('.py')]
    for py in sorted(py_files):
        with open(os.path.join(base_dir, py), 'r', encoding='utf-8') as fp:
            ast.parse(fp.read(), filename=py)
        print(f"  [Python AST]   {py} 语法正确")

    dict_dir = os.path.join(base_dir, 'dicts')
    dict_files = [f for f in os.listdir(dict_dir) if f.endswith('.json')]
    total_entries = 0
    for jf in sorted(dict_files):
        with open(os.path.join(dict_dir, jf), 'r', encoding='utf-8') as fp:
            data = json.load(fp)
            total_entries += len(data)
        print(f"  [JSON 字典]    dicts/{jf} 格式正确（{len(data)} 条）")

    # 第 3 层：源码不变量。ast.parse 过了不代表逻辑还在。
    src = io.open(os.path.join(base_dir, 'jack.py'), encoding='utf-8').read()
    missing = check_source_invariants(src)
    if missing:
        print(f"  [错误] 源码缺失 {len(missing)} 项不变量：")
        for frag, why in missing:
            print(f"         {why}  （找不到 {frag!r}）")
        sys.exit(1)
    print(f"  [不变量]      {len(SOURCE_INVARIANTS)} 项关键结构全部在位")

    engine_js = get_engine_js()
    # 第 4 层之一：控制字符。转义陷阱产出的是「合法语法 + 错误语义」，
    # \b 在 Python 三引号串里会变成退格符 U+0008，node --check 完全拦不住。
    ctrl = {c for c in engine_js if ord(c) < 32 and c not in '\n\t'}
    if ctrl:
        print(f"  [错误] 引擎 JS 混入控制字符 {[hex(ord(c)) for c in ctrl]} —— 又踩转义陷阱")
        sys.exit(1)
    print("  [控制字符]    引擎 JS 无异常控制字符")
    # 引擎 JS 里内联了从本机会话库提炼的真实 prompt（CONV_TITLES）。
    # 不能写成 /tmp 下的固定名 0644 文件——同机其他用户可读。
    # mkstemp 建的是 0600 且名字随机，用完立刻删。
    import tempfile
    fd, temp_js = tempfile.mkstemp(prefix='jack_engine_', suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            fp.write(engine_js)
        node_path = shutil.which('node')
        if node_path:
            res = subprocess.run([node_path, '--check', temp_js],
                                 capture_output=True, text=True)
            if res.returncode == 0:
                print(f"  [Node.js AST]  注入引擎 JavaScript 静态语法分析通过（{len(engine_js)} 字符）")
            else:
                print(f"  [错误] 注入引擎 JavaScript 语法错误: {res.stderr}")
                sys.exit(1)
        else:
            print("  [提示] 未安装 node，跳过 JS 静态语法分析")
    finally:
        if os.path.exists(temp_js):
            os.remove(temp_js)
    print(f"\n[验证完成] 全部自检通过！当前字典共计 {total_entries} 条词条。")



# ============================================================
# 双击图标模式
# ============================================================

def click_main(verbose=True):
    """双击图标模式：Antigravity 在跑就直接注入；没跑就先正常启动（LaunchServices）
    再等界面就绪注入。全程不弹终端，结果通过返回码给 AppleScript 通知。

    返回 (ok, message)。
    """
    def say(msg):
        if verbose:
            print(msg, flush=True)

    if not is_antigravity_running():
        say("[启动] Antigravity 未运行，正在启动...")
        if IS_WINDOWS:
            exe = find_antigravity_exe()
            if not exe:
                return False, "未找到 Antigravity 安装路径"
            launch_antigravity(exe)
        else:
            subprocess.run(['open', '-a', 'Antigravity'])
        for _ in range(150):
            if is_antigravity_running():
                break
            time.sleep(0.02)
        else:
            return False, "Antigravity 启动超时"
    else:
        say("[检测] Antigravity 已在运行")

    port = discover_cdp_port(timeout=30)
    if not port:
        return False, "未发现 CDP 调试端口"
    say(f"[连接] CDP 端口 {port}")

    engine_js = get_engine_js()
    # 先只做快速注入（watch_seconds=0）立刻返回：AppleScript 用 do shell script
    # 同步等这条命令，带 30 秒守护会让通知整整晚 30 秒才弹，Dock 图标一直转，
    # 用户以为卡死——而界面其实早就是中文了。
    resp, err = inject_with_retry(port, engine_js, watch_seconds=0, verbose=verbose)
    ok, detail = inject_result(resp, err)
    if not ok:
        return False, detail

    # 守护交给脱离的子进程：冷启动后的页面重载仍会冲掉引擎，这一步不能省，
    # 但它不该阻塞通知。子进程自己开 CDP 连接，与本进程无关。
    _spawn_guard(seconds=30, port=port)
    return True, "Antigravity 界面已汉化"


def _bg_python():
    """后台静默跑本脚本用的解释器：Windows 优先 pythonw.exe（不弹黑窗）。"""
    exe = sys.executable
    if IS_WINDOWS:
        cand = os.path.join(os.path.dirname(exe), 'pythonw.exe')
        if os.path.isfile(cand):
            return cand
    return exe


def _detached_popen_kwargs():
    """脱离宿主的 Popen 参数（Windows 用 creationflags，macOS 用新会话）。"""
    if IS_WINDOWS:
        return {'creationflags': (
            getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
            | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
            | getattr(subprocess, 'DETACHED_PROCESS', 0x00000008))}
    return {'start_new_session': True}


def _spawn_guard(seconds=30, port=None):
    """把守护补注挪到脱离的子进程，让调用方立刻返回。

    端口透传下去：调用方刚注入成功，端口是已知的，子进程不必再扫一遍。
    """
    try:
        cmd = [_bg_python(), os.path.abspath(__file__),
               '--guard', '--watch', str(seconds)]
        if port:
            cmd += ['--port', str(port)]
        subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **_detached_popen_kwargs())
        return True
    except Exception:
        return False


def guard_main(seconds=30, port=None):
    """只对已运行的实例做一段有界守护：引擎被页面重载冲掉就补注。

    供 --click 派生调用，也可手动跑。不启动 Antigravity、不做首次注入。
    """
    if port and not get_all_pages(port):
        port = None                      # 传进来的端口已失效，退回扫描
    if not port:
        port = discover_cdp_port(timeout=10)
    if not port:
        return False
    engine_js = get_engine_js()
    end = time.time() + max(seconds, 0)
    while time.time() < end:
        time.sleep(1.5)
        try:
            pages = get_all_pages(port)
            missing = [p for p in pages if not engine_alive(p)]
            if missing:
                inject_with_retry(port, engine_js, watch_seconds=0,
                                  attempts=1, verbose=False)
        except Exception:
            pass
    return True


# ============================================================
# 桌面图标生成（可移植，路径自动用当前用户家目录）
# ============================================================

def create_shortcut_win():
    """在桌面（或 Desktop\\app，若存在）生成「Antigravity 汉化」双击快捷方式。

    .lnk 通过 WScript.Shell COM 创建（Python 标准库没有 COM，借 PowerShell 中转）。
    快捷方式内部用当前脚本与 pythonw 的绝对路径——任何人拿到这份代码跑这条
    命令，都会生成指向他自己路径的图标。
    """
    script = os.path.abspath(__file__)
    python = _bg_python()
    desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
    app_dir = os.path.join(desktop, 'app')
    target_dir = app_dir if os.path.isdir(app_dir) else desktop
    lnk_path = os.path.join(target_dir, 'Antigravity 汉化.lnk')
    icon = find_antigravity_exe() or ''

    def psq(s):
        return "'" + s.replace("'", "''") + "'"

    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut({psq(lnk_path)}); "
        f"$s.TargetPath = {psq(python)}; "
        f"$s.Arguments = {psq(chr(34) + script + chr(34) + ' --click')}; "
        f"$s.WorkingDirectory = {psq(os.path.dirname(script))}; "
        f"$s.Description = {psq('Antigravity 界面汉化（运行时注入，不改安装文件）')}; "
    )
    if icon:
        ps += f"$s.IconLocation = {psq(icon + ',0')}; "
    ps += "$s.Save()"
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not os.path.exists(lnk_path):
        print(f"[错误] 生成快捷方式失败: {(r.stderr or r.stdout).strip()}")
        sys.exit(1)
    print(f"[成功] 快捷方式已生成: {lnk_path}")
    print("[用法] 双击即完成汉化（Antigravity 没开会自动启动）")
    print("[提示] 想恢复英文：直接正常打开 Antigravity（不双击汉化图标）即可")


def create_shortcut():
    """在桌面生成「Antigravity 汉化」双击图标。

    AppleScript 内部用当前脚本与 python 的绝对路径——同事拿到包跑这条
    命令，会生成指向他自己路径的图标，不依赖分享者的目录结构。
    """
    if IS_WINDOWS:
        return create_shortcut_win()
    script = os.path.abspath(__file__)
    python = sys.executable or '/usr/bin/python3'
    app_path = os.path.expanduser('~/Desktop/Antigravity 汉化.app')

    # AppleScript：调 --click，成功/失败都发系统通知。
    # 两层引号必须各自转义：AppleScript 字符串层，以及 do shell script 交给
    # /bin/sh 的那一层。路径里的 " 和 \ 不转义会直接把脚本拼坏。
    def as_quote(s):
        """转成 AppleScript 字符串字面量（含两侧引号）"""
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'

    def sh_quote(s):
        """转成 sh 单引号参数，再包进 AppleScript 字符串"""
        return "'" + s.replace("'", "'\\''") + "'"

    cmd = f"{sh_quote(python)} {sh_quote(script)} --click 2>&1"
    applescript = (
        'try\n'
        f'\tdo shell script {as_quote(cmd)}\n'
        '\tdisplay notification "Antigravity 界面已切换为中文" '
        'with title "Antigravity 汉化" sound name "Glass"\n'
        'on error errMsg\n'
        '\tdisplay notification errMsg with title "Antigravity 汉化失败" '
        'sound name "Basso"\n'
        'end try'
    )
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.scpt', delete=False,
                                      encoding='utf-8') as f:
        f.write(applescript)
        scpt_path = f.name
    try:
        # 旧的存在先移除（osacompile 不覆盖会报错）
        if os.path.exists(app_path):
            shutil.rmtree(app_path)
        r = subprocess.run(['osacompile', '-o', app_path, scpt_path],
                           capture_output=True)
        if r.returncode != 0:
            print(f"[错误] 生成失败: {r.stderr.decode(errors='replace')}")
            sys.exit(1)
    finally:
        os.unlink(scpt_path)

    # 借用 Antigravity 原版图标（若已安装）
    icon_src = '/Applications/Antigravity.app/Contents/Resources/icon.icns'
    icon_dst = os.path.join(app_path, 'Contents', 'Resources', 'applet.icns')
    if os.path.exists(icon_src):
        try:
            shutil.copy(icon_src, icon_dst)
            subprocess.run(['touch', app_path])  # 刷新 Finder 图标缓存
        except Exception:
            pass

    print(f"[成功] 桌面图标已生成: {app_path}")
    print("[用法] 双击图标即完成汉化；可拖到 Dock 栏常驻")
    print("[提示] 想恢复英文：直接正常打开 Antigravity（不双击图标）即可")


# ============================================================
# 手动模式主流程
# ============================================================

def manual_main(args):
    """手动模式：探测端口 → 必要时启动 Antigravity → 极速注入 → 后台守护。
    macOS 上 CDP 端口由系统随机分配，自动通过 DevToolsActivePort 或扫描发现。
    """
    print("=== Antigravity 2.0 运行时汉化工具（CDP 注入） ===")

    dict_map = load_dictionary()
    print(f"[字典] 共 {len(dict_map)} 条翻译规则")
    if not dict_map:
        print("[错误] 未加载到任何字典，请检查 dicts/ 目录")
        sys.exit(1)

    port = discover_cdp_port(timeout=3)

    if not port and not args.no_launch:
        if is_antigravity_running():
            print("[错误] Antigravity 已在运行，但扫不到它的 CDP 端口。")
            print("       此时直接启动只会多开一个重复窗口（原窗口仍是英文）。")
            print("       请先退出它再重跑（qidong.command 会自动关闭旧实例），")
            print("       或交给守护进程处理（--daemon / --install）。")
            sys.exit(1)
        exe = find_antigravity_exe(args.install_dir)
        if not exe:
            print("[错误] 未找到 Antigravity。请用 --install-dir 指定安装位置。")
            sys.exit(1)
        if launch_antigravity(exe, proxy=args.proxy) is None:
            print("[错误] Antigravity 启动失败，请检查安装路径与权限。")
            sys.exit(1)
        print("[等待] 扫描 Antigravity 监听的 CDP 端口（随机分配）...")
        port = discover_cdp_port(timeout=60)

    if not port:
        if args.no_launch:
            print("[错误] 未发现 CDP 端口。--no-launch 只注入已运行的实例，"
                  "请确认 Antigravity 正在运行。")
        else:
            print("[错误] 超时未扫到 CDP 端口。")
        sys.exit(1)

    print(f"[连接] CDP 端口 {port}")
    page = wait_for_debug_port(port, timeout=60)
    if not page:
        print("[错误] 端口已开但没等到界面页面（窗口可能被关掉了）。")
        sys.exit(1)
    print(f"[页面] {page.get('title')}  {page.get('url')}")

    print("[注入] 通过 CDP 注入翻译引擎...")
    resp, err = inject_with_retry(port, get_engine_js(), watch_seconds=0)
    ok, detail = inject_result(resp, err)
    if not ok:
        print(f"[错误] {detail}")
        sys.exit(1)
    if args.watch > 0:
        _spawn_guard(seconds=args.watch, port=port)
    print("[成功] 翻译引擎已注入！侧边栏等界面元素将显示中文。")
    print("[提示] 嫌每次手动跑麻烦？--install 装守护进程，打开 Antigravity 自动汉化。")


def main():
    parser = argparse.ArgumentParser(
        description="Antigravity 2.0 运行时汉化工具（CDP 注入 + 自动化守护）")
    parser.add_argument("--install-dir", help="Antigravity 安装目录或可执行文件路径（默认自动探测）")
    parser.add_argument("--port", type=int, default=None,
                        help="CDP 端口（一般不用给：端口随机分配，脚本自动扫描发现；主要供 --guard 内部透传）")
    parser.add_argument("--no-launch", action="store_true",
                        help="不启动 Antigravity，只注入已运行的实例")
    parser.add_argument("--watch", type=int, default=30,
                        help="注入后守护多少秒（页面重载会自动补注，0=不守护，默认 30）")
    parser.add_argument("--proxy", default=None,
                        help="代理地址（如 http://127.0.0.1:7897），启动 Antigravity 时注入环境变量")
    parser.add_argument("--daemon", action="store_true",
                        help="常驻守护模式：巡检 Antigravity，发现未汉化自动注入")
    parser.add_argument("--interval", type=int, default=DAEMON_INTERVAL,
                        help=f"守护巡检间隔秒数（默认 {DAEMON_INTERVAL}）")
    parser.add_argument("--install", action="store_true",
                        help="安装登录自启守护（macOS launchd / Windows 注册表 Run 项）")
    parser.add_argument("--uninstall", action="store_true",
                        help="卸载登录自启守护")
    parser.add_argument("--click", action="store_true",
                        help="双击图标模式：Antigravity 没开就先启动，然后自动注入")
    parser.add_argument("--guard", action="store_true",
                        help="只对已运行实例做一段有界守护补注（--click 会自动派生它）")
    parser.add_argument("--create-shortcut", action="store_true",
                        help="在桌面生成「Antigravity 汉化」双击图标（路径自适应当前用户）")
    parser.add_argument("--status", action="store_true",
                        help="查看状态")
    parser.add_argument("--check-syntax", action="store_true",
                        help="运行自动化语法与完整性自检（Python AST、JSON 字典、Node.js JS 引擎语法检查）")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.check_syntax:
        check_syntax()
    elif args.install:
        install_launchd()
    elif args.uninstall:
        uninstall_launchd()
    elif args.daemon:
        daemon_loop(interval=args.interval)
    elif args.guard:
        guard_main(seconds=args.watch, port=args.port)
    elif args.click:
        ok, msg = click_main()
        print(("[成功] " if ok else "[失败] ") + msg, flush=True)
        if IS_WINDOWS:
            # 双击快捷方式时没有终端窗口，结果用系统弹窗告知（对应 macOS 的通知）
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0, msg,
                    "Antigravity 汉化" if ok else "Antigravity 汉化失败",
                    0x40 if ok else 0x10)
            except Exception:
                pass
        sys.exit(0 if ok else 1)
    elif args.create_shortcut:
        create_shortcut()
    else:
        manual_main(args)


if __name__ == "__main__":
    main()
