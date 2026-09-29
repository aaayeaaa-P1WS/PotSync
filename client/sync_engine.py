#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同步引擎
========

职责：
1. 每 400ms 采样一次本地 PotPlayer 状态（播放/暂停、进度、媒体、总时长）；
2. 侦测"本地用户操作"（在 PotPlayer 里直接按了暂停/播放、拖了进度、换了片子）
   → 广播给房间其他人；
3. 收到他人广播的播放状态 → 应用到本地 PotPlayer（含网络时间补偿）；
4. 周期漂移校准：播放中若本地进度与房间应有进度偏差超过阈值则重新对齐。

时钟：房间状态携带服务器时间戳 ts；server_now() 来自 NetClient 的时钟偏移估算，
      因此 target = position + (server_now - ts) 即"此刻应有进度"。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from pot_bridge import (STATUS_PAUSED, STATUS_RUNNING, PotPlayerBridge, Snapshot,
                        find_media_in_dir, media_matches, normalize_media)

log = logging.getLogger("potsync.sync")

POLL_INTERVAL_MS = 400
WATCHDOG_INTERVAL_MS = 2500

SEEK_DETECT_MS = 1800       # 两次采样间进度突变超过此值视为"用户拖动进度条"
APPLY_SEEK_MIN_MS = 900     # 应用远程状态时，偏差超过此值才真正 seek
DRIFT_CORRECT_MS = 2800     # 漂移校准阈值
GUARD_SECONDS = 2.0         # 应用远程状态后的静默期（避免把"自己执行远程指令"误判为本地操作）
MEDIA_DIFF_MS = 1500        # 总时长差超过此值认为双方媒体不一致

# 文件名自动匹配：优先"零轮巡直开"——不经过播放器逐集试探，直接在文件系统
# 层面定位同名文件（当前文件同目录 → PotPlayer 播放列表文件 .dpl → 已学习
# 目录），找到后启动 PotPlayer 打开，等价用户双击，单次切换零压力；
# 全部找不到时才回退到播放列表轮巡。轮巡是兜底路径，节奏放得更温和：
# 真实 PotPlayer 打开文件需要 0.5~3 秒；切换命令必须等标题真正变化、且文件
# 加载就绪（时长可用）后再发下一条，否则命令在播放器消息队列里堆积，
# 会把 PotPlayer 刷成"未响应"甚至空指针崩溃。
SEARCH_POLL_MS = 150          # 轮巡中标题轮询间隔
SEARCH_SETTLE_MS = 800        # 标题变化后的安定等待（加载未完全就绪，缓一步再切）
SEARCH_STEP_TIMEOUT_MS = 3500 # 单步等待标题变化上限（超过视为已到列表尽头/文件缺失）
SEARCH_RADIUS = 40            # 单方向搜索半径（步数上限）
SEARCH_TOTAL_S = 150.0        # 轮巡总时长预算（秒），超时整体中止


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


