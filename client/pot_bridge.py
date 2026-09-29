#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotPlayer 控制桥
================

通过 Win32 窗口消息远程控制本机的 PotPlayer（32/64 位均可）：

- 窗口类名：PotPlayer64 / PotPlayer / PotPlayerMini64 / PotPlayerMini
- WM_COMMAND(0x0111)：播放 20001、暂停 20000、播放/暂停切换 10014、停止 20002
- WM_USER(0x0400)：0x5002 取总时长(ms)、0x5004 取当前进度(ms)、0x5005 设置进度(ms)、
  0x5006 取播放状态(-1 停止 / 1 暂停 / 2 播放中)、0x5007 设置播放状态(0 切换/1 暂停/2 播放)

所有消息使用 SendMessageTimeout，避免 PotPlayer 卡死时拖挂本程序界面。
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

import win32con
import win32gui
import win32process

log = logging.getLogger("potsync.bridge")

WM_COMMAND = win32con.WM_COMMAND          # 0x0111
WM_USER = win32con.WM_USER                # 0x0400
SMTO_ABORTIFHUNG = 0x0002
MSG_TIMEOUT_MS = 400

# WM_COMMAND 命令
CMD_PAUSE = 20000
CMD_PLAY = 20001
CMD_STOP = 20002
CMD_PLAY_PAUSE = 10014
CMD_PREVIOUS = 10123      # 播放列表：上一集/上一个
CMD_NEXT = 10124          # 播放列表：下一集/下一个

# WM_USER 命令（参数经 lParam 传递，返回值即结果）
POT_GET_TOTAL_TIME = 0x5002     # ms
POT_GET_CURRENT_TIME = 0x5004   # ms
POT_SET_CURRENT_TIME = 0x5005   # ms（lParam）
POT_GET_PLAY_STATUS = 0x5006    # -1 停止 / 1 暂停 / 2 播放中
POT_SET_PLAY_STATUS = 0x5007    # 0 切换 / 1 暂停 / 2 播放（lParam）

STATUS_STOPPED = -1
STATUS_PAUSED = 1
STATUS_RUNNING = 2
# 实测：空闲状态不同版本可能返回 -1 或 0，均视为"已停止"
STATUS_TEXT = {STATUS_STOPPED: "已停止", 0: "已停止",
               STATUS_PAUSED: "已暂停", STATUS_RUNNING: "播放中"}

WINDOW_CLASSES = ("PotPlayer64", "PotPlayer", "PotPlayerMini64", "PotPlayerMini")

# 常见视频扩展名（句柄枚举/目录扫描时过滤用）
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".rmvb", ".rm", ".wmv",
              ".flv", ".mov", ".mpg", ".mpeg", ".webm", ".vob", ".ogm",
              ".3gp", ".f4v"}


def normalize_media(name: str) -> str:
    """媒体名归一化，用于跨机器文件名比较：
    去目录（标题栏可能显示完整路径）、去扩展名、小写、折叠空白。"""
    n = (name or "").strip().lower()
    n = n.replace("\\", "/").rsplit("/", 1)[-1]     # 只留文件名
    base, dot, ext = n.rpartition(".")
    if dot and 1 <= len(ext) <= 5 and ext.isalnum():  # 常见视频扩展名
        n = base
    return re.sub(r"\s+", " ", n).strip()


def media_matches(a: str, b: str) -> bool:
    """两个媒体名是否指向同一文件（规范化后精确相等；任一侧为空视为不匹配）。"""
    na, nb = normalize_media(a), normalize_media(b)
    return bool(na) and bool(nb) and na == nb


def media_name_from_title(title: str) -> str:
    """从窗口标题提取媒体名（去掉 " - PotPlayer" 后缀；空闲未加载时返回 ""）。"""
    title = (title or "").strip()
    for suffix in (" - PotPlayer",):
        if title.endswith(suffix):
            title = title[: -len(suffix)]
    title = title.strip()
    if title.lower() in ("potplayer",):   # 空闲未加载文件时标题就是程序名
        return ""
    return title


def find_media_in_dir(dirpath: str, target_norm: str) -> str:
    """在目录中查找规范化名称等于 target_norm 的视频文件。
    命中返回完整路径，找不到/目录不可读返回 ""。"""
    if not dirpath or not target_norm:
        return ""
    try:
        with os.scandir(dirpath) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if os.path.splitext(entry.name)[1].lower() not in VIDEO_EXTS:
                    continue
                if normalize_media(entry.name) == target_norm:
                    return os.path.join(dirpath, entry.name)
    except OSError:
        return ""
    return ""


