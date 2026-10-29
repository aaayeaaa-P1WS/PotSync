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
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("potsync.tunnel")

BIN_DIR = Path.home() / ".potsync" / "bin"
STARTUP_TIMEOUT = 35          # 等待隧道给出公网地址的最长秒数
PUBLIC_ENDPOINT_TIMEOUT = 120  # 实测新域名 DNS 可能超过 60 秒才生效

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
    requires_registration = False

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

    def registration_ready(self, line: str) -> bool:
        """公网地址之外，是否还要求底层隧道先注册到服务商。"""
        return True


class CloudflaredProvider(TunnelProvider):
    name = "cloudflared"
    requires_registration = True

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

    def registration_ready(self, line: str) -> bool:
        # 地址会在边缘连接注册前打印。过早访问会触发代理 TLS 错误或本地
        # DNS 负缓存，造成明明隧道已注册却仍长时间等待。
        return "Registered tunnel connection" in line


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
        # 错误日志也含 bore.pub:7835；只接受明确的成功监听行。
        if not re.search(r"\blistening\s+at\b", line, re.IGNORECASE):
            return None
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
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and pos:
                # 续传起点超出远端文件大小（多为旧版本残留的临时文件）：
                # 丢弃本地残留，从头重新下载
                log.debug("续传位置无效(416 @%dB)，重新完整下载", pos)
                pos = 0
                dst.unlink(missing_ok=True)
                if status:
                    status("检测到残留文件异常，重新下载…")
                time.sleep(1)
                continue
            log.debug("下载中断(%s @%dB): %s", candidate, pos, exc)
            if status:
                status("下载中断，切换镜像源续传…")
            time.sleep(1)
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
                if self._stopped.is_set():
                    return
                log.warning("%s 隧道失败: %s", provider.name, exc)
                errors.append(f"{provider.name}: {exc}")
                continue
            if endpoint is None or self._stopped.is_set():
                return
            if not self._verify_endpoint(endpoint):
                self._kill_current()
                if self._stopped.is_set():
                    return
                detail = getattr(self, "_verification_error", "未知原因")
                errors.append(f"{provider.name}: 公网地址未生效（{detail}）")
                continue
            self.endpoint = endpoint
            self._on_ready(endpoint)
            return
        if not self._stopped.is_set():
            self._on_error("；".join(errors) or "所有隧道方式均失败")

    @staticmethod
    def _open_public_url(req, timeout: int, direct: bool):
        """公网预检同时支持直连和系统代理，便于与实际客户端路径互备。"""
        if direct:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return opener.open(req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout)

    def _verify_endpoint(self, endpoint: PublicEndpoint) -> bool:
        """注册完成后验证公网入口；直连与系统代理任一可用即可。"""
        self._on_status("隧道连接已就绪，验证公网入口…")
        started = time.monotonic()
        deadline = started + PUBLIC_ENDPOINT_TIMEOUT
        last_error = "尚未收到公网响应"
        next_progress = started + 10
        attempt = 0
        while time.monotonic() < deadline and not self._stopped.is_set():
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                last_error = f"隧道进程已退出（退出码 {proc.returncode}）"
                self._verification_error = last_error
                log.warning("%s 公网校验中止：%s", endpoint.host, last_error)
                return False
            attempt += 1
            if endpoint.provider == "cloudflared":
                route_errors = []
                for route, direct in (("直连", True), ("系统代理", False)):
                    try:
                        req = urllib.request.Request(
                            f"https://{endpoint.host}/",
                            headers={"User-Agent": "PotSync/1.0"})
                        with self._open_public_url(req, timeout=8, direct=direct) as resp:
                            body = resp.read(256)
                        if b"PotSync" in body:
                            elapsed = time.monotonic() - started
                            log.info("%s 公网校验通过（%s），用时 %.1f 秒",
                                     endpoint.host, route, elapsed)
                            return True
                        route_errors.append(f"{route}: 公网响应不是 PotSync 中继服务")
                        log.debug("公网校验返回非中继内容: %r", body[:160])
                    except Exception as exc:
                        route_errors.append(f"{route}: {type(exc).__name__}: {exc}")
                last_error = "；".join(route_errors)
            else:
                try:
                    import socket as _sock
                    with _sock.create_connection((endpoint.host, endpoint.port),
                                                 timeout=8):
                        return True
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
            log.debug("公网校验第 %d 次失败: %s", attempt, last_error)
            now = time.monotonic()
            if now >= next_progress:
                self._on_status(
                    f"等待公网地址生效… 已等待 {int(now - started)} 秒；"
                    f"最近结果：{last_error}")
                next_progress = now + 10
            self._stopped.wait(min(2, max(0, deadline - now)))
        self._verification_error = last_error
        if not self._stopped.is_set():
            log.warning("%s 公网校验超时: %s", endpoint.host, last_error)
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
        deadline = time.monotonic() + STARTUP_TIMEOUT
        endpoint: Optional[PublicEndpoint] = None
        output = queue.Queue()
        # 单独读管道：即使进程完全静默，主线程仍能执行超时与退出检查。
        reader = threading.Thread(target=self._read_output, args=(proc, output),
                                  name=f"potsync-{provider.name}-output", daemon=True)
        reader.start()
        tail = []
        candidate: Optional[PublicEndpoint] = None
        try:
            while not self._stopped.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stage = "完成隧道注册" if candidate is not None else "获取到公网地址"
                    raise TimeoutError(f"{STARTUP_TIMEOUT} 秒内未{stage}")
                if proc.poll() is not None and output.empty():
                    detail = "；".join(tail[-3:]) or "无输出"
                    raise RuntimeError(
                        f"隧道进程提前退出（{proc.returncode}）：{detail}")
                try:
                    line = output.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    detail = "；".join(tail[-3:]) or "无输出"
                    try:
                        returncode = proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        returncode = None
                    state = (f"退出码 {returncode}" if returncode is not None
                             else "进程仍在运行")
                    raise RuntimeError(f"隧道输出已结束（{state}）：{detail}")
                line = line.strip()
                if line:
                    log.debug("[%s] %s", provider.name, line)
                    tail.append(line)
                    del tail[:-8]
                if candidate is None:
                    candidate = provider.parse_line(line)
                    if candidate is not None and provider.requires_registration:
                        self._on_status("公网地址已分配，等待隧道连接注册…")
                if candidate is not None and provider.registration_ready(line):
                    endpoint = candidate
                    return endpoint
            return None
        finally:
            if endpoint is None:
                self._terminate_process(proc)
                if self._proc is proc:
                    self._proc = None
                reader.join(timeout=1)
            else:
                # 保活进程，同时继续排空日志队列，避免长跑后内存增长。
                threading.Thread(target=self._drain_output, args=(output,),
                                 name=f"potsync-{provider.name}-drain",
                                 daemon=True).start()

    @staticmethod
    def _drain_output(output: queue.Queue) -> None:
        while True:
            try:
                if output.get() is None:
                    return
            except Exception:
                return

    @staticmethod
    def _read_output(proc: subprocess.Popen, output: queue.Queue) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                output.put(line)
        except Exception as exc:
            log.debug("读取隧道输出失败: %s", exc)
        finally:
            output.put(None)

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
        except Exception as exc:
            log.debug("回收隧道进程失败: %s", exc)

    def _kill_current(self) -> None:
        proc = self._proc
        if proc is not None:
            self._terminate_process(proc)
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
