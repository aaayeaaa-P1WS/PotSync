#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotPlayer 控制桥测试
====================

启动一个假的 PotPlayer 窗口（tools/fake_potplayer.py），
验证 pot_bridge 的查询与控制：状态读取、进度读取、暂停/播放、seek、媒体名。

运行：python tests/test_bridge.py
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from pot_bridge import STATUS_PAUSED, STATUS_RUNNING, PotPlayerBridge  # noqa: E402

FAKE = ROOT / "tools" / "fake_potplayer.py"
FAKE_CLASS = "FakePotTestA"   # 独立类名，避免与本机真实运行的 PotPlayer 冲突


def wait(cond, timeout=6.0, interval=0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    proc = subprocess.Popen([sys.executable, str(FAKE), "--class", FAKE_CLASS,
                             "--playlist",
                             "ep01.mkv:60000,ep02.mkv:61000,ep03.mkv:62000"])
    try:
        bridge = PotPlayerBridge((FAKE_CLASS,))
        assert wait(lambda: bridge.available()), "未找到假 PotPlayer 窗口"

        snap = bridge.snapshot()
        assert snap is not None
        assert snap.duration == 60000, f"总时长错误: {snap.duration}"
        assert snap.status == STATUS_RUNNING, f"初始应为播放中: {snap.status}"
        assert "ep01.mkv" in snap.media, f"媒体名错误: {snap.media!r}"
        print("PASS  窗口发现 / 状态 / 总时长 / 媒体名")

        # 播放中进度应前进
        p1 = bridge.position_ms()
        time.sleep(0.6)
        p2 = bridge.position_ms()
        assert p2 > p1, f"播放中进度未前进: {p1} -> {p2}"
        print(f"PASS  播放进度推进 ({p1}ms → {p2}ms)")

        # 暂停
        assert bridge.pause()
        assert wait(lambda: bridge.status() == STATUS_PAUSED), "暂停未生效"
        q1 = bridge.position_ms()
        time.sleep(0.4)
        q2 = bridge.position_ms()
        assert abs(q2 - q1) <= 50, f"暂停后进度仍在移动: {q1} -> {q2}"
        print("PASS  暂停 & 进度冻结")

        # seek
        assert bridge.seek_ms(30000)
        assert wait(lambda: abs((bridge.position_ms() or 0) - 30000) < 300), \
            f"seek 未生效: {bridge.position_ms()}"
        print("PASS  跳转 30.0s")

        # 恢复播放
        assert bridge.play()
        assert wait(lambda: bridge.status() == STATUS_RUNNING), "恢复播放未生效"
        r1 = bridge.position_ms()
        time.sleep(0.4)
        assert bridge.position_ms() > r1, "恢复播放后进度未前进"
        print("PASS  恢复播放")

        # 下一集：媒体名与时长切换、进度归零
        assert bridge.next()
        assert wait(lambda: "ep02.mkv" in bridge.media_name()), "下一集未生效"
        assert bridge.duration_ms() == 61000, f"新集时长错误: {bridge.duration_ms()}"
        assert (bridge.position_ms() or 0) < 3000, "切集后进度未归零"
        print("PASS  下一集（媒体/时长切换，进度归零）")

        # 再下一集 → ep03，再上一集 → ep02
        assert bridge.next()
        assert wait(lambda: "ep03.mkv" in bridge.media_name())
        assert bridge.duration_ms() == 62000
        assert bridge.previous()
        assert wait(lambda: "ep02.mkv" in bridge.media_name())
        assert bridge.duration_ms() == 61000
        print("PASS  上一集 / 列表内连续切换")

        # 边界：连续上一集到列表头后不再变化
        assert bridge.previous()
        assert wait(lambda: "ep01.mkv" in bridge.media_name())
        assert bridge.previous()
        time.sleep(0.3)
        assert "ep01.mkv" in bridge.media_name()
        print("PASS  列表边界保护")

        print("\n全部通过 ✔")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
