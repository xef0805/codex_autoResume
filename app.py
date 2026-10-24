"""Windows companion UI. No monitor runs until a chat is selected and Start is clicked."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from bridge import call
from core import Monitor, StopMonitoring, quota_description

ROOT = Path(__file__).resolve().parent


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stopped = threading.Event()
        self.stopped.set()
        self.worker = None
        self.chats = []
        root.title("Codex 额度恢复续跑助手")
        root.geometry("760x540")
        root.minsize(640, 480)
        root.option_add("*Font", "{Microsoft YaHei UI} 10")
        style = ttk.Style()
        style.theme_use("vista")
        frame = ttk.Frame(root, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="额度恢复后，继续原任务", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="只监控你选择的一个聊天。打开工具不会自动续跑。", padding=(0, 8, 0, 16)).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.choice = ttk.Combobox(row, state="readonly")
        self.choice.pack(side="left", fill="x", expand=True)
        self.refresh_button = ttk.Button(row, text="刷新聊天", command=self.refresh)
        self.refresh_button.pack(side="right", padx=(10, 0))
        self.quota = tk.StringVar(value="正在连接 Codex…")
        ttk.Label(frame, textvariable=self.quota, padding=(0, 12)).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.start_button = ttk.Button(row, text="开启监控", command=self.start, state="disabled")
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(row, text="停止监控", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=10)
        self.test_button = ttk.Button(row, text="模拟验证", command=self.simulate)
        self.test_button.pack(side="right")
        self.status = tk.StringVar(value="未绑定聊天，监控关闭")
        ttk.Label(frame, textvariable=self.status, wraplength=680, padding=(0, 15)).pack(anchor="w")
        self.log = tk.Text(frame, height=9, wrap="word", relief="solid", borderwidth=1, state="disabled")
        self.log.pack(fill="both", expand=True)
        ttk.Label(frame, text="每 60 秒检查一次；需电脑保持唤醒、Codex 和本窗口保持运行。\n停止监控不会中断已开始的任务。关闭窗口会停止监控。", padding=(0, 12, 0, 0)).pack(anchor="w")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain)
        self.refresh()

    def background(self, task):
        threading.Thread(target=task, daemon=True).start()

    def refresh(self):
        self.refresh_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        def load():
            try:
                data = call("list_threads", {"limit": 50})
                usage = call("get_usage_limits", {})
                chats = data.get("pinnedThreads", []) + data.get("threads", [])
                chats = [chat for chat in chats if chat.get("kind") == "codex" and chat.get("hostId") == "local"]
                self.events.put(("loaded", (chats, quota_description(usage))))
            except Exception as error:
                self.events.put(("load_error", str(error)))
        self.background(load)

    def start(self):
        index = self.choice.current()
        if index < 0:
            messagebox.showinfo("选择聊天", "请先选择要监控的聊天。")
            return
        target = self.chats[index]
        self.stopped.clear()
        self.set_running(True)
        self.status.set("正在开启监控…")
        def watch():
            try:
                monitor = Monitor(ROOT / "state.json", call, self.stopped)
                quota = monitor.bind(target["id"])
                self.events.put(("quota", quota))
                self.events.put(("status", "已开启监控：" + target["title"]))
                while not self.stopped.is_set():
                    try:
                        status = monitor.tick()
                    except StopMonitoring:
                        raise
                    except Exception as error:
                        status = "连接暂时不可用，下次重试：" + str(error)
                    self.events.put(("status", status))
                    if self.stopped.wait(60):
                        break
            except Exception as error:
                self.events.put(("status", str(error)))
            finally:
                self.stopped.set()
                self.events.put(("stopped", None))
        self.worker = threading.Thread(target=watch, daemon=True)
        self.worker.start()

    def stop(self):
        self.stopped.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止；如有请求已发出，其结果会写入记录")

    def set_running(self, running):
        self.start_button.configure(state="disabled" if running or not self.chats else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.refresh_button.configure(state="disabled" if running else "normal")
        self.choice.configure(state="disabled" if running else "readonly")
        self.test_button.configure(state="disabled" if running else "normal")

    def record(self, text):
        if getattr(self, "last_log", None) == text:
            return
        self.last_log = text
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S") + "  " + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def drain(self):
        while not self.events.empty():
            event, value = self.events.get()
            if event == "loaded":
                self.chats, quota = value
                self.choice.configure(values=[chat["title"] + "  [" + chat["id"][-6:] + "]" for chat in self.chats])
                self.choice.set("")
                self.quota.set(quota)
                self.set_running(False)
                self.record("连接成功；请选择聊天后开启监控。")
            elif event == "load_error":
                self.quota.set("连接失败，请确认 Codex 已打开后刷新")
                self.refresh_button.configure(state="normal")
                self.record(value)
            elif event == "quota":
                self.quota.set(value)
            elif event == "stopped":
                self.set_running(False)
                if self.status.get().startswith("正在停止"):
                    self.status.set("监控已停止")
                self.record("监控已停止。")
            elif event == "test_done":
                self.test_button.configure(state="normal")
            else:
                self.status.set(value)
                self.record(value)
        self.root.after(100, self.drain)

    def simulate(self):
        self.test_button.configure(state="disabled")
        def test():
            result = subprocess.run([sys.executable, str(ROOT / "test_monitor.py")],
                                    capture_output=True, text=True, encoding="utf-8",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.events.put(("status", "模拟验证通过，未向任何聊天发送消息。" if result.returncode == 0
                             else "模拟验证失败：" + result.stdout + result.stderr))
            self.events.put(("test_done", None))
        self.background(test)

    def close(self):
        self.stopped.set()
        if self.worker and self.worker.is_alive():
            self.status.set("正在关闭：等待当前检查或发送结束…")
            self.root.after(200, self.close)
            return
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Read-only desktop connection check")
    args = parser.parse_args()
    if args.check:
        usage = call("get_usage_limits", {})
        data = call("list_threads", {"limit": 5})
        print(json.dumps({"connected": True, "quota": quota_description(usage),
                          "ordinaryUsageAllowed": usage.get("ordinaryUsageAllowed"),
                          "recentThreads": len(data.get("threads", [])), "sentMessages": 0}, ensure_ascii=True))
        return
    root = tk.Tk()
    # Prevent two copies from independently sending the same continuation.
    import msvcrt
    lock = (ROOT / "app.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        messagebox.showinfo("已运行", "续跑助手已经打开，请使用已有窗口。")
        root.destroy()
        lock.close()
        return
    App(root)
    root.mainloop()
    lock.close()


if __name__ == "__main__":
    main()
