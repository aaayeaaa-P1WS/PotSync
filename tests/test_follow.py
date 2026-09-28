#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件名自动匹配 / 播放器内切集跟随测试
=====================================

场景（双客户端 + 双假播放器 + 真服务器）：
- A 播放列表：[ep01, ep02, ep03, ep99]
- B 播放列表：[ep03, ep01, ep02]（顺序不同、缺少 ep99、不循环）

验证：
1. 媒体名规范化匹配（大小写/扩展名/路径/空白）
2. B 入房时媒体不同 → 自动按文件名切换到 ep01
3. A 在"播放器里"直接切下一集（不调引擎按钮，模拟快捷键/播放器按钮）
   → B 自动跟随到 ep02
4. B 位于列表末尾时，对方切到 ep03 → 掉头反向搜索命中
5. A 切到 B 没有的 ep99 → B 全表轮巡未找到 → 返回原媒体并提示；
   后续同媒体状态不再重复轮巡（失败缓存）

运行：python tests/test_follow.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 本机开启系统代理时，强制本地回环地址直连
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from PyQt5.QtCore import QCoreApplication  # noqa: E402

from net_client import NetClient                       # noqa: E402
from pot_bridge import (STATUS_PAUSED, STATUS_RUNNING,  # noqa: E402
                        PotPlayerBridge, media_matches, normalize_media)
from sync_engine import SyncEngine                     # noqa: E402

SERVER = ROOT / "server" / "server.py"
FAKE = ROOT / "tools" / "fake_potplayer.py"
PORT = 18768
HOST = "127.0.0.1"

app = QCoreApplication(sys.argv)


def wait_for(pred, timeout: float = 10.0, desc: str = "") -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.03)
    if desc:
        print(f"  …等待超时: {desc}")
    return False


