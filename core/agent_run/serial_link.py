"""USB 串口链路（设计方案 §6）。

macOS 用 termios；Windows 用 pyserial。使用原始模式、独占打开，不主动翻转 DTR/RTS，
以降低打开端口时把 ESP32-C3 复位的风险。
"""
import os
import re
import select
import time

if os.name != "nt":
    import fcntl
    import termios
    import tty

from .protocol import encode_ping, parse_device_line

# macOS 上 TIOCEXCL = _IO('t', 13)；部分 Python 构建未导出该常量
_TIOCEXCL = getattr(termios, "TIOCEXCL", 0x2000740D) if os.name != "nt" else None

# 读缓冲上限：设备持续输出却没有行尾时丢弃，避免无限增长
_MAX_BUFFER = 4096

_LINE_SPLIT = re.compile(rb"\r\n|\r|\n")


class LinkClosed(Exception):
    """设备断开（拔线或复位）时由读写方法抛出。"""


class SerialLink:
    """一条到设备的串口连接，非阻塞读写。"""

    kind = "usb"

    def __init__(self, path):
        self.path = path
        self._fd = None
        self._serial = None
        self._buf = b""

    def open(self):
        """以独占、非阻塞方式打开串口，并设为原始模式。"""
        if os.name == "nt":
            import serial

            port = serial.Serial(baudrate=115200, timeout=0, write_timeout=1.0)
            port.port = self.path
            port.dtr = False
            port.rts = False
            port.open()
            self._serial = port
            self._buf = b""
            return
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # 独占：防止另一个进程同时打开同一端口抢数据
            fcntl.ioctl(fd, _TIOCEXCL)
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            # cflag：关闭 HUPCL（关闭端口时不拉低 DTR），忽略调制解调器控制线
            attrs[2] = (attrs[2] & ~termios.HUPCL) | termios.CLOCAL | termios.CREAD
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._buf = b""

    def fileno(self):
        """底层文件描述符，供截屏等需要直接读取二进制数据的场合使用。"""
        return self._fd

    def close(self):
        """关闭端口；重复调用无副作用。"""
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def write_line(self, line, timeout=1.0):
        """写出一整行；超时或设备断开返回 False。"""
        data = line.encode("ascii", "replace")
        if self._serial is not None:
            try:
                self._serial.write_timeout = timeout
                return self._serial.write(data) == len(data)
            except OSError:
                return False
        if self._fd is None:
            return False
        deadline = time.monotonic() + timeout
        while data:
            try:
                written = os.write(self._fd, data)
                data = data[written:]
            except BlockingIOError:
                # 发送缓冲满：等可写或超时
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                select.select([], [self._fd], [], min(0.05, remaining))
            except OSError:
                return False
        return True

    def read_bytes(self, size=4096):
        """读取目前可用的字节；无数据返回 b""，设备断开抛出 LinkClosed。"""
        try:
            if self._serial is not None:
                return self._serial.read(size)
            if self._fd is not None:
                data = os.read(self._fd, size)
                if data:
                    return data
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise LinkClosed() from exc
        raise LinkClosed()

    def read_lines(self):
        """读出当前已到达的完整行（去掉行尾）；不完整的行留在缓冲里。

        设备断开时抛出 LinkClosed。
        """
        for _ in range(8):
            chunk = self.read_bytes(1024)
            if not chunk:
                break
            self._buf += chunk
            if len(self._buf) > _MAX_BUFFER:
                self._buf = b""
        parts = _LINE_SPLIT.split(self._buf)
        self._buf = parts.pop()
        return [p.decode("utf-8", "replace") for p in parts if p]

    def handshake(self, timeout=1.0):
        """发送 PING，等待设备回 HELLO；超时返回 None。开机日志等其他行会被跳过。"""
        if not self.write_line(encode_ping()):
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                lines = self.read_lines()
            except LinkClosed:
                return None
            for line in lines:
                msg = parse_device_line(line)
                if msg and msg.get("type") == "HELLO":
                    return msg
            time.sleep(0.02)
        return None
