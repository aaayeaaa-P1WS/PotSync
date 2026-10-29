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

# 文件名自动匹配只走“完整列表索引 / 已知目录 → 精准直开”。
# 不再通过上一集/下一集逐个试开；未命中立即提示，保持当前媒体不变。

# 直开事务：固定短等待会在慢盘/大文件加载时误判失败，进而并发打开。
# 改成按实际标题与时长确认识别完成，且任何时刻最多一个直开请求在途。
DIRECT_OPEN_POLL_MS = 200
DIRECT_OPEN_SETTLE_S = 0.5
DIRECT_OPEN_TIMEOUT_S = 15.0


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
        self._direct_open: Optional[dict] = None  # 唯一在途的直开事务
        self._suppressed_media = ""        # 已取消/超时直开的迟到完成事件（只吞一次）
        self._suppressed_until = 0.0
        self._failed_media = ""            # 最近未匹配的媒体名，避免重复读取列表
        self._media_dirs: list = []        # 已知媒体目录（MRU，最多 8 个），同目录直开用
        self._direct_failed = ""           # 加载失败的媒体名，避免反复发起直开

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
        self._direct_open = None
        self._suppressed_media = ""
        self._suppressed_until = 0.0
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
        self._direct_open = None
        self._suppressed_media = ""
        self._suppressed_until = 0.0
        self._failed_media = ""
        self._media_dirs = []
        self._direct_failed = ""

    def _cancel_media_follow(self) -> None:
        """本地用户主动操作时取消正在进行的匹配跟随（本地意图优先）。"""
        cancelled = self._direct_open is not None
        if self._direct_open is not None:
            # 已发出的进程打开无法撤回；其迟到完成不能被当成新本地操作广播。
            self._suppressed_media = normalize_media(
                os.path.basename(self._direct_open["path"]))
            self._suppressed_until = time.monotonic() + 30.0
        self._direct_open = None       # 旧定时回调会因事务对象不匹配而失效
        if cancelled:
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
        self._cancel_media_follow()
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
        self._cancel_media_follow()
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
        self._cancel_media_follow()
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
        # 直开加载中只记录最新目标，绝不并发打开第二个文件。
        if self._direct_open is not None:
            self.room_state = dict(s)
            self._direct_open["state"] = dict(s)
            self.sigSyncInfo.emit("⏳ 加载媒体中…")
            return

        # 切集动作：优先按目标文件名直开，避免先做了一次多余的盲目切集。
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
        """按远程状态携带的目标媒体跟随切集；无目标名时才使用方向命令。"""
        self.room_state = dict(s)
        remote_media = (s.get("media") or "").strip()
        if remote_media:
            snap = self.bridge.snapshot()
            if snap is not None and media_matches(remote_media, snap.media):
                self._align_after_episode(s)
            else:
                self._start_media_follow(dict(s))
            return

        # 兼容没有媒体名的旧协议对端。
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
        """只通过完整列表索引或已知目录精准直开；找不到也绝不逐集试开。"""
        target = (s.get("media") or "").strip()
        if not target:
            return
        if self._direct_open is not None:
            self.room_state = dict(s)
            self._direct_open["state"] = dict(s)  # 当前加载完成后只追随最新目标
            return
        self._suppressed_media = ""             # 新远程任务的加载完成是预期事件
        self._suppressed_until = 0.0
        snap = self.bridge.snapshot()
        if snap is None or not snap.media:
            self.sigLog.emit("本机未在播放任何媒体，无法自动匹配")
            return
        if self._try_direct_match(target, s):
            return

        norm = normalize_media(target)
        self._failed_media = norm
        self.room_state = dict(s)
        try:
            sources, items = self.bridge.playlist_stats()
        except Exception:
            sources, items = 0, 0
        detail = (f"已读取 {sources} 个列表、{items} 个媒体条目" if items
                  else "未读取到可用的播放列表条目")
        self.sigLog.emit(
            f"⚠ 未找到《{target}》（{detail}）。未执行逐集尝试；"
            "请确认双方文件名一致且本机文件存在")
        self.sigSyncInfo.emit("⚠ 未找到同名媒体（未切换）")
        self._guard(1.0)
        self._last = self.bridge.snapshot()
        self._last_mono = time.monotonic()

    # ---------- 完整列表 / 已知目录精准直开 ----------

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
        """优先读取完整播放列表索引；未命中再检查已知媒体目录。"""
        norm = normalize_media(target)
        if not norm or norm == self._direct_failed:
            return False
        try:
            hit = self.bridge.find_in_playlist(norm)
        except Exception as exc:
            log.debug("find_in_playlist 失败: %s", exc)
            hit = ""
        if not hit:
            try:
                cur_path = self.bridge.current_media_path()
            except Exception as exc:
                log.debug("current_media_path 失败: %s", exc)
                cur_path = ""
            if cur_path:
                self._learn_path(os.path.dirname(cur_path))
            for directory in list(self._media_dirs):
                hit = find_media_in_dir(directory, norm)
                if hit:
                    break
        if not hit:
            return False
        self._learn_path(os.path.dirname(hit))
        if self._open_matched(hit, s):
            return True
        # 已定位文件但打开请求失败也不能退化为逐集搜索。
        self._failed_media = norm
        self._direct_failed = norm
        self.sigLog.emit("⚠ 已定位同名文件，但当前 PotPlayer 未接受打开请求；未执行逐集尝试")
        return True

    def _open_matched(self, path: str, s: dict) -> bool:
        self._guard(DIRECT_OPEN_TIMEOUT_S + 2)
        if not self.bridge.open_file(path):
            return False
        op = {
            "path": path,
            "state": dict(s),          # 加载期间收到新状态时替换为最新目标
            "t0": time.monotonic(),
            "ready_since": 0.0,
        }
        self._direct_open = op
        self.room_state = dict(s)
        self.sigLog.emit(f"✓ 已找到同名文件，等待当前 PotPlayer 窗口加载：{os.path.basename(path)}")
        self.sigSyncInfo.emit("⏳ 加载媒体中…")
        QTimer.singleShot(DIRECT_OPEN_POLL_MS,
                          lambda op=op: self._direct_open_tick(op))
        return True

    def _direct_open_tick(self, op: dict) -> None:
        """等待实际加载完成；超时只停止任务，绝不向加载中的播放器补发命令。"""
        if self._direct_open is not op:       # 已取消或已被其他事务取代
            return
        now = time.monotonic()
        opened_media = os.path.basename(op["path"])
        # open_file 使用 /current 在原实例切换；不要按标题重绑到其他窗口。
        cur = self.bridge.media_name()

        if media_matches(cur, opened_media):
            snap = self.bridge.snapshot()
            if snap is not None and snap.duration > 0:
                if not op["ready_since"]:
                    op["ready_since"] = now
                if now - op["ready_since"] < DIRECT_OPEN_SETTLE_S:
                    QTimer.singleShot(DIRECT_OPEN_POLL_MS,
                                      lambda op=op: self._direct_open_tick(op))
                    return

                latest = dict(op["state"])
                latest_media = (latest.get("media") or "").strip()
                self._direct_open = None
                self._direct_failed = ""
                if media_matches(cur, latest_media):
                    self._failed_media = ""
                    self._guard(2.0)
                    self._align_to_state(latest)
                else:
                    # 旧文件已经加载安定，才允许串行打开最新目标。
                    self._start_media_follow(latest)
                return
            op["ready_since"] = 0.0

        if now - op["t0"] >= DIRECT_OPEN_TIMEOUT_S:
            latest = dict(op["state"])
            target = (latest.get("media") or "").strip()
            norm = normalize_media(target)
            self._direct_open = None
            self._direct_failed = norm
            self._failed_media = norm
            self._suppressed_media = normalize_media(opened_media)
            self._suppressed_until = now + 30.0
            self.sigLog.emit(
                f"⚠ 等待《{target}》加载超时，已停止自动切换；"
                "未向播放器补发切集命令")
            self.sigSyncInfo.emit("⚠ 媒体加载超时")
            self._guard(2.0)
            self._last = self.bridge.snapshot()
            self._last_mono = time.monotonic()
            return

        QTimer.singleShot(DIRECT_OPEN_POLL_MS,
                          lambda op=op: self._direct_open_tick(op))

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
                or self._direct_open is not None:
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

        if self._suppressed_media and now > self._suppressed_until:
            self._suppressed_media = ""
            self._suppressed_until = 0.0
        if self._suppressed_media \
                and (snap.media != prev.media
                     or abs(snap.duration - prev.duration) > 1000) \
                and media_matches(snap.media, self._suppressed_media):
            # 已取消/超时的直开现在才完成：接受播放器现状，但不向房间回弹。
            self._suppressed_media = ""
            self._suppressed_until = 0.0
            self._guard(1.0)
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
                or self._direct_open is not None:
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
        if self._direct_open is not None:
            self.sigSyncInfo.emit("⏳ 加载媒体中…")
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
