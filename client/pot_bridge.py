#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotPlayer 控制桥
================

通过 Win32 窗口消息远程控制本机的 PotPlayer（32/64 位均可）：

- 窗口类名：PotPlayer64 / PotPlayer / PotPlayerMini64 / PotPlayerMini
- WM_COMMAND(0x0111)：播放 20001、暂停 20000、播放/暂停切换 10014、停止 20002
- WM_USER(0x0400)：0x5002 取总时长(ms)、0x5004 取当前进度(ms)、0x5005 设置进度(ms)、
  0x5006 取播放状态(-1 停止 / 1 暂停 / 2 播放中)、0x5007 设置播放状态(0 切换/1 暂停/2 播放)

所有消息使用 SendMessageTimeout，避免 PotPlayer 卡死时拖挂本程序界面。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import win32con
import win32gui

log = logging.getLogger("potsync.bridge")

WM_COMMAND = win32con.WM_COMMAND          # 0x0111
WM_USER = win32con.WM_USER                # 0x0400
SMTO_ABORTIFHUNG = 0x0002
MSG_TIMEOUT_MS = 400

# WM_COMMAND 命令
CMD_PAUSE = 20000
CMD_PLAY = 20001
CMD_STOP = 20002
CMD_PLAY_PAUSE = 10014
CMD_PREVIOUS = 10123      # 播放列表：上一集/上一个
CMD_NEXT = 10124          # 播放列表：下一集/下一个

# WM_USER 命令（参数经 lParam 传递，返回值即结果）
POT_GET_TOTAL_TIME = 0x5002     # ms
POT_GET_CURRENT_TIME = 0x5004   # ms
POT_SET_CURRENT_TIME = 0x5005   # ms（lParam）
POT_GET_PLAY_STATUS = 0x5006    # -1 停止 / 1 暂停 / 2 播放中
POT_SET_PLAY_STATUS = 0x5007    # 0 切换 / 1 暂停 / 2 播放（lParam）

STATUS_STOPPED = -1
STATUS_PAUSED = 1
STATUS_RUNNING = 2
# 实测：空闲状态不同版本可能返回 -1 或 0，均视为"已停止"
STATUS_TEXT = {STATUS_STOPPED: "已停止", 0: "已停止",
               STATUS_PAUSED: "已暂停", STATUS_RUNNING: "播放中"}

WINDOW_CLASSES = ("PotPlayer64", "PotPlayer", "PotPlayerMini64", "PotPlayerMini")


@dataclass
class Snapshot:
    """一次本地播放器状态采样。position/duration 单位毫秒。"""
    status: int          # -1/1/2
    position: int        # ms
    duration: int        # ms
    media: str           # 当前媒体名（取自窗口标题）

    @property
    def playing(self) -> bool:
        return self.status == STATUS_RUNNING


class PotPlayerBridge:
    """定位并操控本机 PotPlayer 窗口。所有方法均线程无关、快速返回。"""

    def __init__(self, class_names: tuple = WINDOW_CLASSES) -> None:
        self._hwnd: int = 0
        self._class_names = class_names

    # ---------- 窗口定位 ----------

    def find_window(self) -> int:
        if self._hwnd and win32gui.IsWindow(self._hwnd):
            return self._hwnd
        for cls in self._class_names:
            hwnd = win32gui.FindWindow(cls, None)
            if hwnd:
                self._hwnd = hwnd
                return hwnd
        self._hwnd = 0
        return 0

    def available(self) -> bool:
        return self.find_window() != 0

    # ---------- 底层收发 ----------

    def _send(self, msg: int, wparam: int = 0, lparam: int = 0) -> Optional[int]:
        """SendMessageTimeout；成功返回消息返回值，窗口不存在/超时返回 None。"""
        hwnd = self.find_window()
        if not hwnd:
            return None
        try:
            ok, ret = win32gui.SendMessageTimeout(
                hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, MSG_TIMEOUT_MS)
        except Exception as exc:  # 窗口刚关闭等情况
            log.debug("SendMessageTimeout 失败: %s", exc)
            self._hwnd = 0
            return None
        if not ok:
            return None
        # 32 位有符号还原（长片源毫秒数不会超过 2^31，保守处理）
        if ret is not None and ret >= 2 ** 31:
            ret -= 2 ** 32
        return ret

    def _command(self, cmd: int) -> bool:
        return self._send(WM_COMMAND, cmd, 0) is not None

    # ---------- 状态查询 ----------

    def status(self) -> Optional[int]:
        return self._send(WM_USER, POT_GET_PLAY_STATUS, 0)

    def position_ms(self) -> Optional[int]:
        return self._send(WM_USER, POT_GET_CURRENT_TIME, 0)

    def duration_ms(self) -> Optional[int]:
        return self._send(WM_USER, POT_GET_TOTAL_TIME, 0)

    def media_name(self) -> str:
        hwnd = self.find_window()
        if not hwnd:
            return ""
        try:
            title = win32gui.GetWindowText(hwnd) or ""
        except Exception:
            return ""
        for suffix in (" - PotPlayer",):
            if title.endswith(suffix):
                title = title[: -len(suffix)]
        title = title.strip()
        if title.lower() in ("potplayer",):   # 空闲未加载文件时标题就是程序名
            return ""
        return title

    def snapshot(self) -> Optional[Snapshot]:
        """一次采样；PotPlayer 未运行返回 None。"""
        if not self.find_window():
            return None
        status = self.status()
        if status is None:
            return None
        position = self.position_ms() or 0
        duration = self.duration_ms() or 0
        if status not in (STATUS_PAUSED, STATUS_RUNNING):   # 已停止
            position = 0
        return Snapshot(status=status, position=max(0, position),
                        duration=max(0, duration), media=self.media_name())

    # ---------- 播放控制 ----------

    def play(self) -> bool:
        return self._command(CMD_PLAY)

    def pause(self) -> bool:
        return self._command(CMD_PAUSE)

    def toggle_play_pause(self) -> bool:
        return self._command(CMD_PLAY_PAUSE)

    def seek_ms(self, position_ms: int) -> bool:
        position_ms = max(0, int(position_ms))
        return self._send(WM_USER, POT_SET_CURRENT_TIME, position_ms) is not None

    def previous(self) -> bool:
        """播放列表上一个（上一集）。"""
        return self._command(CMD_PREVIOUS)

    def next(self) -> bool:
        """播放列表下一个（下一集）。"""
        return self._command(CMD_NEXT)


if __name__ == "__main__":
    # 手动自检：python pot_bridge.py
    logging.basicConfig(level=logging.DEBUG)
    b = PotPlayerBridge()
    snap = b.snapshot()
    if snap is None:
        print("未检测到正在运行的 PotPlayer")
    else:
        print(f"状态={STATUS_TEXT.get(snap.status)} 进度={snap.position}ms "
              f"总长={snap.duration}ms 媒体={snap.media!r}")
