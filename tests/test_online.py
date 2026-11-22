#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公网互联回归测试（2.3.0）
==========================
覆盖四件事：
1. 隧道并发竞速：HTTPS 入口优先，被网络拦截时快速回落到 TCP 入口；
2. Pinggy 输出解析（含 ANSI 颜色码）与「不需要注册确认」的约定；
3. 时钟滤波：心跳 RTT 抖动不会直接灌进服务器时间，抖动幅度可被引擎读取；
4. 进度对齐死区：网络延迟造成的小幅偏差不再触发 seek。

运行：python tests/test_online.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from PyQt5.QtCore import QCoreApplication  # noqa: E402

import tunnel  # noqa: E402
from net_client import NetClient  # noqa: E402
from pot_bridge import STATUS_PAUSED, STATUS_RUNNING, Snapshot  # noqa: E402
from sync_engine import SyncEngine  # noqa: E402

app = QCoreApplication(sys.argv)

_TMP = tempfile.TemporaryDirectory()
tunnel.HEALTH_FILE = Path(_TMP.name) / "tunnel_health.json"


# --------------------------------------------------------------------------- #
# 1. 隧道竞速
# --------------------------------------------------------------------------- #
class FakeProvider(tunnel.TunnelProvider):
    """用 python 子进程冒充隧道：打印一行地址后挂住。"""

    def __init__(self, name: str, kind: str, line: str, delay: float = 0.0):
        self.name = name
        self.kind = kind
        self._line = line
        self._delay = delay

    def binary(self) -> Path:
        return Path(sys.executable)

    def download(self, exe, status) -> None:  # pragma: no cover
        raise AssertionError("不应下载")

    def command(self, exe, local_port) -> list:
        code = ("import time;time.sleep(%r);print(%r,flush=True);"
                "time.sleep(30)" % (self._delay, self._line))
        return [sys.executable, "-c", code]

    def parse_line(self, line: str):
        marker = self._line.strip()
        if marker in line:
            host = marker.split("://")[-1].split(":")[0]
            port = 443 if marker.startswith("https://") else int(
                marker.rsplit(":", 1)[-1])
            return tunnel.PublicEndpoint(host, port, self.name)
        return None


def _manager():
    status = []
    errors = []
    ready = []
    mgr = tunnel.TunnelManager(ready.append, errors.append, status.append)
    return mgr, status, errors, ready


def test_endpoint_kind_and_display():
    https = tunnel.PublicEndpoint("a.trycloudflare.com", 443, "cloudflared")
    tcp = tunnel.PublicEndpoint("bore.pub", 43210, "bore")
    assert https.kind == "https" and https.display == "https://a.trycloudflare.com"
    assert tcp.kind == "tcp" and tcp.display == "bore.pub:43210"
    print("PASS  公网入口类型与展示（https 用域名，tcp 用 host:port）")


def test_race_prefers_https():
    fast_tcp = FakeProvider("fasttcp", "tcp", "bore.pub:41001", delay=0.0)
    slow_https = FakeProvider("slowhttps", "https",
                              "https://demo.trycloudflare.com", delay=0.5)
    mgr, status, errors, ready = _manager()

    def verify(self, endpoint, timeout=None, proc=None):
        return True

    try:
        with patch.object(tunnel, "PROVIDERS", [fast_tcp, slow_https]), \
                patch.object(tunnel.TunnelManager, "_verify_endpoint", verify):
            started = time.monotonic()
            mgr._run(8765)
            elapsed = time.monotonic() - started
        assert ready and ready[0].provider == "slowhttps", ready
        assert not errors, errors
        assert elapsed < 8, f"竞速耗时过长：{elapsed:.1f}s"
        assert mgr.running and mgr._winner_proc is not None, \
            "胜出入口的进程必须记录到 _winner_proc，否则会被 watcher 误判掉线"
        print(f"PASS  TCP 先就绪时仍等 HTTPS 入口胜出（{elapsed:.2f}s）")
    finally:
        mgr.stop()


