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
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import QApplication

import diagnostics


def main() -> int:
    if "--version" in sys.argv:
        from version import APP_NAME, APP_VERSION
        print(f"{APP_NAME} {APP_VERSION} (client + embedded relay server)")
        return 0

    if "--debug" in sys.argv:
        log_path = diagnostics.setup(logging.DEBUG)
    else:
        log_path = diagnostics.setup(logging.INFO)

    # 高 DPI：清单已声明 PerMonitorV2，这里用 1.25/1.5 这类小数缩放因子原样渲染，
    # 避免 Qt 把它四舍五入到 2.0 后再被 Windows 二次拉伸（文字发虚的老问题）。
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except AttributeError:      # Qt < 5.14 没有这个接口，忽略
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("PotSync")
    app.setOrganizationName("PotSync")
    diagnostics.install_qt_message_handler()
    logging.getLogger("potsync.app").info("启动 PotSync，日志：%s", log_path)

    # 跟随 Windows 深色/浅色与强调色（1.5s 热切换，无需重启）
    import theme
    theme.install(app)

    from main_window import MainWindow
    window = MainWindow()
    window.show()
    try:
        scr = app.primaryScreen()
        logging.getLogger("potsync.app").info(
            "显示环境：dpr=%.2f 逻辑DPI=%.0f 物理DPI=%.0f 逻辑分辨率=%dx%d 缩放=%.1f%%",
            scr.devicePixelRatio(), scr.logicalDotsPerInch(), scr.physicalDotsPerInch(),
            scr.availableGeometry().width(), scr.availableGeometry().height(),
            scr.devicePixelRatio() * 100.0)
    except Exception:
        pass
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
