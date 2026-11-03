#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""房间页：房间号 / 成员 / 媒体信息 / 进度条 / 控制按钮 / 日志。

构件把控件挂回主窗口，流程（信号、状态刷新）仍在 main_window.py。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QPlainTextEdit,
                             QPushButton, QSlider, QVBoxLayout, QWidget)


def build_room(window) -> QWidget:
    page = QWidget()
    v = QVBoxLayout(page)
    v.setContentsMargins(28, 22, 28, 18)
    v.setSpacing(10)

    # 房间号 + 复制链接
    row = QHBoxLayout()
    col = QVBoxLayout()
    lab = QLabel("房间号")
    lab.setObjectName("subtitle")
    window.lblRoomCode = QLabel("——————")
    window.lblRoomCode.setObjectName("roomCode")
    window.lblRoomCode.setTextInteractionFlags(Qt.TextSelectableByMouse)
    col.addWidget(lab)
    col.addWidget(window.lblRoomCode)
    row.addLayout(col, 1)
    window.btnCopyLink = QPushButton("🔗 复制邀请链接")
    window.btnCopyLink.clicked.connect(window._copy_invite_link)
    row.addWidget(window.btnCopyLink, 0, Qt.AlignBottom)
    v.addLayout(row)

    # 成员 + 媒体信息
    row = QHBoxLayout()
    window.listMembers = QListWidget()
    window.listMembers.setObjectName("memberList")
    window.listMembers.setMaximumWidth(190)
    row.addWidget(window.listMembers)

    info = QVBoxLayout()
    window.lblMedia = QLabel("媒体：—")
    window.lblMedia.setWordWrap(True)
    window.lblStatus = QLabel("状态：—")
    window.lblSync = QLabel("")
    window.lblSync.setObjectName("syncLabel")
    info.addWidget(window.lblMedia)
    info.addWidget(window.lblStatus)
    info.addWidget(window.lblSync)
    info.addStretch(1)
    row.addLayout(info, 1)
    v.addLayout(row)

    # 进度条
    prow = QHBoxLayout()
    window.lblCur = QLabel("00:00")
    window.slider = QSlider(Qt.Horizontal)
    window.slider.setRange(0, 0)
    window.slider.sliderPressed.connect(window._seek_start)
    window.slider.sliderReleased.connect(window._seek_commit)
    window.lblDur = QLabel("00:00")
    prow.addWidget(window.lblCur)
    prow.addWidget(window.slider, 1)
    prow.addWidget(window.lblDur)
    v.addLayout(prow)

    # 控制按钮
    brow = QHBoxLayout()
    window.btnPrev = QPushButton("⏮ 上一集")
    window.btnPrev.setToolTip("切换到播放列表上一个（全房间同步）")
    window.btnPrev.clicked.connect(lambda: window.engine.user_episode(-1))
    window.btnToggle = QPushButton("▶ 播放")
    window.btnToggle.setObjectName("primaryBtn")
    window.btnToggle.clicked.connect(window.engine.user_toggle_play_pause)
    window.btnNext = QPushButton("⏭ 下一集")
    window.btnNext.setToolTip("切换到播放列表下一个（全房间同步）")
    window.btnNext.clicked.connect(lambda: window.engine.user_episode(+1))
    window.btnSyncMe = QPushButton("⟳ 以我的进度为准")
    window.btnSyncMe.clicked.connect(window.engine.user_force_sync_to_me)
    window.btnLeave = QPushButton("离开房间")
    window.btnLeave.setObjectName("dangerBtn")
    window.btnLeave.clicked.connect(window._leave_room)
    brow.addWidget(window.btnPrev, 2)
    brow.addWidget(window.btnToggle, 2)
    brow.addWidget(window.btnNext, 2)
    brow.addWidget(window.btnSyncMe, 2)
    brow.addWidget(window.btnLeave, 1)
    v.addLayout(brow)

    # 日志
    window.txtLog = QPlainTextEdit()
    window.txtLog.setReadOnly(True)
    window.txtLog.setObjectName("logView")
    window.txtLog.setMaximumBlockCount(300)
    v.addWidget(window.txtLog, 1)
    return page
