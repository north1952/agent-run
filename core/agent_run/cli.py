"""agent-run 命令行（设计方案 §5.6）。

子命令：
  run               前台运行守护进程（launchd 调用的就是它）
  status            打印守护进程汇总的当前状态
  demo              让设备依次演示七种画面
  setup             查找 AI Passport 的串口，握手确认后写入 config.json
  setup-ble         在 Windows 上选择、配对并启用 AI Passport BLE
  install-hooks     部署脚本并为 WorkBuddy、Codex 安装 hook（先备份）
  uninstall-hooks   删除本工具安装的 hook
  install-service   部署脚本并注册 launchd，登录后自动运行
  uninstall-service 注销 launchd（保留运行数据）
  install-dsh-hooks 生成 dsh hook 插件用的配置，并打印挂载方法
  install-bridge-service / uninstall-bridge-service  注册或注销蓝牙桥接程序
  screenshot        经 USB 截取设备屏幕（需先停止守护进程）
"""
import argparse
import json
import logging
import math
import os
import sys
import time

from . import install
from .ble_link import BleLink
from .daemon import DEMO_PAUSE_SECONDS, DEMO_SCREENS, DEMO_STEP_SECONDS, Daemon
from .event_link import default_home, send
from .ports import find_espressif_ports
from .serial_link import SerialLink
from .usage import UsageCollector

# hook 与 launchd 统一使用系统自带的 Python，避免依赖用户环境里的解释器
SYSTEM_PYTHON = sys.executable if os.name == "nt" else "/usr/bin/python3"

# 仓库中 core/ 目录（部署来源）
HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_config(home):
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(home, config):
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "config.json")
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temp, path)


def _port_finder(home):
    """优先用 config.json 记录的端口；未记录时按 Espressif 厂商号自动查找。"""
    def find():
        config = _load_config(home)
        port = config.get("port")
        if port:
            return port
        ports = find_espressif_ports()
        return ports[0] if ports else None
    return find


