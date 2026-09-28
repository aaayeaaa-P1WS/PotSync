#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中继服务器协议集成测试
======================

真实启动 server/server.py 子进程，用两个 WebSocket 客户端模拟双人房间：
建房 → 加入 → 状态广播（播放/暂停/进度）→ 改名 → ping/pong → 离房 → 错误路径。

运行：python tests/test_sync.py
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# 本机开启系统代理时，强制本地回环地址直连（urllib/websockets 均读取该变量）
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

import websockets

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server" / "server.py"
PORT = 18765
URI = f"ws://127.0.0.1:{PORT}"


def wait_port(port: int, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


async def recv(ws, timeout: float = 5.0) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout)
    return json.loads(raw)


async def recv_type(ws, mtype: str, timeout: float = 5.0) -> dict:
    """持续接收直到拿到指定类型的消息。"""
    return await recv_until(ws, lambda m: m.get("type") == mtype,
                            f"等待 {mtype} 超时", timeout)


async def recv_until(ws, pred, desc: str, timeout: float = 5.0) -> dict:
    """持续接收直到拿到满足条件的消息。"""
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            raise AssertionError(desc)
        msg = await recv(ws, left)
        if pred(msg):
            return msg


async def send(ws, **kw) -> None:
    await ws.send(json.dumps(kw))


async def run_tests() -> None:
    # --- 建房 ---
    c1 = await websockets.connect(URI)
    await send(c1, type="create", nickname="小明")
    m = await recv_type(c1, "created")
    room = m["room"]
    assert len(room) == 6 and m["state"] is None and len(m["members"]) == 1
    print(f"PASS  创建房间 {room}")

    # --- 加入 ---
    c2 = await websockets.connect(URI)
    await send(c2, type="join", room=room, nickname="小红")
    m = await recv_type(c2, "joined")
    assert m["room"] == room and len(m["members"]) == 2
    names = {x["name"] for x in m["members"]}
    assert names == {"小明", "小红"}, names
    # c1 应收到加入通知与成员更新
    notice = await recv_type(c1, "notice")
    assert "小红" in notice["text"] and "加入" in notice["text"]
    members = await recv_type(c1, "members")
    assert len(members["members"]) == 2
    print("PASS  加入房间 / 成员通知")

    # --- 状态广播（暂停在某进度） ---
    await send(c1, type="state", paused=True, position=12345,
               media="movie.mkv", duration=3600000, action="pause")
    st = await recv_type(c2, "state")
    assert st["position"] == 12345 and st["paused"] is True
    assert st["byName"] == "小明" and st["media"] == "movie.mkv"
    assert abs(st["ts"] - time.time() * 1000) < 3000, "服务器时间戳异常"
    print("PASS  状态广播（含服务器时间戳）")

    # --- 后加入者拿到房间状态 ---
    c3 = await websockets.connect(URI)
    await send(c3, type="join", room=room, nickname="路人")
    m = await recv_type(c3, "joined")
    assert m["state"]["position"] == 12345 and m["state"]["paused"] is True
    print("PASS  新成员获得房间当前状态")
    await c3.close()

    # --- 改名 ---
    await send(c2, type="rename", nickname="红红")
    members = await recv_until(
        c1,
        lambda m: m.get("type") == "members"
        and "红红" in {x["name"] for x in m["members"]},
        "等待改名后的成员列表超时")
    print("PASS  改名并广播成员列表")

    # --- ping/pong 时钟 ---
    t = int(time.time() * 1000)
    await send(c2, type="ping", t=t)
    pong = await recv_type(c2, "pong")
    assert pong["t"] == t and abs(pong["ts"] - time.time() * 1000) < 3000
    print("PASS  ping/pong 时钟估算")

    # --- 加入不存在的房间 ---
    c4 = await websockets.connect(URI)
    await send(c4, type="join", room="ZZZZZZ", nickname="x")
    err = await recv_type(c4, "error")
    assert err["code"] == "NO_ROOM"
    await c4.close()
    print("PASS  加入不存在房间返回错误")

    # --- 主动离房 ---
    await send(c2, type="leave")
    await recv_type(c2, "left")
    members = await recv_until(
        c1,
        lambda m: m.get("type") == "members" and len(m["members"]) == 1
        and m["members"][0]["name"] == "小明",
        "等待离房后的成员列表超时")
    print("PASS  离房通知")

    # --- 断线自动离房，房间变空即解散 ---
    await c1.close()
    await asyncio.sleep(0.5)
    c5 = await websockets.connect(URI)
    await send(c5, type="join", room=room, nickname="y")
    err = await recv_type(c5, "error")
    assert err["code"] == "NO_ROOM", "空房间应被解散"
    await c5.close()
    print("PASS  空房间自动解散")

    print("\n全部通过 ✔")


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "WARNING"])
    try:
        assert wait_port(PORT), "服务器启动失败"
        asyncio.run(run_tests())
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
