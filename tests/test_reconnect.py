#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
断线自动重连回归测试（2.0）
============================
真实启动 relay 子进程，客户端经一个可随时掐断的 TCP 代理接入：
1. 建房并广播状态；
2. 掐断代理连接（模拟网络波动），客户端应自动重连；
3. 重连后自动 resume，拿回同一房间、同一成员身份、同一份房间状态；
4. 关闭客户端时线程能干净退出。

运行：python tests/test_reconnect.py
"""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from net_client import NetClient  # noqa: E402

app = QCoreApplication(sys.argv)

SERVER = ROOT / "server" / "server.py"
PORT = 18772
PROXY_PORT = 18773


class ChaosProxy:
    """本地 TCP 转发；cut() 会掐断所有在途连接，模拟网络闪断。"""

    def __init__(self, listen_port: int, target_port: int) -> None:
        self.target_port = target_port
        self._conns = []
        self._lock = threading.Lock()
        self._stop = False
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", listen_port))
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(
                    ("127.0.0.1", self.target_port), timeout=3)
            except OSError:
                client.close()
                continue
            with self._lock:
                self._conns.append((client, upstream))
            threading.Thread(target=self._pipe, args=(client, upstream),
                             daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client),
                             daemon=True).start()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def cut(self) -> None:
        with self._lock:
            pairs, self._conns = list(self._conns), []
        for client, upstream in pairs:
            for s in (client, upstream):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                    s.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self.cut()


def wait_port(port: int, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def wait_for(pred, timeout: float = 10.0, desc: str = "") -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    app.processEvents()
    if desc:
        print("  …等待超时:", desc)
    return bool(pred())


def run_test() -> None:
    events = []
    net = NetClient()
    state = {"room": "", "token": "", "state": None, "resumed": 0, "lost": 0,
             "connecting": [], "resume_failed": ""}
    net.sigConnected.connect(lambda: events.append("connected"))
    net.sigConnecting.connect(
        lambda attempt, delay: (state["connecting"].append((attempt, delay)),
                                events.append(f"connecting{attempt}")))
    net.sigConnectionLost.connect(
        lambda reason: (state.__setitem__("lost", state["lost"] + 1),
                        events.append("lost")))
    net.sigRoomCreated.connect(
        lambda room, room_state, members: (state.__setitem__("room", room),
                                           state.__setitem__("state", room_state)))
    net.sigRoomResumed.connect(
        lambda room, room_state, members: (
            state.__setitem__("resumed", state["resumed"] + 1),
            state.__setitem__("room", room),
            state.__setitem__("state", room_state)))
    net.sigResumeFailed.connect(
        lambda reason: (state.__setitem__("resume_failed", reason),
                        events.append("resume_failed")))
    net.sigError.connect(lambda msg: events.append(f"error:{msg}"))

    net.connect_to("127.0.0.1", PROXY_PORT, "重连测试")
    assert wait_for(lambda: net.connected, 8, "首次连接"), events
    print("PASS  经代理连接 relay 服务器")

    net.create_room("重连测试")
    assert wait_for(lambda: bool(state["room"]), 8, "建房"), events
    room = state["room"]
    net.send_state(paused=True, position=50000, media="ep.mkv", duration=600000)
    time.sleep(0.3)
    app.processEvents()
    print(f"PASS  建房 {room} 并广播状态")

    # --- 掐断连接：应自动重连并 resume ---
    proxy.cut()
    assert wait_for(lambda: state["lost"] >= 1, 6, "掉线信号"), events
    assert wait_for(lambda: bool(state["connecting"]), 6, "重连尝试"), events
    assert wait_for(lambda: state["resumed"] >= 1, 15, "重连恢复"), events
    assert net.connected, "重连后应处于已连接状态"
    assert state["room"] == room, f"房间应保持不变: {state['room']} != {room}"
    reconnects = [(a, d) for a, d in state["connecting"] if a >= 2]
    assert reconnects and reconnects[0][1] >= 400, f"重连应退避: {reconnects}"
    resumed_state = state["state"] or {}
    assert int(resumed_state.get("position", 0)) == 50000, resumed_state
    assert resumed_state.get("media") == "ep.mkv", resumed_state
    print(f"PASS  闪断后自动重连（第 {state['resumed']} 次恢复），房间/身份/状态全部保留")

    # --- 再掐一次，验证可以反复重连 ---
    proxy.cut()
    assert wait_for(lambda: state["resumed"] >= 2, 15, "第二次重连"), events
    assert state["room"] == room
    print("PASS  连续闪断可反复重连")

    net.shutdown()
    assert wait_for(lambda: not net._thread.is_alive() if net._thread else True,
                    5, "客户端线程退出")
    print("PASS  关闭客户端时网络线程干净退出")


proxy = None


def main() -> int:
    global proxy
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "WARNING"])
    try:
        assert wait_port(PORT), "relay 服务器启动失败"
        proxy = ChaosProxy(PROXY_PORT, PORT)
        assert wait_port(PROXY_PORT), "代理启动失败"
        run_test()
        print("\n全部通过 ✔")
        return 0
    finally:
        if proxy is not None:
            proxy.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
