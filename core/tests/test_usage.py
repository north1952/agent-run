"""订阅用量采集测试（专用页面方案 §3、§5.3）。

使用本地 Codex 会话文件夹具。
"""
import json
import os
import tempfile
import unittest

from agent_run.usage import (
    NO_LIMIT,
    UsageCollector,
    Window,
    classify_codex_limits,
    codex_plan_label,
    read_codex_usage,
)

NOW = 1794192000.0
TOKEN = "sk-ant-oat01-NEVER-LEAK-THIS"


def token_count(ts, primary=None, secondary=None, limit_id="codex", rate_limits=True, plan_type="prolite"):
    """构造一行与 Codex 会话文件格式一致的 token_count 事件；plan_type 为 None 时不写这个键。"""
    payload = {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1}}}
    if rate_limits:
        payload["rate_limits"] = {
            "limit_id": limit_id,
            "primary": primary,
            "secondary": secondary,
        }
        if plan_type is not None:
            payload["rate_limits"]["plan_type"] = plan_type
    else:
        payload["rate_limits"] = None
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": payload}) + "\n"


def window(used, minutes, resets_at):
    return {"used_percent": used, "window_minutes": minutes, "resets_at": resets_at}


WEEK = window(5.0, 10080, 1794632374)
FIVE = window(40.0, 300, 1794200000)


class ClassifyCodexTest(unittest.TestCase):
    def test_week_only_account_has_no_five_hour_limit(self):
        self.assertEqual(classify_codex_limits({"primary": WEEK, "secondary": None}),
                         (NO_LIMIT, Window(5.0, 1794632374)))

    def test_windows_are_classified_by_length_not_position(self):
        expected = (Window(40.0, 1794200000), Window(5.0, 1794632374))
        self.assertEqual(classify_codex_limits({"primary": FIVE, "secondary": WEEK}), expected)
        self.assertEqual(classify_codex_limits({"primary": WEEK, "secondary": FIVE}), expected)

    def test_unknown_window_length_and_bad_values_are_ignored(self):
        odd = window(9.0, 1440, 1794200000)
        bad = {"used_percent": "x", "window_minutes": 300, "resets_at": 1}
        self.assertEqual(classify_codex_limits({"primary": odd, "secondary": bad}), (NO_LIMIT, NO_LIMIT))

    def test_missing_reset_time_is_unknown(self):
        self.assertEqual(classify_codex_limits({"primary": window(7.0, 300, None)})[0], Window(7.0, None))


class ReadCodexTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="codex-")

    def write(self, name, text, age=0.0):
        day = os.path.join(self.root, "2026", "11", "08")
        os.makedirs(day, exist_ok=True)
        path = os.path.join(day, name)
        with open(path, "w") as f:
            f.write(text)
        os.utime(path, (NOW - age, NOW - age))
        return path

    def test_latest_event_wins_and_truncated_line_is_skipped(self):
        self.write("rollout-a.jsonl",
                   token_count("2026-11-09T01:00:00.000Z", window(3.0, 10080, 1794632374))
                   + token_count("2026-11-09T01:04:23.679Z", WEEK)
                   + '{"timestamp": "2026-11-09T01:05:00.000Z", "type": "event_msg", "payload": {"type": "token_co')
        usage = read_codex_usage(self.root, NOW)
        self.assertEqual((usage.five_hour, usage.seven_day), (NO_LIMIT, Window(5.0, 1794632374)))
        self.assertAlmostEqual(usage.updated, 1794186263.679, places=2)
        self.assertIsNone(usage.error)
        self.assertEqual(usage.plan, "Pro Lite")

    def test_plan_comes_from_the_same_event(self):
        # 套餐取自用量所在的那条事件；没有 plan_type 时为 None，用量照常
        self.write("rollout-a.jsonl",
                   token_count("2026-11-09T01:00:00.000Z", WEEK, plan_type="plus")
                   + token_count("2026-11-09T01:01:00.000Z", WEEK, plan_type=None))
        usage = read_codex_usage(self.root, NOW)
        self.assertEqual(usage.seven_day, Window(5.0, 1794632374))
        self.assertIsNone(usage.plan)

    def test_events_without_rate_limits_or_other_limit_ids_are_skipped(self):
        self.write("rollout-a.jsonl",
                   token_count("2026-11-09T01:00:00.000Z", WEEK)
                   + token_count("2026-11-09T01:01:00.000Z", rate_limits=False)
                   + token_count("2026-11-09T01:02:00.000Z", window(99.0, 10080, 1), limit_id="other"))
        self.assertEqual(read_codex_usage(self.root, NOW).seven_day, Window(5.0, 1794632374))

    def test_latest_event_across_files(self):
        self.write("rollout-old.jsonl", token_count("2026-11-09T02:00:00.000Z", window(8.0, 10080, 5)), age=60)
        self.write("rollout-new.jsonl", token_count("2026-11-09T01:00:00.000Z", window(6.0, 10080, 5)))
        # 修改时间更新的文件里事件反而更早：以事件时间为准
        self.assertEqual(read_codex_usage(self.root, NOW).seven_day, Window(8.0, 5))

    def test_files_older_than_a_week_are_ignored(self):
        self.write("rollout-a.jsonl", token_count("2026-11-01T01:00:00.000Z", WEEK), age=8 * 86400)
        self.assertIsNone(read_codex_usage(self.root, NOW))

    def test_only_the_tail_of_large_files_is_read(self):
        self.write("rollout-a.jsonl", token_count("2026-11-09T01:00:00.000Z", WEEK) + ("x" * 4000 + "\n"))
        self.assertIsNone(read_codex_usage(self.root, NOW, tail_bytes=1024))
        self.assertIsNotNone(read_codex_usage(self.root, NOW, tail_bytes=8192))

    def test_missing_root_and_no_events(self):
        self.assertIsNone(read_codex_usage(os.path.join(self.root, "nope"), NOW))
        self.write("rollout-a.jsonl", '{"type": "session_meta"}\n')
        self.assertIsNone(read_codex_usage(self.root, NOW))


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="codex-")
        day = os.path.join(self.root, "2026", "11", "09")
        os.makedirs(day)
        self.path = os.path.join(day, "rollout-a.jsonl")
        self.collector = UsageCollector(codex_root=self.root)

    def write(self, used):
        with open(self.path, "w") as f:
            f.write(token_count("2026-11-09T01:00:00.000Z", window(used, 10080, 1794632374)))
        os.utime(self.path, (NOW, NOW))

    def test_codex_is_scanned_every_thirty_seconds_or_after_stop(self):
        self.write(5.0)
        self.collector.step(NOW)
        self.assertEqual(self.collector.get("codex").seven_day.used, 5.0)
        self.write(6.0)
        self.collector.step(NOW + 10)
        self.assertEqual(self.collector.get("codex").seven_day.used, 5.0)
        self.collector.on_stop("codex", NOW + 10)
        self.collector.step(NOW + 10)
        self.assertEqual(self.collector.get("codex").seven_day.used, 6.0)

    def test_unavailable_workbuddy_quota(self):
        usage = self.collector.get("workbuddy")
        self.assertEqual((usage.five_hour, usage.seven_day, usage.updated, usage.plan), (None, None, None, None))


if __name__ == "__main__":
    unittest.main()
