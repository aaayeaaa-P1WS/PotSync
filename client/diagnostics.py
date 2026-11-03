#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行日志 / 崩溃诊断
===================

2.0 新增。目的：用户在别人电脑上遇到的"莫名掉线/窗口消失"必须能事后查。
- 日志固定写到 ``~/.potsync/logs/potsync.log``（1MB × 3 轮转，UTF-8）；
- ``sys.excepthook`` / ``threading.excepthook`` 把未捕获异常写盘（PyQt5 在槽函数抛异常时
  会直接 abort 进程，有了文件日志至少能知道最后一步在哪）；
- ``faulthandler`` 把原生崩溃的 C 栈也写进同一个目录，便于排查 pywin32/驱动层崩溃；
- GUI 另装 Qt 消息处理器，把 Qt 自己的警告一起收进来。
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Optional

LOG_DIR = Path.home() / ".potsync" / "logs"
LOG_FILE = LOG_DIR / "potsync.log"

_initialized = False
_crash_stream = None


def setup(level: int = logging.INFO, filename: Optional[Path] = None) -> Path:
    """初始化全局日志；重复调用无副作用，返回日志文件路径。"""
    global _initialized, _crash_stream

    path = Path(filename) if filename else LOG_FILE
    if _initialized:
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(threadName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass

    # windowed 打包版没有 stderr，只有在有控制台时（源码运行）才加屏幕输出
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)

    # 原生崩溃栈：写进单独文件，追加模式，避免和文本日志互相截断
    try:
        _crash_stream = open(path.with_name("crash.log"), "a", encoding="utf-8")
        faulthandler.enable(_crash_stream, all_threads=True)
    except OSError:
        _crash_stream = None

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("potsync.crash").critical(
            "未捕获异常", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_excepthook(args) -> None:
        if args.exc_type is SystemExit:
            return
        logging.getLogger("potsync.crash").critical(
            "线程 %s 未捕获异常", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    _initialized = True
    logging.getLogger("potsync").info("日志已启动：%s", path)
    return path


def install_qt_message_handler() -> None:
    """把 Qt 自己的 qWarning/qCritical 收进同一个日志（需在 QApplication 之后调用）。"""
    try:
        from PyQt5.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return

    level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    log = logging.getLogger("potsync.qt")

    # 打包版里 Qt 找不到字体目录、离屏平台插件不支持 propagateSizeHints，
    # 都只影响观感且会在每次构建控件时重复刷屏，这里直接丢掉，避免淹没真正的错误。
    noisy = (
        "QFontDatabase: Cannot find font directory",
        "Note that Qt no longer ships fonts",
        "This plugin does not support propagateSizeHints()",
    )

    def handler(mode, context, message) -> None:
        text = str(message)
        if any(text.startswith(prefix) or prefix in text for prefix in noisy):
            return
        log.log(level_map.get(mode, logging.INFO), "%s", text)

    qInstallMessageHandler(handler)


def log_path() -> Path:
    return LOG_FILE
