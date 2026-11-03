#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotSync 网络客户端
==================

WebSocket 客户端，运行在独立 asyncio 线程中，通过 Qt 信号与 GUI 线程通信
（pyqtSignal 跨线程投递是线程安全的）。

同时维护与服务器时钟的偏移量（ping/pong 估算），供同步引擎推算"此刻应有的进度"。

2.0 的自动重连
--------------
- 连接线程不再"断一次就退出"，而是循环重连：0.5s 起步、指数退避到 30s 封顶；
- 掉线时发 ``sigConnectionLost``（GUI 保持房间页、显示"正在重连"），
  只有用户主动断开/程序退出才发 ``sigDisconnected``；
- 重连成功后自动发 ``resume``（房间号 + 成员 id），服务器原地恢复成员身份并回传房间状态，
  GUI 收到 ``sigRoomResumed``；房间真的没了才回 ``sigResumeFailed``；
- 应用层心跳每 3s 一次，12s 收不到 pong 判定连接已死并主动断开重连
  （Windows 上 TCP 半开连接可能十几分钟才报错，心跳是"网络波动后及时恢复"的关键）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import threading
import time
import urllib.request
from typing import Optional

import websockets
from websockets.exceptions import InvalidProxy
from PyQt5.QtCore import QObject, pyqtSignal

from protocol import (
    ERR_BAD_TOKEN, ERR_NO_ROOM, HEARTBEAT_S, HEARTBEAT_TIMEOUT_S, MAX_MESSAGE,
    MSG_CREATE, MSG_JOIN, MSG_LEFT, MSG_LEAVE, MSG_MEMBERS, MSG_NOTICE,
    MSG_PONG, MSG_RESUME, MSG_RENAME, MSG_STATE, RECONNECT_DELAYS,
)

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


def preferred_http_proxy(uri: str) -> str:
    """优先选 websockets 支持的 HTTP(S) 代理，不让 SOCKS 项抢先导致误走直连。"""
    proxies = urllib.request.getproxies()
    scheme = "https" if uri.startswith("wss://") else "http"
    candidates = (scheme, "http" if scheme == "https" else "https", "all")
    for key in candidates:
        proxy = (proxies.get(key) or "").strip()
        if not proxy:
            continue
        if "://" not in proxy:
            proxy = "http://" + proxy
        if proxy.lower().startswith(("http://", "https://")):
            return proxy
    return ""