def cmd_run(args):
    home = args.home
    log_options = {}
    if args.log_file:
        log_dir = os.path.join(home, "log")
        os.makedirs(log_dir, exist_ok=True)
        log_options = {"filename": os.path.join(log_dir, "agent-run.log"), "encoding": "utf-8"}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", **log_options)
    config = _load_config(home)
    run_dir = os.path.join(home, "run")
    bridge = ble_factory = ble_hint = None
    if sys.platform == "darwin":
        ble_factory = lambda: BleLink(os.path.join(run_dir, "ble.sock"), os.path.join(run_dir, "ble-status.json"))
    elif sys.platform == "win32" and isinstance(config.get("ble_device"), str) and config["ble_device"]:
        from .windows_ble import BleUnavailable, WindowsBleBridge

        try:
            bridge = WindowsBleBridge(config["ble_device"])
            ble_factory, ble_hint = bridge.link, bridge.hint
        except BleUnavailable as exc:
            message = str(exc)
            logging.warning("Windows 蓝牙：%s", message)
            ble_hint = lambda: message
    daemon = Daemon(
        home=home,
        port_finder=_port_finder(home),
        dsh_root=os.path.expanduser(config.get("dsh_root", os.path.join(os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh"), "sessions"))),
        rules=config.get("rules"),
        # 没有 USB 时经蓝牙桥接程序发送
        ble_factory=ble_factory,
        ble_hint=ble_hint,
        # Codex 订阅用量，供设备上的专用页面显示
        usage=UsageCollector(),
    )
    try:
        daemon.start()
        if bridge:
            bridge.start()
        daemon.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if bridge:
            bridge.close()
    return 0


def _countdown(seconds):
    """距重置时间的文字，与设备专用页面的写法一致（专用页面方案 §4.2）。"""
    minutes = max(1, int(math.ceil(seconds / 60.0)))
    if minutes < 60:
        return "%d 分钟后重置" % minutes
    if minutes < 24 * 60:
        return "%d 小时 %d 分后重置" % (minutes // 60, minutes % 60)
    return "%d 天 %d 小时后重置" % (minutes // (24 * 60), minutes % (24 * 60) // 60)


def describe_window(value, now):
    """status.json 里一个用量窗口的文字：百分比与距重置时间、无此限额或暂无数据。"""
    if value is None:
        return "暂无数据"
    if value == "none":
        return "无此限额"
    resets_at = value.get("resets_at")
    if resets_at is not None and resets_at <= now:
        return "0%（已重置）"
    percent = int(math.floor(value.get("used", 0) + 0.5))
    if resets_at is None:
        return "%d%%（重置时间未知）" % percent
    return "%d%%（%s）" % (percent, _countdown(resets_at - now))


def cmd_status(args):
    path = os.path.join(args.home, "run", "status.json")
    try:
        with open(path, encoding="utf-8") as f:
            status = json.load(f)
    except (OSError, ValueError):
        print("守护进程没有运行（找不到 %s）。" % path)
        return 1
    age = time.time() - status.get("updated", 0)
    if age > 10:
        print("警告：状态已 %d 秒未更新，守护进程可能已停止。" % age)
    via = {"usb": "USB", "ble": "蓝牙"}.get(status.get("link_type"), "")
    link = "已连接 %s（%s）" % (status.get("port"), via) if status.get("link") else "未连接"
    print("设备链路：%s%s" % (link, "（演示中）" if status.get("demo") else ""))
    print("工作中：WorkBuddy %d · Codex %d · dsh %d" % (status["wb"], status["cx"], status["ds"]))
    print("今日完成：%d" % status["done"])
    reasons = {"A": "等待授权", "Q": "等你回答"}
    names = {"workbuddy": "WorkBuddy", "codex": "Codex", "dsh": "dsh"}
    print("等你处理：%d" % status["wn"])
    for tool, reason in status.get("waiting", []):
        print("  - %s：%s" % (names.get(tool, tool), reasons.get(reason, reason)))

    # 专用页面的数据：按工具的计数与订阅用量
    now = time.time()
    for tool, agent in status.get("agents", {}).items():
        plan = agent.get("plan")    # 套餐显示名；旧版守护进程的 status.json 没有这一项
        print("%s%s：等你处理 %d · 后台运行 %d · 今日完成 %d" % (
            names.get(tool, tool), "（%s）" % plan if plan else "", agent["wn"], agent["bg"], agent["done"]))
        print("  5 小时用量 %s · 每周用量 %s" % (
            describe_window(agent.get("five_hour"), now), describe_window(agent.get("seven_day"), now)))
        updated = agent.get("usage_updated")
        if updated:
            print("  用量数据时间：%d 分钟前" % max(0, (now - updated) // 60))
        if agent.get("usage_error"):
            print("  用量未更新：%s" % agent["usage_error"])

    # Codex 会静默跳过未信任的 hook，这里主动提示，避免“跑了任务却不计数”
    untrusted = install.codex_untrusted_events(
        _agent_config_paths()[1],
        os.path.join(os.path.dirname(_agent_config_paths()[1]), "config.toml"),
        os.path.join(args.home, "bin", "ar_hook.py"),
    )
    if untrusted:
        print("注意：Codex 有 %d 个 agent-run hook 尚未信任（%s），Codex 会话不会计入。" % (len(untrusted), "、".join(untrusted)))
        print("      请在 Codex 桌面端 Settings → Hooks，或 CLI 的 /hooks 中审查并信任。")

    # 蓝牙桥接程序报告需要用户处理的问题（如重新烧录后配对失效）
    try:
        with open(os.path.join(args.home, "run", "ble-status.json")) as f:
            bridge_hint = json.load(f).get("hint")
    except (OSError, ValueError, AttributeError):
        bridge_hint = None
    if status.get("ble_hint"):
        print("蓝牙：%s" % status["ble_hint"])
    elif bridge_hint:
        print("蓝牙：%s" % bridge_hint)

    if getattr(args, "events", False):
        print("最近收到的事件：")
        now = time.time()
        for item in status.get("recent", []):
            detail = item.get("tool_name") or item.get("ntype") or ""
            print("  %5.0f 秒前  %-6s %-18s %s" % (now - item["ts"], item["tool"], item["event"], detail))
    return 0


def cmd_install_dsh_hooks(args):
    """写出 dsh hook 插件用的配置文件，并打印需要加到 dsh profile 里的配置。"""
    hook_path, _ = install.deploy(HOST_DIR, args.home)
    path = os.path.join(args.home, "dsh-hooks.json")
    with open(path, "w") as f:
        json.dump(install.dsh_hooks_config(SYSTEM_PYTHON, hook_path), f, indent=2)
        f.write("\n")
    print("已写出 %s。" % path)
    print("在要统计的 dsh profile 的 cordis.patch.yml 中挂载插件：")
    print("  - name: '@deepseek-ai/dsh-hooks-claude-code'")
    print("    config:")
    print("      configPath: %s" % json.dumps(path, ensure_ascii=False))
    return 0


def cmd_demo(args):
    try:
        send(args.home, b'{"ctl":"demo"}')
    except OSError:
        print("发送失败：守护进程没有运行。")
        return 1
    total = len(DEMO_SCREENS) * DEMO_STEP_SECONDS + DEMO_PAUSE_SECONDS
    print("已开始演示：睡觉 → 慢走 → 小跑 → 狂奔 → 冲刺 → 等你处理 → 未连接，共约 %d 秒。" % total)
    return 0


def cmd_setup(args):
    ports = find_espressif_ports()
    if not ports:
        print("没有找到 Espressif 的 USB 串口。请确认 AI Passport 已开机，并用支持数据传输的线连接电脑。")
        return 1
    for port in ports:
        link = SerialLink(port)
        try:
            link.open()
            hello = link.handshake(timeout=1.5)
        except OSError as exc:
            print("%s：打开失败（%s）。如果 agent-run 守护进程正在运行，它可能已占用该端口。" % (port, exc))
            continue
        finally:
            link.close()
        if hello:
            config = _load_config(args.home)
            config["port"] = port
            _save_config(args.home, config)
            print("已找到 AI Passport：%s（固件 %s），已写入 config.json。" % (port, hello.get("fw", "?")))
            return 0
        print("%s：没有收到握手回复，设备上可能还不是 Agent Run! 固件。" % port)
    return 1


def cmd_setup_ble(args):
    if sys.platform != "win32":
        print("setup-ble 用于 Windows；macOS 请使用 install-bridge-service。")
        return 1
    if args.disable:
        config = _load_config(args.home)
        config.pop("ble_device", None)
        _save_config(args.home, config)
        print("已停用蓝牙。重启采集程序后生效；Windows 中的配对保留。")
        return 0

    from .windows_ble import PAIR_SECONDS, HELLO_SECONDS, SCAN_SECONDS, WindowsBleBridge, scan_devices

    print("正在扫描 AI Passport…", flush=True)
    try:
        devices = scan_devices(args.device)
    except Exception as exc:
        print("蓝牙扫描失败：%s" % exc)
        return 1
    if not devices:
        print("未发现设备。请先唤醒 AI Passport，确认蓝牙开启；若设备正被采集程序连接，请先停止它。")
        return 1
    if args.scan or len(devices) != 1:
        for address, name in devices:
            print("%s  %s" % (name, address))
        if not args.scan:
            print("找到多台设备，请用 --device 指定完整设备名或蓝牙地址。")
        return 0 if args.scan else 1

    address, name = devices[0]
    print("正在配对 %s。Windows 如果弹出提示，请确认；设备出现配对画面时请短按 OK。" % name, flush=True)
    bridge = WindowsBleBridge(address)
    bridge.start()
    try:
        deadline = time.monotonic() + SCAN_SECONDS + PAIR_SECONDS + HELLO_SECONDS
        hint = None
        while time.monotonic() < deadline:
            session = bridge.current()
            if session:
                config = _load_config(args.home)
                config["ble_device"] = address
                _save_config(args.home, config)
                print("已连接 %s（固件 %s），已保存设置。请重启采集程序。" % (name, session.hello["fw"]))
                return 0
            current_hint = bridge.hint()
            if current_hint and current_hint != hint:
                print(current_hint, flush=True)
                hint = current_hint
            time.sleep(0.2)
        print("配对未完成；未更改采集配置。")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        bridge.close()


def cmd_screenshot(args):
    """经 USB 截取设备屏幕，保存为 PNG。守护进程运行时占用串口，需先停止。"""
    from . import screenshot

    port = _port_finder(args.home)()
    if not port:
        print("没有找到 AI Passport 的 USB 串口。")
        return 1
    link = SerialLink(port)
    try:
        link.open()
    except OSError as exc:
        print("打开 %s 失败（%s）。截屏前请先停止采集程序。" % (port, exc))
        return 1
    try:
        width, height, rgb = screenshot.capture(link)
    except screenshot.ScreenshotError as exc:
        print("截屏失败：%s" % exc)
        return 1
    finally:
        link.close()
    rgb, width, height = screenshot.scale_rgb(rgb, width, height, args.scale)
    with open(args.output, "wb") as f:
        f.write(screenshot.encode_png(rgb, width, height))
    print("已保存 %s（%d×%d）" % (args.output, width, height))
    return 0


def _agent_config_paths():
    workbuddy = os.environ.get("AGENT_RUN_WORKBUDDY_SETTINGS") or os.path.expanduser("~/.workbuddy/settings.json")
    codex = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.abspath(os.path.expanduser(workbuddy)), os.path.join(codex, "hooks.json")


def cmd_install_hooks(args):
    hook_path, _ = install.deploy(HOST_DIR, args.home)
    workbuddy, codex = _agent_config_paths()
    backup = install.install_hooks(workbuddy, codex, SYSTEM_PYTHON, hook_path, os.path.join(args.home, "backup"))
    print("已为 WorkBuddy 和 Codex 安装 hook，原配置备份在 %s。" % backup)
    print("请完全退出并重启 WorkBuddy；若客户端提示审查 hook，请先审查并启用。")
    print("Codex 需要在 CLI 的 /hooks 或桌面端 Settings → Hooks 里审查并信任新 hook 后才会执行。")
    return 0


def cmd_uninstall_hooks(args):
    hook_path = os.path.join(args.home, "bin", "ar_hook.py")
    workbuddy, codex = _agent_config_paths()
    backup = install.uninstall_hooks(workbuddy, codex, hook_path, os.path.join(args.home, "backup"))
    print("已删除 agent-run 的 hook，修改前的配置备份在 %s。" % backup)
    return 0


def cmd_install_service(args):
    _, script = install.deploy(HOST_DIR, args.home)
    path = install.install_service(SYSTEM_PYTHON, script, args.home)
    print("已注册登录启动项：%s，守护进程已启动。" % path)
    return 0


def cmd_install_bridge_service(args):
    """注册蓝牙桥接程序（需先用 bridge/build.sh 构建并安装 .app）。"""
    if sys.platform != "darwin":
        print("这是 macOS 的蓝牙桥安装命令。Windows 请运行 setup-ble。")
        return 1
    app = os.path.expanduser("~/Applications/AgentRunBridge.app")
    if not os.path.exists(os.path.join(app, "Contents", "MacOS", "AgentRunBridge")):
        print("没有找到 %s，请先运行 transports/macos-ble/build.sh。" % app)
        return 1
    path = install.install_bridge_service(app, args.home)
    print("已注册 launchd：%s。首次运行时 macOS 会请求蓝牙权限，请点“允许”。" % path)
    return 0


def cmd_uninstall_bridge_service(args):
    if sys.platform != "darwin":
        print("这是 macOS 的蓝牙桥卸载命令。Windows 请运行 setup-ble --disable。")
        return 1
    install.uninstall_service(install.BRIDGE_LABEL)
    print("已注销蓝牙桥接程序的登录自启；如需彻底移除，再删除 ~/Applications/AgentRunBridge.app。")
    return 0


def cmd_uninstall_service(args):
    install.uninstall_service()
    print("已注销登录启动项，运行数据保留在 %s。" % args.home)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-run", description="Agent Run! 主机采集端")
    parser.add_argument("--home", default=default_home(), help="运行数据目录（可用 AGENT_RUN_HOME 设置）")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--events", action="store_true", help="同时列出最近收到的事件")
    status.set_defaults(func=cmd_status)
    shot = sub.add_parser("screenshot", help="经 USB 截取设备屏幕（需先停止采集程序）")
    shot.add_argument("-o", "--output", default="agent-run-screen.png", help="输出 PNG 路径")
    shot.add_argument("--scale", type=int, default=1, help="最近邻放大倍数")
    shot.set_defaults(func=cmd_screenshot)
    run = sub.add_parser("run")
    run.add_argument("--log-file", action="store_true", help="将日志写入运行目录")
    run.set_defaults(func=cmd_run)
    ble = sub.add_parser("setup-ble", help="设置 Windows 蓝牙")
    ble.add_argument("--device", help="完整广播名（AgentRun-XXXX）或 BLE 地址")
    mode = ble.add_mutually_exclusive_group()
    mode.add_argument("--scan", action="store_true", help="只列出可用设备")
    mode.add_argument("--disable", action="store_true", help="关闭蓝牙传输，保留系统配对")
    ble.set_defaults(func=cmd_setup_ble)
    for name, func in [
        ("demo", cmd_demo),
        ("install-dsh-hooks", cmd_install_dsh_hooks),
        ("setup", cmd_setup),
        ("install-hooks", cmd_install_hooks),
        ("uninstall-hooks", cmd_uninstall_hooks),
        ("install-service", cmd_install_service),
        ("install-bridge-service", cmd_install_bridge_service),
        ("uninstall-bridge-service", cmd_uninstall_bridge_service),
        ("uninstall-service", cmd_uninstall_service),
    ]:
        sub.add_parser(name).set_defaults(func=func)
    args = parser.parse_args(argv)
    args.home = os.path.abspath(os.path.expanduser(args.home))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
