#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内网穿透（公网隧道）管理
========================

主机模式下，把本机内嵌中继服务器暴露到公网，让异地好友直连加入。

两个免注册公共隧道（首次使用时自动下载到 ~/.potsync/bin/，不进 exe）：

1. Cloudflare Quick Tunnel（cloudflared，约 30MB）
   → 生成 https://xxx.trycloudflare.com，WebSocket 走 wss://…:443
2. bore.pub 公共隧道（bore，约 2MB，cloudflared 失败时自动降级）
   → 生成 bore.pub:端口，纯 TCP 转发，走 ws://

TunnelManager 在独立线程中管理下载、启动、输出解析与进程回收，
通过回调向外通知（GUI 里请用 Qt 信号转投主线程）。
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("potsync.tunnel")

BIN_DIR = Path.home() / ".potsync" / "bin"
STARTUP_TIMEOUT = 35          # 等待隧道给出公网地址的最长秒数

CLOUDFLARED_URL = ("https://github.com/cloudflare/cloudflared/releases/"
                   "latest/download/cloudflared-windows-amd64.exe")
BORE_URL = ("https://github.com/ekzhang/bore/releases/download/"
            "v0.6.0/bore-v0.6.0-x86_64-pc-windows-msvc.zip")

TRYCF_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)")
BORE_RE = re.compile(r"([a-z0-9.-]*bore\.pub):(\d+)")


class PublicEndpoint:
    """隧道公网入口。host/port 供客户端连接；display 供展示与复制。"""

    def __init__(self, host: str, port: int, provider: str) -> None:
        self.host = host
        self.port = port
        self.provider = provider

    @property
    def display(self) -> str:
        if self.provider == "cloudflared":
            return f"https://{self.host}"
        return f"{self.host}:{self.port}"

    def __repr__(self) -> str:
        return f"PublicEndpoint({self.host}:{self.port} via {self.provider})"


class TunnelProvider:
    """一种隧道方式：二进制获取 + 启动命令 + 输出解析。"""

    name = "base"

    def binary(self) -> Path:
        raise NotImplementedError

    def ensure_binary(self, status: Callable[[str], None]) -> Path:
        exe = self.binary()
        if exe.exists():
            return exe
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        self.download(exe, status)
        return exe

    def download(self, exe: Path, status: Callable[[str], None]) -> None:
        raise NotImplementedError

    def command(self, exe: Path, local_port: int) -> list:
        raise NotImplementedError

    def parse_line(self, line: str) -> Optional[PublicEndpoint]:
        raise NotImplementedError


class CloudflaredProvider(TunnelProvider):
    name = "cloudflared"

    def binary(self) -> Path:
        return BIN_DIR / "cloudflared.exe"

    def download(self, exe: Path, status: Callable[[str], None]) -> None:
        status("正在下载 cloudflared（约 30MB，仅首次）…")
        _download_file(CLOUDFLARED_URL, exe.with_suffix(".tmp"), status)
        exe.with_suffix(".tmp").replace(exe)

    def command(self, exe: Path, local_port: int) -> list:
        # --protocol http2：强制走 TCP 7844，规避 UDP/QUIC 被网络阻断的情况
        return [str(exe), "tunnel", "--url", f"http://127.0.0.1:{local_port}",
                "--protocol", "http2", "--no-autoupdate"]

    def parse_line(self, line: str) -> Optional[PublicEndpoint]:
        m = TRYCF_RE.search(line)
        if m:
            return PublicEndpoint(m.group(1), 443, self.name)
        return None


class BoreProvider(TunnelProvider):
    name = "bore"

    def binary(self) -> Path:
        return BIN_DIR / "bore.exe"

    def download(self, exe: Path, status: Callable[[str], None]) -> None:
        status("正在下载 bore（约 2MB，仅首次）…")
        tmp_zip = exe.with_suffix(".zip")
        _download_file(BORE_URL, tmp_zip, status)
        with zipfile.ZipFile(tmp_zip) as z:
            for n in z.namelist():
                if n.lower().endswith("bore.exe"):
                    exe.write_bytes(z.read(n))
                    break
            else:
                raise RuntimeError("bore 压缩包中未找到 bore.exe")
        tmp_zip.unlink(missing_ok=True)

    def command(self, exe: Path, local_port: int) -> list:
        return [str(exe), "local", str(local_port), "--to", "bore.pub"]

    def parse_line(self, line: str) -> Optional[PublicEndpoint]:
        m = BORE_RE.search(line)
        if m:
            return PublicEndpoint(m.group(1), int(m.group(2)), self.name)
        return None


PROVIDERS = [CloudflaredProvider(), BoreProvider()]


# GitHub 下载在部分网络环境下不稳定，按序尝试直连与公共镜像
GH_MIRRORS = ["", "https://ghfast.top/", "https://gh-proxy.com/",
              "https://mirror.ghproxy.com/"]


def _candidate_urls(url: str) -> list:
    if "github.com" not in url:
        return [url]
    return [m + url for m in GH_MIRRORS]


