#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一的提示 / 确认对话框。

不用 QMessageBox：它在 Win11 下既没有云母背板，按钮和排版也和主界面不在
一个体系里。这里用同一个对话框基座，保证提示、警告、确认的观感一致。
"""

import logging

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

import theme

log = logging.getLogger("potsync.message")


class MessageDialog(QDialog):
    def __init__(self, parent, title: str, text: str,
                 ok_text: str = "好", cancel_text: str = "",
                 kind: str = "info") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint |
                            Qt.WindowCloseButtonHint | Qt.WindowSystemMenuHint)
        self.setModal(True)
        self.setMinimumWidth(460)
        self._translucent = theme.translucent_enabled()
        if self._translucent:
            self.setAttribute(Qt.WA_TranslucentBackground, True)

        pal = theme.current()
        accent = {"warn": pal.warn, "error": pal.danger}.get(kind, pal.accent_text)
        self.setStyleSheet(
            "QFrame#dialogSurface{background:%s;border:none;}"
            % theme.dialog_background(pal))

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self.surface = QFrame(self)
        self.surface.setObjectName("dialogSurface")
        shell.addWidget(self.surface)

        outer = QVBoxLayout(self.surface)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(10)

        head = QLabel(title)
        head.setObjectName("notesTitle")
        head.setStyleSheet(f"QLabel#notesTitle{{color:{accent};}}")
        outer.addWidget(head)

        body = QLabel(text)
        body.setObjectName("notesBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setStyleSheet(
            f"QLabel#notesBody{{color:{pal.text};font-size:13px;"
            "background:transparent;border:none;}")
        if len(text) > 260:            # 长文本（例如路径、错误详情）内部滚动
            area = QScrollArea(self)
            area.setObjectName("notesScroll")
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            holder = QWidget()
            lay = QVBoxLayout(holder)
            lay.setContentsMargins(0, 0, 8, 0)
            lay.addWidget(body)
            lay.addStretch(1)
            area.setWidget(holder)
            area.setMinimumHeight(160)
            area.setMaximumHeight(320)
            area.setStyleSheet("QScrollArea{background:transparent;border:none;}")
            outer.addWidget(area, 1)
        else:
            outer.addWidget(body)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        if cancel_text:
            self.btnCancel = QPushButton(cancel_text)
            self.btnCancel.setObjectName("outlineBtn")
            self.btnCancel.clicked.connect(self.reject)
            row.addWidget(self.btnCancel)
        self.btnAccept = QPushButton(ok_text)
        self.btnAccept.setObjectName("primaryBtn")
        self.btnAccept.setDefault(True)
        self.btnAccept.clicked.connect(self.accept)
        row.addWidget(self.btnAccept)
        outer.addLayout(row)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        theme.apply_titlebar(self, theme.current().mode == "dark")
        if self._translucent:
            theme.enable_backdrop(self)


def info(parent, title: str, text: str, ok_text: str = "好") -> None:
    MessageDialog(parent, title, text, ok_text=ok_text).exec_()


def warn(parent, title: str, text: str, ok_text: str = "好") -> None:
    MessageDialog(parent, title, text, ok_text=ok_text, kind="warn").exec_()


def error(parent, title: str, text: str, ok_text: str = "好") -> None:
    MessageDialog(parent, title, text, ok_text=ok_text, kind="error").exec_()


def ask(parent, title: str, text: str,
        ok_text: str = "确定", cancel_text: str = "取消") -> bool:
    dlg = MessageDialog(parent, title, text, ok_text=ok_text,
                        cancel_text=cancel_text, kind="question")
    return dlg.exec_() == QDialog.Accepted
