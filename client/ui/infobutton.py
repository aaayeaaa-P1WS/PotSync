#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""圈 i 说明按钮 + 即时说明浮层。

Qt 自带的 tooltip 有两个毛病：等 700ms 才出现（用户会以为按钮坏了），
样式也跟不上 Win11 的浮层。这里自己画一个：悬浮 90ms 就出现，浮层用
半透明卡片 + 系统背板，和主界面的磨砂质感保持一致。
"""

import logging

from PyQt5.QtCore import QEvent, QObject, QPoint, QTimer, Qt
from PyQt5.QtGui import QColor, QGuiApplication
from PyQt5.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

import theme

log = logging.getLogger("potsync.hint")

_HOVER_DELAY_MS = 90


class HintPopup(QFrame):
    """说明浮层：无边框、半透明、跟随主题。"""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

        outer = QVBoxLayout(self)
        # 留出阴影的空间，否则投影会被窗口裁掉
        outer.setContentsMargins(10, 10, 10, 14)
        self.card = QFrame(self)
        self.card.setObjectName("hintCard")
        inner = QVBoxLayout(self.card)
        inner.setContentsMargins(14, 11, 14, 12)
        self.label = QLabel(self.card)
        self.label.setObjectName("hintText")
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.NoTextInteraction)
        inner.addWidget(self.label)
        outer.addWidget(self.card)

        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 72))
        self.card.setGraphicsEffect(shadow)
        self.apply_palette(theme.current())

    # ------------------------------------------------------------------
    def apply_palette(self, pal) -> None:
        self.card.setStyleSheet(
            "QFrame#hintCard{"
            f"background: rgba({_rgb(pal.surface_alt)}, 0.97);"
            f"border: 1px solid rgba({_rgb(pal.border_strong)}, 0.75);"
            "border-radius: 9px;}")
        self.label.setStyleSheet(
            "QLabel#hintText{"
            f"color: {pal.text};"
            "font-size: 13px;"
            "background: transparent;"
            "border: none;}")

    def set_text(self, text: str) -> None:
        self.label.setText(text)
        width = 300
        self.label.setFixedWidth(272)
        self.card.adjustSize()
        self.adjustSize()

    def show_below(self, anchor: QWidget) -> None:
        screen = QGuiApplication.screenAt(anchor.mapToGlobal(QPoint(0, 0)))
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        size = self.sizeHint()
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() - 2))
        x = min(pos.x(), avail.right() - size.width() - 8)
        x = max(x, avail.left() + 8)
        y = pos.y()
        if y + size.height() > avail.bottom() - 8:      # 下方放不下就翻到上方
            y = anchor.mapToGlobal(QPoint(0, 0)).y() - size.height() - 8
        self.move(QPoint(x, max(y, avail.top() + 8)))
        self.show()
        self.raise_()


class InfoButton(QPushButton):
    """圈 i：悬停即显示说明，不再用 Qt 的 tooltip（延迟太长）。"""

    def __init__(self, text: str, parent: QWidget = None) -> None:
        super().__init__("i", parent)
        self.setObjectName("infoBtn")
        self.setFixedSize(18, 18)
        self.setCursor(Qt.WhatsThisCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._text = text
        self._popup = HintPopup(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_HOVER_DELAY_MS)
        self._timer.timeout.connect(self._show_hint)
        self.setToolTip("")          # 不注册系统 tooltip，避免双重提示

    def text_hint(self) -> str:
        return self._text

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._timer.start()

    def leaveEvent(self, event) -> None:
        self._timer.stop()
        _safe_hide(self._popup)
        super().leaveEvent(event)

    def _show_hint(self) -> None:
        if not self.isVisible():
            return
        self._popup.apply_palette(theme.current())
        self._popup.set_text(self._text)
        self._popup.show_below(self)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        _safe_hide(self._popup)
        super().hideEvent(event)


class _HintFilter(QObject):
    """给任意控件挂上即时说明（替代 Qt 自带的 700ms tooltip）。"""

    def __init__(self, widget: QWidget, text: str) -> None:
        super().__init__(widget)
        self.widget = widget
        self.text = text
        self.popup = HintPopup(None)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(_HOVER_DELAY_MS)
        self.timer.timeout.connect(self._show)
        widget.setToolTip("")
        widget.installEventFilter(self)
        widget.destroyed.connect(self._on_destroyed)

    def _on_destroyed(self, *_) -> None:
        self.timer.stop()
        _safe_hide(self.popup)

    def _show(self) -> None:
        if not self.widget.isVisible():
            return
        try:
            self.popup.apply_palette(theme.current())
            self.popup.set_text(self.text)
            self.popup.show_below(self.widget)
        except RuntimeError:
            pass

    def eventFilter(self, obj, event) -> bool:      # noqa: N802
        if obj is self.widget:
            kind = event.type()
            if kind in (QEvent.Enter, QEvent.HoverEnter):
                self.timer.start()
            elif kind in (QEvent.Leave, QEvent.Hide, QEvent.MouseButtonPress):
                self.timer.stop()
                _safe_hide(self.popup)
        return super().eventFilter(obj, event)


def attach_hint(widget: QWidget, text: str) -> _HintFilter:
    """给按钮/勾选框加即时说明，返回过滤器（已挂在控件上）。"""
    return _HintFilter(widget, text)


def _safe_hide(popup) -> None:
    try:
        popup.hide()
    except RuntimeError:
        pass


def _rgb(color: str) -> str:
    c = QColor(color)
    if not c.isValid():
        return "255, 255, 255"
    return f"{c.red()}, {c.green()}, {c.blue()}"
