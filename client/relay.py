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

2.0 起「成员」与「连接」解耦：
- 每个成员有稳定 id（由服务器首次建房/加入时下发，即 ``you``），连接断了成员仍在；
- 掉线成员进入 2 分钟宽限期，其他人看到的是「连接中断（正在自动重连）」而不是「离开了房间」；
- 客户端用 ``resume`` + 成员 id 原地恢复，不必重新建房、房间状态（进度/媒体）不丢；
- 房间内没有在线成员时保留 15 分钟，方便主人重启客户端后恢复；
- janitor 每 15 秒清理超时离线的成员与过期空房。
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
from typing import Any, Callable, Optional

import websockets

from protocol import (
    ACTION_TEXT, CODE_ALPHABET, CODE_LEN, ERR_BAD_TOKEN, ERR_BAD_TYPE,
    ERR_NO_ROOM, ERR_NOT_IN_ROOM, JANITOR_INTERVAL_S, MAX_MEDIA_NAME,
    MAX_MESSAGE, MAX_NICKNAME, MSG_CREATE, MSG_CREATED, MSG_JOIN, MSG_JOINED,
    MSG_LEFT, MSG_LEAVE, MSG_MEMBERS, MSG_NOTICE, MSG_PING, MSG_PONG,
    MSG_RESUME, MSG_RENAME, MSG_STATE, OFFLINE_GRACE_S, PROTOCOL_VERSION,
    ROOM_TTL_S,
)

TYPE_FIELD = "type"

log = logging.getLogger("potsync-relay")


def now_ms() -> int:
    return int(time.time() * 1000)


def gen_code() -> str:
    return "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def gen_id() -> str:
    return "".join(random.choice(string.hexdigits.lower()) for _ in range(8))


@dataclass
class Client:
    """一条 WebSocket 连接。id 在 resume 时会被替换成原成员的 id。"""
    ws: Any
    id: str = field(default_factory=gen_id)
    name: str = "匿名"
    room: Optional["Room"] = None
    left: bool = False          # 主动 leave 过：连接关闭时不要再按掉线保留

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "online": True}


@dataclass
class Member:
    """房间成员（跨连接存续）。ws 为 None 表示当前离线、还在重连宽限期内。"""
    id: str
    name: str = "匿名"
    ws: Any = None
    joined_ms: int = 0
    offline_since: float = 0.0

    def online(self) -> bool:
        return self.ws is not None

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "online": self.online()}


@dataclass
class Room:
    code: str
    members: dict[str, Member] = field(default_factory=dict)  # 按加入顺序
    state: Optional[dict] = None  # {paused, position, media, duration, ts, by, byName, action}
    empty_since: float = 0.0      # 无人在线时开始计时，用于 TTL 回收