def test_race_falls_back_when_https_blocked():
    tcp = FakeProvider("bore", "tcp", "bore.pub:41002", delay=0.0)
    blocked = FakeProvider("cloudflared", "https",
                           "https://blocked.trycloudflare.com", delay=0.2)
    mgr, status, errors, ready = _manager()

    def verify(self, endpoint, timeout=None, proc=None):
        if endpoint.kind == "https":
            self._verification_error = "域名无法解析（该服务可能被网络拦截）"
            return False
        return True

    try:
        with patch.object(tunnel, "PROVIDERS", [tcp, blocked]), \
                patch.object(tunnel.TunnelManager, "_verify_endpoint", verify):
            started = time.monotonic()
            mgr._run(8765)
            elapsed = time.monotonic() - started
        assert ready and ready[0].provider == "bore", ready
        assert elapsed < tunnel.RACE_HTTPS_GRACE + 4, f"回落偏慢：{elapsed:.1f}s"
        assert mgr.running and mgr._winner_proc is not None, \
            "TCP 兜底胜出后必须记录 _winner_proc，否则 watcher 会无限重建"
        print(f"PASS  HTTPS 被拦截时快速回落到 TCP 入口（{elapsed:.2f}s）")
    finally:
        mgr.stop()


# --------------------------------------------------------------------------- #
# 2. Pinggy 输出解析
# --------------------------------------------------------------------------- #
def test_pinggy_parse_line():
    provider = tunnel.PinggyProvider()
    assert not provider.requires_registration, "Pinggy 只要打印地址即可用"
    assert provider.registration_ready("随便什么") is True
    plain = provider.parse_line("https://kxcdv-240e.free.pinggy.net")
    assert plain is not None and plain.host == "kxcdv-240e.free.pinggy.net"
    assert plain.port == 443 and plain.display.startswith("https://")
    ansi = provider.parse_line("\x1b[32mhttps://abc-123.run.pinggy-free.link\x1b[0m")
    assert ansi is not None and ansi.host == "abc-123.run.pinggy-free.link"
    assert provider.parse_line("Permission denied (publickey).") is None
    print("PASS  Pinggy 地址解析（含 ANSI 颜色码，拒绝错误行）")


# --------------------------------------------------------------------------- #
# 3. 时钟滤波
# --------------------------------------------------------------------------- #
def test_clock_uses_min_rtt_sample():
    net = NetClient()
    net._clock_add(180, 9000)      # 抖动大的一次
    net._clock_add(40, 9500)       # RTT 最小、最可信的一次
    net._clock_add(120, 9100)
    assert net._clock_offset() == 9500, net._clock_offset()
    net.offset_ms = 7
    net._clock_samples.clear()
    assert net._clock_offset() == 7, "没有样本时应保留上一次的偏移"
    print("PASS  时钟偏移取 RTT 最小的样本，抽风样本不污染时间")


def test_rtt_jitter():
    net = NetClient()
    net._clock_add(100, 0)
    net._clock_add(140, 0)
    assert net.rtt_jitter() == 0, "样本少于 3 个时不报抖动"
    net._clock_add(90, 0)
    assert net.rtt_jitter() == 50, net.rtt_jitter()
    print("PASS  RTT 抖动可量化（供引擎放宽对齐阈值）")


# --------------------------------------------------------------------------- #
# 4. 进度对齐死区
# --------------------------------------------------------------------------- #
class DummyNet:
    def server_now_ms(self) -> int:
        return int(time.time() * 1000)

    def rtt_jitter(self) -> int:
        return 0


class ProbeBridge:
    def __init__(self, position: int, paused: bool = False) -> None:
        self.position = position
        self.duration = 600000
        self.paused = paused
        self.seeks = []

    def snapshot(self):
        return Snapshot(status=STATUS_PAUSED if self.paused else STATUS_RUNNING,
                        position=self.position, duration=self.duration,
                        media="ep01.mkv")

    def seek_ms(self, position: int) -> bool:
        self.seeks.append(position)
        self.position = position
        return True

    def pause(self) -> bool:
        self.paused = True
        return True

    def play(self) -> bool:
        self.paused = False
        return True


def _engine(bridge):
    engine = SyncEngine(bridge, DummyNet())
    engine._poll_timer.stop()
    engine._watchdog_timer.stop()
    return engine


