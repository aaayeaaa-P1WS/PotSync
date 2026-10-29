#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体直开安全性回归：慢加载、连续远程操作与本地打断。"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from PyQt5.QtCore import QCoreApplication, QTimer  # noqa: E402

import sync_engine  # noqa: E402
from pot_bridge import STATUS_PAUSED, STATUS_RUNNING, Snapshot  # noqa: E402
from sync_engine import SyncEngine  # noqa: E402


app = QCoreApplication(sys.argv)


class DummyNet:
    def server_now_ms(self):
        return int(time.time() * 1000)


class SlowOpenBridge:
    """无 Win32 依赖的可控播放器：open_file 异步完成并记录并发数。"""

    def __init__(self, load_delay_ms=2500):
        self.current = "ep01.mkv"
        self.duration = 600000
        self.position = 1000
        self.paused = False
        self.load_delay_ms = load_delay_ms
        self.opened = []
        self.nav_commands = []
        self.loading = 0
        self.max_loading = 0

    def snapshot(self):
        return Snapshot(
            status=STATUS_PAUSED if self.paused else STATUS_RUNNING,
            position=self.position,
            duration=self.duration,
            media=self.current,
        )

    def media_name(self):
        return self.current

    def current_media_path(self):
        return "D:/media/" + self.current

    def find_in_playlist(self, target_norm):
        return "D:/media/" + target_norm + ".mkv"

    def open_file(self, path):
        target = os.path.basename(path)
        self.opened.append(target)
        self.loading += 1
        self.max_loading = max(self.max_loading, self.loading)

        def loaded():
            self.loading -= 1
            self.current = target
            self.position = 0

        QTimer.singleShot(self.load_delay_ms, loaded)
        return True

    def next(self):
        self.nav_commands.append("next")
        return True

    def previous(self):
        self.nav_commands.append("previous")
        return True

    def pause(self):
        self.paused = True
        return True

    def play(self):
        self.paused = False
        return True

    def seek_ms(self, position):
        self.position = position
        return True


def wait_for(pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(pred())


def make_engine(bridge):
    engine = SyncEngine(bridge, DummyNet())
    engine.in_room = True
    return engine


def cleanup(engine):
    engine._poll_timer.stop()
    engine._watchdog_timer.stop()
    engine._direct_open = None
    engine.deleteLater()
    app.processEvents()


def state(media, action="open"):
    return {
        "media": media,
        "action": action,
        "paused": False,
        "position": 0,
        "duration": 600000,
        "ts": int(time.time() * 1000),
    }


def test_slow_open_never_falls_back_early():
    bridge = SlowOpenBridge(load_delay_ms=2500)
    engine = make_engine(bridge)
    try:
        engine._start_media_follow(state("ep02.mkv"))
        assert bridge.opened == ["ep02.mkv"]
        assert wait_for(lambda: bridge.current == "ep02.mkv", 4)
        assert wait_for(lambda: engine._direct_open is None, 1)
        assert not bridge.nav_commands, (
            "直开尚未完成就发送了轮巡命令: " + repr(bridge.nav_commands))
        print("PASS  慢速直开期间不提前轮巡、不发送切集命令")
    finally:
        cleanup(engine)


def test_rapid_remote_changes_are_serialized_latest_wins():
    bridge = SlowOpenBridge(load_delay_ms=1000)
    engine = make_engine(bridge)
    try:
        engine.apply_remote_state(state("ep02.mkv"))
        engine.apply_remote_state(state("ep03.mkv"))
        assert bridge.opened == ["ep02.mkv"], "加载中不应并发打开第二个文件"
        assert wait_for(lambda: bridge.current == "ep03.mkv", 5)
        assert bridge.opened == ["ep02.mkv", "ep03.mkv"]
        assert bridge.max_loading == 1, "出现多个同时加载的 PotPlayer 打开请求"
        assert not bridge.nav_commands
        print("PASS  连续远程切集串行执行，最终落在最新目标")
    finally:
        cleanup(engine)


def test_local_action_cancels_pending_follow():
    bridge = SlowOpenBridge(load_delay_ms=3000)
    engine = make_engine(bridge)
    engine._announced_local = True
    engine._last = bridge.snapshot()
    engine._last_mono = time.monotonic()
    try:
        engine.apply_remote_state(state("ep02.mkv"))
        engine.user_toggle_play_pause()
        sent = []
        engine.sigBroadcastState.connect(sent.append)
        time.sleep(2.1)                 # 越过普通操作静默期，旧加载仍未完成
        app.processEvents()
        engine._guard_until = 0
        assert not bridge.nav_commands, "本地操作后旧直开任务仍触发了轮巡"
        assert engine._direct_open is None
        assert wait_for(lambda: bridge.current == "ep02.mkv", 2)
        time.sleep(0.6)
        app.processEvents()
        assert not any(s.get("media") == "ep02.mkv" for s in sent), \
            "已取消的迟到加载被当成本地换片重新广播"
        print("PASS  本地操作取消旧任务，迟到加载也不会回弹广播")
    finally:
        cleanup(engine)


def test_remote_episode_goes_direct_without_blind_navigation():
    bridge = SlowOpenBridge(load_delay_ms=300)
    engine = make_engine(bridge)
    try:
        engine.apply_remote_state(state("ep02.mkv", action="next"))
        assert bridge.opened == ["ep02.mkv"]
        assert not bridge.nav_commands, "已知目标文件时仍盲目发送了下一集"
        assert wait_for(lambda: bridge.current == "ep02.mkv", 3)
        print("PASS  远程上下集按目标文件直开，不先做盲目切集")
    finally:
        cleanup(engine)


def test_open_timeout_stops_without_extra_commands():
    bridge = SlowOpenBridge(load_delay_ms=60000)
    engine = make_engine(bridge)
    old_timeout = sync_engine.DIRECT_OPEN_TIMEOUT_S
    sync_engine.DIRECT_OPEN_TIMEOUT_S = 0.6
    try:
        engine.apply_remote_state(state("ep02.mkv"))
        assert wait_for(lambda: engine._direct_open is None, 2)
        assert bridge.opened == ["ep02.mkv"]
        assert not bridge.nav_commands, "加载超时后仍补发了轮巡命令"
        assert engine._failed_media == "ep02"
        assert engine._direct_open is None
        print("PASS  直开彻底超时后有界停止，不补发任何切集命令")
    finally:
        sync_engine.DIRECT_OPEN_TIMEOUT_S = old_timeout
        cleanup(engine)


def main():
    tests = [
        test_slow_open_never_falls_back_early,
        test_rapid_remote_changes_are_serialized_latest_wins,
        test_local_action_cancels_pending_follow,
        test_remote_episode_goes_direct_without_blind_navigation,
        test_open_timeout_stops_without_extra_commands,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
    if failures:
        print(f"\n{failures} 项媒体安全回归失败")
        return 1
    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
