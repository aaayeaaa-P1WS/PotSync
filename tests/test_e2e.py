#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端联调测试
==============

同一台机器上模拟两位异地用户：
- 真实启动中继服务器（server/server.py）
- 启动两个"假 PotPlayer"窗口（不同窗口类名，模拟两台电脑上的播放器）
- 两套 NetClient + SyncEngine（与真实客户端同款代码）

验证：A 在自己播放器里 暂停 / 恢复 / 拖动进度（含暂停中拖动），
      B 的播放器在数秒内自动跟随。

运行：python tests/test_e2e.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 本机开启系统代理时，强制本地回环地址直连（urllib/websockets 均读取该变量）
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from PyQt5.QtCore import QCoreApplication  # noqa: E402

from net_client import NetClient            # noqa: E402
from pot_bridge import STATUS_PAUSED, STATUS_RUNNING, PotPlayerBridge  # noqa: E402
from sync_engine import SyncEngine          # noqa: E402

SERVER = ROOT / "server" / "server.py"
FAKE = ROOT / "tools" / "fake_potplayer.py"
PORT = 18766
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
    server = subprocess.Popen(
        [sys.executable, str(SERVER), "--host", HOST, "--port", str(PORT),
         "--log-level", "WARNING"])
    fake_a = subprocess.Popen([sys.executable, str(FAKE),
                               "--class", "FakePotE2EA", "--playlist",
                               "ep01.mkv:600000,ep02.mkv:610000,ep03.mkv:620000"])
    fake_b = subprocess.Popen([sys.executable, str(FAKE),
                               "--class", "FakePotE2EB", "--playlist",
                               "ep01.mkv:600000,ep02.mkv:610000,ep03.mkv:620000"])
    try:
        # 独立类名，避免与本机真实运行的 PotPlayer 冲突
        bridge_a = PotPlayerBridge(("FakePotE2EA",))
        bridge_b = PotPlayerBridge(("FakePotE2EB",))
        assert wait_for(lambda: bridge_a.available() and bridge_b.available(),
                        desc="假播放器启动")
        print("PASS  两个假播放器就绪")

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

        conn = {"a": False, "b": False}
        net_a.sigConnected.connect(lambda: conn.__setitem__("a", True))
        net_b.sigConnected.connect(lambda: conn.__setitem__("b", True))
        net_a.connect_to(HOST, PORT)
        net_b.connect_to(HOST, PORT)
        assert wait_for(lambda: conn["a"] and conn["b"], desc="连接服务器")
        print("PASS  两个客户端已连接服务器")

        # A 建房
        created = {}
        def _a_created(room, state, members):
            created.update(room=room)
            eng_a.enter_room(state)
        net_a.sigRoomCreated.connect(_a_created)
        net_a.create_room("小明")
        assert wait_for(lambda: "room" in created, desc="建房")
        room = created["room"]
        # 等 A 把本地播放状态上报为房间初始状态
        assert wait_for(lambda: eng_a.room_state is not None, 6, "上报初始状态")
        print(f"PASS  A 创建房间 {room} 并上报初始状态")

        # B 加入，应自动获得并对齐房间状态
        joined = {}
        def _b_joined(r, state, members):
            joined.update(room=r, state=state)
            eng_b.enter_room(state)
        net_b.sigRoomJoined.connect(_b_joined)
        net_b.join_room(room, "小红")
        assert wait_for(lambda: joined.get("state") is not None, desc="B 加入")
        print("PASS  B 加入房间并收到房间状态")

        # ① A 在播放器里直接按暂停 → B 应跟随暂停
        bridge_a.pause()
        assert wait_for(lambda: bridge_b.status() == STATUS_PAUSED, 8,
                        "B 跟随暂停"), "B 未跟随暂停"
        print("PASS  暂停同步：A 暂停 → B 暂停")

        # ② A 恢复播放 → B 应跟随播放
        bridge_a.play()
        assert wait_for(lambda: bridge_b.status() == STATUS_RUNNING, 8,
                        "B 跟随播放"), "B 未跟随播放"
        print("PASS  播放同步：A 播放 → B 播放")

        # ③ 播放中 A 拖动进度到 200s → B 对齐（容差 4s）
        bridge_a.seek_ms(200000)
        ok = wait_for(
            lambda: abs((bridge_b.position_ms() or 0)
                        - (bridge_a.position_ms() or 0)) < 4000, 8,
                        "B 跟随进度跳转")
        assert ok, f"B 进度未对齐: A={bridge_a.position_ms()} B={bridge_b.position_ms()}"
        print(f"PASS  进度同步（播放中拖动）: A={bridge_a.position_ms()}ms "
              f"B={bridge_b.position_ms()}ms")

        # ④ 暂停中 A 拖动进度到 400s → B 对齐
        bridge_a.pause()
        assert wait_for(lambda: bridge_b.status() == STATUS_PAUSED, 8)
        bridge_a.seek_ms(400000)
        ok = wait_for(
            lambda: abs((bridge_b.position_ms() or 0) - 400000) < 4000, 8,
                        "暂停中拖动")
        assert ok, f"暂停中拖动未对齐: B={bridge_b.position_ms()}"
        print(f"PASS  进度同步（暂停中拖动）: B={bridge_b.position_ms()}ms")

        # ⑤ 最终一致：人为把 B 拉偏 10s，房间应自动重新收敛
        #    （B 的本地侦测把跳变广播出去、A 跟随；或 B 的看门狗拉回——任一均可）
        eng_b._guard_until = 0
        bridge_b.seek_ms(410000)
        ok = wait_for(
            lambda: abs((bridge_b.position_ms() or 0) - (bridge_a.position_ms() or 0)) < 4000
            and bridge_a.status() == STATUS_PAUSED and bridge_b.status() == STATUS_PAUSED,
            12, "重新收敛")
        assert ok, f"未收敛: A={bridge_a.position_ms()} B={bridge_b.position_ms()}"
        print(f"PASS  异常跳变后重新收敛: A={bridge_a.position_ms()}ms "
              f"B={bridge_b.position_ms()}ms")

        # ⑥ 切集同步：A 下一集 → B 跟随到 ep02；A 上一集 → B 回到 ep01
        eng_a.user_episode(+1)
        ok = wait_for(lambda: "ep02.mkv" in bridge_b.media_name(), 12,
                      "B 跟随下一集")
        assert ok, f"B 未跟随下一集: {bridge_b.media_name()!r}"
        assert bridge_b.duration_ms() == 610000
        print(f"PASS  切集同步（下一集）: B 当前 {bridge_b.media_name()!r}")

        eng_a.user_episode(-1)
        ok = wait_for(lambda: "ep01.mkv" in bridge_b.media_name(), 12,
                      "B 跟随上一集")
        assert ok, f"B 未跟随上一集: {bridge_b.media_name()!r}"
        assert bridge_b.duration_ms() == 600000
        print(f"PASS  切集同步（上一集）: B 当前 {bridge_b.media_name()!r}")

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
