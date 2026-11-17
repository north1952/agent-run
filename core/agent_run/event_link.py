"""Local hook/control datagrams: Unix socket on macOS, loopback UDP on Windows."""
import os
import socket
from pathlib import Path


def default_home():
    override = os.environ.get("AGENT_RUN_HOME")
    if override:
        return override
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.environ["LOCALAPPDATA"], "agent-run")
    return os.path.expanduser("~/.agent-run")


def _endpoint(home):
    return Path(home, "run", "port.txt" if os.name == "nt" else "ar.sock")


def open_receiver(home):
    endpoint = _endpoint(home)
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_INET if os.name == "nt" else socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        if os.name == "nt":
            sock.bind(("127.0.0.1", 0))
            temp = endpoint.with_name("port.%d.tmp" % os.getpid())
            try:
                temp.write_text(str(sock.getsockname()[1]), encoding="ascii")
                temp.replace(endpoint)
            finally:
                temp.unlink(missing_ok=True)
        else:
            endpoint.unlink(missing_ok=True)
            sock.bind(str(endpoint))
        return sock
    except BaseException:
        sock.close()
        raise


def close_receiver(sock, home):
    endpoint = _endpoint(home)
    try:
        if os.name != "nt" or endpoint.read_text(encoding="ascii") == str(sock.getsockname()[1]):
            endpoint.unlink(missing_ok=True)
    except OSError:
        pass
    finally:
        sock.close()


def send(home, data):
    endpoint = _endpoint(home)
    if os.name == "nt":
        try:
            port = int(endpoint.read_text(encoding="ascii"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError as exc:
            raise OSError("invalid agent-run port file") from exc
        family, address = socket.AF_INET, ("127.0.0.1", port)
    else:
        family, address = socket.AF_UNIX, str(endpoint)
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.1)
        sock.sendto(data, address)
