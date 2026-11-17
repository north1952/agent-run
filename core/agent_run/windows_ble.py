"""Windows BLE transport for Agent Run's AR1 stream over Nordic UART."""

import asyncio
import logging
import threading
from dataclasses import dataclass

from .protocol import encode_ping, parse_device_line
from .serial_link import LinkClosed

SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
SCAN_SECONDS = 6.0
PAIR_SECONDS = 55.0
HELLO_SECONDS = 35.0
RETRY_SECONDS = 3.0
MAX_LINE = 256
WRITE_SECONDS = 3.0
log = logging.getLogger("agent-run")


class BleUnavailable(Exception):
    """The optional Windows BLE dependency is missing."""


def _bleak():
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise BleUnavailable("请先安装 requirements-windows-ble.txt") from exc
    return BleakClient, BleakScanner


def _matches(discovered, target):
    """Find only Agent Run advertisements; other NUS firmware isn't AR1."""
    target = target.casefold() if target else None
    matches = []
    for device, advertisement in discovered.values():
        name = advertisement.local_name or device.name or ""
        if not name.startswith("AgentRun-"):
            continue
        if SERVICE not in (uuid.casefold() for uuid in advertisement.service_uuids):
            continue
        if target is None or target in (name.casefold(), device.address.casefold()):
            matches.append((device, name))
    return sorted(matches, key=lambda item: (item[1], item[0].address))


async def _scan(scanner, target):
    return _matches(await scanner.discover(timeout=SCAN_SECONDS, return_adv=True), target)


def scan_devices(target=None):
    _, scanner = _bleak()
    return [(device.address, name) for device, name in asyncio.run(_scan(scanner, target))]


class _Hello:
    """Collect the firmware HELLO across arbitrary notification boundaries."""

    def __init__(self):
        self.buffer = bytearray()
        self.value = None
        self.ready = asyncio.Event()

    def receive(self, _characteristic, data):
        if self.ready.is_set():
            return
        self.buffer.extend(data)
        while b"\n" in self.buffer:
            raw, _, rest = self.buffer.partition(b"\n")
            self.buffer = rest
            if len(raw) <= MAX_LINE:
                value = parse_device_line(raw.decode("ascii", "replace"))
                if value and value.get("type") == "HELLO" and value.get("proto") == "1" and value.get("fw"):
                    self.value = value
                    self.ready.set()
                    return
        if len(self.buffer) > MAX_LINE:
            self.buffer.clear()


async def _write_frame(client, characteristic, payload):
    # WinRT handles long writes with a response. Splitting each line into 20-byte
    # writes requires several round trips and can time out before the newline.
    await client.write_gatt_char(characteristic, payload, response=True)


async def _wait_for_hello(client, rx, ended, hello):
    deadline = asyncio.get_running_loop().time() + HELLO_SECONDS
    while not ended.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.wait_for(_write_frame(client, rx, encode_ping().encode("ascii")), timeout=WRITE_SECONDS)
        try:
            await asyncio.wait_for(hello.ready.wait(), timeout=1)
            return hello.value
        except asyncio.TimeoutError:
            continue
    return None


@dataclass(eq=False)
class _Connection:
    loop: asyncio.AbstractEventLoop
    client: object
    rx: object
    ended: asyncio.Event
    hello: dict


class WindowsBleBridge:
    """Maintain one selected BLE connection without blocking USB/daemon ticks."""

    def __init__(self, address):
        self.address = address
        self._client_type, self._scanner = _bleak()
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._runner = None
        self._session = None
        self._hint = None
        self._thread = threading.Thread(target=self._run_thread, name="agent-run-win-ble", daemon=True)

    def start(self):
        self._thread.start()

    def close(self):
        self._closing.set()
        with self._lock:
            runner = self._runner
        if runner:
            loop, task = runner
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:  # event loop has already exited
                pass
        if self._thread.ident is not None:
            self._thread.join(timeout=6)

    def current(self):
        with self._lock:
            return self._session

    def hint(self):
        with self._lock:
            return self._hint

    def _set_session(self, session):
        with self._lock:
            self._session = session

    def _set_hint(self, hint):
        with self._lock:
            if self._hint == hint:
                return
            self._hint = hint
        if hint:
            log.info("Windows 蓝牙：%s", hint)

    def link(self):
        return WindowsBleLink(self)

    def _run_thread(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        finally:
            self._set_session(None)

    async def _run(self):
        loop = asyncio.get_running_loop()
        with self._lock:
            self._runner = loop, asyncio.current_task()
        try:
            while not self._closing.is_set():
                try:
                    found = await _scan(self._scanner, self.address)
                    if found:
                        await self._connect(*found[0])
                    else:
                        self._set_hint("未发现已选设备；请唤醒 AI Passport，并确认 Windows 蓝牙已开启。")
                except Exception as exc:
                    self._set_hint("连接失败（%s）；请检查配对与设备状态。" % (str(exc) or type(exc).__name__))
                await asyncio.sleep(RETRY_SECONDS)
        finally:
            with self._lock:
                self._runner = None

    async def _connect(self, device, name):
        loop = asyncio.get_running_loop()
        ended = asyncio.Event()
        self._set_hint("正在连接 %s；首次配对请在设备上按 OK。" % name)
        async with self._client_type(
            device, pair=True, timeout=PAIR_SECONDS, winrt={"use_cached_services": False},
            disconnected_callback=lambda _: ended.set(),
        ) as client:
            service = client.services.get_service(SERVICE)
            rx = service.get_characteristic(RX) if service else None
            tx = service.get_characteristic(TX) if service else None
            if rx is None or tx is None:
                raise RuntimeError("设备没有 Agent Run 所需的 RX/TX 特征")
            hello = _Hello()
            await client.start_notify(tx, hello.receive)
            value = await _wait_for_hello(client, rx, ended, hello)
            if value is None or ended.is_set():
                self._set_hint("未收到设备握手；若显示配对提示，请短按设备 OK。")
                return

            self._set_session(_Connection(loop, client, rx, ended, dict(value, name=name)))
            self._set_hint(None)
            log.info("已通过 Windows 蓝牙握手：%s（固件 %s）", name, value["fw"])
            try:
                await ended.wait()
            finally:
                self._set_session(None)

    def write(self, session, line, timeout):
        if self.current() is not session or session is None:
            return False
        command = _write_frame(session.client, session.rx, line.encode("ascii"))
        try:
            future = asyncio.run_coroutine_threadsafe(command, session.loop)
        except RuntimeError:
            command.close()
            return False
        try:
            future.result(timeout)
            return True
        except Exception as exc:
            future.cancel()
            log.info("Windows 蓝牙写入失败：%s", str(exc) or type(exc).__name__)
            # A failed write may have left a partial line on the device. Reconnect
            # before sending further frames, so the firmware resets its buffer.
            with self._lock:
                if self._session is session:
                    self._session = None
            try:
                session.loop.call_soon_threadsafe(session.ended.set)
            except RuntimeError:
                pass
            return False


class WindowsBleLink:
    kind = "ble"

    def __init__(self, bridge):
        self._bridge = bridge
        self._session = None

    def open(self):
        pass

    def close(self):
        self._session = None

    def handshake(self, timeout=1.0):
        self._session = self._bridge.current()
        return self._session.hello if self._session else None

    def read_lines(self):
        if self._session is None or self._session is not self._bridge.current():
            raise LinkClosed()
        return []

    def write_line(self, line, timeout=WRITE_SECONDS):
        return self._bridge.write(self._session, line, timeout)
