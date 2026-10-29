#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
隧道模块单元测试
================

不下载、不联网：验证隧道输出解析与 WebSocket URI 构造。
真实隧道联调见 tools/live_tunnel_check.py。

运行：python tests/test_tunnel.py
"""

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

import tunnel                                       # noqa: E402
from net_client import build_uri                     # noqa: E402
from tunnel import (BoreProvider, CloudflaredProvider,  # noqa: E402
                    TunnelManager, TunnelProvider)


class FakeProvider(TunnelProvider):
    """用本机 Python 模拟隧道进程，不下载组件、不联网。"""

    name = "fake-cloudflared"

    def __init__(self, script):
        self.script = script

    def ensure_binary(self, status):
        return Path(sys.executable)

    def command(self, exe, local_port):
        return [str(exe), "-c", self.script]

    def parse_line(self, line):
        return CloudflaredProvider().parse_line(line)


def test_silent_process_times_out_and_is_reaped():
    manager = TunnelManager(lambda ep: None, lambda error: None)
    provider = FakeProvider("import time; time.sleep(60)")
    children = []
    real_popen = subprocess.Popen

    def spawn(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    with patch("tunnel.subprocess.Popen", side_effect=spawn), \
            patch.object(tunnel, "STARTUP_TIMEOUT", 0.6):
        started = time.monotonic()
        try:
            manager._run_provider(provider, 8765)
            raise AssertionError("静默进程未触发启动超时")
        except TimeoutError as exc:
            assert "未获取到公网地址" in str(exc)
        elapsed = time.monotonic() - started
        assert len(children) == 1
        proc = children[0]
        assert elapsed < 3, f"静默进程阻塞了 {elapsed:.2f}s"
        assert proc.poll() is not None, "超时后隧道子进程未回收"
        assert manager._proc is None
    print(f"PASS  静默隧道有界超时并回收进程（{elapsed:.2f}s）")


def test_failed_public_check_is_bounded_and_diagnosable():
    errors = []
    manager = TunnelManager(lambda ep: None, errors.append)
    provider = FakeProvider(
        "import time; print('https://unit-test-only.trycloudflare.com', "
        "flush=True); time.sleep(60)")
    try:
        with patch.object(tunnel, "PROVIDERS", [provider]), \
                patch.object(tunnel, "STARTUP_TIMEOUT", 2), \
                patch.object(tunnel, "PUBLIC_ENDPOINT_TIMEOUT", 0.6), \
                patch.object(TunnelManager, "_open_public_url",
                             side_effect=OSError("模拟 DNS 尚未生效")) as request:
            started = time.monotonic()
            manager._run(8765)
            elapsed = time.monotonic() - started
        assert elapsed < 3, f"公网校验超时失效: {elapsed:.2f}s"
        assert request.call_count >= 1, "没有执行公网校验"
        assert len(errors) == 1 and "模拟 DNS 尚未生效" in errors[0]
        assert not manager.running, "公网校验失败后隧道进程未回收"
    finally:
        manager.stop()
    print(f"PASS  公网校验有界失败、保留错误且回收进程（{elapsed:.2f}s）")


class HealthyResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return b"PotSync relay server\n"


def public_endpoint():
    return tunnel.PublicEndpoint("unit-test-only.trycloudflare.com", 443,
                                 "cloudflared")


def test_public_check_retries_until_ready():
    manager = TunnelManager(lambda ep: None, lambda error: None)
    with patch.object(tunnel, "PUBLIC_ENDPOINT_TIMEOUT", 2), \
            patch.object(manager._stopped, "wait", return_value=False), \
            patch.object(TunnelManager, "_open_public_url",
                         side_effect=[OSError("直连暂时失败"), OSError("代理暂时失败"),
                                      HealthyResponse()]) as request:
        assert manager._verify_endpoint(public_endpoint())
        assert request.call_count == 3, "双路失败后没有继续重试公网校验"
    print("PASS  公网校验双路失败后继续重试至成功")


def test_public_check_uses_either_route():
    manager = TunnelManager(lambda ep: None, lambda error: None)
    with patch.object(TunnelManager, "_open_public_url",
                      side_effect=[OSError("直连 TLS 中断"), HealthyResponse()]) as request:
        assert manager._verify_endpoint(public_endpoint())
        assert request.call_args_list[0].kwargs["direct"] is True
        assert request.call_args_list[1].kwargs["direct"] is False
    print("PASS  直连失败时系统代理可独立完成公网校验")


def test_dead_tunnel_never_waits_for_public_timeout():
    class DeadProcess:
        returncode = 17

        def poll(self):
            return self.returncode

    manager = TunnelManager(lambda ep: None, lambda error: None)
    manager._proc = DeadProcess()
    try:
        with patch.object(TunnelManager, "_open_public_url") as request:
            started = time.monotonic()
            assert not manager._verify_endpoint(public_endpoint())
            assert time.monotonic() - started < 1
            assert not request.called, "进程已退出仍进行公网校验"
        assert "17" in manager._verification_error
    finally:
        manager.stop()
    print("PASS  隧道进程退出立即中止校验，不空等公网超时")


def test_cloudflared_waits_for_registration():
    class RegisteringProvider(FakeProvider):
        requires_registration = True

        def registration_ready(self, line):
            return "REGISTERED" in line

    manager = TunnelManager(lambda ep: None, lambda error: None)
    provider = RegisteringProvider(
        "import time; print('https://unit-test-only.trycloudflare.com', flush=True); "
        "time.sleep(0.4); print('REGISTERED', flush=True); time.sleep(60)")
    try:
        started = time.monotonic()
        endpoint = manager._run_provider(provider, 8765)
        elapsed = time.monotonic() - started
        assert endpoint is not None and elapsed >= 0.3, "地址出现后就过早开始公网校验"
        assert elapsed < 3
    finally:
        manager.stop()
    print("PASS  公网地址出现后仍等待隧道注册完成")


def test_http_proxy_choice_and_direct_fallback():
    import asyncio
    from net_client import NetClient, preferred_http_proxy

    proxies = {
        "http": "http://127.0.0.1:20890",
        "https": "http://127.0.0.1:20890",
        "socks": "socks://127.0.0.1:20890",
    }
    with patch("net_client.urllib.request.getproxies", return_value=proxies):
        assert preferred_http_proxy("wss://example.com:443") == \
            "http://127.0.0.1:20890"
        net = NetClient()
        calls, errors = [], []

        async def fake_session(uri, proxy=None):
            calls.append(proxy)
            if proxy is not None:
                raise OSError("模拟代理握手失败")
            net._connected_once = True

        net._session = fake_session
        net.sigError.connect(errors.append)
        asyncio.run(net._main("example.com", 443))
        assert calls == ["http://127.0.0.1:20890", None], calls
        assert not errors, errors
    print("PASS  混合代理环境优先 HTTP，首次失败自动回退直连")


def test_early_process_exit_reports_output():
    manager = TunnelManager(lambda ep: None, lambda error: None)
    provider = FakeProvider(
        "import sys; print('模拟边缘连接被拒绝', file=sys.stderr); sys.exit(17)")
    started = time.monotonic()
    try:
        manager._run_provider(provider, 8765)
        raise AssertionError("提前退出未被识别")
    except RuntimeError as exc:
        assert "17" in str(exc) and "模拟边缘连接被拒绝" in str(exc)
    assert time.monotonic() - started < 3
    assert manager._proc is None
    print("PASS  隧道进程提前退出立即报告原始错误")


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

    # bore 输出解析：错误日志中的控制端口绝不能当成公网入口。
    bore = BoreProvider()
    ep = bore.parse_line("listening at bore.pub:42069")
    assert ep is not None and ep.host == "bore.pub" and ep.port == 42069
    assert ep.display == "bore.pub:42069"
    assert bore.parse_line("connected to server") is None
    assert bore.parse_line("Error: could not connect to bore.pub:7835") is None
    print("PASS  bore 成功/错误输出解析（拒绝错误地址误判）")

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

    test_silent_process_times_out_and_is_reaped()
    test_failed_public_check_is_bounded_and_diagnosable()
    test_public_check_retries_until_ready()
    test_public_check_uses_either_route()
    test_dead_tunnel_never_waits_for_public_timeout()
    test_cloudflared_waits_for_registration()
    test_http_proxy_choice_and_direct_fallback()
    test_early_process_exit_reports_output()

    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
