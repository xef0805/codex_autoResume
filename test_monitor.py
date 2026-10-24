"""No real app calls or messages: reproducible quota-recovery scenarios."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path

from core import Monitor, StopMonitoring


def usage(percent=100, allowed=False, weekly=30):
    return {"accountId": "test-account", "ordinaryUsageAllowed": allowed,
            "rateLimitsByLimitId": {"codex": {
                "primary": {"windowDurationMins": 300, "usedPercent": percent, "resetsAt": 2000000000},
                "secondary": {"windowDurationMins": 10080, "usedPercent": weekly},
                "rateLimitReachedType": None, "spendControlReached": False}}}


def snapshot(status="failed", code="usageLimitExceeded", turn_id="turn-1", active=False):
    return {"thread": {"kind": "codex", "hostId": "local",
                       "status": {"type": "active" if active else "idle"}},
            "turns": [{"id": turn_id, "status": status,
                       "error": {"codexErrorInfo": code}}]}


class FakeDesktop:
    def __init__(self):
        self.usage = usage()
        self.snapshot = snapshot()
        self.messages = []
        self.before_send_read = None
        self.send_error = False
        self.read_count = 0

    def __call__(self, tool, args):
        if tool == "get_usage_limits":
            return copy.deepcopy(self.usage)
        if tool == "read_thread":
            self.read_count += 1
            if self.before_send_read:
                self.before_send_read(self)
            return copy.deepcopy(self.snapshot)
        if tool == "send_message_to_thread":
            self.messages.append(copy.deepcopy(args))
            if self.send_error:
                raise TimeoutError("模拟已发送但回执超时")
            return {"status": "sent"}
        raise AssertionError(tool)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.desktop = FakeDesktop()
        self.stop = threading.Event()
        self.path = Path(self.temp.name) / "state.json"
        self.monitor = Monitor(self.path, self.desktop, self.stop)
        self.monitor.bind("selected-thread")

    def recover(self):
        self.desktop.usage = usage(2, True)

    def test_recovery_sends_once_to_selected_chat(self):
        self.assertIn("等待恢复", self.monitor.tick())
        self.recover()
        self.assertIn("已发送", self.monitor.tick())
        self.monitor.tick()
        self.assertEqual(len(self.desktop.messages), 1)
        self.assertEqual(self.desktop.messages[0]["threadId"], "selected-thread")
        self.assertNotIn("model", self.desktop.messages[0])

    def test_depleted_waits_without_model_calls(self):
        for _ in range(3):
            self.monitor.tick()
        self.assertEqual(self.desktop.messages, [])

    def test_completed_interrupted_and_other_errors_are_ignored(self):
        for state, code in [("completed", "usageLimitExceeded"), ("interrupted", "usageLimitExceeded"),
                            ("failed", "responseStreamDisconnected"), ("failed", None)]:
            with self.subTest(state=state, code=code):
                self.desktop.snapshot = snapshot(state, code)
                self.monitor.tick()
        self.assertEqual(self.desktop.messages, [])

    def test_active_or_waiting_user_is_ignored(self):
        self.monitor.tick()
        self.recover()
        self.desktop.snapshot = snapshot(active=True)
        self.monitor.tick()
        self.assertEqual(self.desktop.messages, [])

    def test_weekly_quota_still_blocks(self):
        self.monitor.tick()
        self.desktop.usage = usage(0, False, weekly=100)
        self.monitor.tick()
        self.assertEqual(self.desktop.messages, [])

    def test_server_permission_required_even_with_percent_reset(self):
        self.monitor.tick()
        self.desktop.usage = usage(0, False)
        self.monitor.tick()
        self.desktop.usage["ordinaryUsageAllowed"] = None
        self.monitor.tick()
        self.assertEqual(self.desktop.messages, [])

    def test_old_failure_without_observed_depletion_is_ignored(self):
        self.recover()
        self.assertIn("未观察到", self.monitor.tick())
        self.assertEqual(self.desktop.messages, [])

    def test_new_manual_turn_cancels_pending(self):
        self.monitor.tick()
        self.recover()
        self.desktop.snapshot = snapshot("completed", turn_id="manual-turn")
        self.monitor.tick()
        self.assertNotIn("pending", self.monitor.state)
        self.assertEqual(self.desktop.messages, [])

    def test_new_turn_during_last_read_cancels_send(self):
        self.monitor.tick()
        self.recover()
        self.desktop.before_send_read = lambda fake: setattr(fake, "snapshot", snapshot("completed", turn_id="new")) if fake.read_count == 3 else None
        self.monitor.tick()
        self.assertEqual(self.desktop.messages, [])

    def test_stop_before_send_cancels(self):
        self.monitor.tick()
        self.recover()
        self.desktop.before_send_read = lambda fake: self.stop.set() if fake.read_count == 3 else None
        self.monitor.tick()
        self.assertEqual(self.desktop.messages, [])

    def test_restart_keeps_pending_and_duplicate_protection(self):
        self.monitor.tick()
        restarted = Monitor(self.path, self.desktop, self.stop)
        restarted.bind("selected-thread")
        self.recover()
        restarted.tick()
        restarted_again = Monitor(self.path, self.desktop, self.stop)
        restarted_again.bind("selected-thread")
        restarted_again.tick()
        self.assertEqual(len(self.desktop.messages), 1)

    def test_ambiguous_send_is_not_retried_after_restart(self):
        self.monitor.tick()
        self.recover()
        self.desktop.send_error = True
        with self.assertRaises(StopMonitoring):
            self.monitor.tick()
        restarted = Monitor(self.path, self.desktop, self.stop)
        restarted.bind("selected-thread")
        restarted.tick()
        self.assertEqual(len(self.desktop.messages), 1)

    def test_repeated_quota_failure_allows_one_retry_per_turn(self):
        self.monitor.tick()
        self.recover()
        self.monitor.tick()
        self.desktop.snapshot = snapshot(turn_id="turn-2")
        self.desktop.usage = usage()
        self.monitor.tick()
        self.recover()
        self.monitor.tick()
        self.assertEqual(len(self.desktop.messages), 2)

    def test_account_switch_stops(self):
        self.monitor.tick()
        self.recover()
        self.desktop.usage["accountId"] = "another-account"
        with self.assertRaises(StopMonitoring):
            self.monitor.tick()
        self.assertEqual(self.desktop.messages, [])

    def test_unknown_window_or_bucket_is_ignored(self):
        for value in [None, 60, 10080]:
            self.desktop.usage["rateLimitsByLimitId"]["codex"]["primary"]["windowDurationMins"] = value
            self.monitor.tick()
        self.desktop.usage["rateLimitsByLimitId"] = {"other": {}}
        self.monitor.tick()
        self.assertEqual(self.desktop.messages, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
