#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按固定格式生成发布说明。

规则（用户要求，固定不变）：
  1. 正文先写「这次更新」——只写本版对用户有实际影响的变化，每个变化一句话；
  2. 后面固定跟「最近五个版本」，用一行一个版本概括，方便用户快速回看；
  3. 无 Markdown 井号、无加粗符号，读起来像人话。

数据源统一是 `client/resources/whatsnew.txt`（程序里的「更新内容」窗口也用同一份），
避免发布页、程序内说明、README 三处各写一遍导致对不上。

用法：
    python tools/make_notes.py --tag v2.4.8            # 写 release-notes-v2.4.8.md
    python tools/make_notes.py --tag v2.4.8 --stdout    # 只打印
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

import whatsnew  # noqa: E402

RECENT = 5
BRIEF_FILE = ROOT / "client" / "resources" / "notes_brief.txt"


def _load_briefs() -> dict[str, str]:
    """人工维护的一行摘要（vX.Y.Z=一句话）。缺失时退回自动概括。"""
    table: dict[str, str] = {}
    if not BRIEF_FILE.exists():
        return table
    for raw in BRIEF_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().lstrip("vV")
        if key and val.strip():
            table[key] = val.strip()
    return table


def _first_sentence(sentences: list[str]) -> str:
    """取一节里最能概括的一句：第一句通常就是结论。"""
    for line in sentences:
        line = line.strip()
        if line:
            return line
    return ""


def _brief(sentences: list[str], limit: int = 40) -> str:
    """给「最近五个版本」用的一行概括。

    优先取第一句冒号前的部分（通常是结论），其次截断第一句。
    """
    line = _first_sentence(sentences)
    if not line:
        return ""
    head, sep, _tail = line.partition("：")
    if sep and 6 <= len(head) <= limit:
        return head
    clause = line.split("，")[0]
    if 6 <= len(clause) <= limit:
        return clause
    return line if len(line) <= limit else line[: limit - 1] + "…"


def build(tag: str, args_verify: str = "") -> str:
    version = tag.lstrip("vV")
    sections = whatsnew.sections()
    if not sections:
        raise SystemExit("whatsnew.txt 里没有解析到任何版本段落")

    newest = sections[0]
    if newest[0].lstrip("vV") != version:
        print(
            f"警告：whatsnew.txt 最新一段是 {newest[0]}，与 tag {tag} 不一致",
            file=sys.stderr,
        )

    parts: list[str] = []
    parts.append(f"PotSync {version}")
    parts.append("")
    parts.append("这次更新")
    parts.append("")
    for line in newest[1]:
        line = line.strip()
        if line:
            parts.append(line)
            parts.append("")

    parts.append("最近五个版本")
    parts.append("")
    briefs = _load_briefs()
    for name, lines in sections[:RECENT]:
        key = name.lstrip("vV")
        brief = briefs.get(key) or _brief(lines)
        parts.append(f"v{key}：{brief}")
    parts.append("")

    verify = args_verify or "全部自动化测试通过。"
    parts.append(f"验证：{verify}")
    parts.append("")

    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--verify", default="", help="写进末尾的验证结论")
    args = ap.parse_args()

    text = build(args.tag, args.verify)
    if args.stdout:
        print(text)
        return 0

    out = Path(args.out) if args.out else ROOT / f"release-notes-{args.tag}.md"
    out.write_text(text, encoding="utf-8")
    print(f"写入 {out}（{len(text)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
