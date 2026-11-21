#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国内网络友好的文件下载
=========================

为什么不用 urllib：直连 github.com / objects.githubusercontent.com 时，
Python 的 TLS 栈在部分网络下会被直接 RST（RemoteDisconnected），而
curl.exe（Windows 10+ 自带）走 HTTP/2 往往能通。所以这里优先调用
curl，失败再退回 urllib。

镜像：GitHub 资源走国内加速镜像通常比直连快一个数量级，因此候选顺序是
"镜像优先、直连兜底"，与旧实现相反（旧实现先试直连，卡满 25 秒才换）。

韧性：每个候选都带"低速自动放弃"（--speed-limit/--speed-time）与断点
续传；总时长不设死线，只要还在增长就继续，连续无进展才换源。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

MIRRORS = [
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://mirror.ghproxy.com/",
    "https://ghproxy.net/",
]

GITHUB_HOSTS = ("github.com", "objects.githubusercontent.com",
                "raw.githubusercontent.com", "codeload.github.com")

CURL_CANDIDATES = [
    shutil.which("curl"),
    r"C:\Windows\System32\curl.exe",
    r"C:\Windows\System32\curl",
]


def _curl() -> Optional[str]:
    for item in CURL_CANDIDATES:
        if item and Path(item).exists():
            return item
    return None


def candidate_urls(url: str) -> List[str]:
    """镜像优先、直连最后；非 GitHub 地址只保留原文。"""
    if not any(host in url for host in GITHUB_HOSTS):
        return [url]
    arms = [m + url for m in MIRRORS]
    arms.append(url)
    return arms


def _status(status: Optional[Callable[[str], None]], text: str) -> None:
    if status is None:
        return
    try:
        status(text)
    except Exception:
        pass


def _mb(size: int) -> str:
    return f"{size / 1048576:.1f}MB"


def _report_progress(path: Path, stop: threading.Event,
                     started_size: int, status: Optional[Callable[[str], None]],
                     label: str) -> None:
    last_size = started_size
    last_time = time.monotonic()
    while not stop.wait(0.6):
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0
        now = time.monotonic()
        speed = (size - last_size) / max(0.001, now - last_time)
        last_size, last_time = size, now
        if speed > 0:
            _status(status, f"{label}… {_mb(size)}（{speed / 1048576:.1f}MB/s）")
        else:
            _status(status, f"{label}… {_mb(size)}")


def _curl_download(url: str, part: Path, timeout: int) -> bool:
    """跑一次 curl 断点续传；返回是否拿到了 2xx。"""
    exe = _curl()
    if exe is None:
        return False
    cmd = [exe, "-L", "--fail", "--silent", "--show-error",
           "--connect-timeout", "10",
           "--speed-limit", "4096", "--speed-time", "15",
           "-C", "-", "-o", str(part), url]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False
    if proc.returncode == 0:
        return True
    text = (proc.stderr or b"").decode("utf-8", "replace")
    # 服务端不支持 Range（416）时不带续传参数重下一次
    if "416" in text or "range" in text.lower():
        part.unlink(missing_ok=True)
        return _curl_plain(url, part, timeout)
    return False


def _curl_plain(url: str, part: Path, timeout: int) -> bool:
    exe = _curl()
    if exe is None:
        return False
    cmd = [exe, "-L", "--fail", "--silent", "--show-error",
           "--connect-timeout", "10",
           "--speed-limit", "4096", "--speed-time", "15",
           "-o", str(part), url]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _urllib_download(url: str, part: Path, timeout: int) -> bool:
    headers = {"User-Agent": "PotSync/1.0"}
    pos = part.stat().st_size if part.exists() else 0
    if pos:
        headers["Range"] = f"bytes={pos}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            mode = "ab"
            if pos and getattr(resp, "status", 200) == 200:
                mode, pos = "wb", 0
            with open(part, mode) as fh:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    pos += len(chunk)
        return part.stat().st_size > 0
    except Exception:
        return False


def fetch(url: str, dst: Path, status: Optional[Callable[[str], None]] = None,
          expected_min: int = 100_000, label: str = "正在下载",
          deadline: Optional[float] = None) -> None:
    """把 url 下载到 dst（可续传）。失败抛 RuntimeError。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    # 目标文件存在也不复用：调用方（更新器）需要的是"这次下载的完整文件"，
    # 旧实现按大小跳过会把上次的脏残留当成新包。
    if dst.exists():
        dst.unlink(missing_ok=True)
    existing = part.stat().st_size if part.exists() else 0
    _status(status, f"{label}… {_mb(existing)}")
    arms = candidate_urls(url)
    started = time.monotonic()
    rounds = 0
    while True:
        rounds += 1
        before = part.stat().st_size if part.exists() else 0
        for arm in arms:
            if deadline is not None and time.monotonic() - started > deadline:
                raise RuntimeError("下载超时")
            stop = threading.Event()
            watcher = threading.Thread(
                target=_report_progress,
                args=(part, stop, before, status, label), daemon=True)
            watcher.start()
            ok = False
            if _curl() is not None:
                ok = _curl_download(arm, part, timeout=600)
            if not ok:
                ok = _urllib_download(arm, part, timeout=30)
            stop.set()
            size = part.stat().st_size if part.exists() else 0
            if ok and size >= expected_min:
                part.replace(dst)
                _status(status, f"{label}完成（{_mb(size)}）")
                return
            if size > before + 262144:
                # 这个源虽然没跑完，但有进展：继续用它续传
                break
        after = part.stat().st_size if part.exists() else 0
        if rounds >= 3 and after <= existing + 262144:
            raise RuntimeError("下载失败：所有下载源都无法建立连接")
        if rounds > 40:
            raise RuntimeError("下载失败：重试次数过多")
        existing = after


def fetch_text(url: str, timeout: int = 20) -> str:
    """下载小文本（校验和、version.json 等），同样走镜像与 curl。"""
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "potsync-fetch.txt"
    tmp.unlink(missing_ok=True)
    fetch(url, tmp, expected_min=1, label="正在获取", deadline=timeout)
    try:
        return tmp.read_text(encoding="utf-8", errors="replace")
    finally:
        tmp.unlink(missing_ok=True)
