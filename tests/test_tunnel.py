#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
隧道模块单元测试
================

不下载、不联网：验证隧道输出解析与 WebSocket URI 构造。
真实隧道联调见 tools/live_tunnel_check.py。

运行：python tests/test_tunnel.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from net_client import build_uri                       # noqa: E402
from tunnel import BoreProvider, CloudflaredProvider   # noqa: E402


def main() -> int:
    # cloudflared 输出解析（真实日志样式）
    cf = CloudflaredProvider()
    line = ("2026-09-29T00:00:00Z INF |  https://tall-bird-flies-fast.trycloudflare.com"
            "  |")
    ep = cf.parse_line(line)
    assert ep is not None and ep.host == "tall-bird-flies-fast.trycloudflare.com"
    assert ep.port == 443 and ep.provider == "cloudflared"
    assert ep.display == "https://tall-bird-flies-fast.trycloudflare.com"
    assert cf.parse_line("2026-09-29T00:00:00Z INF Starting tunnel") is None
    print("PASS  cloudflared 输出解析")

    # bore 输出解析
    bore = BoreProvider()
    ep = bore.parse_line("listening at bore.pub:42069")
    assert ep is not None and ep.host == "bore.pub" and ep.port == 42069
    assert ep.display == "bore.pub:42069"
    assert bore.parse_line("connected to server") is None
    print("PASS  bore 输出解析")

    # WebSocket URI：443 → wss，其余 → ws
    assert build_uri("1.2.3.4", 8765) == "ws://1.2.3.4:8765"
    assert build_uri("x.trycloudflare.com", 443) == "wss://x.trycloudflare.com:443"
    print("PASS  ws/wss URI 构造")

    # 启动命令构造
    cf_cmd = cf.command(Path("cf.exe"), 8765)
    assert "--url" in cf_cmd and "http://127.0.0.1:8765" in cf_cmd
    bore_cmd = bore.command(Path("bore.exe"), 8765)
    assert bore_cmd[1:3] == ["local", "8765"] and "bore.pub" in bore_cmd
    print("PASS  启动命令构造")

    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
