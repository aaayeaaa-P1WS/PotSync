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
import theme  # noqa: E402
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

    # 侧边栏导航（未入房时房间页禁用）与主题切换
    assert w.sidebar.current() == 0
    assert not w.sidebar._buttons[1][0].isEnabled()
    w.sidebar._buttons[2][0].click()
    app.processEvents()
    assert w.stack.currentIndex() == 2
    w.sidebar._buttons[0][0].click()
    app.processEvents()
    assert w.stack.currentIndex() == 0
    print("PASS  侧边栏导航（设置页往返，未入房时房间页禁用）")

    for mode, word in (("light", "浅色"), ("dark", "深色")):
        w._on_theme_changed(theme.build_palette(mode))
        app.processEvents()
        assert word in w.lblThemeMode.text(), w.lblThemeMode.text()
    assert not w.windowIcon().isNull()
    print("PASS  深浅色主题切换与窗口图标")

    # 强调色跟随系统：图标与强调色变体都要能生成（3 号同步箭头图形）
    from ui import appicon
    for accent in ("#0067C0", "#107C10", "#8764B8", "#C2410C"):
        assert not appicon.icon(accent).isNull(), accent
        assert not appicon.pixmap(24, accent).isNull(), accent
    pal = theme.build_palette("dark", "#8764B8")
    assert pal.accent == theme.fit_accent("#8764B8", "dark")
    print(f"PASS  强调色跟随（多色图标生成，当前 {pal.accent}）")

    # 设置页：冗余说明收进 ⓘ，按钮有边框；更新说明可滚动
    tips = w.settings_page.findChildren(type(w.btnUpdate), "infoBtn")
    assert len(tips) >= 3, len(tips)
    assert all(t.text_hint() for t in tips), "ⓘ 必须自带悬浮说明"
    assert all(not t.toolTip() for t in tips), "ⓘ 不再使用系统 tooltip（会延迟 700ms）"
    assert w.btnUpdate.objectName() == "outlineBtn"
    assert w.lblUpdateHint.text() == ""
    assert w.btnNotes.text() == "更新内容"
    print(f"PASS  设置页精简（{len(tips)} 个 ⓘ 即时说明 / 更新内容入口）")

    # 更新内容：纯文本解析 + 覆盖到 2.0.0
    import whatsnew
    secs = whatsnew.sections()
    versions = [v for v, _ in secs]
    assert "2.2.2" in versions and "2.0.0" in versions, versions
    text = whatsnew.load_text()
    assert "#" not in text and "**" not in text, "更新内容不能带 Markdown 符号"
    one = dict(secs)["2.2.0"]
    assert 2 <= len(one) <= 6, len(one)
    sec = whatsnew.section_for("v2.2.1")
    assert sec is not None and sec[1] == dict(secs)["2.2.1"], sec
    print("PASS  更新内容（纯文本 / 含 2.0.0 / 按版本取用）")

    # 更新弹窗：内容可滚动，且按钮文案明确
    from ui.notes_dialog import NotesDialog
    dlg = NotesDialog(w, whatsnew.recent_sections(3))
    dlg.show()
    app.processEvents()
    assert dlg.view.verticalScrollBar().maximum() > 0, "更新内容需要可滚动"
    assert dlg.btnAccept.text() == "知道了", "缺少关闭按钮"
    dlg.close()
    dlg.deleteLater()
    print("PASS  更新内容对话框（可滚动 / 有按钮）")

    # 半透明配色：开启时底色是 rgba，关闭时退回不透明 hex
    glass = theme.build_palette("dark", "#8764B8", translucent=True)
    solid = theme.build_palette("dark", "#8764B8", translucent=False)
    g_bg = glass.tokens()["bg_a"]
    s_bg = solid.tokens()["bg_a"]
    assert g_bg.startswith("rgba(") and not g_bg.endswith("1.00)"), g_bg
    assert s_bg.endswith("1.00)"), s_bg
    assert glass.tokens()["window_base"] == "transparent"
    assert solid.tokens()["window_base"] == solid.bg
    print("PASS  半透明配色（rgba 底色 / 关闭时退回不透明）")

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
