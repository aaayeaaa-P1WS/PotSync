#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Windows 多尺寸 .ico（exe 图标用），颜色跟随系统强调色。

用法：
    python tools/make_icon.py                 # 自动读系统强调色
    python tools/make_icon.py --accent "#107C10"
    python tools/make_icon.py --out client/resources/potsync.ico
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))


def build(out: Path, accent: str) -> None:
    from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt5.QtGui import QImage
    from PyQt5.QtWidgets import QApplication

    from ui import appicon

    app = QApplication.instance() or QApplication(sys.argv[:1])
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)

    # Qt 自带 ICO 写入器只写单张图，这里手工拼多尺寸 ICO
    images = []
    for size in sizes:
        pm = appicon.pixmap(size, accent, 1.0)
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        pm.toImage().save(buf, "PNG")
        images.append((size, bytes(buf.data())))
        buf.close()

    header = bytearray()
    header += (0).to_bytes(2, "little")          # reserved
    header += (1).to_bytes(2, "little")          # type = icon
    header += len(images).to_bytes(2, "little")  # count

    entries = bytearray()
    blob = bytearray()
    offset = 6 + 16 * len(images)
    for size, data in images:
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        entries += bytes((w, h, 0, 0))
        entries += (1).to_bytes(2, "little")     # color planes
        entries += (32).to_bytes(2, "little")    # bpp
        entries += len(data).to_bytes(4, "little")
        entries += offset.to_bytes(4, "little")
        blob += data
        offset += len(data)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(header + entries + blob))
    print(f"saved {out} ({out.stat().st_size} bytes, accent {accent})")
    _ = app
    _ = QImage


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accent", default="")
    ap.add_argument("--out", default=str(ROOT / "client" / "resources" / "potsync.ico"))
    args = ap.parse_args()

    accent = args.accent
    if not accent:
        os_env = __import__("os").environ.get("POTSYNC_ACCENT", "").strip()
        if os_env:
            accent = os_env
        else:
            from theme import system_accent
            accent = system_accent()
    build(Path(args.out), accent)


if __name__ == "__main__":
    main()
    _ = subprocess  # 保留 import，避免某些冻结环境下被裁掉
