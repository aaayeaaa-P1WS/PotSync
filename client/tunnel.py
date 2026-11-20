#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内网穿透（公网隧道）管理
========================

主机模式下，把本机内嵌中继服务器暴露到公网，让异地好友直连加入。

三种免注册公共隧道，程序同时尝试、谁先可用用谁：

1. Cloudflare Quick Tunnel（cloudflared，约 30MB）
   → 生成 https://xxx.trycloudflare.com，WebSocket 走 wss://…:443
2. bore.pub 公共隧道（bore，约 2MB）
   → 生成 bore.pub:端口，纯 TCP 转发，走 ws://
3. Pinggy（系统自带 ssh，走 443 端口）
   → 生成 https://xxx.run.pinggy-free.link，免登录隧道约 60 分钟后过期

并发竞速的好处：不同网络对不同服务商的连通性差别很大，串行尝试会把
「最坏 35+120 秒再乘份数」变成用户看到的「创建房间一直转圈」。

TunnelManager 在独立线程中管理下载、启动、输出解析与进程回收，
通过回调向外通知（GUI 里请用 Qt 信号转投主线程）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
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
SSH_DIR = Path.home() / ".potsync" / "ssh"
HEALTH_FILE = Path.home() / ".potsync" / "tunnel_health.json"
HEALTH_TTL_S = 6 * 3600.0
STARTUP_TIMEOUT = 35          # 等待隧道给出公网地址的最长秒数（单次尝试）
PUBLIC_ENDPOINT_TIMEOUT = 120  # 实测新域名 DNS 可能超过 60 秒才生效
RACE_STARTUP_S = 18.0         # 竞速时每种方式最多等多久拿到地址
RACE_VERIFY_S = 14.0          # 竞速时拿到地址后最多校验多久
RACE_TOTAL_S = 36.0
RACE_HTTPS_GRACE = 5.0           # TCP 兜底前等 HTTPS 入口的时间上限
RACE_HTTPS_GRACE_PENDING = 9.0   # 已拿到 HTTPS 地址、正在校验时的宽限

CLOUDFLARED_URL = ("https://github.com/cloudflare/cloudflared/releases/"
                   "latest/download/cloudflared-windows-amd64.exe")
BORE_URL = ("https://github.com/ekzhang/bore/releases/download/"
            "v0.6.0/bore-v0.6.0-x86_64-pc-windows-msvc.zip")

TRYCF_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)")
BORE_RE = re.compile(r"([a-z0-9.-]*bore\.pub):(\d+)")


def _load_health() -> dict:
    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _mark_health(name: str, ok: bool) -> None:
    """记录隧道方式的可用性：被网络拦截的方式 6 小时内不再尝试。"""
    try:
        data = _load_health()
        if ok:
            data.pop(name, None)
        else:
            data[name] = time.time()
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_FILE.write_text(json.dumps(data, ensure_ascii=False),
                               encoding="utf-8")
    except Exception as exc:
        log.debug("记录隧道可用性失败: %s", exc)


class PublicEndpoint:
    """隧道公网入口。host/port 供客户端连接；display 供展示与复制。"""

    def __init__(self, host: str, port: int, provider: str,
                 kind: str = "") -> None:
        self.host = host
        self.port = port
        self.provider = provider
        self.kind = kind or ("https" if provider in ("cloudflared", "pinggy")
                             else "tcp")

    @property
    def display(self) -> str:
        if self.provider in ("cloudflared", "pinggy"):
            return f"https://{self.host}"
        return f"{self.host}:{self.port}"

    def __repr__(self) -> str:
        return f"PublicEndpoint({self.host}:{self.port} via {self.provider})"


class TunnelProvider:
    """一种隧道方式：二进制获取 + 启动命令 + 输出解析。"""

    name = "base"
    kind = "https"          # https: 用 HTTP GET 校验；tcp: 用 TCP 连接校验
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
    kind = "tcp"

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