def _download_file(url: str, dst: Path,
                   status: Optional[Callable[[str], None]] = None,
                   expected_min: int = 100_000) -> None:
    """断点续传 + 多镜像轮询的健壮下载（直连 GitHub 不稳定时自动切换镜像）。"""
    candidates = _candidate_urls(url)
    pos = dst.stat().st_size if dst.exists() else 0
    start = time.time()
    attempt = 0
    while True:
        candidate = candidates[attempt % len(candidates)]
        attempt += 1
        if time.time() - start > 600:
            raise RuntimeError("下载超时（10 分钟）")
        try:
            headers = {"User-Agent": "PotSync/1.0"}
            if pos:
                headers["Range"] = f"bytes={pos}-"
            req = urllib.request.Request(candidate, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                if pos and getattr(resp, "status", 200) == 200:
                    pos = 0          # 该源不支持断点续传，重新下载
                    mode = "wb"
                else:
                    mode = "ab"
                with open(dst, mode) as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                                break
                        f.write(chunk)
                        pos += len(chunk)
                        if status:
                            status(f"正在下载（{pos / 1048576:.0f}MB）…")
            if pos >= expected_min:
                return
            raise RuntimeError(f"下载文件过小（{pos}B），疑似镜像异常")
        except Exception as exc:
            log.debug("下载中断(%s @%dB): %s", candidate, pos, exc)
            if status:
                status("下载中断，切换镜像源续传…")
            time.sleep(1)


class TunnelManager:
    """按优先级尝试各隧道提供商，拿到公网入口后回调。

    on_ready(endpoint: PublicEndpoint)
    on_error(message: str)        —— 所有提供商都失败
    on_status(message: str)       —— 进度提示（下载/连接中等）
    所有回调都在隧道线程中触发，GUI 里请转投主线程。
    """

    def __init__(self,
                 on_ready: Callable[[PublicEndpoint], None],
                 on_error: Callable[[str], None],
                 on_status: Optional[Callable[[str], None]] = None) -> None:
        self._on_ready = on_ready
        self._on_error = on_error
        self._on_status = on_status or (lambda m: None)
        self.endpoint: Optional[PublicEndpoint] = None
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, local_port: int) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run, args=(local_port,),
                                        name="potsync-tunnel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None

    # ---------- 内部 ----------

    def _run(self, local_port: int) -> None:
        errors = []
        for provider in PROVIDERS:
            if self._stopped.is_set():
                return
            self._on_status(f"正在通过 {provider.name} 建立公网隧道…")
            try:
                endpoint = self._run_provider(provider, local_port)
            except Exception as exc:
                log.warning("%s 隧道失败: %s", provider.name, exc)
                errors.append(f"{provider.name}: {exc}")
                continue
            if endpoint is None:
                errors.append(f"{provider.name}: 启动超时")
                continue
            if not self._verify_endpoint(endpoint):
                self._kill_current()
                errors.append(f"{provider.name}: 公网地址未生效")
                continue
            self.endpoint = endpoint
            self._on_ready(endpoint)
            return
        self._on_error("；".join(errors) or "所有隧道方式均失败")

    def _verify_endpoint(self, endpoint: PublicEndpoint) -> bool:
        """等待公网地址真正可用（DNS 记录生效可能有数秒延迟），最长 40 秒。"""
        self._on_status("隧道已建立，等待公网地址生效…")
        deadline = time.time() + 40
        while time.time() < deadline and not self._stopped.is_set():
            try:
                if endpoint.provider == "cloudflared":
                    req = urllib.request.Request(
                        f"https://{endpoint.host}/",
                        headers={"User-Agent": "PotSync/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        body = resp.read(256)
                    if b"PotSync" in body:
                        return True
                else:
                    import socket as _sock
                    with _sock.create_connection((endpoint.host, endpoint.port),
                                                 timeout=8):
                        return True
            except Exception:
                pass
            time.sleep(2)
        return False

    def _run_provider(self, provider: TunnelProvider,
                      local_port: int) -> Optional[PublicEndpoint]:
        exe = provider.ensure_binary(self._on_status)
        if self._stopped.is_set():
            return None
        proc = subprocess.Popen(
            provider.command(exe, local_port),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._proc = proc
        deadline = time.time() + STARTUP_TIMEOUT
        endpoint: Optional[PublicEndpoint] = None
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._stopped.is_set():
                    return None
                line = line.strip()
                if line:
                    log.debug("[%s] %s", provider.name, line)
                endpoint = provider.parse_line(line)
                if endpoint is not None:
                    break
                if time.time() > deadline:
                    return None
            return endpoint
        finally:
            if endpoint is None:
                # 失败/超时：回收进程
                try:
                    proc.kill()
                except Exception:
                    pass
                if self._proc is proc:
                    self._proc = None
            else:
                # 成功：保留隧道进程，后台排空日志防止管道写满阻塞
                threading.Thread(target=self._drain, args=(proc,),
                                 daemon=True).start()

    @staticmethod
    def _drain(proc: subprocess.Popen) -> None:
        try:
            for _ in proc.stdout:
                pass
        except Exception:
            pass

    def _kill_current(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            self._proc = None


if __name__ == "__main__":
    # 手动联调：python tunnel.py <本地端口>
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(message)s")
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

    mgr = TunnelManager(
        on_ready=lambda ep: print(f"\n公网入口: {ep.display}  ({ep!r})\n"),
        on_error=lambda e: print(f"\n隧道失败: {e}\n"),
        on_status=lambda s: print(f"[状态] {s}"))
    mgr.start(port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mgr.stop()