def parse_dpl(path: str) -> dict:
    """解析 PotPlayer 播放列表文件（.dpl，UTF-8 带 BOM 的文本）。

    格式：首行 DAUMPLAYLIST；playname=<当前播放项完整路径>；
    N*file*<完整路径> 为列表第 N 项（可能穿插 N*duration2* / N*start* 等行）。
    返回 {"playname": str, "files": [str, ...]}；读不到/格式不符返回空。
    """
    playname, files = "", []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read(2 << 20)          # 列表文件很小，防御性截断
    except OSError:
        return {"playname": "", "files": []}
    if not text.startswith("DAUMPLAYLIST"):
        return {"playname": "", "files": []}
    for line in text.splitlines():
        if line.startswith("playname="):
            playname = line[len("playname="):].strip()
            continue
        m = re.match(r"^\d+\*file\*(.*)$", line)
        if m:
            p = m.group(1).strip()
            if p:
                files.append(p)
    return {"playname": playname, "files": files}


@dataclass
class Snapshot:
    """一次本地播放器状态采样。position/duration 单位毫秒。"""
    status: int          # -1/1/2
    position: int        # ms
    duration: int        # ms
    media: str           # 当前媒体名（取自窗口标题）

    @property
    def playing(self) -> bool:
        return self.status == STATUS_RUNNING


