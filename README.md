# Agent Run! — WorkBuddy 版

在 FoloToy AI Passport 上查看本机 WorkBuddy、Codex 和 dsh 会话。工作会话越多，宠物跑得越快；审批、提问和完成事件会切换画面并发出提示音。

本分支基于 [Agent Run! 上游](https://github.com/north1952/agent-run) 和 [社区玩法](https://ai-passport.folotoy.cn/plays/2095/)，沿用原版屏幕、按键、动画、省电和勿扰功能。首页显示 WB、CODEX、DSH 计数；按上下键切换到 WorkBuddy 和 Codex 专页。WorkBuddy 显示会话计数，用量显示「暂无数据」；Codex 从本地会话记录读取 5 小时及每周用量。设备只接收计数、工具代号、等待原因和可用的用量信息，不接收对话内容。

| 工具 | 接入方式 | 可识别的等待事件 |
| --- | --- | --- |
| WorkBuddy | 桌面端用户 Hook；独立 CodeBuddy CLI 可指定自己的配置文件 | 权限、交互提问、计划确认 |
| Codex | 桌面端或 CLI Hook，需在 Codex 内审查信任 | 权限、同步或异步提问 |
| dsh | CLI profile 使用官方 Hook 插件；无 Hook 时按会话文件写入时间估算工作状态 | 插件上报的问答工具调用 |

## Windows 快速开始（USB）

需要 Python 3.9+。按 [WorkBuddy / CodeBuddy Hook 文档](https://www.workbuddy.cn/docs/cli/hooks) 配置 Git Bash（已安装但客户端未找到时，设置 `CODEBUDDY_CODE_GIT_BASH_PATH`）。从仓库根目录运行：

```powershell
python -m pip install -r requirements-windows.txt
python core/bin/agent-run install-hooks
python core/bin/agent-run setup
python core/bin/agent-run run
```

完全退出并重新打开 WorkBuddy；如果客户端提示审查 Hook，请按提示启用。Codex 在桌面端 Settings → Hooks 或 CLI 的 `/hooks` 中信任新条目。安装器默认修改 `~/.workbuddy/settings.json` 和 `${CODEX_HOME:-~/.codex}/hooks.json`，先备份再追加；其他 Hook 会保留。独立 CodeBuddy CLI 可将环境变量 `AGENT_RUN_WORKBUDDY_SETTINGS` 设为它实际使用的配置文件，例如 `~/.codebuddy/settings.json`。

在第二个终端运行 `python core/bin/agent-run status --events` 查看状态，或运行 `python core/bin/agent-run demo` 让设备依次显示七种画面。可选：停止前台进程后运行 `install-service` 注册当前用户的 `AgentRun` 登录任务。运行数据默认在 `%LOCALAPPDATA%\agent-run`，可用 `AGENT_RUN_HOME` 或全局 `--home` 指定。

Windows 使用 USB；macOS 仍可使用原有蓝牙桥。macOS 上可直接用 `core/bin/agent-run` 执行相同子命令，运行目录为 `~/.agent-run`；蓝牙桥用 `transports/macos-ble/build.sh` 构建，再运行 `install-bridge-service`。

### dsh

运行 `python core/bin/agent-run install-dsh-hooks`，再把输出的配置追加到需要监控的 profile 的 `cordis.patch.yml`。需要用 `dsh plugin --profile <名称> add @deepseek-ai/dsh-hooks-claude-code@<与 dsh 一致的版本>` 安装官方插件，然后重启该 profile。未挂载插件时，默认从 `~/.dsh/sessions`（或 `DSH_HOME` 下的 `sessions`）估算活跃会话；自定义目录可用运行目录的 `config.json` 中的 `dsh_root` 设置。DeepSeek Harness 桌面端是否可用取决于会话目录和文件格式；它的插件管理不能直接加载这个 CLI 插件。

## 固件与开发

使用 [ESP-IDF 5.5.3](https://github.com/espressif/esp-idf/releases/tag/v5.5.3)。详见[固件构建与烧录](displays/ai-passport/README.md)和[展示端协议](docs/protocol.md)。旧版 Claude Code 上位机与本版固件使用不同的工具代码，请成对升级。设计文档 `docs/specs/` 记录原版实现。

```powershell
python -m unittest discover -s core/tests -t core
python displays/ai-passport/tests/test_ar_font_coverage.py
idf.py -C displays/ai-passport build
idf.py -C displays/ai-passport merge-bin -o agent-run-ai-passport-full.bin
```

合并镜像输出到 `displays/ai-passport/build/agent-run-ai-passport-full.bin`，从 `0x0` 刷写。完整镜像会重置设备上的 NVS 设置及蓝牙配对。开发时可用 `idf.py flash` 在兼容分区布局下保留设置。刷写或截图前请停止上位机守护进程。

感谢 [FoloToy AI Passport](https://github.com/FoloToy/ai-passport) 的板级支持和 [WorkBuddy × AI Passport](https://github.com/zhongdan110/ai-passport-workbuddy) 的桌面接入参考。代码遵循 [MIT 许可](LICENSE)；固件字体来自 Noto Sans SC，详见固件目录。
