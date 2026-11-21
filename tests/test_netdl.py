#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载器 / 内置组件 / 隧道健康记忆 的离线测试（无需外网）。"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

import netdl          # noqa: E402
import tunnel         # noqa: E402


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_mirror_order():
    url = "https://github.com/a/b/releases/download/v1/x.exe"
    arms = netdl.candidate_urls(url)
    assert arms[-1] == url, "直连必须放在最后（国内直连常被重置）"
    assert arms[0].startswith("https://ghfast.top/"), f"应镜像优先：{arms[0]}"
    assert len(arms) == len(netdl.MIRRORS) + 1

    plain = "http://127.0.0.1:8000/x.json"
    assert netdl.candidate_urls(plain) == [plain], "非 GitHub 地址不应加镜像"
    print("PASS  下载候选：GitHub 镜像优先、直连兜底；其它地址原样")


def test_bundled_binaries():
    cf = tunnel._bundled_bin("cloudflared")
    bore = tunnel._bundled_bin("bore")
    assert cf is not None and cf.exists(), "cloudflared 应随程序内置"
    assert bore is not None and bore.exists(), "bore 应随程序内置"
    assert tunnel._bundled_bin("pinggy") is None, "pinggy 用系统 ssh，无内置文件"
    # ensure_binary 命中内置时不再走下载
    prov = tunnel.CloudflaredProvider()
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("有内置组件时不应下载")

    prov.download = boom          # type: ignore[assignment]
    assert prov.ensure_binary(lambda _s: None) == cf
    assert called["n"] == 0
    print("PASS  内置组件：cloudflared/bore 随程序分发，命中时不触发下载")


def test_health_memory():
    old = tunnel.HEALTH_FILE
    tmp = Path(tempfile.gettempdir()) / "potsync_health_test.json"
    try:
        tunnel.HEALTH_FILE = tmp
        now = time.time()
        tmp.write_text(json.dumps({
            "cloudflared": now,                     # 刚失败 → 暂时跳过
            "pinggy": now - 60 * 60,                # 1 小时前 → 已过期，重试
            "bore": now + 3 * 3600,                 # 时钟回拨脏数据 → 不能永久禁掉
        }), encoding="utf-8")
        mgr = tunnel.TunnelManager(lambda _ep: None, lambda _m: None,
                                   lambda _s: None)
        names = [p.name for p in mgr._providers_for(None)]
        assert "cloudflared" not in names, "刚失败的方式应暂时跳过"
        assert "pinggy" in names, "超过 30 分钟应重新尝试"
        assert "bore" in names, "未来时间戳属于脏数据，不能永久跳过"
        print("PASS  健康记忆：新鲜失败才跳过，过期/脏数据自动恢复尝试")
    finally:
        tunnel.HEALTH_FILE = old
        tmp.unlink(missing_ok=True)


def test_download_local():
    payload = os.urandom(1_200_000)
    hits = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits["n"] += 1
            rng = self.headers.get("Range")
            start = 0
            if rng and rng.startswith("bytes="):
                try:
                    start = int(rng.split("=")[1].split("-")[0])
                except ValueError:
                    start = 0
            body = payload[start:]
            self.send_response(206 if start else 200)
            if start:
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(payload)-1}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        dst = Path(tempfile.gettempdir()) / "potsync_netdl_test.bin"
        dst.unlink(missing_ok=True)
        seen = []
        netdl.fetch(f"http://127.0.0.1:{port}/blob", dst,
                    status=seen.append, expected_min=1_000_000)
        assert dst.read_bytes() == payload, "内容必须与源一致"
        assert seen and any("完" in s for s in seen), "应有进度与完成回调"

        # 脏残留：目标文件已存在但内容错误 → 必须重新下载而不是复用
        dst.write_bytes(b"\x00" * len(payload))
        netdl.fetch(f"http://127.0.0.1:{port}/blob", dst, expected_min=1_000_000)
        assert dst.read_bytes() == payload, "不能复用旧的脏文件"
        assert hits["n"] >= 2
        dst.unlink(missing_ok=True)
        print("PASS  下载器：内容一致、进度回调、脏残留不复用")
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> int:
    test_mirror_order()
    test_bundled_binaries()
    test_health_memory()
    test_download_local()
    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