class Hub:
    """房间与成员管理。参数可注入，便于测试用很短的 TTL。"""

    def __init__(self, room_ttl_s: float = ROOM_TTL_S,
                 offline_grace_s: float = OFFLINE_GRACE_S,
                 janitor_interval_s: float = JANITOR_INTERVAL_S) -> None:
        self.rooms: dict[str, Room] = {}
        self.room_ttl_s = float(room_ttl_s)
        self.offline_grace_s = float(offline_grace_s)
        self.janitor_interval_s = float(janitor_interval_s)

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
    async def _send_ws(ws: Any, obj: dict) -> bool:
        try:
            await ws.send(json.dumps(obj, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def _send(self, client: Client, obj: dict) -> bool:
        return await self._send_ws(client.ws, obj)

    async def _broadcast(self, room: Room, obj: dict,
                         exclude_id: Optional[str] = None) -> None:
        """发给房间内所有在线成员。发送失败先把成员标记离线，由 janitor 统一清理/通知。"""
        text = json.dumps(obj, ensure_ascii=False)
        for m in list(room.members.values()):
            ws = m.ws
            if ws is None or (exclude_id is not None and m.id == exclude_id):
                continue
            try:
                await ws.send(text)
            except Exception:
                m.ws = None
                m.offline_since = time.time()
                self._mark_empty_if_needed(room)

    @staticmethod
    def _members_payload(room: Room) -> list:
        members = sorted(room.members.values(), key=lambda m: m.joined_ms)
        return [m.info() for m in members]

    async def _broadcast_members(self, room: Room) -> None:
        await self._broadcast(room, {"type": MSG_MEMBERS,
                                     "members": self._members_payload(room)})

    async def _notice(self, room: Room, text: str) -> None:
        await self._broadcast(room, {"type": MSG_NOTICE, "text": text, "ts": now_ms()})

    def _any_online(self, room: Room) -> bool:
        return any(m.ws is not None for m in room.members.values())

    def _mark_empty_if_needed(self, room: Room) -> None:
        if not self._any_online(room) and not room.empty_since:
            room.empty_since = time.time()

    # ---------- 房间进出 ----------

    async def _enter(self, client: Client, room: Room) -> Member:
        client.room = room
        client.left = False
        member = room.members.get(client.id)
        if member is None:
            member = Member(id=client.id, name=client.name, joined_ms=now_ms())
            room.members[client.id] = member
        member.name = client.name
        member.ws = client.ws
        member.offline_since = 0.0
        room.empty_since = 0.0
        return member

    async def _exit(self, client: Client, reason: str = "离开了房间",
                    dropped: bool = False) -> None:
        room = client.room
        if room is None:
            return
        client.room = None
        member = room.members.get(client.id)
        if member is None:
            return

        keep = dropped and not client.left and self.offline_grace_s > 0
        if keep:
            # 网络断开：保留成员位，等客户端 resume；其他人看到的是「重连中」
            member.ws = None
            member.offline_since = time.time()
            await self._notice(room, f"{member.name} 连接中断（正在自动重连）")
        else:
            room.members.pop(client.id, None)
            await self._notice(room, f"{member.name} {reason}")

        if not room.members:
            self.rooms.pop(room.code, None)
            log.info("房间解散: %s", room.code)
            return
        self._mark_empty_if_needed(room)
        await self._broadcast_members(room)

    # ---------- 消息处理 ----------

    async def handle(self, client: Client, msg: dict) -> None:
        mtype = msg.get(TYPE_FIELD)

        if mtype == MSG_PING:
            await self._send(client, {"type": MSG_PONG, "t": msg.get("t", 0),
                                      "ts": now_ms()})
            return

        if mtype == MSG_CREATE:
            if client.room is not None:
                client.left = True
                await self._exit(client)
                client.left = False
            client.name = str(msg.get("nickname") or "匿名")[:MAX_NICKNAME]
            room = self._new_room()
            await self._enter(client, room)
            await self._send(client, {
                "type": MSG_CREATED,
                "protocol": PROTOCOL_VERSION,
                "room": room.code,
                "you": client.id,
                "state": room.state,
                "members": self._members_payload(room),
            })
            log.info("[%s] %s 创建并加入", room.code, client.name)
            return

        if mtype == MSG_JOIN:
            if client.room is not None:
                client.left = True
                await self._exit(client)
                client.left = False
            code = str(msg.get("room") or "").strip().upper()
            room = self.rooms.get(code)
            if room is None:
                await self._send(client, {"type": "error", "code": ERR_NO_ROOM,
                                          "message": f"房间 {code} 不存在或已解散"})
                return
            client.name = str(msg.get("nickname") or "匿名")[:MAX_NICKNAME]
            await self._enter(client, room)
            await self._send(client, {
                "type": MSG_JOINED,
                "protocol": PROTOCOL_VERSION,
                "room": room.code,
                "you": client.id,
                "state": room.state,
                "members": self._members_payload(room),
            })
            await self._notice(room, f"{client.name} 加入了房间")
            await self._broadcast_members(room)
            log.info("[%s] %s 加入（在线 %d 人）", room.code, client.name,
                     sum(1 for m in room.members.values() if m.online()))
            return

        if mtype == MSG_RESUME:
            # 断线恢复：房间号 + 成员 id（首次 created/joined 的 you 字段）
            code = str(msg.get("room") or "").strip().upper()
            token = str(msg.get("token") or "").strip()
            room = self.rooms.get(code)
            if room is None:
                await self._send(client, {"type": "error", "code": ERR_NO_ROOM,
                                          "fatal": True,
                                          "message": f"房间 {code} 已解散"})
                return
            member = room.members.get(token)
            if member is None:
                await self._send(client, {"type": "error", "code": ERR_BAD_TOKEN,
                                          "fatal": False,
                                          "message": "原成员身份已失效，需要重新加入"})
                return
            if member.ws is not None and member.ws is not client.ws:
                # 同一成员重复连接（例如旧连接还没被对端关闭）：以新连接为准
                old = member.ws
                try:
                    await old.close(4001, "replaced by new connection")
                except Exception:
                    pass
            client.id = token
            client.name = member.name
            await self._enter(client, room)
            await self._send(client, {
                "type": MSG_JOINED,
                "protocol": PROTOCOL_VERSION,
                "resumed": True,
                "room": room.code,
                "you": token,
                "state": room.state,
                "members": self._members_payload(room),
            })
            await self._notice(room, f"{client.name} 已重新连接")
            await self._broadcast_members(room)
            log.info("[%s] %s 重连成功", room.code, client.name)
            return

        # 以下消息都要求已在房间内
        room = client.room
        if room is None:
            await self._send(client, {"type": "error", "code": ERR_NOT_IN_ROOM,
                                      "message": "尚未加入房间"})
            return

        if mtype == MSG_RENAME:
            new_name = str(msg.get("nickname") or "").strip()[:MAX_NICKNAME]
            if new_name and new_name != client.name:
                old = client.name
                client.name = new_name
                member = room.members.get(client.id)
                if member is not None:
                    member.name = new_name
                await self._notice(room, f"{old} 改名为 {new_name}")
                await self._broadcast_members(room)
            return

        if mtype == MSG_STATE:
            try:
                position = max(0, int(msg.get("position", 0)))
                duration = max(0, int(msg.get("duration", 0)))
                paused = bool(msg.get("paused", True))
            except (TypeError, ValueError):
                return
            action = str(msg.get("action") or "state")[:16]
            media = str(msg.get("media") or "")[:MAX_MEDIA_NAME]
            state = {
                "type": MSG_STATE,
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
            await self._broadcast(room, state, exclude_id=client.id)
            if action in ACTION_TEXT:
                extra = f" 至 {position / 1000:.1f}s" if action == "seek" else ""
                extra = (f"（{media}）" if action in ("next", "prev", "open") and media
                         else extra)
                await self._notice(room, f"{client.name} {ACTION_TEXT[action]}{extra}")
            return

        if mtype == MSG_LEAVE:
            client.left = True
            await self._exit(client)
            await self._send(client, {"type": MSG_LEFT})
            return

        await self._send(client, {"type": "error", "code": ERR_BAD_TYPE,
                                  "message": f"未知消息类型: {mtype}"})

    # ---------- 过期清理 ----------

    async def start_janitor(self) -> asyncio.Task:
        return asyncio.create_task(self._janitor_loop(), name="potsync-janitor")

    async def _janitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.janitor_interval_s)
            try:
                await self.prune()
            except Exception:
                log.exception("清理过期房间/成员出错")

    async def prune(self) -> None:
        now = time.time()
        for code, room in list(self.rooms.items()):
            changed = False
            for mid, m in list(room.members.items()):
                if m.ws is None and m.offline_since \
                        and now - m.offline_since > self.offline_grace_s:
                    room.members.pop(mid, None)
                    changed = True
                    log.info("[%s] %s 离线超时被移出", code, m.name)
                    await self._notice(room, f"{m.name} 离线超时，已移出房间")
            if not room.members:
                self.rooms.pop(code, None)
                log.info("房间解散（无成员）: %s", code)
                continue
            self._mark_empty_if_needed(room)
            if not self._any_online(room) and room.empty_since \
                    and now - room.empty_since > self.room_ttl_s:
                self.rooms.pop(code, None)
                log.info("房间解散（空置超时）: %s", code)
                continue
            if changed:
                await self._broadcast_members(room)


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
        # 连接意外结束时按「掉线」处理：保留成员位等待 resume
        await hub._exit(client, dropped=True)
        log.info("连接断开: %s", client.id)


def process_request(connection, request):
    """对普通 HTTP GET 返回健康信息，便于部署后检查存活；WebSocket 升级请求照常放行。"""
    try:
        if "upgrade" not in request.headers.get("Connection", "").lower():
            return connection.respond(200, "PotSync relay server\n")
    except Exception:
        pass
    return None


async def serve(host: str, port: int, stop: Optional[asyncio.Event] = None,
                hub_instance: Optional[Hub] = None) -> None:
    """启动服务器；传入 stop 事件则可被外部停止，否则永久运行。"""
    active_hub = hub_instance or hub
    janitor = await active_hub.start_janitor()
    try:
        async with websockets.serve(handler, host, port, process_request=process_request,
                                    max_size=MAX_MESSAGE, ping_interval=30, ping_timeout=30):
            log.info("PotSync 中继服务器已启动: ws://%s:%d", host, port)
            if stop is None:
                await asyncio.Future()
            else:
                await stop.wait()
    finally:
        janitor.cancel()
    log.info("PotSync 中继服务器已停止")


class EmbeddedServer:
    """在客户端进程内运行的中继服务器（独立线程 + 独立 asyncio 事件循环）。

    on_ready(ok: bool, error: str) 会在服务器线程中被调用——
    GUI 里请通过 Qt 信号转投到主线程再操作界面。

    hub_instance 可注入自定义 Hub（测试里用很小的 TTL 验证回收逻辑）。
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765,
                 on_ready: Optional[Callable[[bool, str], None]] = None,
                 hub_instance: Optional[Hub] = None) -> None:
        self.host = host
        self.port = port
        self.on_ready = on_ready
        self.hub = hub_instance
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
        active_hub = self.hub or hub
        janitor = await active_hub.start_janitor()
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
            janitor.cancel()
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
    parser.add_argument("--room-ttl", type=float, default=ROOM_TTL_S,
                        help="无人在线时房间保留秒数（默认 900，0=立即解散）")
    parser.add_argument("--offline-grace", type=float, default=OFFLINE_GRACE_S,
                        help="掉线成员保留秒数（默认 120，0=掉线即移除）")
    parser.add_argument("--janitor-interval", type=float, default=JANITOR_INTERVAL_S,
                        help="过期清理间隔秒数（默认 15）")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    global hub
    hub = Hub(room_ttl_s=args.room_ttl, offline_grace_s=args.offline_grace,
              janitor_interval_s=args.janitor_interval)
    try:
        asyncio.run(serve(args.host, args.port, hub_instance=hub))
    except KeyboardInterrupt:
        log.info("服务器已停止")


if __name__ == "__main__":
    main()
