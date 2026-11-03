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
# 第二个服务器：极短的宽限期/房间保留期，用来验证过期回收（默认值 120s/900s 测试等不起）
TTL_PORT = 18766
TTL_URI = f"ws://127.0.0.1:{TTL_PORT}"


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
    token1 = m["you"]
    assert len(room) == 6 and m["state"] is None and len(m["members"]) == 1
    assert m.get("protocol") == 2 and token1, "协议 2.0 应下发 protocol 与成员 id"
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

    # --- 连接断开：保留成员位，等待自动重连（2.0 语义） ---
    await c3.close()
    notice = await recv_until(
        c1,
        lambda m: m.get("type") == "notice" and "连接中断" in (m.get("text") or ""),
        "等待掉线通知超时")
    assert "路人" in notice["text"], notice
    await recv_until(
        c1,
        lambda m: m.get("type") == "members"
        and any(x["name"] == "路人" and x.get("online") is False
                for x in m["members"]),
        "等待离线成员列表超时")
    print("PASS  掉线成员保留为离线（等待自动重连）")

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

    # --- 主动离房：立即移除；掉线的路人仍保留 ---
    await send(c2, type="leave")
    await recv_type(c2, "left")
    await recv_until(
        c1,
        lambda m: m.get("type") == "members"
        and [x["name"] for x in m["members"] if x.get("online")] == ["小明"],
        "等待离房后的在线成员列表超时")
    print("PASS  离房通知（在线成员只剩创建者）")

    # --- 断线重连：resume 恢复身份与房间状态 ---
    await c1.close()                      # 模拟网络掉线（不是主动 leave）
    await asyncio.sleep(0.4)
    c6 = await websockets.connect(URI)
    await send(c6, type="resume", room=room, token=token1)
    m = await recv_type(c6, "joined")
    assert m.get("resumed") is True and m["you"] == token1, m
    assert m["state"]["position"] == 12345, "重连后房间状态应保留"
    assert "小明" in {x["name"] for x in m["members"] if x.get("online")}
    print("PASS  断线重连恢复成员身份与房间状态")

    # --- 错误 token ---
    c7 = await websockets.connect(URI)
    await send(c7, type="resume", room=room, token="deadbeefdeadbeef")
    err = await recv_type(c7, "error")
    assert err["code"] == "BAD_TOKEN" and err.get("fatal") is False, err
    await c7.close()
    print("PASS  无效 token 返回 BAD_TOKEN")

    # --- 房间过期：宽限期过后掉线成员被清、空房解散 ---
    await ttl_expiry_test()
    print("PASS  掉线超时后房间解散（NO_ROOM）")

    await send(c6, type="leave")
    await recv_type(c6, "left")

    print("\n全部通过 ✔")


async def ttl_expiry_test() -> None:
    """在 TTL 服务器上建房 → 掉线 → 等过期 → 再 join 应 NO_ROOM。"""
    c = await websockets.connect(TTL_URI)
    await send(c, type="create", nickname="甲")
    m = await recv_type(c, "created")
    code = m["room"]
    await c.close()
    # 服务器参数：--offline-grace 1 --room-ttl 1 --janitor-interval 0.2
    await asyncio.sleep(2.0)
    c2 = await websockets.connect(TTL_URI)
    await send(c2, type="join", room=code, nickname="乙")
    err = await recv_type(c2, "error")
    assert err["code"] == "NO_ROOM", f"过期房间应解散，实际 {err}"
    await c2.close()


def _spawn(port: int, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(SERVER), "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "WARNING", *extra])


def main() -> int:
    proc = _spawn(PORT)
    ttl_proc = _spawn(TTL_PORT, "--room-ttl", "1", "--offline-grace", "1",
                      "--janitor-interval", "0.2")
    try:
        assert wait_port(PORT), "服务器启动失败"
        assert wait_port(TTL_PORT), "TTL 测试服务器启动失败"
        asyncio.run(run_tests())
        return 0
    finally:
        for p in (proc, ttl_proc):
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