class SyncEngine(QObject):
    # 给 UI 的信号
    sigLocalState = pyqtSignal(object)     # Snapshot | None
    sigSyncInfo = pyqtSignal(str)          # "已同步" / "偏差 +1.2s" / "媒体不一致" ...
    sigLog = pyqtSignal(str)
    # 给 NetClient 的信号（MainWindow 负责连接）
    sigBroadcastState = pyqtSignal(dict)   # {paused, position, media, duration, action}

    def __init__(self, bridge: PotPlayerBridge, net, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.net = net                     # NetClient，提供 server_now_ms()
        self.in_room = False
        self.room_state: Optional[dict] = None   # 房间最近共享状态（含 ts）
        self._last: Optional[Snapshot] = None
        self._last_mono: float = 0.0
        self._guard_until: float = 0.0
        self._announced_local = False      # 入房后是否已上报过本地状态
        self._mismatch_since: float = 0.0  # 看门狗首次发现不一致的时刻（二次确认用）
        self._search: Optional[dict] = None    # 文件名匹配轮巡状态（None=未在搜索）
        self._failed_media = ""            # 最近一次匹配失败的媒体名（规范化），避免反复全表轮巡
        self._media_dirs: list = []        # 已知媒体目录（MRU，最多 8 个），同目录直开用
        self._direct_failed = ""           # 直开匹配失败的媒体名（规范化），防直开→轮巡→直开死循环

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setInterval(WATCHDOG_INTERVAL_MS)
        self._watchdog_timer.timeout.connect(self._watchdog)
        self._watchdog_timer.start()

    # ---------- 房间状态 ----------

    def enter_room(self, state: Optional[dict]) -> None:
        self.in_room = True
        self.room_state = None
        self._announced_local = False
        self._last = None
        self._mismatch_since = 0.0
        self._search = None
        self._failed_media = ""
        self._media_dirs = []
        self._direct_failed = ""
        if state:
            self.apply_remote_state(state, initial=True)

    def leave_room(self) -> None:
        self.in_room = False
        self.room_state = None
        self._last = None
        self._mismatch_since = 0.0
        self._search = None
        self._failed_media = ""
        self._media_dirs = []
        self._direct_failed = ""

    def _cancel_search(self) -> None:
        """本地用户主动操作时取消正在进行的匹配跟随（本地意图优先）。"""
        if self._search is not None:
            self._search = None
            self.sigLog.emit("已取消自动匹配（本地操作优先）")
        self._failed_media = ""
        self._direct_failed = ""

    def server_now(self) -> int:
        try:
            return self.net.server_now_ms()
        except Exception:
            return int(time.time() * 1000)

    def _guarded(self) -> bool:
        return time.monotonic() < self._guard_until

    def _guard(self, seconds: float = GUARD_SECONDS) -> None:
        self._guard_until = time.monotonic() + seconds

    # ---------- 本地操作（UI 按钮 / 也涵盖直接在 PotPlayer 里操作） ----------

    def user_toggle_play_pause(self) -> None:
        snap = self.bridge.snapshot()
        if snap is None:
            self.sigLog.emit("未检测到 PotPlayer，请先打开播放器")
            return
        self._cancel_search()
        self._guard()
        if snap.playing:
            self.bridge.pause()
        else:
            self.bridge.play()
        self._broadcast(
            paused=snap.playing, position=snap.position,
            media=snap.media, duration=snap.duration,
            action="pause" if snap.playing else "play")

    def user_seek(self, position_ms: int) -> None:
        snap = self.bridge.snapshot()
        if snap is None:
            return
        self._cancel_search()
        if snap.duration:
            position_ms = clamp(position_ms, 0, snap.duration)
        self._guard()
        self.bridge.seek_ms(position_ms)
        self._broadcast(paused=not snap.playing, position=position_ms,
                        media=snap.media, duration=snap.duration, action="seek")

    def user_force_sync_to_me(self) -> None:
        """以"我"当前进度为准，让全房间对齐。"""
        snap = self.bridge.snapshot()
        if snap is None:
            return
        self._broadcast(paused=not snap.playing, position=snap.position,
                        media=snap.media, duration=snap.duration, action="seek")
        self.sigLog.emit("已将房间进度对齐到我当前位置")

    def user_episode(self, direction: int) -> None:
        """切换上一集/下一集并广播（direction: -1 / +1）。"""
        snap = self.bridge.snapshot()
        if snap is None:
            self.sigLog.emit("未检测到 PotPlayer，请先打开播放器")
            return
        self._cancel_search()
        self._guard(3.0)   # 切集后媒体加载更慢，静默期加长
        ok = self.bridge.next() if direction > 0 else self.bridge.previous()
        if not ok:
            self.sigLog.emit("切换失败（PotPlayer 未响应）")
            return
        # 等新媒体加载后再采集名称/时长广播
        QTimer.singleShot(800, lambda: self._broadcast_episode(direction))

    def _broadcast_episode(self, direction: int) -> None:
        snap = self.bridge.snapshot()
        if snap is None:
            return
        self._last, self._last_mono = snap, time.monotonic()
        self._broadcast(paused=not snap.playing, position=snap.position,
                        media=snap.media, duration=snap.duration,
                        action="next" if direction > 0 else "prev")
        self.sigLog.emit(f"已切换到{'下一集' if direction > 0 else '上一集'}："
                         f"{snap.media or '未知'}")

    def _broadcast(self, paused: bool, position: int, media: str,
                   duration: int, action: str) -> None:
        if not self.in_room:
            return
        state = {
            "paused": bool(paused), "position": int(position),
            "media": media, "duration": int(duration), "action": action,
            "ts": self.server_now(),   # 本地估算的服务器时间，用于本地显示
        }
        self.room_state = state          # 自己就是最新状态源
        self.sigBroadcastState.emit(state)

    # ---------- 远程状态应用 ----------

    def apply_remote_state(self, s: dict, initial: bool = False) -> None:
        # 正在匹配跟随中：轮巡期间只更新目标为最新状态，不打断搜索
        if self._search is not None:
            self.room_state = dict(s)
            sr = self._search
            if sr.get("phase") != "return":
                # 若当前已落在旧目标上（收尾节拍未跑但标题已到），以它为新的
                # 返回锚点重新出发——返回点应始终是用户此刻实际看到的影片
                cur = self.bridge.media_name()
                old_target = (sr["state"].get("media") or "").strip()
                if old_target and media_matches(cur, old_target):
                    sr["start"] = cur
                    sr["phase"] = "fwd"
                    sr["direction"] = -1 if s.get("action") == "prev" else 1
                    sr["phase_steps"] = 0
                    sr["offset"] = 0
                    sr["await"] = None
                sr["state"] = dict(s)
            return

        # 切集动作：先跟随切换，再按状态对齐
        if s.get("action") in ("next", "prev") and not initial:
            self._apply_remote_episode(s)
            return

        self.room_state = dict(s)
        snap = self.bridge.snapshot()
        if snap is None:
            self.sigLog.emit("收到同步状态，但本机 PotPlayer 未运行")
            return

        # 对方媒体与本机不同名 → 在本机播放列表按文件名自动匹配切换
        remote_media = (s.get("media") or "").strip()
        if remote_media and snap.media and not media_matches(remote_media, snap.media):
            if self._failed_media and \
                    normalize_media(remote_media) == self._failed_media:
                pass   # 该文件已找过且没有：走下方旧逻辑（仅播放/暂停跟随 + 提示）
            else:
                self._start_media_follow(dict(s))
                return

        self._guard()
        paused = bool(s.get("paused", True))
        position = int(s.get("position", 0))
        duration = int(s.get("duration", 0))
        ts = int(s.get("ts", self.server_now()))

        media_mismatch = (
            duration and snap.duration and abs(duration - snap.duration) > MEDIA_DIFF_MS
        )
        if media_mismatch:
            media = s.get("media") or "未知媒体"
            if s.get("action") == "open":
                self.sigLog.emit(
                    f"⚠ 对方切换到了《{media}》，本机未自动跟随："
                    "请确认 PotPlayer 播放列表一致后手动切到同一集")
            else:
                self.sigLog.emit(
                    f"⚠ 媒体可能不一致：对方时长 {duration / 1000:.0f}s，"
                    f"本机 {snap.duration / 1000:.0f}s，仅同步播放/暂停")
        else:
            target = position if paused else position + max(0, self.server_now() - ts)
            hi = duration or snap.duration or target
            target = clamp(target, 0, hi)
            if initial or abs(snap.position - target) > APPLY_SEEK_MIN_MS:
                if self.bridge.seek_ms(target):
                    self.sigLog.emit(f"进度对齐 → {target / 1000:.1f}s")

        if paused and snap.status != STATUS_PAUSED:
            self.bridge.pause()
        elif not paused and snap.status != STATUS_RUNNING:
            self.bridge.play()

        self._last = self.bridge.snapshot()
        self._last_mono = time.monotonic()

    def _apply_remote_episode(self, s: dict) -> None:
        """跟随对方切换上一集/下一集，加载完成后按房间状态对齐。"""
        self.room_state = dict(s)
        self._guard(3.0)
        direction = 1 if s.get("action") == "next" else -1
        ok = self.bridge.next() if direction > 0 else self.bridge.previous()
        if ok:
            self.sigLog.emit(f"跟随切换{'下一集' if direction > 0 else '上一集'}…")
        else:
            self.sigLog.emit("切换失败（PotPlayer 未响应）")
            return
        QTimer.singleShot(900, lambda: self._align_after_episode(dict(s)))

    def _align_after_episode(self, s: dict) -> None:
        snap = self.bridge.snapshot()
        if snap is None:
            return
        # 盲目跟随切集后落点不对（双方播放列表顺序不同）→ 改按文件名匹配
        remote_media = (s.get("media") or "").strip()
        if remote_media and snap.media and not media_matches(remote_media, snap.media):
            self._start_media_follow(dict(s))
            return
        self._guard(2.0)
        duration = int(s.get("duration", 0))
        if duration and snap.duration and abs(duration - snap.duration) > MEDIA_DIFF_MS:
            self.sigLog.emit(
                f"⚠ 跟随切换后媒体不一致（对方《{s.get('media') or '未知'}》，"
                f"本机《{snap.media or '未知'}》）：请确认双方播放列表一致")
        else:
            position = int(s.get("position", 0))
            if position > APPLY_SEEK_MIN_MS and abs(snap.position - position) > APPLY_SEEK_MIN_MS:
                self.bridge.seek_ms(position)
        paused = bool(s.get("paused", True))
        if paused and snap.status != STATUS_PAUSED:
            self.bridge.pause()
        elif not paused and snap.status != STATUS_RUNNING:
            self.bridge.play()
        self._last = self.bridge.snapshot()
        self._last_mono = time.monotonic()

    # ---------- 文件名自动匹配跟随 ----------

    def _start_media_follow(self, s: dict) -> None:
        """在本机播放列表中轮巡查找与对方同名的文件并切换过去（自适应步进状态机）。

        关键安全约束：同一时刻最多只有一条切换命令在途——必须等窗口标题真正
        变化（或超时）才发下一条，否则快速连发会堵死 PotPlayer 的消息队列，
        把播放器刷成"未响应"。
        """
        target = (s.get("media") or "").strip()
        if not target:
            return
        if self._search is not None:            # 已在搜索：仅更新目标
            self._search["state"] = dict(s)
            return
        snap = self.bridge.snapshot()
        if snap is None or not snap.media:
            self.sigLog.emit("本机未在播放任何媒体，无法自动匹配")
            return
        # 首选零轮巡路径：定位当前文件完整路径 → 同目录找同名文件直接打开
        if self._try_direct_match(target, s):
            return
        if snap.playing:
            self.bridge.pause()     # 暂停后轮巡：只加载不解码渲染，压力骤降
        direction = -1 if s.get("action") == "prev" else 1
        self._search = {
            "state": dict(s),          # 最新目标状态（含媒体名/进度/暂停）
            "start": snap.media,       # 轮巡起点（找不到时回到这里）
            "direction": direction,
            "phase": "fwd",            # fwd（正向）→ back（掉头反搜）→ return（返回起点）
            "phase_steps": 0,          # 本阶段已完成的切换步数
            "offset": 0,               # fwd/back 阶段净位移（next=+1，prev=-1），返回起点用
            "await": None,             # {"from", "deadline", "resent"}：等待标题变化中
            "t0": time.monotonic(),
        }
        self.sigLog.emit(f"正在本机播放列表查找《{target}》（已暂停播放，逐集匹配）…")
        self.sigSyncInfo.emit("🔍 匹配媒体中…")
        QTimer.singleShot(0, self._search_tick)

    # ---------- 同目录直开（零轮巡匹配） ----------

    def _learn_path(self, dirpath: str) -> None:
        """记录媒体所在目录（MRU，最多 8 个），供后续同目录查找。"""
        if not dirpath:
            return
        try:
            dirpath = os.path.normpath(dirpath)
        except Exception:
            return
        if dirpath in self._media_dirs:
            self._media_dirs.remove(dirpath)
        self._media_dirs.insert(0, dirpath)
        del self._media_dirs[8:]

    def _try_direct_match(self, target: str, s: dict) -> bool:
        """零轮巡直开：不经过播放器逐集试探，直接在文件系统层面定位同名
        文件并让 PotPlayer 打开（等价用户双击，单次切换，对播放器零压力）。

        查找顺序（全部毫秒级）：
        1. 当前文件所在目录（剧集通常同文件夹，当前路径来自 .dpl 当前项
           或系统句柄枚举）；
        2. PotPlayer 播放列表文件（.dpl）记录的各项完整路径；
        3. 本次会话已学习的媒体目录。
        成功返回 True；都找不到/打开失败返回 False，调用方回退轮巡。"""
        norm = normalize_media(target)
        if not norm or norm == self._direct_failed:
            return False
        try:
            cur_path = self.bridge.current_media_path()
        except Exception as exc:
            log.debug("current_media_path 失败: %s", exc)
            cur_path = ""
        if cur_path:
            self._learn_path(os.path.dirname(cur_path))
        hit = ""
        for d in list(self._media_dirs):
            hit = find_media_in_dir(d, norm)
            if hit:
                break
        if not hit:
            try:
                hit = self.bridge.find_in_playlist(norm)
            except Exception as exc:
                log.debug("find_in_playlist 失败: %s", exc)
                hit = ""
        if not hit:
            return False
        self._learn_path(os.path.dirname(hit))
        return self._open_matched(hit, s)

    def _open_matched(self, path: str, s: dict) -> bool:
        self._guard(3.0)
        if not self.bridge.open_file(path):
            return False
        self.sigLog.emit(f"✓ 已在同目录找到同名文件，直接打开：{os.path.basename(path)}")
        self.sigSyncInfo.emit("⏳ 加载媒体中…")
        QTimer.singleShot(1600, lambda: self._after_direct_open(path, dict(s)))
        return True

    def _after_direct_open(self, path: str, s: dict) -> None:
        target = (s.get("media") or "").strip()
        norm = normalize_media(target)
        cur = self.bridge.media_name()
        if not media_matches(cur, target):
            # 多实例：文件可能开在了另一个窗口 → 按标题重新绑定一次
            try:
                self.bridge.rebind_to_title(norm)
            except Exception:
                pass
            cur = self.bridge.media_name()
        if not media_matches(cur, target):
            # 直开未生效（播放器拒绝/标题未刷新）：回退到轮巡匹配，
            # 并记住本次直开失败，避免 直开→轮巡→直开 死循环
            self._direct_failed = norm
            self._start_media_follow(s)
            return
        self._direct_failed = ""
        self._guard(2.0)
        self._align_to_state(s)

    def _search_move(self, sr: dict) -> bool:
        """发一条切换命令；False = PotPlayer 消息超时（可能已卡死）。"""
        return self.bridge.next() if sr["direction"] > 0 else self.bridge.previous()

    def _search_tick(self) -> None:
        sr = self._search
        if sr is None:
            return
        s = sr["state"]
        target = (s.get("media") or "").strip()
        if not target:
            self._finish_search(False)
            return
        if time.monotonic() - sr["t0"] > SEARCH_TOTAL_S:
            self.sigLog.emit("⌛ 匹配超时，停止查找")
            self._finish_search(False)
            return
        cur = self.bridge.media_name()
        if media_matches(cur, target):
            self._finish_search(True)
            return

        aw = sr["await"]
        if aw is not None:
            if cur != aw["from"]:
                # 标题已变化，但文件可能还在加载（时长未就绪）——等到就绪或
                # 本步 deadline 再收尾，避免在加载中途又发下一条切换命令
                dur = self.bridge.duration_ms()
                if (not dur or dur <= 0) and time.monotonic() < aw["deadline"]:
                    QTimer.singleShot(SEARCH_POLL_MS, self._search_tick)
                    return
                # 本步完成；稍候片刻等加载安定再走下一步
                sr["await"] = None
                sr["phase_steps"] += 1
                if sr["phase"] != "return":
                    sr["offset"] += sr["direction"]
                    # 循环列表：整圈回到起点 = 未找到
                    if sr["phase"] == "fwd" and media_matches(cur, sr["start"]):
                        self._finish_search(False)
                        return
                QTimer.singleShot(SEARCH_SETTLE_MS, self._search_tick)
                return
            if time.monotonic() < aw["deadline"]:
                QTimer.singleShot(SEARCH_POLL_MS, self._search_tick)
                return
            # 一步内标题始终未变
            sr["await"] = None
            if not aw["resent"]:
                # 命令可能被加载过程吞掉：补发一次再等一轮
                if not self._search_move(sr):
                    self._abort_search_unresponsive()
                    return
                sr["await"] = {
                    "from": cur,
                    "deadline": time.monotonic() + SEARCH_STEP_TIMEOUT_MS / 1000,
                    "resent": True,
                }
                QTimer.singleShot(SEARCH_POLL_MS, self._search_tick)
                return
            # 补发后仍不动：此方向已到尽头（或文件缺失打不开）
            self._search_phase_done()
            return

        # 未处于等待：半径检查 → 发下一步
        if sr["phase_steps"] >= self._search_radius(sr):
            self._search_phase_done()
            return
        if not self._search_move(sr):
            self._abort_search_unresponsive()
            return
        sr["await"] = {
            "from": cur,
            "deadline": time.monotonic() + SEARCH_STEP_TIMEOUT_MS / 1000,
            "resent": False,
        }
        QTimer.singleShot(SEARCH_POLL_MS, self._search_tick)

    @staticmethod
    def _search_radius(sr: dict) -> int:
        if sr["phase"] == "return":
            return abs(sr["offset"]) + 4    # 返回起点只需走来时的步数（留余量）
        return SEARCH_RADIUS

    def _search_phase_done(self) -> None:
        """当前方向搜完（到尽头或超半径）：正向→掉头反搜；反搜→未找到收尾。"""
        sr = self._search
        if sr is None:
            return
        if sr["phase"] == "fwd":
            sr["phase"] = "back"
            sr["direction"] = -sr["direction"]
            sr["phase_steps"] = 0
            sr["await"] = None
            QTimer.singleShot(0, self._search_tick)
            return
        self._finish_search(False)

    def _abort_search_unresponsive(self) -> None:
        """PotPlayer 消息超时（可能卡死）：立即停止轮巡，绝不再发命令。"""
        sr = self._search
        target = ""
        if sr is not None:
            target = (sr["state"].get("media") or "").strip()
        self._search = None
        if target:
            self._failed_media = normalize_media(target)
        self.sigLog.emit("⚠ PotPlayer 未响应，已停止自动匹配；请检查播放器是否卡住")
        self._guard(2.0)
        self._last = self.bridge.snapshot()
        self._last_mono = time.monotonic()

    def _finish_search(self, found: bool) -> None:
        sr = self._search
        if sr is None:
            return
        s = dict(sr["state"])
        if found:
            if sr["phase"] == "return":        # 已回到起点
                self._search = None
                self._guard(1.5)
                self._last = self.bridge.snapshot()
                self._last_mono = time.monotonic()
                return
            self._search = None
            self._failed_media = ""
            cur = self.bridge.media_name()
            self.sigLog.emit(f"✓ 已按文件名自动切换到《{cur}》")
            self._guard(2.5)
            self._align_to_state(s)
            return
        if sr["phase"] != "return":
            # 未找到：缓存失败结果（避免对同一文件反复轮巡），然后返回起点
            target = (s.get("media") or "").strip()
            self._failed_media = normalize_media(target)
            self.sigLog.emit(
                f"⚠ 本机播放列表中未找到《{target}》，正在返回原媒体；"
                "把同名文件加入播放列表后，对方再操作一次即可自动匹配")
            cur = self.bridge.media_name()
            if sr["offset"] == 0 or media_matches(cur, sr["start"]):
                self._search = None
                self._guard(1.5)
                self._last = self.bridge.snapshot()
                self._last_mono = time.monotonic()
                return
            sr["phase"] = "return"
            sr["state"] = dict(s, media=sr["start"])   # 返回阶段目标=起点
            sr["direction"] = -1 if sr["offset"] > 0 else 1
            sr["phase_steps"] = 0
            sr["await"] = None
            QTimer.singleShot(0, self._search_tick)
            return
        # 返回起点也失败（极端情况）：放弃，保持当前位置
        self._search = None
        self._guard(1.5)
        self._last = self.bridge.snapshot()
        self._last_mono = time.monotonic()

    def _align_to_state(self, s: dict) -> None:
        """按房间状态对齐进度与播放/暂停（含服务器时间补偿）。"""
        snap = self.bridge.snapshot()
        if snap is None:
            return
        paused = bool(s.get("paused", True))
        position = int(s.get("position", 0))
        duration = int(s.get("duration", 0))
        ts = int(s.get("ts", self.server_now()))
        target = position if paused else position + max(0, self.server_now() - ts)
        target = clamp(target, 0, duration or snap.duration or target)
        if abs(snap.position - target) > APPLY_SEEK_MIN_MS:
            if self.bridge.seek_ms(target):
                self.sigLog.emit(f"进度对齐 → {target / 1000:.1f}s")
        if paused and snap.status != STATUS_PAUSED:
            self.bridge.pause()
        elif not paused and snap.status != STATUS_RUNNING:
            self.bridge.play()
        self._last = self.bridge.snapshot()
        self._last_mono = time.monotonic()

    # ---------- 周期采样：侦测本地操作 ----------

    def _poll(self) -> None:
        snap = self.bridge.snapshot()
        now = time.monotonic()
        prev, prev_mono = self._last, self._last_mono
        self._last, self._last_mono = snap, now
        self.sigLocalState.emit(snap)
        self._emit_sync_info(snap)

        if snap is None or not self.in_room or self._guarded() \
                or self._search is not None:
            return

        # 入房后，若房间还没有任何状态而本地正在播放 → 由我把本地状态设为房间状态
        if not self._announced_local:
            self._announced_local = True
            if self.room_state is None and snap.duration > 0:
                self._broadcast(paused=not snap.playing, position=snap.position,
                                media=snap.media, duration=snap.duration, action="state")
            return

        if prev is None:
            return

        # 1) 播放/暂停切换
        if snap.status != prev.status and snap.status in (STATUS_PAUSED, STATUS_RUNNING) \
                and prev.status in (STATUS_PAUSED, STATUS_RUNNING):
            self._broadcast(paused=not snap.playing, position=snap.position,
                            media=snap.media, duration=snap.duration,
                            action="pause" if not snap.playing else "play")
            return

        # 2) 换片（媒体名或总时长变化；含在 PotPlayer 里直接切集/双击列表文件）
        if snap.media != prev.media or abs(snap.duration - prev.duration) > 1000:
            if not snap.media:
                return  # 加载中转场（标题尚未刷新），下一拍拿到名字再广播
            self._broadcast(paused=not snap.playing, position=snap.position,
                            media=snap.media, duration=snap.duration, action="open")
            self.sigLog.emit(f"检测到切换媒体：{snap.media or '未知'}")
            return

        # 3) 进度突变（播放或暂停中拖动进度条；状态与媒体均未变）
        elapsed = max(0, int((now - prev_mono) * 1000))
        expected = prev.position + (elapsed + 300 if prev.playing else 0)  # 含轮询冗余
        if abs(snap.position - expected) > SEEK_DETECT_MS:
            self._broadcast(paused=not snap.playing, position=snap.position,
                            media=snap.media, duration=snap.duration, action="seek")

    # ---------- 漂移校准 ----------

    def _watchdog(self) -> None:
        if not self.in_room or not self.room_state or self._guarded() \
                or self._search is not None:
            self._mismatch_since = 0.0
            return
        snap = self.bridge.snapshot()
        if snap is None:
            return
        s = self.room_state
        # 媒体名不同（等待匹配或没有该文件）时不做进度校准
        remote_media = (s.get("media") or "").strip()
        if remote_media and snap.media and not media_matches(remote_media, snap.media):
            self._mismatch_since = 0.0
            return
        duration = int(s.get("duration", 0))
        if duration and snap.duration and abs(duration - snap.duration) > MEDIA_DIFF_MS:
            self._mismatch_since = 0.0
            return  # 媒体不一致时不做进度校准

        paused = bool(s.get("paused", True))
        position = int(s.get("position", 0))
        ts = int(s.get("ts", self.server_now()))

        # 判定需要的校准动作（None = 本地与房间状态一致）
        action = None  # ("play",) / ("pause",) / ("seek", target, drift)
        if not paused and snap.status == STATUS_PAUSED:
            action = ("play",)
        elif paused and snap.status == STATUS_RUNNING:
            action = ("pause",)
        elif not paused and snap.playing:
            expected = clamp(position + max(0, self.server_now() - ts),
                             0, duration or snap.duration or position)
            if abs(snap.position - expected) > DRIFT_CORRECT_MS:
                action = ("seek", expected, snap.position - expected)
        elif paused and snap.status == STATUS_PAUSED and snap.duration:
            if abs(snap.position - position) > DRIFT_CORRECT_MS:
                action = ("seek", clamp(position, 0, snap.duration),
                          snap.position - position)

        if action is None:
            self._mismatch_since = 0.0
            return
        if not self._confirm_mismatch():
            return  # 首次发现：可能是本地刚操作、广播在路上，下轮再纠
        self._guard()
        if action[0] == "play":
            self.bridge.play()
            self.sigLog.emit("校准：恢复播放")
        elif action[0] == "pause":
            self.bridge.pause()
            self.sigLog.emit("校准：暂停")
        else:
            if self.bridge.seek_ms(action[1]):
                self.sigLog.emit(f"校准进度（偏差 {action[2] / 1000:+.1f}s）")

    def _confirm_mismatch(self) -> bool:
        """二次确认：不一致需持续满一个看门狗周期才执行校准，
        避免与用户刚做出、还没来得及广播的本地操作打架。"""
        now = time.monotonic()
        if not self._mismatch_since:
            self._mismatch_since = now
            return False
        if now - self._mismatch_since < WATCHDOG_INTERVAL_MS / 1000:
            return False
        self._mismatch_since = 0.0
        return True

    # ---------- 同步状态展示 ----------

    def _emit_sync_info(self, snap: Optional[Snapshot]) -> None:
        if snap is None:
            self.sigSyncInfo.emit("未检测到 PotPlayer")
            return
        if not self.in_room or not self.room_state:
            self.sigSyncInfo.emit("")
            return
        if self._search is not None:
            self.sigSyncInfo.emit("🔍 匹配媒体中…"
                                  if self._search.get("phase") != "return"
                                  else "↩ 未找到，返回原媒体…")
            return
        s = self.room_state
        remote_media = (s.get("media") or "").strip()
        if remote_media and snap.media and not media_matches(remote_media, snap.media):
            self.sigSyncInfo.emit("⚠ 媒体不一致")
            return
        duration = int(s.get("duration", 0))
        if duration and snap.duration and abs(duration - snap.duration) > MEDIA_DIFF_MS:
            self.sigSyncInfo.emit("⚠ 媒体不一致")
            return
        paused = bool(s.get("paused", True))
        ts = int(s.get("ts", self.server_now()))
        position = int(s.get("position", 0))
        expected = position if paused else position + max(0, self.server_now() - ts)
        drift = snap.position - expected
        if abs(drift) <= 1000:
            self.sigSyncInfo.emit("✓ 已同步")
        else:
            self.sigSyncInfo.emit(f"偏差 {drift / 1000:+.1f}s")
