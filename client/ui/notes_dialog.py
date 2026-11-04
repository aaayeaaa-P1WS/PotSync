#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「更新内容」对话框：可滚动、跟随主题、Win11 云母材质。

Windows 11 的对话框规范：标题 + 内容区 + 右下角主操作；内容多时在内容区
内部滚动，窗口本身不跟着变高。这里刻意不用 QMessageBox，因为它既不能滚动，
也没法套用云母背板，和主窗口放在一起会很突兀。
"""

import logging

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QTextBrowser, QVBoxLayout)

import theme

log = logging.getLogger("potsync.notes")


def _html(sections, accent: str, text_color: str, dim: str) -> str:
    """把 [(版本, [句子...]), ...] 渲染成简洁的 HTML。"""
    parts = ['<div style="margin:0;padding:0;">']
    for index, (version, lines) in enumerate(sections):
        top = "0px" if index == 0 else "18px"
        if version:
            parts.append(
                f'<p style="margin:{top} 0 6px 0;font-size:15px;font-weight:600;'
                f'color:{accent};">v{version}</p>')
        for line in lines:
            parts.append(
                f'<p style="margin:0 0 7px 0;font-size:13px;line-height:150%;'
                f'color:{text_color};">{_escape(line)}</p>')
    if not sections:
        parts.append(
            f'<p style="margin:0;font-size:13px;color:{dim};">这个版本没有提供更新说明。</p>')
    parts.append("</div>")
    return "".join(parts)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class NotesDialog(QDialog):
    """更新内容查看 / 更新确认共用的对话框。"""

    def __init__(self, parent, sections, subtitle: str = "",
                 accept_text: str = "知道了", cancel_text: str = ""):
        super().__init__(parent)
        self.setWindowTitle("更新内容")
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint |
                            Qt.WindowCloseButtonHint | Qt.WindowSystemMenuHint)
        self.setMinimumSize(560, 420)
        self.resize(600, 480)
        self._accept_text = accept_text
        self._cancel_text = cancel_text

        pal = theme.current()
        self._translucent = theme.translucent_enabled()
        if self._translucent:
            self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 对话框自己带一层高不透明度的表面：Mica 只透出一点，文字对比度才稳定
        # （否则碰上深色壁纸，浅色主题的对话框会变成黑底黑字）。
        self.setStyleSheet(
            "QFrame#dialogSurface{background:%s;border:none;}"
            % theme.dialog_background(pal))

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self.surface = QFrame(self)
        self.surface.setObjectName("dialogSurface")
        shell.addWidget(self.surface)

        outer = QVBoxLayout(self.surface)
        outer.setContentsMargins(24, 18, 24, 16)
        outer.setSpacing(10)

        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("notesSub")
            sub.setWordWrap(True)
            outer.addWidget(sub)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        outer.addWidget(divider)

        self.view = view = QTextBrowser(self)
        view.setObjectName("notesView")
        view.setFrameShape(QFrame.NoFrame)
        view.setOpenExternalLinks(True)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setStyleSheet("QTextBrowser{background:transparent;border:none;}")
        view.setHtml(_html(sections, pal.accent_text, pal.text, pal.text_muted))
        outer.addWidget(view, 1)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        if cancel_text:
            self.btnCancel = QPushButton(cancel_text)
            self.btnCancel.setObjectName("outlineBtn")
            self.btnCancel.clicked.connect(self.reject)
            row.addWidget(self.btnCancel)
        self.btnAccept = QPushButton(accept_text)
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


def show_notes(parent, sections, subtitle: str = "") -> None:
    """只读查看：设置页「更新内容」与更新完成后的首次提示都走这里。"""
    dlg = NotesDialog(parent, sections, subtitle=subtitle)
    dlg.exec_()


def ask_update(parent, version: str, sections, subtitle: str = "") -> bool:
    """更新前确认：展示更新内容，用户点「下载并更新」才继续。"""
    dlg = NotesDialog(parent, sections, subtitle=subtitle,
                      accept_text="下载并更新", cancel_text="稍后")
    return dlg.exec_() == QDialog.Accepted
