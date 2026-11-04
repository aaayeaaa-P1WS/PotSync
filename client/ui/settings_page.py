#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置页：外观 / 关于 / 日志与诊断。

按 Windows 11 设置页的写法：卡片里只放「当前值 + 可操作项」，
解释性长文案收进「ⓘ」按钮的悬停提示，不再直接铺在界面上。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from ui.infobutton import InfoButton
from version import APP_NAME, APP_VERSION


def _card(title: str, spacing: int = 8) -> tuple:
    card = QFrame()
    card.setObjectName("card")
    box = QVBoxLayout(card)
    box.setContentsMargins(20, 16, 20, 18)
    box.setSpacing(spacing)
    lbl = QLabel(title)
    lbl.setObjectName("sectionTitle")
    box.addWidget(lbl)
    return card, box


def _info(text: str) -> QPushButton:
    """圈 i 按钮：说明放进悬停浮层，界面上只留一个图标。"""
    return InfoButton(text)


def _row(key: str, value: QLabel, info: str = "") -> QWidget:
    holder = QWidget()
    h = QHBoxLayout(holder)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(8)
    k = QLabel(key)
    k.setObjectName("fieldLabel")
    k.setFixedWidth(64)
    value.setObjectName("statusLine")
    value.setWordWrap(True)
    value.setTextInteractionFlags(Qt.TextSelectableByMouse)
    h.addWidget(k)
    h.addWidget(value, 1)
    if info:
        h.addWidget(_info(info))
    return holder


def _kv(key: str, value: str, info: str = "") -> QWidget:
    return _row(key, QLabel(value), info)


def build_settings(window) -> QWidget:
    page = QWidget()
    v = QVBoxLayout(page)
    v.setContentsMargins(40, 28, 40, 22)
    v.setSpacing(14)

    title = QLabel("设置")
    title.setObjectName("title")
    v.addWidget(title)

    # ---------------- 外观
    card, box = _card("外观")
    window.lblThemeMode = QLabel("当前：跟随系统")
    box.addWidget(_row(
        "模式", window.lblThemeMode,
        "跟随 Windows 的深色 / 浅色设置自动切换，1.5 秒内生效，无需重启。"))
    window.lblAccent = QLabel("强调色：跟随系统")
    box.addWidget(_row(
        "强调色", window.lblAccent,
        "取 Windows 主题色，用于按钮、选中项、勾选框与应用图标。\n"
        "在「Windows 设置 → 个性化 → 颜色」里改，界面上会实时跟着变。"))
    window.lblTranslucent = QLabel("半透明：跟随系统")
    box.addWidget(_row(
        "半透明", window.lblTranslucent,
        "使用 Windows 11 的 Mica 材质；系统版本过低或远程桌面下会自动退回不透明。\n"
        "想强制关闭半透明：设置环境变量 POTSYNC_OPAQUE=1。"))
    v.addWidget(card)

    # ---------------- 关于
    card, box = _card("关于")
    box.addWidget(_kv("应用", APP_NAME))
    box.addWidget(_kv("版本", f"v{APP_VERSION}"))
    box.addWidget(_kv("同步协议", "2.0 · 断线自动重连 · 房间状态保留"))

    row = QHBoxLayout()
    row.setSpacing(10)
    window.btnUpdate = QPushButton("检查更新")
    window.btnUpdate.setObjectName("outlineBtn")
    window.btnUpdate.clicked.connect(lambda: window._check_update(silent=False))
    row.addWidget(window.btnUpdate)
    window.btnNotes = QPushButton("更新内容")
    window.btnNotes.setObjectName("outlineBtn")
    window.btnNotes.clicked.connect(window._show_notes)
    row.addWidget(window.btnNotes)
    window.lblUpdateHint = QLabel("")
    window.lblUpdateHint.setObjectName("tip")
    row.addWidget(window.lblUpdateHint)
    row.addStretch(1)
    box.addLayout(row)
    v.addWidget(card)

    # ---------------- 日志
    card, box = _card("日志与诊断")
    from diagnostics import log_path
    try:
        path_text = str(log_path())
    except Exception:
        path_text = "~/.potsync/logs/potsync.log"
    box.addWidget(_kv(
        "日志", path_text,
        "运行日志会追加写入该文件；遇到问题时可把它发给开发者排查。"))

    row = QHBoxLayout()
    window.btnOpenLog = QPushButton("打开日志目录")
    window.btnOpenLog.setObjectName("outlineBtn")
    window.btnOpenLog.clicked.connect(window._open_log_dir)
    row.addWidget(window.btnOpenLog)
    row.addStretch(1)
    box.addLayout(row)
    v.addWidget(card)

    v.addStretch(1)
    return page
