#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
假 PotPlayer 窗口（测试用）
==========================

注册一个与真实 PotPlayer 相同窗口类名（PotPlayer64，可用 --class 指定别名）的窗口，
实现 PotPlayer 的 WM_USER / WM_COMMAND 远程控制接口并模拟播放进度推进，
用于在没有安装/运行 PotPlayer 时测试 pot_bridge 与整套同步链路。

用法：
    python fake_potplayer.py [媒体名] [总时长ms] [--paused] [--class 类名]
                             [--playlist "ep01.mkv:600000,ep02.mkv:601000,…"]

--playlist 提供时启用播放列表，支持上一集/下一集（CMD 10123/10124）切换；
列表项格式为 名称:时长ms，以逗号分隔，首项为初始媒体。
"""

from __future__ import annotations

import sys
import time
from typing import Optional

import win32con
import win32gui
import win32api

WM_USER = win32con.WM_USER
WM_COMMAND = win32con.WM_COMMAND

POT_GET_TOTAL_TIME = 0x5002
POT_GET_CURRENT_TIME = 0x5004
POT_SET_CURRENT_TIME = 0x5005
POT_GET_PLAY_STATUS = 0x5006
POT_SET_PLAY_STATUS = 0x5007

CMD_PAUSE = 20000
CMD_PLAY = 20001
CMD_PLAY_PAUSE = 10014
CMD_PREVIOUS = 10123
CMD_NEXT = 10124

STATUS_PAUSED = 1
STATUS_RUNNING = 2


class FakePotPlayer:
    def __init__(self, media: str, duration_ms: int, paused: bool,
                 class_name: str = "PotPlayer64",
                 playlist: Optional[list] = None) -> None:
        self.media = media
        self.duration = duration_ms
        self.status = STATUS_PAUSED if paused else STATUS_RUNNING
        self.class_name = class_name
        self.base_pos = 0
        self.started = time.monotonic()
        self.hwnd = 0
        # 播放列表：[(名称, 时长ms), ...]
        self.playlist = playlist or [(media, duration_ms)]
        self.index = 0

    # ---------- 播放模拟 ----------

    def position(self) -> int:
        if self.status == STATUS_RUNNING:
            pos = self.base_pos + int((time.monotonic() - self.started) * 1000)
            if pos >= self.duration:          # 播放到头自动暂停
                self.base_pos = self.duration
                self.status = STATUS_PAUSED
                return self.duration
            return pos
        return self.base_pos

    def set_position(self, ms: int) -> None:
        self.base_pos = max(0, min(self.duration, int(ms)))
        self.started = time.monotonic()

    def set_status(self, status: int) -> None:
        self.set_position(self.position())  # 先固化进度
        self.status = status

    def switch(self, delta: int) -> None:
        """切换上/下一集：进度归零、保持播放状态、刷新窗口标题。"""
        new_index = max(0, min(len(self.playlist) - 1, self.index + delta))
        if new_index == self.index:
            return
        self.index = new_index
        self.media, self.duration = self.playlist[self.index]
        self.base_pos = 0
        self.started = time.monotonic()
        if self.hwnd:
            win32gui.SetWindowText(self.hwnd, f"{self.media} - PotPlayer")

    # ---------- Win32 ----------

    def run(self) -> None:
        fake = self

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_USER:
                if wparam == POT_GET_TOTAL_TIME:
                    return fake.duration
                if wparam == POT_GET_CURRENT_TIME:
                    return fake.position()
                if wparam == POT_SET_CURRENT_TIME:
                    fake.set_position(lparam)
                    return 0
                if wparam == POT_GET_PLAY_STATUS:
                    return fake.status
                if wparam == POT_SET_PLAY_STATUS:
                    if lparam == 0:
                        fake.set_status(STATUS_PAUSED if fake.status == STATUS_RUNNING
                                        else STATUS_RUNNING)
                    elif lparam in (STATUS_PAUSED, STATUS_RUNNING):
                        fake.set_status(lparam)
                    return 0
            elif msg == WM_COMMAND:
                if wparam == CMD_PLAY:
                    fake.set_status(STATUS_RUNNING)
                elif wparam == CMD_PAUSE:
                    fake.set_status(STATUS_PAUSED)
                elif wparam == CMD_PLAY_PAUSE:
                    fake.set_status(STATUS_PAUSED if fake.status == STATUS_RUNNING
                                    else STATUS_RUNNING)
                elif wparam == CMD_PREVIOUS:
                    fake.switch(-1)
                elif wparam == CMD_NEXT:
                    fake.switch(+1)
                return 0
            elif msg == win32con.WM_DESTROY:
                win32api.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        wc = win32gui.WNDCLASS()
        wc.lpszClassName = self.class_name
        wc.lpfnWndProc = wndproc
        wc.hInstance = win32api.GetModuleHandle(None)
        win32gui.RegisterClass(wc)

        self.hwnd = win32gui.CreateWindow(
            self.class_name, f"{self.media} - PotPlayer",
            win32con.WS_OVERLAPPEDWINDOW, 0, 0, 480, 320,
            0, 0, wc.hInstance, None)
        print(f"[fake-potplayer] hwnd={self.hwnd} media={self.media!r} "
              f"duration={self.duration}ms status={self.status} "
              f"playlist={len(self.playlist)}项", flush=True)
        win32gui.PumpMessages()


def parse_playlist(spec: str) -> list:
    items = []
    for part in spec.split(","):
        name, _, dur = part.rpartition(":")
        items.append((name.strip(), int(dur)))
    return items


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    paused = "--paused" in sys.argv
    class_name = "PotPlayer64"
    if "--class" in sys.argv:
        i = sys.argv.index("--class")
        class_name = sys.argv[i + 1]

    if "--playlist" in sys.argv:
        i = sys.argv.index("--playlist")
        playlist = parse_playlist(sys.argv[i + 1])
        media, duration = playlist[0]
    else:
        playlist = None
        media = args[0] if len(args) > 0 else "demo.mp4"
        duration = int(args[1]) if len(args) > 1 else 60000

    FakePotPlayer(media, duration, paused, class_name, playlist).run()


if __name__ == "__main__":
    main()
