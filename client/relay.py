#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotSync 中继服务器核心（既可独立部署，也可内嵌进客户端 exe）
==========================================================

- 独立部署：python server/server.py --host 0.0.0.0 --port 8765
  （server.py 只是对本模块的薄壳封装）
- 内嵌使用：EmbeddedServer(port=8765, on_ready=cb).start()
  服务器在独立线程中运行 asyncio 事件循环，stop() 线程安全。

协议：WebSocket + JSON 文本消息，房间制（6 位房间号），
房间保存最近一次共享播放状态，服务器为每条状态加盖毫秒时间戳 ts，
并提供 ping/pong 供客户端估算时钟偏移与延迟。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import websockets

log = logging.getLogger("potsync-relay")

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混淆的 0/O/1/I/L
CODE_LEN = 6
MAX_MESSAGE = 64 * 1024


def now_ms() -> int:
    return int(time.time() * 1000)


def gen_code() -> str:
    return "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def gen_id() -> str:
    return "".join(random.choice(string.hexdigits.lower()) for _ in range(8))


@dataclass
class Client:
    ws: "websockets.ServerConnection"
    id: str = field(default_factory=gen_id)
    name: str = "匿名"
    room: Optional["Room"] = None

    def info(self) -> dict:
        return {"id": self.id, "name": self.name}


@dataclass
class Room:
    code: str
    clients: dict[str, Client] = field(default_factory=dict)
    state: Optional[dict] = None  # {paused, position, media, duration, ts, by, byName, action}


