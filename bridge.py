"""Small client for the desktop app-tools pipe shipped with Codex.

This is a local, version-dependent integration. It never reads login tokens.
"""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import uuid

ALLOWED_TOOLS = {"list_threads", "read_thread", "get_usage_limits", "send_message_to_thread"}


def find_pipe():
    # Re-discover on every call: the desktop app changes its pipe after restart.
    names = [name for name in os.listdir("\\\\.\\pipe\\")
             if name.startswith("codex-app-tools-")]
    if len(names) != 1:
        raise RuntimeError("请保持一个 Codex 桌面应用运行；未找到唯一的应用连接。")
    return "\\\\.\\pipe\\" + names[0]


def read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        part = stream.read(size - len(chunks))
        if not part:
            raise RuntimeError("Codex 应用连接已断开。")
        chunks.extend(part)
    return chunks


def exchange(stream, method, params):
    request_id = str(uuid.uuid4())
    payload = json.dumps({"jsonrpc": "2.0", "id": request_id,
                          "method": method, "params": params}).encode("utf-8")
    frame = struct.pack("<I", len(payload)) + payload
    offset = 0
    while offset < len(frame):
        written = stream.write(frame[offset:])
        if not written:
            raise RuntimeError("无法发送本地请求。")
        offset += written
    size = struct.unpack("<I", read_exact(stream, 4))[0]
    if not 0 < size <= 8 * 1024 * 1024:
        raise RuntimeError("Codex 返回了无效的数据长度。")
    response = json.loads(read_exact(stream, size))
    if response.get("id") != request_id:
        raise RuntimeError("Codex 返回了不匹配的请求编号。")
    if "error" in response:
        raise RuntimeError(response["error"].get("message", "Codex 请求失败。"))
    return response["result"]


def pipe_call(tool, arguments):
    if tool not in ALLOWED_TOOLS:
        raise ValueError("不支持的工具。")
    settings = Path(__file__).with_name("local_settings.json")
    if not settings.exists():
        raise RuntimeError("请按 README 配置 local_settings.json 中的管理聊天 ID。")
    controller = json.loads(settings.read_text(encoding="utf-8"))["controllerThreadId"]
    uuid.UUID(controller)
    with open(find_pipe(), "r+b", buffering=0) as stream:
        catalog = exchange(stream, "tools/list", {"threadStartKind": "all"})
        entry = next((item for item in catalog["tools"] if item["name"] == tool), None)
        if entry is None:
            raise RuntimeError("当前 Codex 版本没有所需接口：" + tool)
        result = exchange(stream, "tools/call", {
            "namespace": entry["namespace"], "tool": tool, "arguments": arguments,
            "callerSource": "codex", "hostId": "local", "threadId": controller,
            "turnId": "auto-resume-" + str(uuid.uuid4()),
            "callId": "auto-resume-" + str(uuid.uuid4()),
        })
    texts = [item["text"] for item in result.get("contentItems", [])
             if item.get("type") == "inputText"]
    text = "\n".join(texts)
    if not result.get("success"):
        raise RuntimeError(text or "Codex 拒绝了请求。")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"message": text}


def call(tool, arguments):
    # A child process provides a hard timeout for a stalled Windows pipe.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), tool, json.dumps(arguments)],
        capture_output=True, text=True, encoding="utf-8", timeout=35,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or result.stderr.strip() or "本地请求失败。")
    return json.loads(result.stdout)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(pipe_call(sys.argv[1], json.loads(sys.argv[2])), ensure_ascii=False))
    except Exception as error:
        print(str(error))
        sys.exit(1)
