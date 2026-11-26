#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端口占用回归：127.0.0.1 被别的服务占住时，主程序必须能识别出来并换端口。"""

import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from PyQt5.QtWidgets import QApplication  # noqa: E402

import relay  # noqa: E402
from main_window import MainWindow  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Squatter(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"error":"Missing or invalid Authorization header."}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D401
        pass


def start_squatter(port: int) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), _Squatter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return srv


def start_relay(port: int):
    hub = relay.Hub()
    loop = __import__("asyncio").new_event_loop()

    def run():
        __import__("asyncio").set_event_loop(loop)
        loop.run_until_complete(relay.serve("127.0.0.1", port, hub_instance=hub))

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.8)
    return loop


def probe(port: int):
    return MainWindow._local_relay_probe(object(), port)


def test_pick_lan_ip_falls_back_to_loopback():
    """端口没人监听时，候选地址都连不通，必须退回回环而不是给死地址。"""
    import socket

    import main_window

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                      # 端口空出来，谁都不监听
    chosen = main_window.pick_lan_ip(port)
    assert chosen == "127.0.0.1", f"没有监听时不应返回 {chosen}"
    assert "127.0.0.1" in main_window.lan_candidates()
    print("PASS  局域网地址都连不通时退回回环")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    failures = []

    # 1) 别人的 Web 服务占着端口 → 必须判失败
    p1 = free_port()
    srv = start_squatter(p1)
    ok, err = probe(p1)
    srv.shutdown()
    if ok:
        failures.append("被占用的端口应当判失败")
        print(f"FAIL 被占用端口被判成可用：{p1}")
    else:
        print(f"PASS 识别被其它服务占用（{p1}）：{err[:60]}")

    # 2) 真正的 PotSync 中继 → 必须判成功
    p2 = free_port()
    loop = start_relay(p2)
    ok, err = probe(p2)
    if ok:
        print(f"PASS 命中自家中继（{p2}）")
    else:
        failures.append("自家中继应当判成功")
        print(f"FAIL 自家中继被误判：{p2} {err}")
    loop.call_soon_threadsafe(loop.stop)

    # 3) 端口没人监听 → 判失败（不能把连接拒绝当成可用）
    p3 = free_port()
    ok, err = probe(p3)
    if not ok:
        print(f"PASS 无人监听判失败（{p3}）：{err[:60]}")
    else:
        failures.append("空端口应当判失败")
        print(f"FAIL 空端口被判成可用：{p3}")

    # 4) 「端口是否已被占用」预检：有人监听才算占用，空端口不能误报
    p4 = free_port()
    srv4 = start_squatter(p4)
    used = MainWindow._port_in_use(p4)
    srv4.shutdown()
    if used:
        print(f"PASS 预检识别监听中的端口（{p4}）")
    else:
        failures.append("监听中的端口应判为占用")
        print(f"FAIL 预检漏判监听端口：{p4}")

    p5 = free_port()
    if not MainWindow._port_in_use(p5):
        print(f"PASS 预检放行空闲端口（{p5}）")
    else:
        failures.append("空闲端口不应判为占用")
        print(f"FAIL 预检误报空闲端口：{p5}")

    # 6) 局域网地址选择：没有监听时退回回环
    try:
        test_pick_lan_ip_falls_back_to_loopback()
    except AssertionError as exc:
        failures.append(f"局域网地址选择：{exc}")
        print(f"FAIL 局域网地址选择：{exc}")

    app.processEvents()
    if failures:
        print("失败：", "；".join(failures))
        return 1
    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