def main() -> int:
    # ---------- 单元：媒体名规范化匹配 ----------
    assert normalize_media("Ep02.MKV") == "ep02"
    assert normalize_media("  ep02 .mkv ") == "ep02"
    assert normalize_media(r"D:\ Anime \ep02.mkv") == "ep02"
    assert normalize_media("") == ""
    assert media_matches("ep02.mkv", "EP02.mkv")
    assert media_matches(r"E:\series\ep02.mkv", "ep02.MKV")
    assert media_matches("[V2]  ep01.mkv", "[v2] ep01.mkv")
    assert not media_matches("ep01v2.mkv", "ep01.mkv")
    assert not media_matches("ep02.mkv", "")
    assert not media_matches("", "")
    print("PASS  媒体名规范化匹配（大小写/扩展名/路径/空白/误判防护）")

    server = subprocess.Popen(
        [sys.executable, str(SERVER), "--host", HOST, "--port", str(PORT),
         "--log-level", "WARNING"])
    fake_a = subprocess.Popen(
        [sys.executable, str(FAKE), "--class", "FakePotFlwA", "--playlist",
         "ep01.mkv:600000,ep02.mkv:610000,ep03.mkv:620000,ep99.mkv:999000"])
    fake_b = subprocess.Popen(
        [sys.executable, str(FAKE), "--class", "FakePotFlwB", "--playlist",
         "ep03.mkv:620000,ep01.mkv:600000,ep02.mkv:610000"])
    try:
        bridge_a = PotPlayerBridge(("FakePotFlwA",))
        bridge_b = PotPlayerBridge(("FakePotFlwB",))
        assert wait_for(lambda: bridge_a.available() and bridge_b.available(),
                        desc="假播放器启动")
        print("PASS  双假播放器就绪（B 列表顺序不同且无 ep99，B 当前在 ep03）")

        net_a, net_b = NetClient(), NetClient()
        eng_a = SyncEngine(bridge_a, net_a)
        eng_b = SyncEngine(bridge_b, net_b)
        eng_a.sigBroadcastState.connect(
            lambda s: net_a.send_state(s["paused"], s["position"], s["media"],
                                       s["duration"], s["action"]))
        eng_b.sigBroadcastState.connect(
            lambda s: net_b.send_state(s["paused"], s["position"], s["media"],
                                       s["duration"], s["action"]))
        net_a.sigState.connect(eng_a.apply_remote_state)
        net_b.sigState.connect(eng_b.apply_remote_state)
        logs_b = []
        eng_b.sigLog.connect(logs_b.append)

        conn = {"a": False, "b": False}
        net_a.sigConnected.connect(lambda: conn.__setitem__("a", True))
        net_b.sigConnected.connect(lambda: conn.__setitem__("b", True))
        net_a.connect_to(HOST, PORT)
        net_b.connect_to(HOST, PORT)
        assert wait_for(lambda: conn["a"] and conn["b"], desc="连接服务器")

        created = {}
        def _a_created(room, state, members):
            created.update(room=room)
            eng_a.enter_room(state)
        net_a.sigRoomCreated.connect(_a_created)
        net_a.create_room("小明")
        assert wait_for(lambda: "room" in created, desc="建房")
        assert wait_for(lambda: eng_a.room_state is not None, 6, "上报初始状态")
        room = created["room"]

        # ① B 入房时在 ep03，房间状态是 ep01 → 应自动按文件名切到 ep01
        joined = {}
        def _b_joined(r, state, members):
            joined.update(room=r, state=state)
            eng_b.enter_room(state)
        net_b.sigRoomJoined.connect(_b_joined)
        net_b.join_room(room, "小红")
        assert wait_for(lambda: joined.get("state") is not None, desc="B 加入")
        ok = wait_for(lambda: media_matches(bridge_b.media_name(), "ep01.mkv"), 20,
                      "B 入房自动匹配 ep01")
        assert ok, f"B 入房未自动匹配: 当前 {bridge_b.media_name()!r}"
        assert bridge_b.duration_ms() == 600000
        print(f"PASS  入房自动匹配：B 从 ep03 自动切到 {bridge_b.media_name()!r}")

        # ② A 在播放器里直接切下一集（模拟快捷键/播放器按钮，不经引擎）
        bridge_a.next()     # ep01 → ep02
        ok = wait_for(lambda: media_matches(bridge_b.media_name(), "ep02.mkv"), 20,
                      "B 跟随 ep02")
        assert ok, f"B 未跟随播放器内切集: 当前 {bridge_b.media_name()!r}"
        assert bridge_b.duration_ms() == 610000
        print(f"PASS  播放器内切集跟随：A 切 ep02 → B 自动切到 "
              f"{bridge_b.media_name()!r}")

        # ③ A 再切到 ep03：B 当前 ep02 已在自身列表末尾（不循环）→ 掉头反搜命中
        bridge_a.next()     # ep02 → ep03
        ok = wait_for(lambda: media_matches(bridge_b.media_name(), "ep03.mkv"), 25,
                      "B 掉头反搜 ep03")
        assert ok, f"B 反搜未命中: 当前 {bridge_b.media_name()!r}"
        assert bridge_b.duration_ms() == 620000
        print("PASS  列表尽头掉头反搜：B 从末尾反向找到 ep03")

        # ④ A 切到 B 没有的 ep99 → B 全表轮巡未找到 → 返回 ep03 并提示
        bridge_a.next()     # ep03 → ep99
        ok = wait_for(
            lambda: any("未找到" in m for m in logs_b), 30, "B 提示未找到")
        assert ok, f"B 未产生未找到提示。日志: {logs_b[-5:]}"
        ok = wait_for(lambda: media_matches(bridge_b.media_name(), "ep03.mkv")
                      and eng_b._search is None, 25, "B 返回原媒体")
        assert ok, f"B 未返回原媒体: 当前 {bridge_b.media_name()!r}"
        print("PASS  未找到兜底：B 全表轮巡后返回原媒体 ep03 并给出提示")

        # ⑤ 失败缓存：对方同一媒体再发状态（如暂停）不应触发二次全表轮巡
        logs_mark = len(logs_b)
        bridge_a.pause()
        ok = wait_for(lambda: bridge_b.status() == STATUS_PAUSED, 8,
                      "B 跟随暂停")
        assert ok, "B 未跟随暂停（不同媒体也应跟随播放/暂停）"
        time.sleep(1.5)
        app.processEvents()
        new_logs = logs_b[logs_mark:]
        assert not any("正在本机播放列表查找" in m for m in new_logs), \
            f"失败缓存失效，重复轮巡了: {new_logs}"
        print("PASS  失败缓存：同一缺失媒体不重复轮巡（播放/暂停仍跟随）")

        print("\n全部通过 ✔")
        return 0
    finally:
        for p in (fake_a, fake_b, server):
            p.terminate()
        for p in (fake_a, fake_b, server):
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
