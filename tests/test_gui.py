#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI 冒烟测试（离屏）
====================

不显示窗口，验证：界面构建、邀请链接解析、剪贴板复制/读取加入、
主机模式（勾选"在本机启动服务器"→ 内置中继启动 → 自动创建房间 → 进入房间页）。

运行：python tests/test_gui.py
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 本机开启系统代理时，强制本地回环地址直连（urllib/websockets 均读取该变量）
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from PyQt5.QtWidgets import QApplication  # noqa: E402
from PyQt5.QtGui import QGuiApplication   # noqa: E402

app = QApplication(sys.argv)

from main_window import MainWindow, detect_lan_ip, fmt_time, parse_invite  # noqa: E402
from version import APP_VERSION  # noqa: E402
import updater  # noqa: E402

# 测试中禁用静默检查更新：避免真实网络请求与弹窗阻塞离屏测试
updater.configured_source = lambda cfg: None


def wait_for(pred, timeout: float = 15.0, desc: str = "") -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    if desc:
        print(f"  …等待超时: {desc}")
    return False


def main() -> int:
    # 链接解析
    assert parse_invite("potsync://1.2.3.4:8765/AB12CD") == ("1.2.3.4", 8765, "AB12CD")
    assert parse_invite("potsync://1.2.3.4/AB12CD") == ("1.2.3.4", 8765, "AB12CD")
    assert parse_invite("potsync://example.com/XY9Z22") == ("example.com", 443, "XY9Z22")
    assert parse_invite("potsync://abc-def.trycloudflare.com/XY9Z22") == \
        ("abc-def.trycloudflare.com", 443, "XY9Z22")
    assert parse_invite("  ab12cd ") == (None, None, "AB12CD")
    assert parse_invite("hello world") == (None, None, None)
    assert fmt_time(3661000) == "1:01:01"
    print("PASS  邀请链接解析 / 时间格式化")

    w = MainWindow()
    w.show()
    app.processEvents()

    # 标题栏与主页展示版本号，检查更新按钮可用
    assert APP_VERSION in w.windowTitle(), w.windowTitle()
    assert w.btnUpdate.text() == "检查更新" and w.btnUpdate.isEnabled()
    print(f"PASS  标题栏与主页版本号展示 -> v{APP_VERSION}")

    # 一键复制邀请链接
    w.room_code = "AB12CD"
    w._copy_invite_link()
    clip = QGuiApplication.clipboard().text()
    assert clip.startswith("potsync://") and clip.endswith("/AB12CD"), clip
    print(f"PASS  一键复制邀请链接 -> {clip}")

    # 从剪贴板读取链接并加入（自动填充房间号与服务器）
    w._join_from_clipboard()
    app.processEvents()
    assert w.editJoin.text() == "AB12CD"
    print("PASS  从剪贴板读取链接并加入（自动填充 + 触发加入）")

    # 主机模式：勾选后自动填本机 IP
    w.chkHost.setChecked(True)
    app.processEvents()
    host_field = w.editServer.text()
    assert host_field.endswith(":8765"), host_field
    assert host_field.split(":")[0] == detect_lan_ip(), host_field
    print(f"PASS  主机模式自动填充本机地址 -> {host_field}")

    # 主机模式创建房间：内嵌服务器启动 + 自动连接 + 进入房间页
    # （测试中关闭内网穿透，避免真实下载/建隧道；穿透链路见 tools/live_tunnel_check.py）
    w.chkTunnel.setChecked(False)
    w._create_room()
    assert wait_for(lambda: w.stack.currentIndex() == 1, 15, "创建房间并进入房间页")
    code = w.lblRoomCode.text()
    assert len(code) == 6, code
    assert w.embedded is not None and w.embedded.running
    print(f"PASS  主机模式一键建房：内嵌服务器运行中，房间 {code}")

    # 成员列表显示自己
    assert w.listMembers.count() >= 1
    print("PASS  房间页成员列表")

    # 收尾：断开并停止内嵌服务器
    w.net.leave_room()
    w._stop_embedded()
    assert w.embedded is None
    print("PASS  离开房间并停止内嵌服务器")

    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
