#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内嵌服务器测试
==============

在进程内启动 EmbeddedServer（与客户端"主机模式"同一代码路径），
验证建房/加入/状态广播后优雅停止。

运行：python tests/test_embedded.py
"""

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

# 本机开启系统代理时，强制本地回环地址直连（urllib/websockets 均读取该变量）
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from relay import EmbeddedServer  # noqa: E402

PORT = 18767
URI = f"ws://127.0.0.1:{PORT}"


async def scene() -> None:
    c1 = await websockets.connect(URI)
    await c1.send(json.dumps({"type": "create", "nickname": "主机"}))
    m = json.loads(await asyncio.wait_for(c1.recv(), 5))
    assert m["type"] == "created"
    room = m["room"]

    c2 = await websockets.connect(URI)
    await c2.send(json.dumps({"type": "join", "room": room, "nickname": "好友"}))
    m = json.loads(await asyncio.wait_for(c2.recv(), 5))
    assert m["type"] == "joined" and len(m["members"]) == 2

    await c2.send(json.dumps({"type": "state", "paused": False, "position": 7777,
                              "media": "a.mkv", "duration": 60000, "action": "play"}))
    # c1 队列里可能先有 c2 加入时的 notice/members，读到 state 为止
    end = time.time() + 5
    while True:
        m = json.loads(await asyncio.wait_for(c1.recv(), max(0.1, end - time.time())))
        if m["type"] == "state":
            break
    assert m["position"] == 7777 and m["byName"] == "好友"
    print(f"PASS  内嵌服务器建房/加入/状态广播（房间 {room}）")

    await c1.close()
    await c2.close()


def main() -> int:
    ready = threading.Event()
    result = {}

    def on_ready(ok: bool, err: str) -> None:
        result.update(ok=ok, err=err)
        ready.set()

    server = EmbeddedServer("127.0.0.1", PORT, on_ready=on_ready)
    server.start()
    assert ready.wait(8), "内嵌服务器启动超时"
    assert result["ok"], f"内嵌服务器启动失败: {result['err']}"
    assert server.running
    print("PASS  EmbeddedServer 启动")

    asyncio.run(scene())

    server.stop()
    server.wait_stopped(3)
    assert not server.running
    print("PASS  EmbeddedServer 优雅停止")

    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
