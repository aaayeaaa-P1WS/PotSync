#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotSync 协议常量（客户端与服务器共用）
=====================================

2.0 起把消息类型、错误码、上限集中到这里，避免各模块用裸字符串拼协议：
- 服务器（relay.py）与客户端（net_client.py）引用同一份常量；
- 错误码带 ``ERR_`` 前缀，便于客户端判断是否可恢复（如 NO_ROOM 表示房间真的没了）；
- 协议版本号随包发布，服务器在 created/joined 里回传，便于以后做兼容判断。

2.0 新增消息：
- 客户端 → 服务器 ``resume``：断线后用房间号 + 成员令牌原地恢复，不重新建房；
- 服务器 → 客户端 ``joined``（带 ``resumed: true``）：恢复成功，附带房间最新状态与成员；
- members 列表每项新增 ``online`` 字段：掉线但仍在重连宽限期内的成员会以灰色显示。
"""

from __future__ import annotations

PROTOCOL_VERSION = 2

# ---- 客户端 → 服务器 ----
MSG_PING = "ping"
MSG_CREATE = "create"
MSG_JOIN = "join"
MSG_RESUME = "resume"
MSG_RENAME = "rename"
MSG_STATE = "state"
MSG_LEAVE = "leave"

# ---- 服务器 → 客户端 ----
MSG_PONG = "pong"
MSG_CREATED = "created"
MSG_JOINED = "joined"
MSG_LEFT = "left"
MSG_MEMBERS = "members"
MSG_NOTICE = "notice"
MSG_ERROR = "error"

# ---- 错误码 ----
ERR_NO_ROOM = "NO_ROOM"          # 房间不存在/已解散（不可恢复，需要重新建房或输入房间号）
ERR_BAD_TOKEN = "BAD_TOKEN"      # 掉线太久被移出房间（可降级为普通加入）
ERR_NOT_IN_ROOM = "NOT_IN_ROOM"
ERR_BAD_TYPE = "BAD_TYPE"

# ---- 状态动作（服务器据此生成提示文案） ----
ACTION_PLAY = "play"
ACTION_PAUSE = "pause"
ACTION_SEEK = "seek"
ACTION_NEXT = "next"
ACTION_PREV = "prev"
ACTION_OPEN = "open"
ACTION_STATE = "state"
ACTION_TEXT = {
    ACTION_PLAY: "播放",
    ACTION_PAUSE: "暂停",
    ACTION_SEEK: "拖动进度",
    ACTION_NEXT: "切换到下一集",
    ACTION_PREV: "切换到上一集",
    ACTION_OPEN: "切换了媒体",
}

# ---- 上限 ----
MAX_MESSAGE = 64 * 1024          # 单条 WebSocket 消息上限（字节）
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混淆的 0/O/1/I/L
CODE_LEN = 6
MAX_NICKNAME = 24
MAX_MEDIA_NAME = 256

# ---- 房间生命周期（秒） ----
# 掉线成员保留在列表里的时间：网络抖动/切网通常几秒内恢复，留 2 分钟足够；
# 超过后由 janitor 移出房间并广播。
OFFLINE_GRACE_S = 120.0
# 房间内没有在线成员时，保留房间状态的时间：覆盖"主人临时断网/重启客户端"的场景。
ROOM_TTL_S = 900.0
JANITOR_INTERVAL_S = 15.0

# ---- 客户端重连策略 ----
RECONNECT_DELAYS = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 18.0, 25.0, 30.0)
HEARTBEAT_S = 3.0                # 心跳间隔
HEARTBEAT_TIMEOUT_S = 12.0       # 超过这么久没收到 pong 就判定连接已死
