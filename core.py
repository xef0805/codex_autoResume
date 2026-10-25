"""Deterministic monitoring: only a confirmed quota failure can resume."""
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path

RESUME_PROMPT = (
    "这是你此前授权的额度恢复后自动续跑。请先核对原任务目标、已有文件和已完成的操作，"
    "仅继续尚未完成的工作，避免重复执行。保持原有范围与权限。"
    "如果用户已要求停止、工作已完成，或下一步需要用户的关键决定或授权，请停下说明。"
)


class StopMonitoring(RuntimeError):
    pass


def quota_bucket(usage):
    buckets = usage.get("rateLimitsByLimitId")
    if buckets is not None:
        return buckets.get("codex", {})
    bucket = usage.get("rateLimits", {})
    return bucket if bucket.get("limitId") == "codex" else {}


def five_hour_window(usage):
    window = quota_bucket(usage).get("primary") or {}
    percent = window.get("usedPercent")
    if window.get("windowDurationMins") != 300 or type(percent) not in (int, float):
        return None
    return window if 0 <= percent <= 100 else None


def ordinary_usage_ready(usage):
    # This authoritative field is required. Percentages alone cannot prove recovery.
    if usage.get("ordinaryUsageAllowed") is not True:
        return False
    bucket = quota_bucket(usage)
    if not bucket or bucket.get("rateLimitReachedType") or bucket.get("spendControlReached"):
        return False
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if window and window.get("usedPercent", 100) >= 100:
            return False
    individual = bucket.get("individualLimit")
    return not individual or individual.get("remainingPercent", 0) > 0


def quota_failure(turn):
    if turn.get("status") != "failed":
        return False
    error = turn.get("error") or {}
    info = error.get("codexErrorInfo")
    if info is not None:
        return isinstance(info, str) and info.lower() == "usagelimitexceeded"
    # App-tools can omit codexErrorInfo. Match only the actual turn error,
    # never assistant text or arbitrary mentions of a rate limit.
    return bool(re.match(r"^you['’]ve hit your usage limit(?:[.!:\s]|$)",
                         error.get("message", ""), re.IGNORECASE))


def quota_description(usage):
    window = five_hour_window(usage)
    if window is None:
        return "5 小时额度状态不可用"
    return "5 小时额度剩余约 {}%".format(100 - window["usedPercent"])


class Monitor:
    def __init__(self, state_path, rpc, stopped):
        self.path = Path(state_path)
        self.rpc = rpc
        self.stopped = stopped
        self.current_usage = None
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            import os
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def bind(self, thread_id):
        usage = self.rpc("get_usage_limits", {})
        self.current_usage = usage
        if not usage.get("accountId"):
            raise RuntimeError("无法确认登录账号；未开启监控。")
        if self.state.get("threadId") != thread_id or self.state.get("accountId") != usage["accountId"]:
            self.state = {"threadId": thread_id, "accountId": usage["accountId"],
                          "attempts": self.state.get("attempts", {})}
        self.save()
        return quota_description(usage)

    def read_latest(self):
        return self.rpc("read_thread", {"threadId": self.state["threadId"], "hostId": "local",
                                       "turnLimit": 1, "includeOutputs": False,
                                       "maxOutputCharsPerItem": 1})

    def tick(self):
        if self.stopped.is_set():
            return "监控已停止"
        snapshot = self.read_latest()
        usage = self.rpc("get_usage_limits", {})
        self.current_usage = usage
        if usage.get("accountId") != self.state["accountId"]:
            raise StopMonitoring("登录账号发生变化；已停止监控，请重新选择聊天。")
        thread = snapshot.get("thread", {})
        turns = snapshot.get("turns", [])
        if thread.get("kind") != "codex" or thread.get("hostId") != "local":
            raise StopMonitoring("仅支持本机 Codex 聊天；已停止监控。")
        if not turns:
            return "正在监控，尚无任务记录"
        latest = turns[0]
        if self.state.get("pending") and self.state["pending"] != latest.get("id"):
            self.state.pop("pending")
            self.save()
        status = thread.get("status", {}).get("type")
        if status == "active" or latest.get("status") == "inProgress":
            return "正在监控：任务运行中或等待用户操作"
        if status not in ("idle", "notLoaded", "systemError"):
            return "聊天状态不明确，暂不续跑"
        if not quota_failure(latest):
            if latest.get("status") == "completed":
                return "正在监控：最近一轮已完成，等待新的额度中断"
            if latest.get("status") == "interrupted":
                return "正在监控：最近一轮被中断，不自动续跑"
            return "正在监控：最近一轮不是已识别的额度失败，不自动续跑"
        if not latest.get("id"):
            return "无法确认失败记录，暂不续跑"
        key = self.state["threadId"] + ":" + latest["id"]
        attempts = self.state.setdefault("attempts", {})
        if key in attempts:
            return "该次中断已处理或发送结果待核实；不会重复发送"
        window = five_hour_window(usage)
        if window is None:
            return "无法确认 5 小时额度状态，暂不续跑"
        if self.state.get("pending") != latest["id"]:
            self.state["pending"] = latest["id"]
            self.save()
        if window["usedPercent"] >= 100:
            reset = window.get("resetsAt")
            when = (datetime.fromtimestamp(reset, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
                    if isinstance(reset, (int, float)) else "未知")
            return "已确认额度中断，等待恢复（预计北京时间 {}）".format(when)
        if not ordinary_usage_ready(usage):
            return "5 小时额度已变化，但账号仍不可用（可能受周额度等限制），继续等待"
        # Re-read immediately before sending: manual continuation or stopping cancels this retry.
        confirm = self.read_latest()
        newest = (confirm.get("turns") or [{}])[0]
        if (newest.get("id") != latest["id"] or not quota_failure(newest)
                or confirm.get("thread", {}).get("status", {}).get("type") not in ("idle", "notLoaded", "systemError")):
            return "聊天已有新进展，取消本次自动续跑"
        if self.stopped.is_set():
            return "监控已停止"
        # Reserve durably before the external effect. An ambiguous timeout is never blindly retried.
        attempts[key] = "reserved"
        self.save()
        if self.stopped.is_set():
            del attempts[key]
            self.save()
            return "监控已停止"
        try:
            result = self.rpc("send_message_to_thread", {
                "threadId": self.state["threadId"], "hostId": "local", "prompt": RESUME_PROMPT,
            })
            if result.get("isError") or result.get("error"):
                raise RuntimeError("续跑请求未确认成功。")
        except Exception as error:
            attempts[key] = "uncertain"
            self.save()
            raise StopMonitoring("续跑消息的发送结果未确认。为避免重复，已停止监控；请在原聊天核实。") from error
        attempts[key] = "sent"
        self.state.pop("pending", None)
        self.save()
        return "已发送续跑消息；如任务再次因 5 小时额度耗尽而失败，将继续等待恢复"
