#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内网穿透真实联调脚本
====================

完整验证"点击即用"链路：
1. 进程内启动内嵌中继服务器（127.0.0.1:18768）
2. TunnelManager 自动下载隧道组件并建立公网隧道
3. 通过公网地址做 HTTP 健康检查 + WebSocket 建房/加入/广播全链路

运行：python tools/live_tunnel_check.py
（会真实下载 cloudflared 约 30MB，并经由公共隧道访问公网，耗时约 1 分钟）
"""

import asyncio
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from relay import EmbeddedServer            # noqa: E402
from net_client import build_uri            # noqa: E402
from tunnel import TunnelManager            # noqa: E402

LOCAL_PORT = 18768


async def ws_roundtrip(base_uri: str) -> None:
    import websockets
    c1 = await websockets.connect(base_uri)
    await c1.send(json.dumps({"type": "create", "nickname": "穿透主机"}))
    m = json.loads(await asyncio.wait_for(c1.recv(), 10))
    assert m["type"] == "created", m
    room = m["room"]

    c2 = await websockets.connect(base_uri)
    await c2.send(json.dumps({"type": "join", "room": room, "nickname": "异地好友"}))
    m = json.loads(await asyncio.wait_for(c2.recv(), 10))
    assert m["type"] == "joined", m

    await c1.send(json.dumps({"type": "state", "paused": True, "position": 42424,
                              "media": "movie.mkv", "duration": 3600000,
                              "action": "pause"}))
    end = time.time() + 10
    while True:
        m = json.loads(await asyncio.wait_for(c2.recv(), max(0.1, end - time.time())))
        if m["type"] == "state":
            break
    assert m["position"] == 42424 and m["byName"] == "穿透主机"
    await c1.close()
    await c2.close()
    print(f"PASS  公网 WebSocket 全链路（建房 {room} → 加入 → 状态广播）")


def main() -> int:
    ready = threading.Event()
    holder = {}
    mgr = TunnelManager(
        on_ready=lambda ep: (holder.update(ep=ep), ready.set()),
        on_error=lambda e: (holder.update(err=e), ready.set()),
        on_status=lambda s: print(f"[隧道] {s}", flush=True))

    server = EmbeddedServer("127.0.0.1", LOCAL_PORT)
    server.start()
    time.sleep(1)
    assert server.running, "内嵌服务器未启动"
    print("PASS  内嵌中继服务器启动（127.0.0.1:%d）" % LOCAL_PORT)

    print("建立公网隧道（首次需下载组件，请稍候）…")
    mgr.start(LOCAL_PORT)
    if not ready.wait(90):
        print("FAIL  隧道建立超时")
        return 1
    if "err" in holder:
        print(f"FAIL  隧道失败: {holder['err']}")
        return 1

    ep = holder["ep"]
    print(f"PASS  公网隧道已建立: {ep.display}  ({ep.provider})")

    # HTTP 健康检查（cloudflared 走 https；bore 是纯 TCP 无 HTTP）
    if ep.provider == "cloudflared":
        body = urllib.request.urlopen(f"https://{ep.host}/", timeout=15) \
            .read().decode().strip()
        assert "PotSync" in body, body
        print(f"PASS  公网 HTTP 健康检查: {body!r}")

    asyncio.run(ws_roundtrip(build_uri(ep.host, ep.port)))

    mgr.stop()
    server.stop()
    server.wait_stopped(3)
    print("\n穿透联调全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
