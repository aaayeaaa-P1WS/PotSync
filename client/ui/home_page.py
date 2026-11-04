#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主页：个人资料 / 创建房间 / 加入房间。

版式参考 Windows 11 设置应用的「卡片 + 分组」：标题区、分组卡片、
右对齐的说明文字与底部工具条。控件仍挂回主窗口（self.editNickname 等），
窗口负责流程；这样测试和内部代码可直接用窗口属性访问控件。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
                             QWidget)


def _card(title: str, desc: str = "", spacing: int = 10) -> tuple:
    """返回 (卡片, 内容布局)。卡片自带标题与说明，内容从分割线下方开始。"""
    card = QFrame()
    card.setObjectName("card")
    box = QVBoxLayout(card)
    box.setContentsMargins(18, 14, 18, 16)
    box.setSpacing(spacing)

    head = QVBoxLayout()
    head.setSpacing(3)
    lbl = QLabel(title)
    lbl.setObjectName("sectionTitle")
    head.addWidget(lbl)
    if desc:
        sub = QLabel(desc)
        sub.setObjectName("tip")
        sub.setWordWrap(True)
        head.addWidget(sub)
    box.addLayout(head)
    return card, box


def _field(label: str, widget: QWidget, hint: str = "") -> QWidget:
    """标签在上、控件在下的表单行（Win11 设置页常见排法）。"""
    holder = QWidget()
    v = QVBoxLayout(holder)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(4)
    lab = QLabel(label)
    lab.setObjectName("fieldLabel")
    v.addWidget(lab)
    v.addWidget(widget)
    if hint:
        tip = QLabel(hint)
        tip.setObjectName("tip")
        tip.setWordWrap(True)
        v.addWidget(tip)
    return holder


def build_home(window) -> QWidget:
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    outer.addWidget(scroll)

    body = QWidget()
    scroll.setWidget(body)
    body_row = QHBoxLayout(body)
    body_row.setContentsMargins(32, 22, 32, 16)
    body_row.setSpacing(0)

    column = QWidget()
    column.setMaximumWidth(860)
    column.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    v = QVBoxLayout(column)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(12)

    # ---------------- 标题区
    header = QVBoxLayout()
    header.setSpacing(4)
    title = QLabel("一起看")
    title.setObjectName("title")
    subtitle = QLabel("在多台电脑之间同步 PotPlayer 的播放、暂停与进度")
    subtitle.setObjectName("subtitle")
    header.addWidget(title)
    header.addWidget(subtitle)
    v.addLayout(header)
    v.addSpacing(4)

    # ---------------- 个人资料
    card, box = _card("个人资料", "昵称会显示在房间成员列表里，随时可以改。")
    row = QHBoxLayout()
    row.setSpacing(8)
    window.editNickname = QLineEdit()
    window.editNickname.setPlaceholderText("给自己起个名字")
    window.editNickname.setMaxLength(24)
    window.btnSaveNick = QPushButton("保存")
    window.btnSaveNick.setObjectName("outlineBtn")
    window.btnSaveNick.setMinimumHeight(34)
    window.btnSaveNick.clicked.connect(window._save_nickname)
    row.addWidget(window.editNickname, 1)
    row.addWidget(window.btnSaveNick)
    box.addLayout(row)
    v.addWidget(card)

    # ---------------- 创建房间
    card, box = _card("创建房间", "由你来当主机，好友通过房间号加入。")
    window.chkHost = QCheckBox("在本机启动中继服务器")
    window.chkHost.setToolTip("无需单独部署服务器：建房时自动在本机启动内置中继。\n"
                              "勾选下方内网穿透即可让异地好友加入。")
    window.chkHost.toggled.connect(window._toggle_host_mode)
    box.addWidget(window.chkHost)

    tunnel_row = QHBoxLayout()
    tunnel_row.setContentsMargins(22, 0, 0, 0)
    window.chkTunnel = QCheckBox("内网穿透（自动生成公网地址，异地好友可直接加入）")
    window.chkTunnel.setToolTip("通过 Cloudflare 免费隧道把本机服务器暴露到公网，\n"
                                "首次使用自动下载组件（约 30MB），无需注册。")
    tunnel_row.addWidget(window.chkTunnel, 1)
    box.addLayout(tunnel_row)

    window.btnCreate = QPushButton("创建房间")
    window.btnCreate.setObjectName("primaryBtn")
    window.btnCreate.setMinimumHeight(36)
    window.btnCreate.clicked.connect(window._create_room)
    box.addWidget(window.btnCreate)
    v.addWidget(card)

    # ---------------- 加入房间
    card, box = _card("加入房间", "输入好友分享的房间号或邀请链接。")
    window.editServer = QLineEdit()
    window.editServer.setPlaceholderText("1.2.3.4:8765")
    box.addWidget(_field("服务器地址", window.editServer,
                         "局域网填内网地址；好友用公网地址（主机开了内网穿透会自动生成）。"))

    window.editJoin = QLineEdit()
    window.editJoin.setPlaceholderText("房间号，或 potsync://… 邀请链接")
    box.addWidget(_field("房间号 / 邀请链接", window.editJoin))

    row = QHBoxLayout()
    row.setSpacing(8)
    window.btnJoin = QPushButton("加入房间")
    window.btnJoin.setObjectName("primaryBtn")
    window.btnJoin.setMinimumHeight(36)
    window.btnJoin.clicked.connect(window._join_from_input)
    window.btnJoinClip = QPushButton("从剪贴板读取")
    window.btnJoinClip.setObjectName("outlineBtn")
    window.btnJoinClip.setMinimumHeight(36)
    window.btnJoinClip.setToolTip("读取剪贴板里的邀请链接并直接加入")
    window.btnJoinClip.clicked.connect(window._join_from_clipboard)
    row.addWidget(window.btnJoin, 1)
    row.addWidget(window.btnJoinClip)
    box.addLayout(row)

    tip = QLabel("双方需要打开同一个视频文件；房间号即邀请凭证，请只分享给一起看的人。")
    tip.setObjectName("tip")
    tip.setWordWrap(True)
    box.addWidget(tip)
    v.addWidget(card)

    v.addStretch(1)

    body_row.addWidget(column, 1)
    return page
