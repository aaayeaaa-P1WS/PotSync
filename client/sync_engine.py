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
import time
from typing import Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from pot_bridge import STATUS_PAUSED, STATUS_RUNNING, PotPlayerBridge, Snapshot

log = logging.getLogger("potsync.sync")

POLL_INTERVAL_MS = 400
WATCHDOG_INTERVAL_MS = 2500

SEEK_DETECT_MS = 1800       # 两次采样间进度突变超过此值视为"用户拖动进度条"
APPLY_SEEK_MIN_MS = 900     # 应用远程状态时，偏差超过此值才真正 seek
DRIFT_CORRECT_MS = 2800     # 漂移校准阈值
GUARD_SECONDS = 2.0         # 应用远程状态后的静默期（避免把"自己执行远程指令"误判为本地操作）
MEDIA_DIFF_MS = 1500        # 总时长差超过此值认为双方媒体不一致


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
        if state:
            self.apply_remote_state(state, initial=True)

    def leave_room(self) -> None:
        self.in_room = False
        self.room_state = None
        self._last = None
        self._mismatch_since = 0.0

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
        # 切集动作：先跟随切换，再按状态对齐
        if s.get("action") in ("next", "prev") and not initial:
            self._apply_remote_episode(s)
            return

        self.room_state = dict(s)
        snap = self.bridge.snapshot()
        if snap is None:
            self.sigLog.emit("收到同步状态，但本机 PotPlayer 未运行")
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

    # ---------- 周期采样：侦测本地操作 ----------

    def _poll(self) -> None:
        snap = self.bridge.snapshot()
        now = time.monotonic()
        prev, prev_mono = self._last, self._last_mono
        self._last, self._last_mono = snap, now
        self.sigLocalState.emit(snap)
        self._emit_sync_info(snap)

        if snap is None or not self.in_room or self._guarded():
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

        # 2) 换片（媒体名或总时长变化）
        if snap.media != prev.media or abs(snap.duration - prev.duration) > 1000:
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
        if not self.in_room or not self.room_state or self._guarded():
            self._mismatch_since = 0.0
            return
        snap = self.bridge.snapshot()
        if snap is None:
            return
        s = self.room_state
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
        s = self.room_state
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