class PinggyProvider(TunnelProvider):
    """Pinggy：借用系统 OpenSSH 的远程转发，走 443 端口，抗封锁能力好。

    首次运行时程序自己生成一对 ed25519 密钥（Pinggy 只要求有公钥即可），
    免登录隧道的公网地址形如 https://xxx.run.pinggy-free.link，
    大约 60 分钟后过期（到期后需要重新建立，界面会提示）。
    """

    name = "pinggy"
    kind = "https"
    PINGGY_RE = re.compile(
        r"https://([a-z0-9-]+\.(?:run\.pinggy-free\.link|free\.pinggy\.net))")
    ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

    def binary(self) -> Path:
        found = shutil.which("ssh")
        if found:
            return Path(found)
        return Path(r"C:\Windows\System32\OpenSSH\ssh.exe")

    def ensure_binary(self, status: Callable[[str], None]) -> Path:
        ssh = self.binary()
        if not ssh.exists():
            raise RuntimeError("系统缺少 OpenSSH 客户端（ssh.exe），无法使用 Pinggy")
        self._ensure_key(status)
        return ssh

    @staticmethod
    def key_path() -> Path:
        return SSH_DIR / "id_ed25519"

    def _ensure_key(self, status: Callable[[str], None]) -> None:
        key = self.key_path()
        if key.exists():
            return
        SSH_DIR.mkdir(parents=True, exist_ok=True)
        keygen = shutil.which("ssh-keygen") or str(
            Path(r"C:\Windows\System32\OpenSSH\ssh-keygen.exe"))
        if not Path(keygen).exists():
            raise RuntimeError("系统缺少 ssh-keygen，无法生成 Pinggy 密钥")
        status("正在生成隧道密钥（仅首次）…")
        proc = subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", "potsync",
             "-f", str(key)],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0 or not key.exists():
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"生成隧道密钥失败：{detail or proc.returncode}")

    def command(self, exe: Path, local_port: int) -> list:
        return [
            str(exe), "-p", "443",
            "-i", str(self.key_path()),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=NUL",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=20",
            "-o", "ServerAliveCountMax=3",
            "-R", f"0:localhost:{local_port}",
            "a.pinggy.io",
        ]

    def parse_line(self, line: str) -> Optional[PublicEndpoint]:
        clean = self.ANSI_RE.sub("", line)
        m = self.PINGGY_RE.search(clean)
        if m:
            return PublicEndpoint(m.group(1), 443, self.name)
        return None