def test_seek_deadband():
    bridge = ProbeBridge(10_000, paused=False)
    engine = _engine(bridge)
    try:
        engine.enter_room({"media": "ep01.mkv", "paused": False,
                           "position": 10_400, "duration": 600000,
                           "action": "state", "ts": int(time.time() * 1000)})
        bridge.seeks.clear()
        engine._last_seek_at = 0.0
        engine._align_to_state({"media": "ep01.mkv", "paused": False,
                                "position": 10_400, "duration": 600000,
                                "ts": int(time.time() * 1000)})
        assert not bridge.seeks, f"400ms 抖动不该 seek：{bridge.seeks}"
        engine._last_seek_at = 0.0
        engine._align_to_state({"media": "ep01.mkv", "paused": False,
                                "position": 13_000, "duration": 600000,
                                "ts": int(time.time() * 1000)})
        assert bridge.seeks, "大偏差仍应纠正"
        print("PASS  小幅网络偏差在死区内不触发 seek，大偏差照常纠正")
    finally:
        engine._poll_timer.stop()
        engine._watchdog_timer.stop()
        engine.deleteLater()
        app.processEvents()


def test_seek_throttle():
    bridge = ProbeBridge(0, paused=True)
    engine = _engine(bridge)
    try:
        engine.enter_room({"media": "ep01.mkv", "paused": True,
                           "position": 0, "duration": 600000,
                           "action": "state", "ts": int(time.time() * 1000)})
        bridge.seeks.clear()
        engine._last_seek_at = 0.0
        engine._align_to_state({"media": "ep01.mkv", "paused": True,
                                "position": 30_000, "duration": 600000,
                                "ts": int(time.time() * 1000)})
        first = len(bridge.seeks)
        engine._align_to_state({"media": "ep01.mkv", "paused": True,
                                "position": 5_000, "duration": 600000,
                                "ts": int(time.time() * 1000)})
        assert len(bridge.seeks) == first, "3 秒内不应连续 seek"
        print("PASS  同一时段只对齐一次，避免画面来回跳动")
    finally:
        engine._poll_timer.stop()
        engine._watchdog_timer.stop()
        engine.deleteLater()
        app.processEvents()


# --------------------------------------------------------------------------- #
# 公网入口校验：协议级探测
# --------------------------------------------------------------------------- #

def test_ws_probe_accepts_relay():
    """真正的中继会回 pong；用本地 WS 服务模拟。"""
    import json as _json
    import threading
    from websockets.sync.server import serve as ws_serve

    ready = threading.Event()
    port = {}

    def handler(conn):
        try:
            msg = conn.recv()
            data = _json.loads(msg)
            if data.get("type") == "ping":
                conn.send(_json.dumps({"type": "pong", "t": data.get("t")}))
                conn.send(_json.dumps({"type": "pong", "ts": 1}))
        finally:
            pass

    with ws_serve(handler, "127.0.0.1", 0) as server:
        port["p"] = server.socket.getsockname()[1]
        ready.set()
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            ok, err = tunnel._ws_probe("127.0.0.1", port["p"], "tcp", timeout=6)
        finally:
            server.shutdown()
    assert ok is True and err == "", (ok, err)
    print("PASS  WebSocket 探测能识别自家中继")


def test_ws_probe_rejects_plain_http():
    """别人的网页（普通 HTTP）应当被判为不可用。"""
    import http.server
    import socketserver
    import threading

    class Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html>not potsync</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Quiet)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        ok, err = tunnel._ws_probe("127.0.0.1", port, "tcp", timeout=6)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert ok is False and err, (ok, err)
    print("PASS  普通网页不会被误判为中继")


def main() -> int:
    test_endpoint_kind_and_display()
    test_race_prefers_https()
    test_race_falls_back_when_https_blocked()
    test_pinggy_parse_line()
    test_clock_uses_min_rtt_sample()
    test_rtt_jitter()
    test_seek_deadband()
    test_seek_throttle()
    test_ws_probe_accepts_relay()
    test_ws_probe_rejects_plain_http()
    _TMP.cleanup()
    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
