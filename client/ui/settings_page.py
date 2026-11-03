#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置页：外观（跟随系统主题）、关于、日志与诊断。"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from version import APP_NAME, APP_VERSION


def _card(title: str, desc: str = "") -> tuple:
    card = QFrame()
    card.setObjectName("card")
    box = QVBoxLayout(card)
    box.setContentsMargins(20, 16, 20, 18)
    box.setSpacing(8)
    lbl = QLabel(title)
    lbl.setObjectName("sectionTitle")
    box.addWidget(lbl)
    if desc:
        sub = QLabel(desc)
        sub.setObjectName("tip")
        sub.setWordWrap(True)
        box.addWidget(sub)
    return card, box


def _kv(key: str, value: str) -> QWidget:
    holder = QWidget()
    h = QHBoxLayout(holder)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(12)
    k = QLabel(key)
    k.setObjectName("fieldLabel")
    k.setFixedWidth(72)
    val = QLabel(value)
    val.setObjectName("statusLine")
    val.setWordWrap(True)
    val.setTextInteractionFlags(Qt.TextSelectableByMouse)
    h.addWidget(k)
    h.addWidget(val, 1)
    return holder


def build_settings(window) -> QWidget:
    page = QWidget()
    v = QVBoxLayout(page)
    v.setContentsMargins(40, 28, 40, 22)
    v.setSpacing(14)

    title = QLabel("设置")
    title.setObjectName("title")
    v.addWidget(title)

    # ---------------- 外观
    card, box = _card("外观", "界面会跟随 Windows 的深色 / 浅色设置自动切换，"
                             "强调色取自系统主题色。")
    window.lblThemeMode = QLabel("当前：跟随系统")
    window.lblThemeMode.setObjectName("statusLine")
    box.addWidget(window.lblThemeMode)
    tip = QLabel("在「Windows 设置 → 个性化 → 颜色」里切换模式或主题色，"
                 "PotSync 会在 1.5 秒内自动适配，无需重启。")
    tip.setObjectName("tip")
    tip.setWordWrap(True)
    box.addWidget(tip)
    v.addWidget(card)

    # ---------------- 关于
    card, box = _card("关于")
    box.addWidget(_kv("应用", f"{APP_NAME} {APP_VERSION}"))
    box.addWidget(_kv("协议", "PotSync 2.0 · 断线自动重连 · 房间状态保留"))
    v.addWidget(card)

    # ---------------- 日志
    card, box = _card("日志与诊断", "运行日志会追加写入本地文件，遇到问题时可发给开发者排查。")
    from diagnostics import log_path
    try:
        path_text = str(log_path())
    except Exception:
        path_text = "~/.potsync/logs/potsync.log"
    box.addWidget(_kv("路径", path_text))

    row = QHBoxLayout()
    window.btnOpenLog = QPushButton("打开日志目录")
    window.btnOpenLog.setObjectName("outlineBtn")
    window.btnOpenLog.clicked.connect(window._open_log_dir)
    window.btnUpdateSettings = QPushButton("检查更新")
    window.btnUpdateSettings.setObjectName("outlineBtn")
    window.btnUpdateSettings.clicked.connect(lambda: window._check_update(silent=False))
    row.addWidget(window.btnOpenLog)
    row.addWidget(window.btnUpdateSettings)
    row.addStretch(1)
    box.addLayout(row)
    v.addWidget(card)

    v.addStretch(1)
    return page
