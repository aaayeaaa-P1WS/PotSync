#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 GitHub REST API 推送本地提交（不依赖 github.com 的 git 端口）。

背景：本机经常只能直连 api.github.com，而 github.com:443 被重置，`git push`
必然失败。Git Data API 走的是 api.github.com，因此可以照常更新 main 与 tag。

用法：
    python tools/push_via_api.py --token-file ~/.potsync/gh_token.txt --tag v2.4.6
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
REPO = "aaayeaaa-P1WS/PotSync"


def _req(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "PotSync-release")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"{method} {path} -> {exc.code}: {detail}") from exc


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return out.stdout.decode("utf-8", "replace").strip()


def _changed(base: str, head: str) -> list[tuple[str, str]]:
    raw = _git("diff", "--name-status", "-z", base, head)
    parts = raw.split("\0")
    items: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        if not status:
            i += 1
            continue
        if status.startswith("R"):
            items.append(("D", parts[i + 1]))
            items.append(("A", parts[i + 2]))
            i += 3
        else:
            items.append((status[0], parts[i + 1]))
            i += 2
    return items


def _load_token(preferred: Path) -> str:
    """按 环境变量 → DPAPI 加密仓库 → 明文文件 的顺序取令牌。"""
    import os

    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    store = Path.home() / ".potsync" / "gh_token.bin"
    if store.exists():
        try:
            import win32crypt

            blob = win32crypt.CryptUnprotectData(store.read_bytes(), None, None, None, 0)
            return blob[1].decode("utf-8").strip()
        except Exception:  # noqa: BLE001
            pass
    if preferred.exists():
        return preferred.read_text(encoding="utf-8").strip()
    raise SystemExit("没有可用的 GitHub 令牌（环境变量 / gh_token.bin / gh_token.txt 都为空）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", default=str(Path.home() / ".potsync" / "gh_token.txt"))
    ap.add_argument("--tag", default="")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--base", default="", help="远端基线在本地对应的提交（API 造的提交本地没有时用）")
    args = ap.parse_args()

    token = _load_token(Path(args.token_file))
    head = _git("rev-parse", "HEAD")
    message = _git("log", "-1", "--pretty=%B", "HEAD").strip()

    ref = _req("GET", f"/repos/{REPO}/git/ref/heads/{args.branch}", token)
    remote_head = ref["object"]["sha"]
    if remote_head == head:
        print("远端已是最新，无需推送")
        return 0
    base = args.base or remote_head
    if subprocess.run(["git", "cat-file", "-e", f"{base}^{{commit}}"], cwd=ROOT).returncode != 0:
        raise SystemExit(
            f"本机没有基线提交 {base[:8]}（多半是上次用 API 推送生成的对象）。"
            "请用 --base 指定本地对应的提交，例如 --base HEAD~1。"
        )
    remote_commit = _req("GET", f"/repos/{REPO}/git/commits/{remote_head}", token)
    base_tree = remote_commit["tree"]["sha"]

    changes = _changed(base, head)
    print(f"本地 {head[:8]} 基于远端 {base[:8]}，需推送 {len(changes)} 个文件变更")
    if args.dry_run:
        for st, path in changes:
            print(f"  {st} {path}")
        return 0

    entries = []
    for status, path in changes:
        if status == "D":
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            print(f"  - {path}")
            continue
        blob = (ROOT / path).read_bytes()
        res = _req("POST", f"/repos/{REPO}/git/blobs", token,
                   {"content": base64.b64encode(blob).decode("ascii"), "encoding": "base64"})
        mode = "100755" if (ROOT / path).stat().st_mode & 0o111 else "100644"
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": res["sha"]})
        print(f"  + {path} ({len(blob)} B)")

    tree = _req("POST", f"/repos/{REPO}/git/trees", token,
                {"base_tree": base_tree, "tree": entries})
    commit = _req("POST", f"/repos/{REPO}/git/commits", token,
                  {"message": message, "tree": tree["sha"], "parents": [remote_head]})
    _req("PATCH", f"/repos/{REPO}/git/refs/heads/{args.branch}", token, {"sha": commit["sha"]})
    print(f"main → {commit['sha'][:8]}")

    subprocess.run(["git", "update-ref", f"refs/remotes/origin/{args.branch}", commit["sha"]],
                   cwd=ROOT, check=False)

    if args.tag:
        try:
            _req("POST", f"/repos/{REPO}/git/refs", token,
                 {"ref": f"refs/tags/{args.tag}", "sha": commit["sha"]})
            print(f"tag {args.tag} → {commit['sha'][:8]}")
        except SystemExit as exc:
            if "already exists" in str(exc):
                _req("PATCH", f"/repos/{REPO}/git/refs/tags/{args.tag}", token, {"sha": commit["sha"], "force": True})
                print(f"tag {args.tag} 已存在，已强制指向 {commit['sha'][:8]}")
            else:
                raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
