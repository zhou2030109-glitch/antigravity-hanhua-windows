# Antigravity 汉化（Windows 版）

把 Antigravity 的界面变成中文。**不改动 Antigravity 的任何安装文件**，
官方更新不受影响，想恢复英文也不需要卸载什么。

原理：Antigravity 启动时会随机开放一个本地 CDP 调试端口，本工具通过它把
翻译引擎注入界面内存（MutationObserver 实时翻译）。重启 Antigravity 后
注入失效，由后台守护进程自动补注。

## 当前状态（已配置完成）

- 汉化工具安装在：`C:\Users\33845\AppData\Local\Programs\antigravity-hanhua\`
- 已注册**登录自启守护**（注册表 Run 项 `AntigravityHanhuaDaemon`）：
  每次开机登录后守护进程自动在后台运行（pythonw，无窗口，约占 12 MB 内存，
  每 5 秒巡检一次，平时零 CPU）
- 已在 `桌面\app\` 生成「Antigravity 汉化.lnk」快捷方式（借用原版图标），
  双击 = 启动 Antigravity（如未开）+ 立即汉化 + 弹窗告知结果

## 日常使用

**什么都不用做。** 正常双击你的 Antigravity 图标，界面会在几秒内自动变中文。
（守护进程检测到未汉化的窗口会自动注入；启动初期的页面重载冲掉翻译也会自动补回。）

如果偶尔遇到界面是英文（比如守护进程没跑），双击「Antigravity 汉化」快捷方式即可。

## 恢复英文

汉化只存在于内存：退出 Antigravity、正常重开即回到英文（守护进程关掉的前提下）。
临时恢复：关掉守护进程后重启 Antigravity。彻底恢复：见下方「卸载」。

## 命令一览（在终端里，进入安装目录后执行）

```
python jack.py --status          # 查看状态（进程 / CDP 端口 / 引擎 / 自启）
python jack.py --no-launch       # 只注入正在运行的 Antigravity
python jack.py --install         # 注册登录自启守护（已装过会覆盖，幂等）
python jack.py --uninstall       # 停止守护 + 移除登录自启（彻底恢复英文）
python jack.py --create-shortcut # 重新生成桌面快捷方式
python jack.py --check-syntax    # 自检（Python AST / JSON 字典 / JS 语法）
python caiji.py                  # 采集当前界面未翻译的英文 → pending.json
```

守护日志：`%LOCALAPPDATA%\com.nick.jack-hanhua\antigravity-hanhua.log`

## 卸载（彻底恢复英文）

```
cd C:\Users\33845\AppData\Local\Programs\antigravity-hanhua
python jack.py --uninstall
```
然后删除 `C:\Users\33845\AppData\Local\Programs\antigravity-hanhua\` 目录和
`桌面\app\Antigravity 汉化.lnk` 即可。

## 一点说明

汉化只翻界面，**不翻你的内容**：会话标题、代码、终端输出、文件路径、
输入框里的字都保持原样。产品名（Antigravity / Gemini / MCP 等）也照原样保留。

有词条没翻出来、或者翻得不对，可以运行 `python caiji.py` 采集后补进
`dicts\common.json` 或 `dicts\ui_v2.json`（格式 `{"原文": "译文"}`），
下次注入自动生效。

---
原项目为 macOS 版（见 LICENSE），本目录为 Windows 适配版：
tasklist 进程检测、netstat 端口扫描、注册表 Run 自启、.lnk 快捷方式、
MessageBox 结果弹窗，核心翻译引擎与原项目一致。