class NetClient(QObject):
    # ---- 发往 GUI 的信号 ----
    sigConnecting = pyqtSignal(int, int)       # (第几次尝试, 等待毫秒)
    sigConnected = pyqtSignal()
    sigConnectionLost = pyqtSignal(str)        # 意外掉线（随后会自动重连）
    sigDisconnected = pyqtSignal(str)          # 连接线程结束（用户主动关闭/无法恢复）
    sigError = pyqtSignal(str)                 # 可读错误信息
    sigRoomCreated = pyqtSignal(str, object, list)   # room, state|None, members
    sigRoomJoined = pyqtSignal(str, object, list)    # room, state|None, members
    sigRoomResumed = pyqtSignal(str, object, list)   # room, state|None, members（重连恢复）
    sigResumeFailed = pyqtSignal(str)                # 房间已解散/身份失效
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
        self._host = ""
        self._port = 0
        self._nickname = ""
        self._room = ""
        self._token = ""            # 成员身份，服务器 created/joined 里的 you
        self._session_count = 0
        self._last_pong = 0.0
        self._proxy_disabled = False
        self._local_close = False   # 本次断开是本地主动发起（切换服务器/退出）
        self._switching = False

    # ---------- 生命周期 ----------

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def room(self) -> str:
        return self._room

    @property
    def token(self) -> str:
        return self._token

    def connect_to(self, host: str, port: int, nickname: str = "") -> None:
        """连接（或切换）服务器。已运行时切换地址会打断当前连接、立即按新地址重连。"""
        host, port = (host or "").strip(), int(port)
        if nickname:
            self._nickname = nickname
        same_target = (host, port) == (self._host, self._port)
        self._host, self._port = host, port

        if self._running:
            if not same_target:
                self._proxy_disabled = False
                self._switching = True
                self._call(self._close())
            return
        if not host:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="potsync-net")
        self._thread.start()

    def shutdown(self) -> None:
        self._running = False
        self._call(self._close())
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.5)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            log.exception("网络线程异常")
            self.sigError.emit(f"网络错误: {exc}")
        finally:
            self._running = False
            self._ws = None
            self._loop = None
            self.sigDisconnected.emit("连接已断开")

    async def _main(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        self._loop = asyncio.get_running_loop()
        if host is not None:            # 兼容直接调用（测试里注入固定地址）
            self._host, self._port = host, int(port or 443)
        attempt = 0
        while self._running:
            host, port = self._host, self._port
            if not host:
                break
            uri = build_uri(host, port)
            proxy = self._choose_proxy(host, uri)
            attempt += 1
            if attempt > 1:
                delay = RECONNECT_DELAYS[min(attempt - 2, len(RECONNECT_DELAYS) - 1)]
                self.sigConnecting.emit(attempt, int(delay * 1000))
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break
                if not self._running:
                    break
            else:
                self.sigConnecting.emit(1, 0)

            reason = "连接已断开"
            sessions_before = self._session_count
            try:
                await self._session(uri, proxy)
            except asyncio.CancelledError:
                break
            except (OSError, websockets.WebSocketException, InvalidProxy) as exc:
                reason = self._describe(exc)
                log.warning("连接 %s 失败: %s", uri, exc)
                # 首次连接若走系统代理失败，自动改直连再试（只回退一次，不影响重连计数）
                if proxy and not self._proxy_disabled and self._session_count == 0:
                    self._proxy_disabled = True
                    attempt -= 1
                    self.sigNotice.emit("HTTP 代理连接失败，正在尝试直连")
                    continue
                self.sigError.emit(f"无法连接服务器 {uri}：{reason}")
            except Exception as exc:  # 兜底，避免一个意外异常把重连循环打死
                reason = self._describe(exc)
                log.exception("连接会话异常")
                self.sigError.emit(f"网络错误：{reason}")

            if not self._running:
                break
            # 上一次连接确实建立过：退避从 0.5s 重新开始（否则长会话掉一次就要等 30s）
            if self._session_count > sessions_before:
                attempt = 1
            if self._local_close:
                self._local_close = False
                if self._switching:
                    self._switching = False
                    attempt = 0
                    continue
                break
            self.sigConnectionLost.emit(reason)

    def _choose_proxy(self, host: str, uri: str) -> str:
        if self._proxy_disabled or _is_private_host(host):
            return ""
        if not _is_private_host(host) and urllib.request.getproxies().get("socks"):
            # 只有 SOCKS 时提醒一次：websockets 不支持，走直连
            pass
        return preferred_http_proxy(uri)

    @staticmethod
    def _describe(exc: Exception) -> str:
        text = str(exc).strip()
        return text or exc.__class__.__name__

    async def _session(self, uri: str, proxy: Optional[str] = None) -> None:
        kwargs = dict(max_size=MAX_MESSAGE, ping_interval=None, proxy=proxy or None)
        async with websockets.connect(uri, **kwargs) as ws:
            self._ws = ws
            self._session_count += 1
            self._last_pong = time.monotonic()
            self._local_close = False
            self.sigConnected.emit()
            self._ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                # 非首次连接：自动原地恢复房间身份（服务器保留房间状态）
                if self._session_count > 1 and self._room and self._token:
                    log.info("尝试恢复房间 %s（成员 %s）", self._room, self._token)
                    await self._send({"type": MSG_RESUME, "room": self._room,
                                      "token": self._token,
                                      "nickname": self._nickname})
                async for raw in ws:
                    self._on_raw(raw)
            finally:
                if self._ping_task is not None:
                    self._ping_task.cancel()
                    self._ping_task = None

    async def _close(self) -> None:
        ws = self._ws
        self._local_close = True
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # ---------- 时钟 / 心跳 ----------

    async def _ping_loop(self, ws) -> None:
        while True:
            try:
                await ws.send(json.dumps({"type": "ping", "t": local_ms()}))
            except Exception:
                return
            if time.monotonic() - self._last_pong > HEARTBEAT_TIMEOUT_S:
                log.warning("心跳超时（%.0fs 未收到 pong），主动断开以便重连",
                            HEARTBEAT_TIMEOUT_S)
                self.sigNotice.emit("连接心跳超时，正在自动重连")
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            await asyncio.sleep(HEARTBEAT_S)

    def server_now_ms(self) -> int:
        return local_ms() + self.offset_ms

    # ---------- 消息收发 ----------

    def _call(self, coro) -> None:
        loop = self._loop
        if loop is None or self._ws is None:
            coro.close()            # 连接已结束时主动关闭协程，避免退出告警
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()

    async def _send(self, obj: dict) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(json.dumps(obj, ensure_ascii=False))
            except Exception as exc:
                log.debug("发送失败: %s", exc)

    # ---- 公共 API（GUI 线程调用，线程安全） ----

    def create_room(self, nickname: str) -> None:
        self._nickname = nickname
        self._call(self._send({"type": MSG_CREATE, "nickname": nickname}))

    def join_room(self, room: str, nickname: str) -> None:
        self._nickname = nickname
        self._call(self._send({"type": MSG_JOIN, "room": room, "nickname": nickname}))

    def rename(self, nickname: str) -> None:
        self._nickname = nickname
        self._call(self._send({"type": MSG_RENAME, "nickname": nickname}))

    def send_state(self, paused: bool, position: int, media: str,
                   duration: int, action: str = "state") -> None:
        self._call(self._send({
            "type": MSG_STATE, "paused": paused, "position": int(position),
            "media": media, "duration": int(duration), "action": action,
        }))

    def leave_room(self) -> None:
        self._room = ""
        self._token = ""
        self._call(self._send({"type": MSG_LEAVE}))

    # ---------- 接收分发 ----------

    def _on_raw(self, raw) -> None:
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                return
            self._dispatch(msg)
        except Exception:
            log.exception("处理服务器消息出错: %r", raw)

    def _dispatch(self, msg: dict) -> None:
        mtype = msg.get("type")

        if mtype == MSG_PONG:
            now = local_ms()
            self._last_pong = time.monotonic()
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
            self._room = str(msg.get("room", ""))
            self._token = str(msg.get("you", ""))
            self.sigRoomCreated.emit(self._room, msg.get("state"),
                                     msg.get("members", []))
        elif mtype == "joined":
            room = str(msg.get("room", ""))
            self._room = room
            self._token = str(msg.get("you", self._token))
            if msg.get("resumed"):
                log.info("房间 %s 恢复成功", room)
                self.sigRoomResumed.emit(room, msg.get("state"), msg.get("members", []))
            else:
                self.sigRoomJoined.emit(room, msg.get("state"), msg.get("members", []))
        elif mtype == MSG_LEFT:
            self.sigLeftRoom.emit()
        elif mtype == MSG_STATE:
            self.sigState.emit(msg)
        elif mtype == MSG_MEMBERS:
            self.sigMembers.emit(msg.get("members", []))
        elif mtype == MSG_NOTICE:
            self.sigNotice.emit(str(msg.get("text", "")))
        elif mtype == "error":
            code = str(msg.get("code", ""))
            text = str(msg.get("message", "未知错误"))
            if code == ERR_BAD_TOKEN and self._room:
                # 服务器还在，只是掉线超过宽限期、旧身份被清理：直接重新加入同一房间
                log.info("恢复身份失败，改为重新加入房间 %s", self._room)
                room = self._room
                self._token = ""
                self.sigNotice.emit("重连后身份已过期，正在重新加入房间…")
                asyncio.ensure_future(self._send(
                    {"type": MSG_JOIN, "room": room, "nickname": self._nickname}))
                return
            if code == ERR_NO_ROOM:
                had_room = bool(self._room)
                self._room = ""
                self._token = ""
                if had_room:
                    self.sigResumeFailed.emit(text)
                    return
            self.sigError.emit(text)
