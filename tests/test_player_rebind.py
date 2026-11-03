#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
播放器生命周期回归测试（2.0）
==============================
验证三件事：
1. 关掉 PotPlayer 窗口/进程时，房间与成员身份保留，不进离房流程；
2. 重新打开同一个文件时，对齐房间进度且不重复广播；
3. 重新打开的是另一个文件时，只广播一次 open，把新文件设为房间媒体；
4. 关窗期间房间已被别人换片时，重开后跟随房间而不是把本机文件回弹给所有人。

运行：python tests/test_player_rebind.py
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from pot_bridge import STATUS_PAUSED, STATUS_RUNNING, Snapshot  # noqa: E402
from sync_engine import SyncEngine  # noqa: E402

app = QCoreApplication(sys.argv)


class DummyNet:
    def server_now_ms(self) -> int:
        return int(time.time() * 1000)


class RebindBridge:
    """无 Win32 依赖的可控播放器；present=False 模拟窗口/进程消失。"""

    def __init__(self, media: str = "ep01.mkv", duration: int = 600000,
                 position: int = 0) -> None:
        self.media = media
        self.duration = duration
        self.position = position
        self.paused = True
        self.present = True
        self.opened = []
        self.nav_commands = []

    def snapshot(self):
        if not self.present:
            return None
        return Snapshot(status=STATUS_PAUSED if self.paused else STATUS_RUNNING,
                        position=self.position, duration=self.duration,
                        media=self.media)

    def media_name(self) -> str:
        return self.media

    def current_media_path(self) -> str:
        return "D:/media/" + self.media

    def find_in_playlist(self, target_norm):
        return None

    def open_file(self, path) -> bool:
        self.opened.append(path)
        return True

    def pause(self) -> bool:
        self.paused = True
        return True

    def play(self) -> bool:
        self.paused = False
        return True

    def seek_ms(self, position: int) -> bool:
        self.position = position
        return True

    def next(self) -> bool:
        self.nav_commands.append("next")
        return True

    def previous(self) -> bool:
        self.nav_commands.append("previous")
        return True


def room_state(media: str = "ep01.mkv", position: int = 42000,
               duration: int = 600000, paused: bool = True) -> dict:
    return {"media": media, "paused": paused, "position": position,
            "duration": duration, "action": "state",
            "ts": int(time.time() * 1000)}


class Harness:
    def __init__(self, bridge, state=None):
        self.bridge = bridge
        self.engine = SyncEngine(bridge, DummyNet())
        self.logs = []
        self.infos = []
        self.broadcasts = []
        self.engine.sigLog.connect(self.logs.append)
        self.engine.sigSyncInfo.connect(self.infos.append)
        self.engine.sigBroadcastState.connect(self.broadcasts.append)
        self.engine.enter_room(state or room_state())
        self.engine._poll_timer.stop()
        self.engine._watchdog_timer.stop()

    def poll(self, times: int = 1):
        for _ in range(times):
            self.engine._poll()

    def opens(self):
        return [b for b in self.broadcasts if b.get("action") == "open"]

    def close(self):
        self.engine._poll_timer.stop()
        self.engine._watchdog_timer.stop()
        self.engine._direct_open = None
        self.engine.deleteLater()
        app.processEvents()


def test_close_keeps_room():
    h = Harness(RebindBridge(), room_state(position=1000))
    try:
        h.poll()
        h.bridge.present = False
        h.poll(3)
        assert h.engine.in_room, "关闭播放器后不应退出房间"
        assert h.engine.room_state.get("media") == "ep01.mkv", "房间媒体不应被清空"
        assert any("房间与成员身份保留" in t for t in h.logs), h.logs
        assert h.infos and "等待 PotPlayer 打开" in h.infos[-1], h.infos
        print("PASS  关闭 PotPlayer 不进离房流程（房间/成员/媒体全部保留）")
    finally:
        h.close()


def test_reopen_same_media_aligns():
    h = Harness(RebindBridge(position=1000), room_state(position=1000))
    try:
        h.poll()
        h.bridge.present = False
        h.poll()
        # 关窗期间房间进度继续前进
        h.engine.room_state = room_state(position=42000)
        h.poll()                      # 播放器缺席时只跟踪房间
        h.bridge.present = True
        h.bridge.position = 0
        h.poll()
        assert h.bridge.position >= 41000, f"没有对齐房间进度: {h.bridge.position}"
        assert h.bridge.paused, "应保持暂停状态"
        assert not h.opens(), f"同片重开不应广播 open: {h.opens()}"
        assert any("已对齐房间进度" in t for t in h.logs), h.logs
        print("PASS  重新打开同一文件：对齐房间进度且不回广播")
    finally:
        h.close()


def test_reopen_new_media_broadcasts_once():
    h = Harness(RebindBridge(), room_state())
    try:
        h.poll()
        h.bridge.present = False
        h.poll()
        h.bridge.media = "ep02.mkv"
        h.bridge.duration = 720000
        h.bridge.position = 0
        h.bridge.present = True
        h.poll()
        opens = h.opens()
        assert len(opens) == 1, f"新文件应只广播一次 open: {opens}"
        assert opens[0]["media"] == "ep02.mkv" and opens[0]["action"] == "open"
        h.poll(6)                      # 后续几拍不能重复广播
        assert len(h.opens()) == 1, f"重复广播: {h.opens()}"
        assert h.engine.room_state.get("media") == "ep02.mkv"
        print("PASS  重新打开新文件：只广播一次 open（含 duration 0→N 的加载过程）")
    finally:
        h.close()


def test_room_moved_while_absent_is_followed():
    h = Harness(RebindBridge(), room_state())
    try:
        h.poll()
        h.bridge.present = False
        h.poll()
        h.engine.apply_remote_state(room_state(media="ep03.mkv", position=5000))
        h.poll()
        h.bridge.media = "ep02.mkv"
        h.bridge.present = True
        h.poll()
        assert not h.opens(), f"跟随房间时不应把本机文件广播出去: {h.opens()}"
        assert any("房间已切换到《ep03.mkv》" in t for t in h.logs), h.logs
        print("PASS  关窗期间房间换片：重开后跟随房间、不回弹本机文件")
    finally:
        h.close()


def main() -> int:
    test_close_keeps_room()
    test_reopen_same_media_aligns()
    test_reopen_new_media_broadcasts_once()
    test_room_moved_while_absent_is_followed()
    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
