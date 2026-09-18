# Antigravity 汉化（Windows 版）

Antigravity 界面汉化工具 —— **不修改任何安装文件**，通过 CDP（Chrome DevTools
Protocol）把 MutationObserver 翻译引擎注入运行中的界面内存，官方更新不受影响。

> 移植说明：原项目为 macOS 版（见 `LICENSE`）。本仓库为 Windows 适配版——
> tasklist 进程检测、netstat 端口扫描、注册表 Run 项自启、.lnk 快捷方式、
> MessageBox 结果弹窗；核心翻译引擎（MutationObserver + 禁区隔离）与原项目一致。

## 特性

- **零第三方依赖**：内置迷你 WebSocket 客户端（MiniWS），Python 3.7+ 标准库直接可跑
- **官方升级免疫**：不碰安装文件；CDP 端口随机分配、自动发现（DevToolsActivePort →
  端口缓存 → netstat 扫描三级探测）
- **自动守护**：注册表 Run 项登录自启（pythonw 无窗口，约 12 MB 内存，每 5 秒巡检），
  打开 Antigravity 几秒内自动变中文；页面重载冲掉翻译自动补注
- **只翻界面不翻内容**：会话标题、代码、终端输出、文件路径、输入框内容保持原样；
  产品名（Antigravity / Gemini / MCP 等）原样保留
- **可自助补词**：`caiji.py` 采集未翻译文案 / 检测官方更新后的失效词条

## 快速开始

```powershell
# 1. 克隆后进入目录，先自检
python jack.py --check-syntax

# 2. 注入正在运行的 Antigravity（或用它直接启动并注入）
python jack.py --no-launch
python jack.py            # 未运行则先启动再注入，并守护 30 秒

# 3. 一劳永逸：注册登录自启守护 + 生成桌面快捷方式
python jack.py --install
python jack.py --create-shortcut
```

恢复英文：直接正常重开 Antigravity（注入只存在于内存）。
彻底卸载：`python jack.py --uninstall`，再删除本目录与桌面快捷方式。

## 命令一览

```
python jack.py --status          # 状态：进程 / CDP 端口 / 引擎 / 自启
python jack.py --no-launch       # 只注入正在运行的实例
python jack.py --daemon          # 常驻守护（--install 会自动拉起）
python jack.py --install         # 注册登录自启守护（HKCU Run 项）
python jack.py --uninstall       # 停止守护 + 移除自启
python jack.py --click           # 双击模式：没开就启动，然后注入（快捷方式用它）
python jack.py --create-shortcut # 生成「Antigravity 汉化」.lnk（借用原版图标）
python jack.py --check-syntax    # 四层自检：AST / JSON / 结构不变量 / JS 语法
python caiji.py                  # 采集界面未翻译英文 → pending.json
python caiji.py --misses         # 检测失效词条 → misses.json
```

守护日志：`%LOCALAPPDATA%\com.nick.jack-hanhua\antigravity-hanhua.log`

## 补充词条

译文按 `{"原文": "译文"}` 加进 `dicts/`（`common.json` 通用词 / `ui_v2.json`
界面文案），下次注入自动生效。三条铁律：只写完整 UI 字符串；不翻用户内容和
产品名；带变量的句子走引擎里的 `REGEX_RULES`。

## 原理

Antigravity（Electron）启动时会在 127.0.0.1 随机端口开放 CDP。本工具发现该端口后，
通过 `Page.addScriptToEvaluateOnNewDocument` + `Runtime.evaluate` 注入翻译引擎：
MutationObserver 监听 DOM 变化，按词典与正则规则翻译界面文本，并用禁区清单
（编辑器 / 终端 / 输入框 / 会话内容）与原文暂存（`data-ag-i18n`）保护用户数据。

字典共 2252 条词条（`ui_v2.json` 2225 + `common.json` 27）。

## License

MIT（沿自原 macOS 项目，见 `LICENSE`）
