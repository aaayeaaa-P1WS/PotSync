#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotSync 客户端入口
==================

用法：
    python app.py            # 启动图形界面
    python app.py --debug    # 输出调试日志
"""

import logging
import sys
from pathlib import Path

# 保证以脚本方式运行 / PyInstaller 打包后都能正确找到同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


def main() -> int:
    if "--version" in sys.argv:
        from version import APP_NAME, APP_VERSION
        print(f"{APP_NAME} {APP_VERSION} (client + embedded relay server)")
        return 0

    if "--debug" in sys.argv:
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s [%(name)s] %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING,
                            format="%(asctime)s [%(name)s] %(message)s")

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("PotSync")
    app.setOrganizationName("PotSync")

    qss = Path(__file__).resolve().parent / "resources" / "style.qss"
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))

    from main_window import MainWindow
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
