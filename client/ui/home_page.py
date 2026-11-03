#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主页：昵称 / 服务器 / 主机模式 / 建房 / 加入房间 / 检查更新。

构件把控件挂回主窗口（self.editNickname 等），窗口负责流程；
这样测试和内部代码仍可直接用窗口属性访问控件。
"""

from PyQt5.QtCore import Qt  # noqa: F401  (保持与其它界面文件一致)
from PyQt5.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QVBoxLayout, QWidget)

from version import APP_VERSION


def build_home(window) -> QWidget:
    page = QWidget()
    v = QVBoxLayout(page)
    v.setContentsMargins(36, 30, 36, 24)
    v.setSpacing(14)

    title = QLabel("PotSync 一起看")
    title.setObjectName("title")
    subtitle = QLabel("在多台电脑之间同步 PotPlayer 的播放、暂停与进度")
    subtitle.setObjectName("subtitle")
    v.addWidget(title)
    v.addWidget(subtitle)
    v.addSpacing(10)

    # 昵称
    row = QHBoxLayout()
    row.addWidget(QLabel("昵　称"))
    window.editNickname = QLineEdit()
    window.editNickname.setPlaceholderText("给自己起个名字（房间成员可见）")
    window.editNickname.setMaxLength(24)
    window.btnSaveNick = QPushButton("保存")
    window.btnSaveNick.setObjectName("smallBtn")
    window.btnSaveNick.clicked.connect(window._save_nickname)
    row.addWidget(window.editNickname, 1)
    row.addWidget(window.btnSaveNick)
    v.addLayout(row)

    # 服务器
    row = QHBoxLayout()
    row.addWidget(QLabel("服务器"))
    window.editServer = QLineEdit()
    window.editServer.setPlaceholderText("中继服务器地址，如 1.2.3.4:8765")
    row.addWidget(window.editServer, 1)
    v.addLayout(row)

    # 主机模式：本机内嵌服务器
    window.chkHost = QCheckBox("在本机启动服务器（我当主机，好友直连我的电脑）")
    window.chkHost.setToolTip("无需单独部署服务器：建房时自动在本机启动内置中继。\n"
                              "勾选下方内网穿透即可让异地好友加入。")
    window.chkHost.toggled.connect(window._toggle_host_mode)
    v.addWidget(window.chkHost)

    # 内网穿透
    row = QHBoxLayout()
    row.addSpacing(22)
    window.chkTunnel = QCheckBox("内网穿透（自动生成公网地址，异地好友可直接加入）")
    window.chkTunnel.setToolTip("通过 Cloudflare 免费隧道把本机服务器暴露到公网，\n"
                                "首次使用自动下载组件（约 30MB），无需注册。")
    row.addWidget(window.chkTunnel, 1)
    v.addLayout(row)

    v.addSpacing(8)
    window.btnCreate = QPushButton("创 建 房 间")
    window.btnCreate.setObjectName("primaryBtn")
    window.btnCreate.clicked.connect(window._create_room)
    v.addWidget(window.btnCreate)

    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setObjectName("divider")
    v.addWidget(line)

    v.addWidget(QLabel("加入好友的房间："))
    window.editJoin = QLineEdit()
    window.editJoin.setPlaceholderText("输入房间号 或 邀请链接 potsync://…")
    v.addWidget(window.editJoin)

    row = QHBoxLayout()
    window.btnJoin = QPushButton("加 入 房 间")
    window.btnJoin.setObjectName("primaryBtn")
    window.btnJoin.clicked.connect(window._join_from_input)
    window.btnJoinClip = QPushButton("📋 从剪贴板读取链接并加入")
    window.btnJoinClip.clicked.connect(window._join_from_clipboard)
    row.addWidget(window.btnJoin, 1)
    row.addWidget(window.btnJoinClip, 1)
    v.addLayout(row)

    tip = QLabel("提示：创建房间后把邀请链接发给好友，对方复制链接后点\n"
                 "「从剪贴板读取链接并加入」即可。双方需打开同一个视频文件。")
    tip.setObjectName("tip")
    tip.setWordWrap(True)
    v.addSpacing(6)
    v.addWidget(tip)
    v.addStretch(1)

    # 版本 + 检查更新
    row = QHBoxLayout()
    lbl_ver = QLabel(f"v{APP_VERSION}")
    lbl_ver.setObjectName("tip")
    row.addWidget(lbl_ver)
    row.addStretch(1)
    window.btnUpdate = QPushButton("检查更新")
    window.btnUpdate.setObjectName("smallBtn")
    window.btnUpdate.setFlat(True)
    window.btnUpdate.clicked.connect(lambda: window._check_update(silent=False))
    row.addWidget(window.btnUpdate)
    v.addLayout(row)
    return page