class PotPlayerBridge:
    """定位并操控本机 PotPlayer 窗口。所有方法均线程无关、快速返回。"""

    def __init__(self, class_names: tuple = WINDOW_CLASSES) -> None:
        self._hwnd: int = 0
        self._class_names = class_names

    # ---------- 窗口定位 ----------

    def find_window(self) -> int:
        if self._hwnd and win32gui.IsWindow(self._hwnd):
            return self._hwnd
        for cls in self._class_names:
            hwnd = win32gui.FindWindow(cls, None)
            if hwnd:
                self._hwnd = hwnd
                return hwnd
        self._hwnd = 0
        return 0

    def available(self) -> bool:
        return self.find_window() != 0

    # ---------- 底层收发 ----------

    def _send(self, msg: int, wparam: int = 0, lparam: int = 0) -> Optional[int]:
        """SendMessageTimeout；成功返回消息返回值，窗口不存在/超时返回 None。"""
        hwnd = self.find_window()
        if not hwnd:
            return None
        try:
            ok, ret = win32gui.SendMessageTimeout(
                hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, MSG_TIMEOUT_MS)
        except Exception as exc:  # 窗口刚关闭等情况
            log.debug("SendMessageTimeout 失败: %s", exc)
            self._hwnd = 0
            return None
        if not ok:
            return None
        # 32 位有符号还原（长片源毫秒数不会超过 2^31，保守处理）
        if ret is not None and ret >= 2 ** 31:
            ret -= 2 ** 32
        return ret

    def _command(self, cmd: int) -> bool:
        return self._send(WM_COMMAND, cmd, 0) is not None

    # ---------- 状态查询 ----------

    def status(self) -> Optional[int]:
        return self._send(WM_USER, POT_GET_PLAY_STATUS, 0)

    def position_ms(self) -> Optional[int]:
        return self._send(WM_USER, POT_GET_CURRENT_TIME, 0)

    def duration_ms(self) -> Optional[int]:
        return self._send(WM_USER, POT_GET_TOTAL_TIME, 0)

    def media_name(self) -> str:
        hwnd = self.find_window()
        if not hwnd:
            return ""
        try:
            return media_name_from_title(win32gui.GetWindowText(hwnd))
        except Exception:
            return ""

    def snapshot(self) -> Optional[Snapshot]:
        """一次采样；PotPlayer 未运行返回 None。"""
        if not self.find_window():
            return None
        status = self.status()
        if status is None:
            return None
        position = self.position_ms() or 0
        duration = self.duration_ms() or 0
        if status not in (STATUS_PAUSED, STATUS_RUNNING):   # 已停止
            position = 0
        return Snapshot(status=status, position=max(0, position),
                        duration=max(0, duration), media=self.media_name())

    # ---------- 播放控制 ----------

    def play(self) -> bool:
        return self._command(CMD_PLAY)

    def pause(self) -> bool:
        return self._command(CMD_PAUSE)

    def toggle_play_pause(self) -> bool:
        return self._command(CMD_PLAY_PAUSE)

    def seek_ms(self, position_ms: int) -> bool:
        position_ms = max(0, int(position_ms))
        return self._send(WM_USER, POT_SET_CURRENT_TIME, position_ms) is not None

    def previous(self) -> bool:
        """播放列表上一个（上一集）。"""
        return self._command(CMD_PREVIOUS)

    def next(self) -> bool:
        """播放列表下一个（下一集）。"""
        return self._command(CMD_NEXT)

    # ---------- 进程 / 文件定位（同目录直开用） ----------

    def get_pid(self) -> int:
        """PotPlayer 窗口所属进程 PID（无窗口返回 0）。"""
        hwnd = self.find_window()
        if not hwnd:
            return 0
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return int(pid or 0)
        except Exception:
            return 0

    def exe_path(self) -> str:
        """PotPlayer 可执行文件完整路径（取不到返回 ""）。"""
        pid = self.get_pid()
        if not pid:
            return ""
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(h)

    def open_media_paths(self) -> list:
        """枚举系统句柄表，列出 PotPlayer 进程当前打开的视频文件完整路径。

        用 NtQuerySystemInformation(SystemHandleInformation) 取全系统句柄，
        过滤出目标进程的句柄后 DuplicateHandle 到本进程，再用
        GetFinalPathNameByHandleW 取路径——非文件句柄会立即失败返回，
        不存在 NtQueryObject 查询对象名时挂死命名管道的风险。
        """
        pid = self.get_pid()
        if not pid:
            return []
        ntdll = ctypes.windll.ntdll
        kernel32 = ctypes.windll.kernel32
        entry_size = ctypes.sizeof(_SYSTEM_HANDLE_TABLE_ENTRY_INFO)
        # 1) 取系统句柄表（缓冲区不足时按返回长度扩容重试）
        size = 1 << 20
        buf = None
        for _ in range(6):
            buf = ctypes.create_string_buffer(size)
            retlen = wintypes.ULONG(0)
            status = ntdll.NtQuerySystemInformation(16, buf, size,
                                                    ctypes.byref(retlen))
            if status == 0:
                break
            if status & 0xFFFFFFFF == 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
                size = max(int(retlen.value) + (1 << 16), size * 2)
                continue
            return []
        else:
            return []
        count = wintypes.ULONG.from_buffer(buf, 0).value
        handles = []
        for i in range(count):
            off = 8 + i * entry_size      # 表头 ULONG count + x64 对齐 padding
            if off + entry_size > size:
                break
            e = _SYSTEM_HANDLE_TABLE_ENTRY_INFO.from_buffer(buf, off)
            if e.UniqueProcessId == pid:
                handles.append(e.HandleValue)
        if not handles:
            return []
        # 2) 复制句柄取最终路径，按视频扩展名过滤去重
        hproc = kernel32.OpenProcess(0x0040, False, pid)  # PROCESS_DUP_HANDLE
        if not hproc:
            return []
        paths, seen = [], set()
        cur = kernel32.GetCurrentProcess()
        try:
            for hv in handles:
                dup = wintypes.HANDLE()
                if not kernel32.DuplicateHandle(
                        hproc, hv, cur, ctypes.byref(dup),
                        0, False, 2):          # DUPLICATE_SAME_ACCESS
                    continue
                try:
                    pbuf = ctypes.create_unicode_buffer(1024)
                    n = kernel32.GetFinalPathNameByHandleW(dup, pbuf, 1024, 0)
                    if not (0 < n < 1024):
                        continue
                    p = pbuf.value
                    if p.startswith("\\\\?\\UNC\\"):
                        p = "\\\\" + p[8:]
                    elif p.startswith("\\\\?\\"):
                        p = p[4:]
                    if os.path.splitext(p)[1].lower() in VIDEO_EXTS \
                            and p not in seen:
                        seen.add(p)
                        paths.append(p)
                finally:
                    kernel32.CloseHandle(dup)
        finally:
            kernel32.CloseHandle(hproc)
        return paths

    def current_media_path(self) -> str:
        """PotPlayer 当前正在播放文件的完整路径（定位不到返回 ""）。

        首选播放列表文件（.dpl）记录的当前项——便宜、且不受"标题栏显示
        内嵌元数据标题而非文件名"影响；记录滞后（与标题不符）时回退到
        系统句柄枚举。
        """
        cur = normalize_media(self.media_name())
        try:
            pn = self.current_playname()
        except Exception as exc:
            log.debug("读取播放列表当前项失败: %s", exc)
            pn = ""
        if pn and os.path.exists(pn):
            if not cur or normalize_media(os.path.basename(pn)) == cur:
                return pn
        try:
            paths = self.open_media_paths()
        except Exception as exc:
            log.debug("枚举句柄失败: %s", exc)
            return pn if pn and os.path.exists(pn) else ""
        if not paths:
            return pn if pn and os.path.exists(pn) else ""
        if cur:
            for p in paths:
                if normalize_media(os.path.basename(p)) == cur:
                    return p
        if len(paths) == 1:       # 只打开了一个视频：即是它
            return paths[0]
        return ""

    # ---------- 播放列表文件（.dpl） ----------

    def _playlist_dirs(self) -> list:
        """候选 Playlist 目录：便携版（exe 旁）优先，再 %APPDATA% 64/32 位。"""
        dirs = []
        exe = self.exe_path()
        if exe:
            dirs.append(os.path.join(os.path.dirname(exe), "Playlist"))
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            dirs.append(os.path.join(appdata, "PotPlayerMini64", "Playlist"))
            dirs.append(os.path.join(appdata, "PotPlayer", "Playlist"))
        return dirs

    def _dpl_files(self) -> list:
        """所有 .dpl 文件（按修改时间新→旧）。"""
        dpls = []
        for d in self._playlist_dirs():
            try:
                for name in os.listdir(d):
                    if name.lower().endswith(".dpl"):
                        full = os.path.join(d, name)
                        try:
                            dpls.append((os.path.getmtime(full), full))
                        except OSError:
                            pass
            except OSError:
                continue
        dpls.sort(reverse=True)
        return [p for _, p in dpls]

    def current_playname(self) -> str:
        """最新 .dpl 记录的当前播放项完整路径（可能略滞后于真实状态）。"""
        for full in self._dpl_files():
            pn = parse_dpl(full)["playname"]
            if pn:
                return pn
        return ""

    def playlist_files(self) -> list:
        """PotPlayer 播放列表中的全部视频文件完整路径（.dpl 新的优先，去重）。"""
        seen, out = set(), []
        for full in self._dpl_files():
            for p in parse_dpl(full)["files"]:
                if os.path.splitext(p)[1].lower() in VIDEO_EXTS \
                        and p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    def find_in_playlist(self, target_norm: str) -> str:
        """在播放列表各项中找同名（规范化）视频文件。
        返回完整路径；文件已不存在/找不到返回 ""。"""
        if not target_norm:
            return ""
        for p in self.playlist_files():
            if normalize_media(os.path.basename(p)) == target_norm \
                    and os.path.exists(p):
                return p
        return ""

    def open_file(self, path: str) -> bool:
        """用当前 PotPlayer 直接打开指定文件（等价用户双击，单次切换零轮巡）。"""
        exe = self.exe_path()
        if not exe or not path:
            return False
        try:
            subprocess.Popen([exe, path])
            return True
        except Exception as exc:
            log.warning("启动 PotPlayer 打开文件失败: %s", exc)
            return False

    def rebind_to_title(self, norm: str) -> bool:
        """多实例场景：按规范化媒体名重新绑定到标题匹配的 PotPlayer 窗口。"""
        norm = (norm or "").strip().lower()
        if not norm:
            return False
        classes = set(self._class_names)
        found = []

        def _cb(hwnd, _):
            try:
                if win32gui.GetClassName(hwnd) in classes:
                    title = media_name_from_title(win32gui.GetWindowText(hwnd))
                    if normalize_media(title) == norm:
                        found.append(hwnd)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass
        if found:
            self._hwnd = found[0]
            return True
        return False


class _SYSTEM_HANDLE_TABLE_ENTRY_INFO(ctypes.Structure):
    """NtQuerySystemInformation(SystemHandleInformation) 的表项（x64 24 字节）。"""
    _fields_ = [
        ("UniqueProcessId", wintypes.USHORT),
        ("CreatorBackTraceIndex", wintypes.USHORT),
        ("ObjectTypeIndex", wintypes.BYTE),
        ("HandleAttributes", wintypes.BYTE),
        ("HandleValue", wintypes.USHORT),
        ("Object", ctypes.c_void_p),
        ("GrantedAccess", wintypes.ULONG),
    ]


if __name__ == "__main__":
    # 手动自检：python pot_bridge.py
    logging.basicConfig(level=logging.DEBUG)
    b = PotPlayerBridge()
    snap = b.snapshot()
    if snap is None:
        print("未检测到正在运行的 PotPlayer")
    else:
        print(f"状态={STATUS_TEXT.get(snap.status)} 进度={snap.position}ms "
              f"总长={snap.duration}ms 媒体={snap.media!r}")
