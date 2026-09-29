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
6. 同目录直开：定位当前文件完整路径 → 同目录找同名文件直接打开，
   零轮巡一次切换到位（场景⑨）
7. 播放列表文件匹配：同目录没有时，直接解析 PotPlayer 播放列表文件
   （.dpl）拿到各项完整路径精准打开（场景⑩）

运行：python tests/test_follow.py
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 本机开启系统代理时，强制本地回环地址直连
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

import win32gui                            # noqa: E402
from PyQt5.QtCore import QCoreApplication  # noqa: E402

from net_client import NetClient                       # noqa: E402
from pot_bridge import (STATUS_PAUSED, STATUS_RUNNING,  # noqa: E402
                        PotPlayerBridge, Snapshot,
                        find_media_in_dir, media_matches, normalize_media)
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
        assert wait_for(lambda: eng_b._search is None, 10, "搜索①收尾")
        print(f"PASS  入房自动匹配：B 从 ep03 自动切到 {bridge_b.media_name()!r}")

        # ② A 在播放器里直接切下一集（模拟快捷键/播放器按钮，不经引擎）
        bridge_a.next()     # ep01 → ep02
        ok = wait_for(lambda: media_matches(bridge_b.media_name(), "ep02.mkv"), 20,
                      "B 跟随 ep02")
        assert ok, f"B 未跟随播放器内切集: 当前 {bridge_b.media_name()!r}"
        assert bridge_b.duration_ms() == 610000
        assert wait_for(lambda: eng_b._search is None, 10, "搜索②收尾")
        print(f"PASS  播放器内切集跟随：A 切 ep02 → B 自动切到 "
              f"{bridge_b.media_name()!r}")

        # ③ A 再切到 ep03：B 当前 ep02 已在自身列表末尾（不循环）→ 掉头反搜命中
        bridge_a.next()     # ep02 → ep03
        ok = wait_for(lambda: media_matches(bridge_b.media_name(), "ep03.mkv"), 25,
                      "B 掉头反搜 ep03")
        assert ok, f"B 反搜未命中: 当前 {bridge_b.media_name()!r}"
        assert bridge_b.duration_ms() == 620000
        assert wait_for(lambda: eng_b._search is None, 10, "搜索③收尾")
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

        # ⑥ 慢加载真实场景：文件打开需 900ms（标题滞后），仍能正确匹配且不误判尽头
        fake_c = subprocess.Popen(
            [sys.executable, str(FAKE), "--class", "FakePotSlowC", "--playlist",
             "ep02.mkv:610000,ep01.mkv:600000,ep03.mkv:620000",
             "--load-delay", "900"])
        fake_d = subprocess.Popen(
            [sys.executable, str(FAKE), "--class", "FakePotSlowD", "--playlist",
             "ep03.mkv:620000,ep01.mkv:600000,ep02.mkv:610000",
             "--load-delay", "900"])
        try:
            bridge_c = PotPlayerBridge(("FakePotSlowC",))
            bridge_d = PotPlayerBridge(("FakePotSlowD",))
            assert wait_for(lambda: bridge_c.available() and bridge_d.available(),
                            desc="慢加载假播放器启动")
            net_c, net_d = NetClient(), NetClient()
            eng_c = SyncEngine(bridge_c, net_c)
            eng_d = SyncEngine(bridge_d, net_d)
            eng_c.sigBroadcastState.connect(
                lambda s: net_c.send_state(s["paused"], s["position"], s["media"],
                                           s["duration"], s["action"]))
            eng_d.sigBroadcastState.connect(
                lambda s: net_d.send_state(s["paused"], s["position"], s["media"],
                                           s["duration"], s["action"]))
            net_c.sigState.connect(eng_c.apply_remote_state)
            net_d.sigState.connect(eng_d.apply_remote_state)
            conn2 = {"c": False, "d": False}
            net_c.sigConnected.connect(lambda: conn2.__setitem__("c", True))
            net_d.sigConnected.connect(lambda: conn2.__setitem__("d", True))
            net_c.connect_to(HOST, PORT)
            net_d.connect_to(HOST, PORT)
            assert wait_for(lambda: conn2["c"] and conn2["d"], desc="慢场景连接")

            created2 = {}
            net_c.sigRoomCreated.connect(
                lambda r, st, m: (created2.update(room=r), eng_c.enter_room(st)))
            net_c.create_room("小慢")
            assert wait_for(lambda: "room" in created2, desc="慢场景建房")
            assert wait_for(lambda: eng_c.room_state is not None, 6, "慢场景初始状态")
            joined2 = {}
            net_d.sigRoomJoined.connect(
                lambda r, st, m: (joined2.update(room=r), eng_d.enter_room(st)))
            net_d.join_room(created2["room"], "小加载")
            # d 在 ep03(index0)，房间 ep02 → 正向 2 步命中（加载 900ms < 步超时 3.5s）
            ok = wait_for(lambda: media_matches(bridge_d.media_name(), "ep02.mkv"),
                          25, "慢加载匹配 ep02")
            assert ok, f"慢加载未匹配: 当前 {bridge_d.media_name()!r}"
            assert wait_for(lambda: eng_d._search is None, 10, "慢加载搜索收尾")
            print("PASS  慢加载匹配：900ms 加载延迟下正向 2 步命中 ep02（未误判尽头）")

            # c 切到 ep03：d 当前 ep02(自身列表末尾) → 正向尽头探测 → 掉头反搜命中
            bridge_c.next()      # ep02(index0) → 累积 +2 → ep03(index2)
            bridge_c.next()
            ok = wait_for(lambda: media_matches(bridge_d.media_name(), "ep03.mkv"),
                          40, "慢加载反搜 ep03")
            assert ok, f"慢加载反搜未命中: 当前 {bridge_d.media_name()!r}"
            print("PASS  慢加载反搜：真实加载耗时下掉头搜索仍正确命中 ep03")
        finally:
            for p in (fake_c, fake_d):
                p.terminate()
            for p in (fake_c, fake_d):
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()

        # ⑦ 播放器对上/下一集完全无反应（卡死/弹窗挡住）：有界中止，绝不轰炸
        fake_e = subprocess.Popen(
            [sys.executable, str(FAKE), "--class", "FakePotStuckE", "--playlist",
             "ep02.mkv:610000,ep01.mkv:600000", "--ignore-nav"])
        try:
            bridge_e = PotPlayerBridge(("FakePotStuckE",))
            assert wait_for(bridge_e.available, desc="无响应假播放器启动")
            eng_e = SyncEngine(bridge_e, NetClient())
            logs_e = []
            eng_e.sigLog.connect(logs_e.append)
            eng_e._start_media_follow({"media": "ep01.mkv", "action": "open",
                                       "paused": False, "position": 0,
                                       "duration": 600000, "ts": 0})
            # 两个方向各 1 步×(3.5s 等待 + 3.5s 补发) ≈ 14s 内有界收尾
            ok = wait_for(lambda: any("未找到" in m for m in logs_e), 30,
                          "无响应有界中止")
            assert ok, f"未按预期中止。日志: {logs_e[-5:]}"
            assert wait_for(lambda: eng_e._search is None, 15, "搜索状态清理")
            assert fake_e.poll() is None, "假播放器进程异常退出"
            assert media_matches(bridge_e.media_name(), "ep02.mkv"), \
                f"起点媒体被意外改动: {bridge_e.media_name()!r}"
            print("PASS  无响应保护：播放器不执行切换时 14s 内有界收尾，进程存活")
        finally:
            fake_e.terminate()
            try:
                fake_e.wait(timeout=3)
            except subprocess.TimeoutExpired:
                fake_e.kill()

        # ⑧ SendMessage 全面超时（播放器已彻底卡死）：立即中止，绝不再发命令
        class DeadBridge:
            def snapshot(self):
                return Snapshot(status=STATUS_PAUSED, position=0,
                                duration=600000, media="ep01.mkv")
            def media_name(self):
                return "ep01.mkv"
            def pause(self):
                return True
            def play(self):
                return True
            def seek_ms(self, p):
                return True
            def next(self):
                return False
            def previous(self):
                return False

        eng_x = SyncEngine(DeadBridge(), NetClient())
        logs_x = []
        eng_x.sigLog.connect(logs_x.append)
        eng_x._start_media_follow({"media": "ep02.mkv", "action": "open",
                                   "paused": False, "position": 0,
                                   "duration": 610000, "ts": 0})
        ok = wait_for(lambda: eng_x._search is None
                      and any("未响应" in m for m in logs_x), 8, "卡死立即中止")
        assert ok, f"卡死未立即中止。日志: {logs_x}"
        print("PASS  卡死保护：命令超时立即中止轮巡，不再向播放器发任何命令")

        # ⑨ 同目录直开：定位当前文件完整路径 → 同目录找同名文件直接打开，零轮巡
        tmpdir = tempfile.mkdtemp(prefix="potsync_dir_")
        for n in ("ep01.mkv", "ep02.mkv", "ep03.mkv"):
            open(os.path.join(tmpdir, n), "w").close()
        ep01_path = os.path.join(tmpdir, "ep01.mkv")
        # find_media_in_dir 规范化单测
        assert find_media_in_dir(tmpdir, "ep01") == ep01_path
        assert find_media_in_dir(tmpdir, "ep01v2") == ""      # 前缀相似不误判
        assert find_media_in_dir(tmpdir, "ep09") == ""        # 不存在
        open(os.path.join(tmpdir, "note.txt"), "w").close()
        assert find_media_in_dir(tmpdir, "note") == ""        # 非视频扩展名不匹配
        tmpdir2 = tempfile.mkdtemp(prefix="potsync_dir2_")
        ep02_up = os.path.join(tmpdir2, "EP02 .MKV")
        open(ep02_up, "w").close()
        assert find_media_in_dir(tmpdir2, "ep02") == ep02_up  # 大小写/空白规范化命中
        print("PASS  目录扫描：同名视频命中，相似名/非视频/缺失均不误判")

        class DirBridge(PotPlayerBridge):
            """模拟可定位当前文件路径的桥：直开时改写窗口标题（等价换了文件）。"""
            def __init__(self, classes, cur_path):
                super().__init__(classes)
                self._cur_path = cur_path
                self.opened = []

            def current_media_path(self):
                return self._cur_path

            def open_file(self, path):
                self.opened.append(path)
                win32gui.SetWindowText(
                    self.find_window(), os.path.basename(path) + " - PotPlayer")
                return True

        fake_g = subprocess.Popen(
            [sys.executable, str(FAKE), "--class", "FakePotDirG", "--paused",
             "--playlist", "ep03.mkv:620000"])
        try:
            bridge_g = DirBridge(("FakePotDirG",), os.path.join(tmpdir, "ep03.mkv"))
            assert wait_for(bridge_g.available, desc="直开场景假播放器启动")
            eng_g = SyncEngine(bridge_g, NetClient())
            eng_g._start_media_follow({"media": "ep01.mkv", "action": "open",
                                       "paused": True, "position": 5000,
                                       "duration": 600000, "ts": 0})
            assert bridge_g.opened == [ep01_path], \
                f"未直开同目录文件: {bridge_g.opened}"
            assert eng_g._search is None, "直开成功不应进入轮巡"
            ok = wait_for(lambda: media_matches(bridge_g.media_name(), "ep01.mkv")
                          and bridge_g.position_ms() == 5000
                          and bridge_g.status() == STATUS_PAUSED,
                          8, "直开后对齐")
            assert ok, (f"直开后未对齐: media={bridge_g.media_name()!r} "
                        f"pos={bridge_g.position_ms()}")
            print("PASS  同目录直开：零轮巡一次切换到位，并按房间状态对齐进度/暂停")
        finally:
            fake_g.terminate()
            try:
                fake_g.wait(timeout=3)
            except subprocess.TimeoutExpired:
                fake_g.kill()

        # ⑩ 播放列表文件匹配：同目录没有目标，但 .dpl 记录的各项里有 → 精准直开
        from pot_bridge import parse_dpl
        # parse_dpl 单测：BOM / playname / N*file* / 忽略 duration/start 行
        dpl_path = os.path.join(tmpdir, "PotPlayerMini64.dpl")
        with open(dpl_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("DAUMPLAYLIST\r\n")
            f.write(f"playname={tmpdir2}\\ep03.mkv\r\n")
            f.write("topindex=0\r\nsaveplaypos=0\r\n")
            f.write(f"1*file*{tmpdir}\\ep01.mkv\r\n")
            f.write("1*duration2*1420063\r\n1*start*3043\r\n")
            f.write(f"2*file*{tmpdir}\\ep02.mkv\r\n")
            f.write("3*file*D:\\音乐\\song.mp3\r\n")      # 非视频项
        parsed = parse_dpl(dpl_path)
        assert parsed["playname"].lower().endswith("ep03.mkv")
        assert len(parsed["files"]) == 3
        assert parsed["files"][0].endswith("ep01.mkv")
        assert parse_dpl(os.path.join(tmpdir, "note.txt"))["files"] == []
        assert parse_dpl(r"X:\不存在的\none.dpl")["files"] == []
        print("PASS  .dpl 解析：BOM/playname/列表项/非视频保留/坏文件容错")

        class PlBridge(PotPlayerBridge):
            """当前文件在一个没有目标的目录，但播放列表里有目标文件。"""
            def __init__(self, classes, cur_path, pl_entries):
                super().__init__(classes)
                self._cur_path = cur_path
                self._pl = pl_entries
                self.opened = []

            def current_media_path(self):
                return self._cur_path

            def playlist_files(self):
                return list(self._pl)

            def open_file(self, path):
                self.opened.append(path)
                win32gui.SetWindowText(
                    self.find_window(), os.path.basename(path) + " - PotPlayer")
                return True

        other_dir = tempfile.mkdtemp(prefix="potsync_other_")
        open(os.path.join(other_dir, "ep03.mkv"), "w").close()   # 只有 ep03
        fake_h = subprocess.Popen(
            [sys.executable, str(FAKE), "--class", "FakePotPlH", "--paused",
             "--playlist", "ep03.mkv:620000"])
        try:
            bridge_h = PlBridge(
                ("FakePotPlH",), os.path.join(other_dir, "ep03.mkv"),
                [os.path.join(tmpdir, "ep01.mkv"),
                 os.path.join(tmpdir, "ep02.mkv"),
                 os.path.join(tmpdir, "ep03.mkv")])
            assert wait_for(bridge_h.available, desc="播放列表场景假播放器启动")
            eng_h = SyncEngine(bridge_h, NetClient())
            eng_h._start_media_follow({"media": "ep01.mkv", "action": "open",
                                       "paused": True, "position": 3000,
                                       "duration": 600000, "ts": 0})
            assert bridge_h.opened == [os.path.join(tmpdir, "ep01.mkv")], \
                f"未按播放列表精准直开: {bridge_h.opened}"
            assert eng_h._search is None, "播放列表直开成功不应进入轮巡"
            ok = wait_for(lambda: media_matches(bridge_h.media_name(), "ep01.mkv")
                          and bridge_h.position_ms() == 3000, 8, "直开后对齐")
            assert ok, f"直开后未对齐: {bridge_h.media_name()!r}"
            print("PASS  播放列表文件匹配：同目录没有 → 读 .dpl 各项精准直开，零轮巡")
        finally:
            fake_h.terminate()
            try:
                fake_h.wait(timeout=3)
            except subprocess.TimeoutExpired:
                fake_h.kill()

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
