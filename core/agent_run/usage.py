"""订阅用量采集（专用页面方案 §3、§5.3）。

为每个工具给出两个限额窗口：5 小时与每周。每个窗口取下列三者之一：
- Window(used, resets_at)：已用百分比（0–100）与重置时间（Unix 秒，未知时为 None）；
- NO_LIMIT：读到了用量记录，但账户没有这一窗口（如 Codex prolite 套餐没有 5 小时限额）；
- None：暂无数据。

数据来源：
- Codex：读 ~/.codex/sessions 下最近修改的会话文件末尾，取最后一条 token_count 事件的
  rate_limits。primary / secondary 不固定对应 5 小时与每周，按 window_minutes 归类。
- WorkBuddy：没有可验证的本地订阅用量来源，返回暂无数据。

Codex 的套餐名取自同一条 token_count 事件的 plan_type。
"""
import calendar
import json
import os
import re
from collections import namedtuple
from datetime import datetime, timezone

# 单个限额窗口：已用百分比与重置时间（Unix 秒或 None）
Window = namedtuple("Window", "used resets_at")

# 账户没有这一窗口（区别于 None 表示的“暂无数据”）
NO_LIMIT = "none"

# 单个工具的用量：两个窗口、数据时间（Unix 秒）、最近一次失败原因与订阅类型的显示名（未知时为 None）
ToolUsage = namedtuple("ToolUsage", "five_hour seven_day updated error plan", defaults=(None,))
EMPTY = ToolUsage(None, None, None, None)

# 窗口长度（分钟）
FIVE_HOUR_MINUTES = 300
WEEK_MINUTES = 10080

# Codex：扫描周期、Stop 后提前扫描的最小间隔、只看最近几天修改过的文件、最多读几个文件、每个读多少字节
CODEX_SCAN_SECONDS = 30.0
CODEX_MIN_GAP_SECONDS = 5.0
CODEX_RECENT_DAYS = 7
CODEX_MAX_FILES = 3
CODEX_TAIL_BYTES = 512 * 1024

_FRACTION = re.compile(r"\.(\d+)")

# 订阅类型：需要特别写法的 Codex 套餐；其余套餐按原文把下划线换成空格、各词首字母大写
CODEX_PLANS = {"prolite": "Pro Lite", "promax": "Pro Max"}


def _is_number(value):
    """是否为数值（排除 bool，JSON 里的 true/false 不算数值）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_time(value):
    """把重置时间换算成 Unix 秒：接受 Unix 秒、Unix 毫秒与 ISO 8601 字符串；无法识别时返回 None。"""
    if _is_number(value):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python 3.9 的 fromisoformat 只认 3 位或 6 位小数，统一补齐或截断到 6 位
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return calendar.timegm(parsed.utctimetuple()) + parsed.microsecond / 1e6


def _plan_label(raw, known):
    """套餐原文换成显示名：先查 known，查不到时把下划线换成空格、各词首字母大写。

    不是非空字符串时返回 None。字符清洗与长度限制由 A 帧编码负责（protocol._plan_field）。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip().lower()
    if raw in known:
        return known[raw]
    return " ".join(word.capitalize() for word in raw.split("_") if word)


def codex_plan_label(raw):
    """Codex 会话记录里 plan_type 的显示名，如 prolite → "Pro Lite"、plus → "Plus"；没有时返回 None。"""
    return _plan_label(raw, CODEX_PLANS)


def classify_codex_limits(rate_limits):
    """把 Codex 的 rate_limits 按窗口长度归类，返回 (5 小时窗口, 每周窗口)。"""
    five = seven = NO_LIMIT
    for key in ("primary", "secondary"):
        entry = rate_limits.get(key)
        if not isinstance(entry, dict) or not _is_number(entry.get("used_percent")):
            continue
        window = Window(float(entry["used_percent"]), _parse_time(entry.get("resets_at")))
        minutes = entry.get("window_minutes")
        if minutes == FIVE_HOUR_MINUTES:
            five = window
        elif minutes == WEEK_MINUTES:
            seven = window
    return five, seven


def _last_codex_event(path, tail_bytes, fallback_ts):
    """读一个会话文件的末尾，返回 (事件时间, 5 小时窗口, 每周窗口, 套餐显示名)；没有可用事件时返回 None。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            offset = max(0, size - tail_bytes)
            f.seek(offset)
            data = f.read()
    except OSError:
        return None
    lines = data.split(b"\n")
    if offset > 0:
        lines = lines[1:]   # 从文件中间开始读时，第一行不完整
    # 从后往前找：最后一条带 rate_limits 的 token_count 就是最新的用量
    for line in reversed(lines):
        if b'"token_count"' not in line:
            continue
        try:
            event = json.loads(line.decode("utf-8"))
        except ValueError:
            continue    # 正在写入的末行可能不完整
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict) or limits.get("limit_id") not in (None, "codex"):
            continue
        five, seven = classify_codex_limits(limits)
        ts = _parse_time(event.get("timestamp"))
        # 套餐与用量取自同一条事件，两者总是一致
        return (ts if ts is not None else fallback_ts), five, seven, codex_plan_label(limits.get("plan_type"))
    return None


def read_codex_usage(root, now, days=CODEX_RECENT_DAYS, max_files=CODEX_MAX_FILES, tail_bytes=CODEX_TAIL_BYTES):
    """读 Codex 会话文件中的最新用量；没有找到时返回 None。

    只看最近 days 天修改过的文件，按修改时间取最新 max_files 个，各读末尾 tail_bytes 字节；
    再在这些文件里取事件时间最晚的一条。
    """
    cutoff = now - days * 86400
    candidates = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                continue
            path = os.path.join(dirpath, name)
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                candidates.append((mtime, path))
    candidates.sort(reverse=True)

    best = None
    for mtime, path in candidates[:max_files]:
        found = _last_codex_event(path, tail_bytes, mtime)
        if found is not None and (best is None or found[0] > best[0]):
            best = found
    if best is None:
        return None
    return ToolUsage(five_hour=best[1], seven_day=best[2], updated=best[0], error=None, plan=best[3])


class UsageCollector:
    """定期扫描本机 Codex 会话文件；WorkBuddy 只提供会话计数。"""

    def __init__(self, codex_root=None):
        self._codex_root = codex_root or os.path.join(
            os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "sessions")
        self._codex = None
        self._next_codex = 0.0
        self._last_codex_scan = None

    def step(self, now):
        if now >= self._next_codex:
            self._last_codex_scan = now
            self._next_codex = now + CODEX_SCAN_SECONDS
            self._codex = read_codex_usage(self._codex_root, now)

    def on_stop(self, tool, now):
        if tool == "codex":
            earliest = now if self._last_codex_scan is None else max(now, self._last_codex_scan + CODEX_MIN_GAP_SECONDS)
            self._next_codex = min(self._next_codex, earliest)

    def get(self, tool):
        if tool == "codex":
            return self._codex or EMPTY
        return EMPTY