class Hub:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    # ---------- 工具 ----------

    def _new_room(self) -> Room:
        while True:
            code = gen_code()
            if code not in self.rooms:
                break
        room = Room(code=code)
        self.rooms[code] = room
        log.info("房间创建: %s", code)
        return room

    @staticmethod
    async def _send(client: Client, obj: dict) -> bool:
        try:
            await client.ws.send(json.dumps(obj, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def _broadcast(self, room: Room, obj: dict, exclude: Optional[Client] = None) -> None:
        dead = []
        # 发送会让出协程，期间可能有成员并发进出 → 先拍快照再迭代
        for c in list(room.clients.values()):
            if exclude is not None and c.id == exclude.id:
                continue
            if not await self._send(c, obj):
                dead.append(c.id)
        for cid in dead:
            room.clients.pop(cid, None)

    async def _broadcast_members(self, room: Room) -> None:
        await self._broadcast(room, {
            "type": "members",
            "members": [c.info() for c in room.clients.values()],
        })

    async def _notice(self, room: Room, text: str) -> None:
        await self._broadcast(room, {"type": "notice", "text": text, "ts": now_ms()})

    # ---------- 房间进出 ----------

    async def _enter(self, client: Client, room: Room) -> None:
        client.room = room
        room.clients[client.id] = client

    async def _exit(self, client: Client, reason: str = "离开了房间") -> None:
        room = client.room
        if room is None:
            return
        client.room = None
        room.clients.pop(client.id, None)
        if room.clients:
            await self._notice(room, f"{client.name} {reason}")
            await self._broadcast_members(room)
        else:
            self.rooms.pop(room.code, None)
            log.info("房间解散: %s", room.code)

    # ---------- 消息处理 ----------

    async def handle(self, client: Client, msg: dict) -> None:
        mtype = msg.get("type")

        if mtype == "ping":
            await self._send(client, {"type": "pong", "t": msg.get("t", 0), "ts": now_ms()})
            return

        if mtype == "create":
            if client.room is not None:
                await self._exit(client)
            name = str(msg.get("nickname") or "匿名")[:24]
            client.name = name
            room = self._new_room()
            await self._enter(client, room)
            await self._send(client, {
                "type": "created",
                "room": room.code,
                "you": client.id,
                "state": room.state,
                "members": [c.info() for c in room.clients.values()],
            })
            log.info("[%s] %s 创建并加入", room.code, client.name)
            return

        if mtype == "join":
            if client.room is not None:
                await self._exit(client)
            code = str(msg.get("room") or "").strip().upper()
            room = self.rooms.get(code)
            if room is None:
                await self._send(client, {"type": "error", "code": "NO_ROOM",
                                          "message": f"房间 {code} 不存在或已解散"})
                return
            name = str(msg.get("nickname") or "匿名")[:24]
            client.name = name
            await self._enter(client, room)
            await self._send(client, {
                "type": "joined",
                "room": room.code,
                "you": client.id,
                "state": room.state,
                "members": [c.info() for c in room.clients.values()],
            })
            await self._notice(room, f"{client.name} 加入了房间")
            await self._broadcast_members(room)
            log.info("[%s] %s 加入（%d 人）", room.code, client.name, len(room.clients))
            return

        # 以下消息都要求已在房间内
        room = client.room
        if room is None:
            await self._send(client, {"type": "error", "code": "NOT_IN_ROOM",
                                      "message": "尚未加入房间"})
            return

        if mtype == "rename":
            new_name = str(msg.get("nickname") or "").strip()[:24]
            if new_name and new_name != client.name:
                old = client.name
                client.name = new_name
                await self._notice(room, f"{old} 改名为 {new_name}")
                await self._broadcast_members(room)
            return

        if mtype == "state":
            try:
                position = max(0, int(msg.get("position", 0)))
                duration = max(0, int(msg.get("duration", 0)))
                paused = bool(msg.get("paused", True))
            except (TypeError, ValueError):
                return
            action = str(msg.get("action") or "state")[:16]
            media = str(msg.get("media") or "")[:256]
            state = {
                "type": "state",
                "paused": paused,
                "position": position,
                "duration": duration,
                "media": media,
                "action": action,
                "ts": now_ms(),
                "by": client.id,
                "byName": client.name,
            }
            room.state = state
            await self._broadcast(room, state, exclude=client)
            if action in ("play", "pause", "seek", "next", "prev", "open"):
                text = {"play": "播放", "pause": "暂停", "seek": "拖动进度",
                        "next": "切换到下一集", "prev": "切换到上一集",
                        "open": "切换了媒体"}[action]
                extra = f" 至 {position / 1000:.1f}s" if action == "seek" else ""
                extra = f"（{media}）" if action in ("next", "prev", "open") and media else extra
                await self._notice(room, f"{client.name} {text}{extra}")
            return

        if mtype == "leave":
            await self._exit(client)
            await self._send(client, {"type": "left"})
            return

        await self._send(client, {"type": "error", "code": "BAD_TYPE",
                                  "message": f"未知消息类型: {mtype}"})


hub = Hub()


async def handler(ws: "websockets.ServerConnection") -> None:
    client = Client(ws=ws)
    peer = getattr(ws, "remote_address", None)
    log.info("连接建立: %s (%s)", client.id, peer)
    try:
        async for raw in ws:
            if not isinstance(raw, (str, bytes)) or len(raw) > MAX_MESSAGE:
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            try:
                await hub.handle(client, msg)
            except Exception:
                log.exception("处理消息出错: %r", msg)
    except websockets.ConnectionClosed:
        pass
    finally:
        await hub._exit(client)
        log.info("连接断开: %s", client.id)


def process_request(connection, request):
    """对普通 HTTP GET 返回健康信息，便于部署后检查存活；WebSocket 升级请求照常放行。"""
    try:
        if "upgrade" not in request.headers.get("Connection", "").lower():
            return connection.respond(200, "PotSync relay server\n")
    except Exception:
        pass
    return None


async def serve(host: str, port: int, stop: Optional[asyncio.Event] = None) -> None:
    """启动服务器；传入 stop 事件则可被外部停止，否则永久运行。"""
    async with websockets.serve(handler, host, port, process_request=process_request,
                                max_size=MAX_MESSAGE, ping_interval=30, ping_timeout=30):
        log.info("PotSync 中继服务器已启动: ws://%s:%d", host, port)
        if stop is None:
            await asyncio.Future()
        else:
            await stop.wait()
    log.info("PotSync 中继服务器已停止")


class EmbeddedServer:
    """在客户端进程内运行的中继服务器（独立线程 + 独立 asyncio 事件循环）。

    on_ready(ok: bool, error: str) 会在服务器线程中被调用——
    GUI 里请通过 Qt 信号转投到主线程再操作界面。
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765,
                 on_ready: Optional[Callable[[bool, str], None]] = None) -> None:
        self.host = host
        self.port = port
        self.on_ready = on_ready
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._thread_main,
                                        name="potsync-relay", daemon=True)
        self._thread.start()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # 端口占用等
            log.warning("内嵌服务器异常退出: %s", exc)
            if self.on_ready:
                self.on_ready(False, str(exc))

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            async with websockets.serve(
                    handler, self.host, self.port,
                    process_request=process_request, max_size=MAX_MESSAGE,
                    ping_interval=30, ping_timeout=30):
                self.running = True
                log.info("内嵌中继服务器已启动: ws://%s:%d", self.host, self.port)
                if self.on_ready:
                    self.on_ready(True, "")
                await self._stop.wait()
        except OSError as exc:
            log.warning("内嵌服务器启动失败: %s", exc)
            if self.on_ready:
                self.on_ready(False, str(exc))
        finally:
            self.running = False

    def stop(self) -> None:
        loop, ev = self._loop, self._stop
        if loop is not None and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def wait_stopped(self, timeout: float = 3.0) -> None:
        if self._thread:
            self._thread.join(timeout)


def main() -> None:
    parser = argparse.ArgumentParser(description="PotSync 中继服务器")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        log.info("服务器已停止")


if __name__ == "__main__":
    main()
