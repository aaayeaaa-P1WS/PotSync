#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""完整播放列表索引与精准直开测试；禁止任何逐集搜索兜底。"""

import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from net_client import NetClient  # noqa: E402
from pot_bridge import (STATUS_PAUSED, STATUS_RUNNING, PotPlayerBridge,  # noqa: E402
                        Snapshot, media_matches, normalize_media, parse_dpl)
from relay import EmbeddedServer  # noqa: E402
from sync_engine import SyncEngine  # noqa: E402

app = QCoreApplication(sys.argv)
HOST, PORT = "127.0.0.1", 18768


def wait_for(pred, timeout=10.0, desc=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    app.processEvents()
    if desc:
        print("  …等待超时:", desc)
    return bool(pred())


def media_name(index):
    return f"episode {index:04d}.mkv"


def make_library(root, dpl_path, count=2000, reverse=False):
    os.makedirs(root, exist_ok=True)
    paths = [os.path.join(root, media_name(i)) for i in range(1, count + 1)]
    for path in paths[:20]:
        Path(path).touch()
    if reverse:
        paths.reverse()
    with open(dpl_path, "w", encoding="utf-8-sig", newline="") as file:
        file.write("DAUMPLAYLIST\r\n")
        file.write(f"playname={os.path.join(root, media_name(1))}\r\n")
        file.write("topindex=0\r\nsaveplaypos=0\r\n")
        for index, path in enumerate(paths, 1):
            file.write(f"{index}*file*{path}\r\n")
            file.write(f"{index}*duration2*600000\r\n")
    return paths


class DummyNet:
    def server_now_ms(self):
        return int(time.time() * 1000)


class DirectBridge(PotPlayerBridge):
    """读取真实 .dpl 的可控播放器；所有导航调用都会留下证据。"""

    def __init__(self, dpl_path, current_path):
        super().__init__(("FakeDirectPlaylist",))
        self.dpl_path = dpl_path
        self.current_path = current_path
        self.position = 1000
        self.paused = False
        self.opened = []
        self.nav_commands = []

    def _dpl_files(self):
        return [self.dpl_path]

    @staticmethod
    def duration_for(path):
        match = re.search(r"(\d{4})", os.path.basename(path))
        return 600000 + (int(match.group(1)) * 1000 if match else 0)

    def snapshot(self):
        return Snapshot(status=STATUS_PAUSED if self.paused else STATUS_RUNNING,
                        position=self.position, duration=self.duration_for(self.current_path),
                        media=os.path.basename(self.current_path))

    def media_name(self):
        return os.path.basename(self.current_path)

    def current_media_path(self):
        return self.current_path

    def open_file(self, path):
        if not os.path.exists(path):
            return False
        self.opened.append(path)
        self.current_path = path
        self.position = 0
        self.paused = False
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


def stop_engine(engine):
    engine._poll_timer.stop()
    engine._watchdog_timer.stop()
    engine._direct_open = None
    engine.deleteLater()
    app.processEvents()


def remote_state(path, paused=True, position=42000, action="open"):
    return {"media": os.path.basename(path), "action": action, "paused": paused,
            "position": position, "duration": DirectBridge.duration_for(path),
            "ts": int(time.time() * 1000)}


def test_index_and_direct_open(tmpdir, dpl_b, current_b, target_b):
    bridge = DirectBridge(dpl_b, current_b)
    started = time.monotonic()
    hit = bridge.find_in_playlist(normalize_media(target_b))
    elapsed = time.monotonic() - started
    assert hit == target_b
    assert elapsed < 1.0, f"2000 项文本索引过慢: {elapsed:.3f}s"
    assert bridge.nav_commands == []
    print(f"PASS  2000 项完整列表一次建索引直接命中（{elapsed:.3f}s，零导航）")

    engine = SyncEngine(bridge, DummyNet())
    try:
        engine._start_media_follow(remote_state(target_b))
        assert bridge.opened == [target_b], "没有精准直开目标文件"
        assert wait_for(lambda: engine._direct_open is None, 4, "精准直开收尾")
        assert media_matches(bridge.media_name(), target_b)
        assert bridge.position == 42000 and bridge.paused
        assert bridge.nav_commands == []
        assert not hasattr(engine, "_search"), "逐集搜索状态机仍然存在"
        print("PASS  大列表精准打开一次，等待加载后对齐进度/暂停，零逐集尝试")
    finally:
        stop_engine(engine)


def test_missing_never_searches(dpl_b, current_b):
    missing = os.path.join(os.path.dirname(current_b), media_name(1999))
    bridge = DirectBridge(dpl_b, current_b)
    engine = SyncEngine(bridge, DummyNet())
    logs = []
    engine.sigLog.connect(logs.append)
    try:
        started = time.monotonic()
        engine._start_media_follow(remote_state(missing))
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert bridge.opened == [] and bridge.nav_commands == []
        assert bridge.current_path == current_b and not bridge.paused
        assert engine._direct_open is None
        assert any("未执行逐集尝试" in message for message in logs), logs
        assert any("2000 个媒体条目" in message for message in logs), logs
        print(f"PASS  列表有记录但文件缺失时立即提示，零打开/零导航/不暂停（{elapsed:.3f}s）")
    finally:
        stop_engine(engine)


def test_duplicate_name_uses_existing_candidate(tmpdir, current_b):
    existing_dir = os.path.join(tmpdir, "duplicate-existing")
    os.makedirs(existing_dir, exist_ok=True)
    existing = os.path.join(existing_dir, media_name(2))
    Path(existing).touch()
    missing = os.path.join(tmpdir, "missing-dir", media_name(2))
    dpl = os.path.join(tmpdir, "duplicate.dpl")
    with open(dpl, "w", encoding="utf-8-sig") as file:
        file.write("DAUMPLAYLIST\n")
        file.write(f"1*file*{missing}\n2*file*{existing}\n")
    bridge = DirectBridge(dpl, current_b)
    assert bridge.find_in_playlist("episode 0002") == existing
    assert parse_dpl(dpl)["files"] == [missing, existing]
    print("PASS  同名候选只检查路径存在性，跳过缺失项，不触发播放器导航")


def connect_engine(net, engine):
    engine.sigBroadcastState.connect(
        lambda state: net.send_state(state["paused"], state["position"], state["media"],
                                     state["duration"], state["action"]))
    net.sigState.connect(engine.apply_remote_state)


def test_network_direct_switch(dpl_a, current_a, target_a,
                               dpl_b, current_b, target_b):
    server = EmbeddedServer(HOST, PORT)
    server.start()
    bridge_a = DirectBridge(dpl_a, current_a)
    bridge_b = DirectBridge(dpl_b, current_b)
    net_a, net_b = NetClient(), NetClient()
    eng_a, eng_b = SyncEngine(bridge_a, net_a), SyncEngine(bridge_b, net_b)
    logs_b = []
    eng_b.sigLog.connect(logs_b.append)
    try:
        assert wait_for(lambda: server.running, 3, "内嵌服务器")
        connect_engine(net_a, eng_a)
        connect_engine(net_b, eng_b)
        connected = {"a": False, "b": False}
        net_a.sigConnected.connect(lambda: connected.__setitem__("a", True))
        net_b.sigConnected.connect(lambda: connected.__setitem__("b", True))
        net_a.connect_to(HOST, PORT)
        net_b.connect_to(HOST, PORT)
        assert wait_for(lambda: all(connected.values()), 8, "双端连接")

        created = {}
        net_a.sigRoomCreated.connect(
            lambda room, state, members: (created.update(room=room), eng_a.enter_room(state)))
        net_a.create_room("主机")
        assert wait_for(lambda: "room" in created, 8, "建房")
        assert wait_for(lambda: eng_a.room_state is not None, 6, "初始状态")

        net_b.sigRoomJoined.connect(lambda room, state, members: eng_b.enter_room(state))
        net_b.join_room(created["room"], "参与者")
        assert wait_for(lambda: eng_b.room_state is not None, 8, "加入并收到状态")
        assert bridge_b.opened == []

        assert bridge_a.open_file(target_a)
        # 手动注入测试状态前同步采样基线，避免 A 的轮询把同一换片再广播一次。
        eng_a._last = bridge_a.snapshot()
        eng_a._last_mono = time.monotonic()
        state = remote_state(target_a, paused=True, position=12345)
        eng_a._broadcast(paused=state["paused"], position=state["position"],
                         media=state["media"], duration=state["duration"],
                         action=state["action"])
        assert wait_for(lambda: media_matches(bridge_b.media_name(), target_b)
                        and eng_b._direct_open is None, 8, "公网协议同路径的精准切集")
        assert bridge_b.opened == [target_b], bridge_b.opened
        assert bridge_b.position == 12345 and bridge_b.paused, (
            bridge_b.snapshot(), eng_b.room_state, logs_b)
        assert bridge_b.nav_commands == []
        print("PASS  双端列表顺序相反且目录不同，仍按完整列表精准直开一次")

        # A 有文件、B 的列表有记录但文件缺失：B 应立即报未找到，绝不逐集试开。
        missing_a = os.path.join(os.path.dirname(current_a), media_name(1999))
        Path(missing_a).touch()
        assert bridge_a.open_file(missing_a)
        eng_a._last = bridge_a.snapshot()
        eng_a._last_mono = time.monotonic()
        assert bridge_b.play()
        state = remote_state(missing_a, paused=True, position=0)
        eng_a._broadcast(paused=state["paused"], position=state["position"],
                         media=state["media"], duration=state["duration"],
                         action=state["action"])
        assert wait_for(lambda: any("未执行逐集尝试" in message for message in logs_b),
                        5, "缺失文件提示")
        assert bridge_b.opened == [target_b]
        assert bridge_b.nav_commands == []
        assert media_matches(bridge_b.media_name(), target_b)
        assert not bridge_b.paused, "未命中时不应为了搜索而暂停当前媒体"
        print("PASS  双端缺失场景保持当前媒体，零导航、零额外打开")
    finally:
        net_a.shutdown()
        net_b.shutdown()
        stop_engine(eng_a)
        stop_engine(eng_b)
        server.stop()
        server.wait_stopped(3)


def main():
    assert normalize_media(" Episode 0002.MKV ") == "episode 0002"
    assert media_matches(r"E:\anime\episode 0002.mkv", "EPISODE 0002.mp4")
    assert not media_matches("episode 0002v2.mkv", "episode 0002.mkv")
    print("PASS  文件名规范化与相似名误判防护")

    tmpdir = tempfile.mkdtemp(prefix="potsync_playlist_index_")
    root_a, root_b = os.path.join(tmpdir, "A"), os.path.join(tmpdir, "B")
    dpl_a, dpl_b = os.path.join(tmpdir, "A.dpl"), os.path.join(tmpdir, "B.dpl")
    make_library(root_a, dpl_a)
    make_library(root_b, dpl_b, reverse=True)
    current_a, current_b = os.path.join(root_a, media_name(1)), os.path.join(root_b, media_name(1))
    target_a, target_b = os.path.join(root_a, media_name(18)), os.path.join(root_b, media_name(18))

    test_index_and_direct_open(tmpdir, dpl_b, current_b, target_b)
    test_missing_never_searches(dpl_b, current_b)
    test_duplicate_name_uses_existing_candidate(tmpdir, current_b)
    test_network_direct_switch(dpl_a, current_a, target_a, dpl_b, current_b, target_b)
    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