PROVIDERS = [CloudflaredProvider(), BoreProvider(), PinggyProvider()]


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
        self._winner_proc: Optional[subprocess.Popen] = None
        self._live: dict = {}
        self._errors: list = []
        self._thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()

    @property
    def running(self) -> bool:
        proc = self._winner_proc
        return proc is not None and proc.poll() is None

    def start(self, local_port: int, preferred: Optional[str] = None) -> None:
        """并发尝试所有隧道方式，最先校验通过的胜出。

        preferred：上次成功的 provider 名字，仅用于提速（先启动它），
        不再串行依赖它。
        """
        if self._thread and self._thread.is_alive():
            return
        self._stopped.clear()
        self.endpoint = None
        self._errors = []
        self._thread = threading.Thread(
            target=self._run, args=(local_port, preferred),
            name="potsync-tunnel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        for name, proc in list(self._live.items()):
            self._terminate_process(proc)
        self._live.clear()
        proc = self._proc
        if proc is not None:
            self._terminate_process(proc)
        self._proc = None
        self._winner_proc = None

    # ---------- 内部 ----------

    def _providers_for(self, preferred: Optional[str]) -> list:
        health = _load_health()
        now = time.time()
        items = [p for p in PROVIDERS
                 if now - float(health.get(p.name, 0)) > HEALTH_TTL_S]
        if not items:
            items = list(PROVIDERS)
        if preferred:
            items.sort(key=lambda p: 0 if p.name == preferred else 1)
        return items

    def _run(self, local_port: int, preferred: Optional[str] = None) -> None:
        """所有方式同时开始；优先 HTTPS 入口，TCP 入口作兜底。"""
        providers = self._providers_for(preferred)
        if not providers:
            self._on_error("没有可用的隧道方式")
            return
        results: "queue.Queue" = queue.Queue()
        threads = []
        https_threads = []
        names = "、".join(p.name for p in providers)
        self._on_status(f"正在同时尝试 {names} …")
        for provider in providers:
            name = provider.name
            if name == "pinggy":
                exe = provider.binary()
                if not exe.exists():
                    results.put(("fail", name, "系统缺少 OpenSSH 客户端"))
                    continue
            t = threading.Thread(
                target=self._race_one, args=(provider, local_port, results),
                name=f"potsync-tunnel-{name}", daemon=True)
            t.start()
            threads.append(t)
            if provider.kind == "https":
                https_threads.append(t)

        deadline = time.monotonic() + RACE_TOTAL_S
        endpoint = None
        fallback = None
        fallback_at = 0.0
        https_pending = set()
        errors = []
        while time.monotonic() < deadline and not self._stopped.is_set():
            item = None
            try:
                item = results.get(timeout=0.2)
            except queue.Empty:
                pass
            if item is not None:
                if item[0] == "pending":
                    https_pending.add(item[1])
                elif item[0] == "ok":
                    ep = item[1]
                    if ep.kind == "https":
                        endpoint = ep
                        break
                    if fallback is None:
                        fallback = ep
                        fallback_at = time.monotonic()
                        log.info("TCP 兜底入口已就绪：%s，等待 HTTPS 入口", ep.display)
                else:
                    https_pending.discard(item[1])
                    errors.append(f"{item[1]}: {item[2]}")
            all_done = all(not t.is_alive() for t in threads)
            if fallback is not None and all_done:
                log.info("HTTPS 入口未就绪，使用 TCP 兜底：%s（%s）",
                         fallback.display, "；".join(errors) or "无错误")
                endpoint = fallback
                break
            if item is None and all_done:
                break
            if fallback is not None:
                waited = time.monotonic() - fallback_at
                no_hope = all(not t.is_alive() for t in threads)
                grace = (RACE_HTTPS_GRACE_PENDING if https_pending
                         else RACE_HTTPS_GRACE)
                https_alive = any(t.is_alive() for t in https_threads)
                if no_hope or (not https_pending and not https_alive):
                    grace = 0.0
                if waited >= grace or no_hope:
                    log.info("HTTPS 入口未就绪，使用 TCP 兜底：%s（%s）",
                             fallback.display, "；".join(errors) or "无错误")
                    endpoint = fallback
                    break
        if self._stopped.is_set():
            self._cleanup_losers(None)
            return
        if endpoint is None:
            for t in threads:
                t.join(timeout=0.2)
            self._cleanup_losers(None)
            self._on_error("；".join(errors) or "所有隧道方式均失败")
            return
        self._cleanup_losers(endpoint.provider)
        self.endpoint = endpoint
        self._on_ready(endpoint)

    def _race_one(self, provider, local_port, results) -> None:
        name = provider.name
        if self._stopped.is_set():
            return
        try:
            endpoint = self._run_provider(
                provider, local_port,
                startup_budget=min(STARTUP_TIMEOUT, RACE_STARTUP_S))
            if endpoint is None:
                if not self._stopped.is_set():
                    results.put(("fail", name, "未获取到公网地址"))
                self._terminate_named(name)
                return
            if self._stopped.is_set():
                self._terminate_named(name)
                return
            if endpoint.kind == "https" and not self._stopped.is_set():
                results.put(("pending", name))
            self._on_status(f"{name} 已分配地址，正在校验公网连通性…")
            verified = self._verify_endpoint(
                endpoint, timeout=min(PUBLIC_ENDPOINT_TIMEOUT, RACE_VERIFY_S),
                proc=self._live.get(name))
            if not verified:
                reason = getattr(self, "_verification_error", "公网地址未生效")
                if "域名" in reason or "TLS" in reason:
                    _mark_health(name, ok=False)
                results.put(("fail", name, reason))
                self._terminate_named(name)
                return
            log.info("%s 入口校验通过：%s", name, endpoint.display)
            _mark_health(name, ok=True)
            results.put(("ok", endpoint))
        except Exception as exc:
            log.warning("%s 隧道失败: %s", name, exc)
            if not self._stopped.is_set():
                results.put(("fail", name, str(exc)))
            self._terminate_named(name)

    def _terminate_named(self, name: str) -> None:
        proc = self._live.pop(name, None)
        if proc is not None:
            self._terminate_process(proc)
        if self._proc is proc:
            self._proc = None

    def _cleanup_losers(self, keep: Optional[str]) -> None:
        for name, proc in list(self._live.items()):
            if keep is not None and name == keep:
                continue
            self._terminate_process(proc)
            self._live.pop(name, None)

    @staticmethod
    def _open_public_url(req, timeout: int, direct: bool):
        """公网预检同时支持直连和系统代理，便于与实际客户端路径互备。"""
        if direct:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return opener.open(req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout)

    def _verify_endpoint(self, endpoint: PublicEndpoint,
                         timeout: Optional[float] = None,
                         proc: Optional[subprocess.Popen] = None) -> bool:
        """注册完成后验证公网入口；直连与系统代理任一可用即可。"""
        self._on_status("隧道连接已就绪，验证公网入口…")
        started = time.monotonic()
        budget = PUBLIC_ENDPOINT_TIMEOUT if timeout is None else timeout
        deadline = started + budget
        last_error = "尚未收到公网响应"
        next_progress = started + 10
        attempt = 0
        hard_fail = 0
        while time.monotonic() < deadline and not self._stopped.is_set():
            alive_check = proc if proc is not None else self._proc
            if alive_check is not None and alive_check.poll() is not None:
                last_error = f"隧道进程已退出（退出码 {alive_check.returncode}）"
                self._verification_error = last_error
                log.warning("%s 公网校验中止：%s", endpoint.host, last_error)
                return False
            attempt += 1
            if getattr(endpoint, "kind", "") == "https" or endpoint.provider == "cloudflared":
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
                blob = last_error.lower()
                if "getaddrinfo" in blob or "name or service not known" in blob:
                    hard_fail += 1
                    if hard_fail >= 2:
                        self._verification_error = "域名无法解析（该服务可能被网络拦截）"
                        log.warning("%s 域名解析失败，放弃该方式", endpoint.host)
                        return False
                elif ("remotedisconnected" in blob or "ssl" in blob
                      or "connection reset" in blob or "handshake" in blob):
                    hard_fail += 1
                    if hard_fail >= 2:
                        self._verification_error = "TLS 连接被中断（该服务可能被网络拦截）"
                        log.warning("%s TLS 握手被中断，放弃该方式", endpoint.host)
                        return False
            else:
                try:
                    import socket as _sock
                    with _sock.create_connection((endpoint.host, endpoint.port),
                                                 timeout=8) as sock:
                        sock.sendall(
                            b"GET / HTTP/1.0\r\nHost: "
                            + endpoint.host.encode("ascii", "ignore")
                            + b"\r\n\r\n")
                        data = b""
                        while len(data) < 512:
                            chunk = sock.recv(512)
                            if not chunk:
                                break
                            data += chunk
                    if b"PotSync" in data:
                        return True
                    last_error = "公网响应不是 PotSync 中继服务"
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
                      local_port: int,
                      startup_budget: Optional[float] = None) -> Optional[PublicEndpoint]:
        exe = provider.ensure_binary(self._on_status)
        if self._stopped.is_set():
            return None
        proc = subprocess.Popen(
            provider.command(exe, local_port),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._live[provider.name] = proc
        self._proc = proc
        budget = STARTUP_TIMEOUT if startup_budget is None else startup_budget
        deadline = time.monotonic() + budget
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
                    raise TimeoutError(f"{budget:.0f} 秒内未{stage}")
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
                    candidate.kind = provider.kind
                    endpoint = candidate
                    return endpoint
            return None
        finally:
            if endpoint is None:
                self._terminate_process(proc)
                if self._live.get(provider.name) is proc:
                    self._live.pop(provider.name, None)
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
            proc.wait(timeout=0.6)
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
