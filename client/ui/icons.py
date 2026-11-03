#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 QPainter 画的极简线性图标（不依赖字体/图片资源）。

全部按当前主题颜色生成位图，切换深浅色时重新生成即可。
"""

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap)


def _dpr() -> float:
    try:
        from PyQt5.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        return float(screen.devicePixelRatio()) if screen else 1.0
    except Exception:
        return 1.0


def _canvas(size: int) -> tuple:
    ratio = _dpr()
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(ratio)
    return pm, ratio


def pixmap(kind: str, color: str, size: int = 18) -> QPixmap:
    pm, ratio = _canvas(size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    # 注意：QPixmap 设了 devicePixelRatio 后，QPainter 已按 DPR 放大坐标，
    # 这里不能再 scale 一次，否则图形会被裁到左上角。
    pen = QPen(QColor(color))
    pen.setWidthF(1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    s = float(size)

    if kind == "home":
        path = QPainterPath()
        path.moveTo(s * 0.11, s * 0.47)
        path.lineTo(s * 0.50, s * 0.13)
        path.lineTo(s * 0.89, s * 0.47)
        p.drawPath(path)
        p.drawRoundedRect(QRectF(s * 0.23, s * 0.44, s * 0.54, s * 0.43), 2.0, 2.0)

    elif kind == "room":
        p.drawRoundedRect(QRectF(s * 0.12, s * 0.18, s * 0.76, s * 0.64), s * 0.14, s * 0.14)
        tri = QPainterPath()
        tri.moveTo(QPointF(s * 0.42, s * 0.36))
        tri.lineTo(QPointF(s * 0.66, s * 0.50))
        tri.lineTo(QPointF(s * 0.42, s * 0.64))
        tri.closeSubpath()
        p.setBrush(QColor(color))
        p.drawPath(tri)

    elif kind == "settings":
        # 三根滑杆，比齿轮更贴近 Win11 设置图标
        for i, (y, knob) in enumerate(((0.30, 0.66), (0.50, 0.34), (0.70, 0.58))):
            p.drawLine(QPointF(s * 0.18, s * y), QPointF(s * 0.82, s * y))
            p.setBrush(QColor(color))
            p.drawEllipse(QPointF(s * knob, s * y), s * 0.085, s * 0.085)
            p.setBrush(Qt.NoBrush)

    elif kind == "sun":
        p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.18, s * 0.18)
        for i in range(8):
            import math
            a = math.pi * i / 4
            p.drawLine(QPointF(s * (0.5 + 0.27 * math.cos(a)), s * (0.5 + 0.27 * math.sin(a))),
                       QPointF(s * (0.5 + 0.36 * math.cos(a)), s * (0.5 + 0.36 * math.sin(a))))

    elif kind == "moon":
        outer = QPainterPath()
        outer.addEllipse(QRectF(s * 0.15, s * 0.14, s * 0.68, s * 0.68))
        inner = QPainterPath()
        inner.addEllipse(QRectF(s * 0.36, s * 0.02, s * 0.62, s * 0.62))
        p.fillPath(outer.subtracted(inner), QColor(color))

    elif kind == "link":
        p.drawRoundedRect(QRectF(s * 0.10, s * 0.30, s * 0.42, s * 0.26), s * 0.13, s * 0.13)
        p.drawRoundedRect(QRectF(s * 0.48, s * 0.44, s * 0.42, s * 0.26), s * 0.13, s * 0.13)

    elif kind == "log":
        for y in (0.28, 0.50, 0.72):
            p.drawLine(QPointF(s * 0.18, s * y), QPointF(s * 0.82, s * y))

    p.end()
    return pm


def icon(kind: str, color: str, active_color: str = None, size: int = 18) -> QIcon:
    """侧边导航用：普通态 color，选中态 active_color。"""
    ic = QIcon()
    ic.addPixmap(pixmap(kind, color, size), QIcon.Normal, QIcon.Off)
    ic.addPixmap(pixmap(kind, color, size), QIcon.Active, QIcon.Off)
    active = active_color or color
    ic.addPixmap(pixmap(kind, active, size), QIcon.Normal, QIcon.On)
    ic.addPixmap(pixmap(kind, active, size), QIcon.Active, QIcon.On)
    return ic


def logo(size: int = 28, accent: str = "#0067c0") -> QPixmap:
    """圆角方块 + 字母 P 的应用图标。"""
    pm, ratio = _canvas(size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor(accent).lighter(118))
    grad.setColorAt(1.0, QColor(accent).darker(112))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), size * 0.26, size * 0.26)

    font = QFont("Segoe UI Variable Display", int(size * 0.52))
    font.setWeight(QFont.DemiBold)
    p.setFont(font)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(0, -size * 0.02, size, size), Qt.AlignCenter, "P")
    p.end()
    return pm
