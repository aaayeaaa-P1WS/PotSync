#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用图标：环形同步箭头 + 播放键（跟随系统强调色）。

图形按矢量坐标绘制，任意尺寸重绘，保证 16px 到 256px 都清晰。
颜色由强调色推导：底色用强调色的明暗渐变，图形为白色。
"""

import math

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (QColor, QIcon, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap, QRadialGradient)

S = 1024.0

# 图形参数（与设计稿一致：箭头大、播放键略小，突出“同步”）
RING = 0.225          # 环半径（占图标宽度比例）
STROKE = 0.082        # 环线宽
SPAN = 138.0          # 每段弧的角度
GAP = 42.0            # 两段弧之间的缺口
HEAD_LEN = 0.145      # 箭头长度
HEAD_HALF = 0.085     # 箭头半宽
PLAY_W = 0.195        # 播放键宽度


def _accent_colors(accent: str):
    """由强调色推出底色渐变（左上亮、右下暗）。"""
    c = QColor(accent)
    light = c.lighter(122)
    dark = c.darker(118)
    return light.name(QColor.HexRgb), dark.name(QColor.HexRgb), c


def _paint(p: QPainter, accent: str, scale: float = 1.0) -> None:
    s = S * scale
    c1, c2, base = _accent_colors(accent)

    # 1) 圆角方块底
    tile = QPainterPath()
    tile.addRoundedRect(QRectF(s * 0.02, s * 0.02, s * 0.96, s * 0.96),
                        s * 0.225, s * 0.225)
    grad = QLinearGradient(0, 0, s * 0.82, s)
    grad.setColorAt(0.0, QColor(c1))
    grad.setColorAt(1.0, QColor(c2))
    p.fillPath(tile, grad)

    # 2) 左上柔光，让纯色不呆板
    hl = QRadialGradient(QPointF(s * 0.28, s * 0.18), s * 0.9)
    hl.setColorAt(0.0, QColor(255, 255, 255, 46))
    hl.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(tile, hl)

    # 3) 细描边（浅色强调色时给一点边界）
    p.setPen(QPen(QColor(255, 255, 255, 30), s * 0.008))
    p.setBrush(Qt.NoBrush)
    p.drawPath(tile)

    # 4) 双弧
    r = (s - 2 * s * RING) / 2
    c = s / 2
    rect = QRectF(c - r, c - r, 2 * r, 2 * r)
    pen = QPen(QColor(255, 255, 255, 246), s * STROKE)
    pen.setCapStyle(Qt.FlatCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    starts = (GAP / 2, 180 + GAP / 2)
    for a in starts:
        path = QPainterPath()
        path.arcMoveTo(rect, a)
        path.arcTo(rect, a, SPAN)
        p.drawPath(path)

    # 5) 箭头（实心三角，沿切线方向）
    for a in starts:
        deg = a + SPAN
        rad = math.radians(deg)
        cx = c + r * math.cos(rad)
        cy = c - r * math.sin(rad)
        tan = rad + math.pi / 2
        tip = QPointF(cx + math.cos(tan) * s * HEAD_LEN,
                     cy - math.sin(tan) * s * HEAD_LEN)
        bx = cx - math.cos(tan) * s * HEAD_LEN * 0.30
        by = cy + math.sin(tan) * s * HEAD_LEN * 0.30
        left = QPointF(bx + math.cos(tan + math.pi / 2) * s * HEAD_HALF,
                       by - math.sin(tan + math.pi / 2) * s * HEAD_HALF)
        right = QPointF(bx + math.cos(tan - math.pi / 2) * s * HEAD_HALF,
                        by - math.sin(tan - math.pi / 2) * s * HEAD_HALF)
        tri = QPainterPath()
        tri.moveTo(tip)
        tri.lineTo(left)
        tri.lineTo(right)
        tri.closeSubpath()
        p.fillPath(tri, QColor(255, 255, 255, 246))

    # 6) 播放键
    w, h = s * PLAY_W, s * PLAY_W * 1.08
    play = QPainterPath()
    play.moveTo(c - w * 0.34, c - h * 0.5)
    play.lineTo(c + w * 0.55, c)
    play.lineTo(c - w * 0.34, c + h * 0.5)
    play.closeSubpath()
    p.fillPath(play, QColor(255, 255, 255, 252))


def pixmap(size: int, accent: str = "#0067C0", dpr: float = 1.0) -> QPixmap:
    """按逻辑尺寸 size 生成图标位图（自动按 dpr 放大）。"""
    pm = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / S, size / S)
    _paint(p, accent)
    p.end()
    return pm


def icon(accent: str = "#0067C0") -> QIcon:
    """多尺寸 QIcon（窗口/任务栏用），包含 16~256 各档。"""
    ic = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
        ic.addPixmap(pixmap(size, accent))
    return ic


def save_png(path: str, size: int = 512, accent: str = "#0067C0") -> None:
    pixmap(size, accent).save(path, "PNG")
