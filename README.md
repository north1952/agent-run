# Agent Run! · WorkBuddy 版

把电脑上的 **WorkBuddy、Codex、dsh** 会话显示在 AI Passport 上。工作时宠物会加速；等待授权或回答时，屏幕会标出是哪一个工具。设备只接收计数、工具代号、等待原因和可用的用量指标；提示词、工具参数和会话正文留在电脑上。

## 先选连接方式

| 电脑 | USB 数据线 | 蓝牙 |
| --- | --- | --- |
| Windows 10/11 | 支持，最省事 | 支持 BLE；首次需在设备上确认配对 |
| macOS | 支持 | 使用原版蓝牙桥；本次未重新实测 |

设备需要 [WorkBuddy 版固件 0.6.2-wb](https://github.com/north1952/agent-run/releases/tag/v0.6.2-wb)。**已经运行这个固件的设备，升级 Windows 蓝牙只需更新电脑端。** 首次安装参见[固件刷写说明](displays/ai-passport/README.md)：从 `0x0` 刷入合并镜像会重置设备设置和蓝牙配对。旧 Claude Code 版主机或固件使用不同的工具代号，两端请成对升级。

## Windows：首次设置

建议安装 Python 3.10 或以上，并在 PowerShell 确认 `python --version` 可用。以下命令在仓库根目录执行；下载代码时请选择 `feature/workbuddy-passport` 分支。

```powershell
python -m pip install -r requirements-windows.txt
python core/bin/agent-run install-hooks
```

`install-hooks` 会先备份配置，然后只添加本工具的 Hook。按实际使用的工具完成一次设置：

- **WorkBuddy**：完全退出后重新打开。如果 WorkBuddy 要求审查 Hook，请启用。默认配置在 `~/.workbuddy/settings.json`。独立 CodeBuddy CLI 可用环境变量 `AGENT_RUN_WORKBUDDY_SETTINGS` 指明它的配置文件，例如 `~/.codebuddy/settings.json`。若 Windows 客户端报告找不到 Bash，按 [Hook 文档](https://www.workbuddy.cn/docs/cli/hooks)设置 Git Bash 路径。
- **Codex**：在 Settings → Hooks 或 CLI 的 `/hooks` 审查并信任 Agent Run 新增的条目。不信任则不会执行这些 Hook。
- **dsh**：不配置插件时，采集端可按会话记录的修改时间估算工作数量。要显示提问提醒，请看下方 [dsh 配置](#dsh-配置)。

### 用 USB

插上支持数据传输的 USB-C 数据线，启动采集：

```powershell
python core/bin/agent-run install-service
python core/bin/agent-run status
```

`install-service` 会部署电脑端并注册当前用户的 `AgentRun` 登录任务，同时启动它。以后只需登录 Windows、接上设备。如果连了多块 Espressif 板子，先让采集程序释放串口（如已注册，运行 `schtasks /End /TN AgentRun`），再运行 `python core/bin/agent-run setup` 握手并固定端口。换 USB 口之后可以重复 `setup`。

### 改用 Windows 蓝牙

保持 Windows 蓝牙开启，并唤醒设备。蓝牙适配器需支持 BLE。已在用旧版采集程序的用户请先更新代码；如果后台**已通过蓝牙**连接此设备，先结束 `AgentRun` 任务以重新配对。

```powershell
python -m pip install -r requirements-windows-ble.txt
python core/bin/agent-run setup-ble
python core/bin/agent-run install-service
```

1. 扫描发现多个设备时，用 `setup-ble --scan` 查设备名，再运行 `setup-ble --device AgentRun-XXXX`。
2. Windows 若弹出配对确认，请同意；设备显示「配对这台电脑？」时，短按设备中间的 **OK**。电脑端收到设备回复后才保存配置。
3. USB 与蓝牙同时可用时优先 USB。拔掉数据线后，后台会切换到 BLE；双击你已创建的桌面快捷方式，或运行 `status`，应显示「已连接 AgentRun-XXXX（蓝牙）」。

已配对的设备无需每次确认。若要停用蓝牙，运行 `setup-ble --disable` 后重新执行 `install-service`，系统配对会保留。

## 日常操作与排障

| 想做什么 | 操作 |
| --- | --- |
| 看会话状态 | 设备首页显示 WB、CODEX、DSH 的工作数量。等待面板提示授权或回答；请回到对应电脑应用处理。 |
| 看专页或设置 | 上下键切换首页、WorkBuddy、Codex；短按 OK 打开设置。Codex 用量来自近期本地会话；WorkBuddy 无可用订阅用量来源，因此显示「暂无数据」。 |
| 查看连接或 Hook | `python core/bin/agent-run status --events`。没有任务时计数为 0，是正常状态。 |
| 让设备演示 | `python core/bin/agent-run demo`，约 42 秒。演示包含一次短暂的「未连接」画面，随后会恢复正常。 |
| 手动启动 Windows 后台 | `schtasks /Run /TN AgentRun`。也可以只运行前台命令 `python core/bin/agent-run run`；前台和后台不要同时启动。 |
| USB 显示未连接 | 唤醒设备，确认是数据线并重新插拔；检查后台是否运行，确认没有刷机、截屏或其他串口工具占用同一端口。 |
| 蓝牙找不到设备或一直配对失败 | 唤醒设备并靠近电脑。在 Windows 蓝牙设置中移除对应的 `AgentRun-XXXX`；在设备上长按上键至少 5 秒后松手，清除设备端旧配对，再运行 `setup-ble` 并短按 OK 确认。 |
| 已连接但没有活动 | 先运行 demo 检查设备链路，再检查 WorkBuddy 是否已重启、Codex Hook 是否已信任、dsh profile 是否配置了插件。 |

Windows 数据和日志默认在 `%LOCALAPPDATA%\agent-run`。安装后的命令可直接运行，例如 `python "$env:LOCALAPPDATA\agent-run\bin\agent-run" status --events`。可用 `AGENT_RUN_HOME` 或 CLI 的全局选项 `--home` 指定数据目录。

### dsh 配置

先运行 `dsh --version`。安装与它**同版本**的 Hook 插件，然后运行 `python core/bin/agent-run install-dsh-hooks`，将打印的配置加到所用 profile 的 `cordis.patch.yml` 并重启该 profile。

```text
dsh plugin --profile <profile> add @deepseek-ai/dsh-hooks-claude-code@<dsh-version>
```

没有插件时，默认从 `~/.dsh/sessions`（或 `DSH_HOME` 下的 `sessions`）估算工作状态。可在运行目录的 `config.json` 里用 `dsh_root` 指定会话目录。DeepSeek Harness 桌面端的插件管理不能直接加载这个 CLI 插件。

### macOS

运行 `core/bin/agent-run install-hooks` 和 `core/bin/agent-run install-service`，默认数据目录为 `~/.agent-run`。USB 可直接使用；若要用蓝牙，执行 `transports/macos-ble/build.sh` 构建原版桥，然后运行 `core/bin/agent-run install-bridge-service` 并在设备按 OK 完成配对。

## 验证与开发

已验证 Windows USB 刷机、串口状态与设备中文画面；也已在拔线后验证 Windows BLE 握手及工作计数变化。WorkBuddy 的真实 CLI Hook 已验证，Codex 和 dsh 使用模拟 Hook 验证到真机。macOS 蓝牙、dsh 真实插件会话和长时间无线续航未重新实测。

- [固件构建与刷写](displays/ai-passport/README.md)（ESP-IDF 5.5.3）
- [AR1 展示协议](docs/protocol.md)
- 上位机回归：`python -m unittest discover -s core/tests -t core`；字体覆盖：`python displays/ai-passport/tests/test_ar_font_coverage.py`。

感谢 FoloToy AI Passport 的板级支持和 WorkBuddy × AI Passport 的 Windows BLE 参考。仓库按 [MIT 许可](LICENSE)发布；设备中文字体使用 Noto Sans SC 子集。
