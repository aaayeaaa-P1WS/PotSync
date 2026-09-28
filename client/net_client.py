#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotSync 网络客户端
==================

WebSocket 客户端，运行在独立 asyncio 线程中，通过 Qt 信号与 GUI 线程通信
（pyqtSignal 跨线程投递是线程安全的）。

同时维护与服务器时钟的偏移量（ping/pong 估算），供同步引擎推算"此刻应有的进度"。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import threading
import time
from typing import Optional

import websockets
from websockets.exceptions import InvalidProxy
from PyQt5.QtCore import QObject, pyqtSignal

log = logging.getLogger("potsync.net")


def _is_private_host(host: str) -> bool:
    """本机/内网地址（连接这类地址时跳过系统代理，避免被代理软件拦截）。"""
    h = (host or "").strip().lower()
    if h in ("localhost",) or h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def local_ms() -> int:
    return int(time.time() * 1000)


def build_uri(host: str, port: int) -> str:
    """443 端口视为 HTTPS/WSS 隧道入口（如 *.trycloudflare.com），其余用明文 ws。"""
    scheme = "wss" if int(port) == 443 else "ws"
    return f"{scheme}://{host}:{port}"


class NetClient(QObject):
    # ---- 发往 GUI 的信号 ----
    sigConnected = pyqtSignal()
    sigDisconnected = pyqtSignal(str)          # 原因
    sigError = pyqtSignal(str)                 # 可读错误信息
    sigRoomCreated = pyqtSignal(str, object, list)   # room, state|None, members
    sigRoomJoined = pyqtSignal(str, object, list)    # room, state|None, members
    sigLeftRoom = pyqtSignal()
    sigState = pyqtSignal(dict)                # 他人广播的播放状态
    sigMembers = pyqtSignal(list)
    sigNotice = pyqtSignal(str)
    sigRtt = pyqtSignal(int)                   # 毫秒

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[websockets.ClientConnection] = None
        self._ping_task: Optional[asyncio.Task] = None
        self.offset_ms = 0          # server_time ≈ local_time + offset
        self.rtt_ms = -1
        self._running = False

    # ---------- 生命周期 ----------

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def connect_to(self, host: str, port: int) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._thread_main, args=(host, port), daemon=True,
            name="potsync-net")
        self._thread.start()

    def shutdown(self) -> None:
        self._running = False
        self._call(self._close())

    def _thread_main(self, host: str, port: int) -> None:
        try:
            asyncio.run(self._main(host, port))
        except Exception as exc:
            log.exception("网络线程异常")
            self.sigError.emit(f"网络错误: {exc}")
        finally:
            self._running = False

    async def _main(self, host: str, port: int) -> None:
        self._loop = asyncio.get_running_loop()
        uri = build_uri(host, port)
        try:
            if _is_private_host(host):
                await self._session(uri, proxy=None)
            else:
                try:
                    await self._session(uri)
                except InvalidProxy as exc:
                    # 系统代理为 websockets 不支持的形式（如 Windows 的
                    # socks:// 注册表代理）时降级为直连，保证可用性
                    log.warning("系统代理不可用（%s），改为直连", exc)
                    self.sigNotice.emit("系统代理不受支持，已切换为直连")
                    await self._session(uri, proxy=None)
        except (OSError, websockets.WebSocketException) as exc:
            self.sigError.emit(f"无法连接服务器 {uri}：{exc}")
        finally:
            self._ws = None
            self._loop = None
            self.sigDisconnected.emit("连接已断开")

    async def _session(self, uri: str, proxy=True) -> None:
        kwargs = dict(max_size=64 * 1024, ping_interval=None)
        if proxy is None:
            kwargs["proxy"] = None
        async with websockets.connect(uri, **kwargs) as ws:
            self._ws = ws
            self.sigConnected.emit()
            self._ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    self._on_raw(raw)
            finally:
                if self._ping_task:
                    self._ping_task.cancel()

    async def _close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ---------- 时钟 ----------

    async def _ping_loop(self, ws) -> None:
        while True:
            try:
                await ws.send(json.dumps({"type": "ping", "t": local_ms()}))
            except Exception:
                return
            await asyncio.sleep(3)

    def server_now_ms(self) -> int:
        return local_ms() + self.offset_ms

    # ---------- 消息收发 ----------

    def _call(self, coro) -> None:
        loop = self._loop
        if loop is not None and self._ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                pass

    async def _send(self, obj: dict) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(json.dumps(obj, ensure_ascii=False))
            except Exception as exc:
                log.debug("发送失败: %s", exc)

    # ---- 公共 API（GUI 线程调用，线程安全） ----

    def create_room(self, nickname: str) -> None:
        self._call(self._send({"type": "create", "nickname": nickname}))

    def join_room(self, room: str, nickname: str) -> None:
        self._call(self._send({"type": "join", "room": room, "nickname": nickname}))

    def rename(self, nickname: str) -> None:
        self._call(self._send({"type": "rename", "nickname": nickname}))

    def send_state(self, paused: bool, position: int, media: str,
                   duration: int, action: str = "state") -> None:
        self._call(self._send({
            "type": "state", "paused": paused, "position": int(position),
            "media": media, "duration": int(duration), "action": action,
        }))

    def leave_room(self) -> None:
        self._call(self._send({"type": "leave"}))

    # ---------- 接收分发 ----------

    def _on_raw(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")

        if mtype == "pong":
            now = local_ms()
            t = msg.get("t", now)
            ts = msg.get("ts", now)
            try:
                rtt = max(0, now - int(t))
                self.rtt_ms = rtt
                # 服务器时间 ≈ ts + 单程延迟
                self.offset_ms = int(ts) + rtt // 2 - now
                self.sigRtt.emit(rtt)
            except (TypeError, ValueError):
                pass
        elif mtype == "created":
            self.sigRoomCreated.emit(msg.get("room", ""), msg.get("state"),
                                     msg.get("members", []))
        elif mtype == "joined":
            self.sigRoomJoined.emit(msg.get("room", ""), msg.get("state"),
                                    msg.get("members", []))
        elif mtype == "left":
            self.sigLeftRoom.emit()
        elif mtype == "state":
            self.sigState.emit(msg)
        elif mtype == "members":
            self.sigMembers.emit(msg.get("members", []))
        elif mtype == "notice":
            self.sigNotice.emit(str(msg.get("text", "")))
        elif mtype == "error":
            self.sigError.emit(str(msg.get("message", "未知错误")))
