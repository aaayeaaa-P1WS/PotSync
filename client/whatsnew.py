#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更新内容（whatsnew）读取。

说明文件 `resources/whatsnew.txt` 用最朴素的格式，方便直接编辑：

    v2.2.1
    更新更可靠：等旧版本完全退出再替换，替换失败会回滚。
    高分屏更清晰：4K 屏幕开 150% 缩放，文字按真实像素绘制。

    v2.2.0
    新图标，颜色跟随 Windows 主题色。

版本行只写版本号（带不带 v 都行），其余非空行都是正文句子。
"""

import logging
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger("potsync.whatsnew")

_HERE = Path(__file__).resolve().parent


def _resource(name: str) -> Optional[Path]:
    """与 style.qss 相同的多布局查找：源码、onefile（_MEIPASS）。"""
    import sys
    roots = [_HERE / "resources"]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(Path(meipass) / "resources")
        roots.append(Path(meipass) / "client" / "resources")
    for root in roots:
        candidate = root / name
        if candidate.exists():
            return candidate
    return None


def _looks_like_version(line: str) -> bool:
    text = line.strip().lstrip("vV")
    parts = text.split(".")
    return len(parts) >= 2 and all(p.isdigit() for p in parts)


def parse(text: str) -> List[Tuple[str, List[str]]]:
    """把说明文件解析成 [(版本, [句子...]), ...]，保持文件里的顺序。"""
    sections: List[Tuple[str, List[str]]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _looks_like_version(line):
            sections.append((line.lstrip("vV"), []))
            continue
        if not sections:
            sections.append(("", []))
        sections[-1][1].append(line)
    return sections


def load_text() -> str:
    path = _resource("whatsnew.txt")
    if path is None:
        log.warning("未找到更新内容文件 whatsnew.txt")
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("读取更新内容失败：%s", exc)
        return ""


def sections() -> List[Tuple[str, List[str]]]:
    return parse(load_text())


def recent_sections(limit: int = 5) -> List[Tuple[str, List[str]]]:
    """最近的若干版本，默认覆盖最近一次大版本（2.0.0 起）。"""
    items = [s for s in sections() if s[1]]
    return items[:limit]


def section_for(version: str) -> Optional[Tuple[str, List[str]]]:
    want = (version or "").strip().lstrip("vV")
    for ver, lines in sections():
        if ver == want:
            return (ver, lines)
    return None
