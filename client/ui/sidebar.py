#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""左侧导航栏（Windows 11 设置风格）。

只负责外观与选中态，页面切换由 MainWindow 决定：
`navigate` 信号给出目标页索引，窗口确认后调用 `set_current()`。
"""

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from version import APP_VERSION

from ui import icons

NAV_ITEMS = (
    (0, "home", "主页"),
    (1, "room", "房间"),
    (2, "settings", "设置"),
)


class Sidebar(QFrame):
    navigate = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(216)
        self._buttons = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._current = 0
        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 16, 12, 12)
        root.setSpacing(4)

        # 品牌区
        brand = QWidget()
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(6, 0, 6, 14)
        brand_layout.setSpacing(10)
        self._logo = QLabel()
        self._logo.setFixedSize(30, 30)
        self._logo.setScaledContents(True)
        brand_layout.addWidget(self._logo)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(0)
        name = QLabel("PotSync")
        name.setObjectName("brandName")
        self._version = QLabel(f"v{APP_VERSION}")
        self._version.setObjectName("versionLabel")
        text_box.addWidget(name)
        text_box.addWidget(self._version)
        brand_layout.addLayout(text_box)
        brand_layout.addStretch(1)
        root.addWidget(brand)

        # 导航
        for index, kind, text in NAV_ITEMS:
            btn = QPushButton(text)
            btn.setObjectName("navBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setIconSize(QSize(18, 18))
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.clicked.connect(lambda _checked=False, i=index: self.navigate.emit(i))
            self._group.addButton(btn, index)
            self._buttons[index] = (btn, kind)
            root.addWidget(btn)

        root.addStretch(1)

        # 选中项的左侧强调条（Win11 导航用的竖条，不用整圈边框）
        self._indicator = QFrame(self)
        self._indicator.setObjectName("navIndicator")
        self._indicator.setFixedSize(3, 16)
        self._indicator.hide()

        # 底部：主题状态
        footer = QFrame()
        footer.setObjectName("sidebarFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(6, 10, 6, 2)
        footer_layout.setSpacing(8)
        self._theme_icon = QLabel()
        self._theme_icon.setFixedSize(16, 16)
        self._theme_icon.setScaledContents(True)
        self._theme_label = QLabel("跟随系统")
        self._theme_label.setObjectName("tip")
        footer_layout.addWidget(self._theme_icon)
        footer_layout.addWidget(self._theme_label, 1)
        root.addWidget(footer)

        self._buttons[0][0].setChecked(True)

    # ---------------------------------------------------------------- 状态
    def _place_indicator(self) -> None:
        entry = self._buttons.get(self._current)
        if not entry:
            self._indicator.hide()
            return
        btn = entry[0]
        if not btn.isEnabled() or not btn.isVisible():
            self._indicator.hide()
            return
        self._indicator.setGeometry(3, btn.y() + (btn.height() - 16) // 2, 3, 16)
        self._indicator.show()
        self._indicator.raise_()

    def set_current(self, index: int):
        btn = self._buttons.get(index)
        if not btn:
            return
        self._current = index
        btn[0].setChecked(True)
        self._place_indicator()

    def current(self) -> int:
        return self._current

    def set_page_enabled(self, index: int, enabled: bool):
        btn = self._buttons.get(index)
        if btn:
            btn[0].setEnabled(enabled)
        self._place_indicator()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._place_indicator()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_indicator()

    def refresh(self, pal, mode: str, accent: str):
        """主题/强调色变化后重新生成图标。"""
        self._logo.setPixmap(icons.logo(30, accent))
        for _index, (btn, kind) in self._buttons.items():
            btn.setIcon(icons.icon(kind, pal.text_dim, pal.accent_text, 18))
        is_dark = mode == "dark"
        self._theme_icon.setPixmap(icons.pixmap("moon" if is_dark else "sun",
                                                pal.text_muted, 16))
        self._theme_label.setText("跟随系统 · 深色" if is_dark else "跟随系统 · 浅色")
