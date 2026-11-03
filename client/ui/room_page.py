#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""房间页：房间号 / 成员 / 正在播放 / 进度与控制 / 日志。

版式同主页：顶部信息区 + 两栏卡片 + 可伸缩的日志卡片。
控件挂回主窗口，流程（信号、状态刷新）仍在 main_window.py。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget,
                             QPlainTextEdit, QPushButton, QSlider,
                             QVBoxLayout, QWidget)


def _card(title: str, stretch: int = 0) -> tuple:
    card = QFrame()
    card.setObjectName("card")
    box = QVBoxLayout(card)
    box.setContentsMargins(18, 14, 18, 16)
    box.setSpacing(10)
    head = QHBoxLayout()
    head.setContentsMargins(0, 0, 0, 0)
    lbl = QLabel(title)
    lbl.setObjectName("cardTitle")
    head.addWidget(lbl)
    head.addStretch(1)
    box.addLayout(head)
    return card, box, head


def build_room(window) -> QWidget:
    page = QWidget()
    v = QVBoxLayout(page)
    v.setContentsMargins(32, 24, 32, 18)
    v.setSpacing(14)

    # ---------------- 顶部：房间号 + 操作
    top = QHBoxLayout()
    top.setSpacing(16)
    col = QVBoxLayout()
    col.setSpacing(2)
    lab = QLabel("房间号")
    lab.setObjectName("fieldLabel")
    window.lblRoomCode = QLabel("——————")
    window.lblRoomCode.setObjectName("roomCode")
    window.lblRoomCode.setTextInteractionFlags(Qt.TextSelectableByMouse)
    col.addWidget(lab)
    col.addWidget(window.lblRoomCode)
    top.addLayout(col, 1)

    window.btnCopyLink = QPushButton("复制邀请链接")
    window.btnCopyLink.setObjectName("outlineBtn")
    window.btnCopyLink.setMinimumHeight(32)
    window.btnCopyLink.setCursor(Qt.PointingHandCursor)
    window.btnCopyLink.clicked.connect(window._copy_invite_link)
    window.btnLeave = QPushButton("离开房间")
    window.btnLeave.setObjectName("dangerBtn")
    window.btnLeave.setMinimumHeight(32)
    window.btnLeave.setCursor(Qt.PointingHandCursor)
    window.btnLeave.clicked.connect(window._leave_room)
    top.addWidget(window.btnCopyLink, 0, Qt.AlignBottom)
    top.addWidget(window.btnLeave, 0, Qt.AlignBottom)
    v.addLayout(top)

    # ---------------- 中部：成员 | 正在播放
    middle = QHBoxLayout()
    middle.setSpacing(14)

    members_card, members_box, _ = _card("成员")
    members_card.setMinimumWidth(200)
    members_card.setMaximumWidth(240)
    window.listMembers = QListWidget()
    window.listMembers.setObjectName("memberList")
    window.listMembers.setFrameShape(QFrame.NoFrame)
    window.listMembers.setSpacing(1)
    members_box.addWidget(window.listMembers, 1)
    middle.addWidget(members_card)

    play_card, play_box, play_head = _card("正在播放")
    window.lblBadge = QLabel("等待连接")
    window.lblBadge.setObjectName("badge")
    play_head.addWidget(window.lblBadge)

    window.lblMedia = QLabel("媒体：—")
    window.lblMedia.setObjectName("mediaName")
    window.lblMedia.setWordWrap(True)
    play_box.addWidget(window.lblMedia)

    window.lblStatus = QLabel("状态：—")
    window.lblStatus.setObjectName("statusLine")
    window.lblStatus.setWordWrap(True)
    window.lblSync = QLabel("")
    window.lblSync.setObjectName("syncLabel")
    play_box.addWidget(window.lblStatus)
    play_box.addWidget(window.lblSync)
    play_box.addSpacing(2)

    prow = QHBoxLayout()
    prow.setSpacing(10)
    window.lblCur = QLabel("00:00")
    window.lblCur.setObjectName("tip")
    window.slider = QSlider(Qt.Horizontal)
    window.slider.setRange(0, 0)
    window.slider.setCursor(Qt.PointingHandCursor)
    window.slider.sliderPressed.connect(window._seek_start)
    window.slider.sliderReleased.connect(window._seek_commit)
    window.lblDur = QLabel("00:00")
    window.lblDur.setObjectName("tip")
    prow.addWidget(window.lblCur)
    prow.addWidget(window.slider, 1)
    prow.addWidget(window.lblDur)
    play_box.addLayout(prow)

    brow = QHBoxLayout()
    brow.setSpacing(8)
    window.btnPrev = QPushButton("上一集")
    window.btnPrev.setObjectName("outlineBtn")
    window.btnPrev.setToolTip("切换到播放列表上一个（全房间同步）")
    window.btnPrev.clicked.connect(lambda: window.engine.user_episode(-1))
    window.btnToggle = QPushButton("播放")
    window.btnToggle.setObjectName("primaryBtn")
    window.btnToggle.setMinimumHeight(34)
    window.btnToggle.setMinimumWidth(96)
    window.btnToggle.clicked.connect(window.engine.user_toggle_play_pause)
    window.btnNext = QPushButton("下一集")
    window.btnNext.setObjectName("outlineBtn")
    window.btnNext.setToolTip("切换到播放列表下一个（全房间同步）")
    window.btnNext.clicked.connect(lambda: window.engine.user_episode(+1))
    window.btnSyncMe = QPushButton("以我的进度为准")
    window.btnSyncMe.setObjectName("outlineBtn")
    window.btnSyncMe.clicked.connect(window.engine.user_force_sync_to_me)
    brow.addWidget(window.btnPrev)
    brow.addWidget(window.btnToggle)
    brow.addWidget(window.btnNext)
    brow.addStretch(1)
    brow.addWidget(window.btnSyncMe)
    play_box.addLayout(brow)
    play_box.addStretch(1)
    middle.addWidget(play_card, 1)
    v.addLayout(middle, 1)

    # ---------------- 日志
    log_card, log_box, _ = _card("日志")
    window.txtLog = QPlainTextEdit()
    window.txtLog.setReadOnly(True)
    window.txtLog.setObjectName("logView")
    window.txtLog.setMaximumBlockCount(300)
    window.txtLog.setMinimumHeight(140)
    log_box.addWidget(window.txtLog, 1)
    v.addWidget(log_card, 1)
    return page
