#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发布 PotSync 到 GitHub（推送 main + tag，创建 Release 并上传资产）
=================================================================

用法：
    python tools/publish_release.py [--tag v2.0.0] [--dry-run]

凭据：把 fine-grained PAT 写进 %USERPROFILE%\\.potsync\\gh_token.txt（单行，无空格）。
脚本只读取一次，发布成功后自动删除该文件。

前置：
    - 工作树干净、tag 已打好；
    - 代理可用（默认 http://127.0.0.1:20890），可用 --proxy 覆盖，--proxy "" 表示不走代理。
"""

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "aaayeaaa-P1WS/PotSync"
TOKEN_FILE = Path.home() / ".potsync" / "gh_token.txt"
DEFAULT_PROXY = "http://127.0.0.1:20890"


def read_token() -> str:
    env = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if env:
        return env.strip()
    if not TOKEN_FILE.exists():
        sys.exit(f"找不到凭据：请把 PAT 写进 {TOKEN_FILE}")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        sys.exit(f"凭据文件是空的：{TOKEN_FILE}")
    return token


def api(method: str, url: str, token: str, data: bytes = b"", ctype: str = "application/json"):
    req = urllib.request.Request(url, data=data or None, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "PotSync-release-script")
    if data:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return exc.code, {"raw": detail}


def push(token: str, tag: str, proxy: str) -> None:
    url = f"https://x-access-token:{token}@github.com/{REPO}.git"
    # credential.helper= 必须清空：本机系统 gitconfig 指向 DSH 的 selector 助手，
    # 它会在推送时弹出交互窗口，把非交互的发布脚本永久卡住。
    cmd = ["git", "-c", "credential.helper=", "-c", "core.askPass="]
    if proxy:
        cmd += ["-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}"]
    cmd += ["push", url, "main", f"refs/tags/{tag}:refs/tags/{tag}"]
    print("→ 推送 main 与 tag", tag)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="")
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL, timeout=180,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout + proc.stderr).replace(token, "***")
    print(out.strip()[-1200:])
    if proc.returncode != 0:
        sys.exit(f"git push 失败（exit {proc.returncode}）")


def ensure_release(token: str, tag: str, body: str) -> dict:
    status, rel = api("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{tag}", token)
    if status == 200:
        print(f"→ Release {tag} 已存在，更新正文")
        status, rel = api("PATCH", f"https://api.github.com/repos/{REPO}/releases/{rel['id']}",
                          token, json.dumps({"name": f"PotSync {tag}", "body": body}).encode())
        if status >= 300:
            sys.exit(f"更新 Release 失败：{rel}")
        return rel
    print(f"→ 创建 Release {tag}")
    payload = {"tag_name": tag, "target_commitish": "main", "name": f"PotSync {tag}",
               "body": body, "draft": False, "prerelease": False}
    status, rel = api("POST", f"https://api.github.com/repos/{REPO}/releases", token,
                      json.dumps(payload).encode())
    if status >= 300:
        sys.exit(f"创建 Release 失败：{rel}")
    return rel


def upload(token: str, release: dict, path: Path) -> None:
    name = path.name
    assets = {a["name"]: a for a in release.get("assets", [])}
    if name in assets:
        print(f"→ 删除旧资产 {name}")
        api("DELETE", f"https://api.github.com/repos/{REPO}/releases/assets/{assets[name]['id']}", token)
    data = path.read_bytes()
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    print(f"→ 上传 {name}（{len(data):,} 字节）")
    status, res = api("POST", f"https://uploads.github.com/repos/{REPO}/releases/"
                              f"{release['id']}/assets?name={name}", token, data, ctype)
    if status >= 300:
        sys.exit(f"上传失败：{res}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v2.0.0")
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tag = args.tag
    notes = ROOT / f"release-notes-{tag}.md"
    if not notes.exists():
        sys.exit(f"缺少发布正文：{notes}")
    body = notes.read_text(encoding="utf-8")
    exe = ROOT / "dist" / "PotSync.exe"
    sha = ROOT / "dist" / "PotSync.exe.sha256"
    for f in (exe, sha):
        if not f.exists():
            sys.exit(f"缺少资产：{f}")
    digest = sha.read_text(encoding="utf-8").split()[0].strip()
    if "SHA-256" not in body:
        body = body.rstrip() + f"\n\nSHA-256：`{digest}`\n"

    if args.dry_run:
        print(f"dry-run：资产与正文已就绪，tag={tag}")
        print(f"  {exe}  {exe.stat().st_size:,} 字节")
        print(f"  {sha}")
        print(body[:400])
        return 0

    token = read_token()
    print(f"凭据：{TOKEN_FILE}（{len(token)} 字符）  代理：{args.proxy or '直连'}")

    if args.proxy:
        os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = args.proxy

    push(token, tag, args.proxy)
    release = ensure_release(token, tag, body)
    upload(token, release, exe)
    upload(token, release, sha)

    try:
        TOKEN_FILE.unlink()
        print(f"已删除凭据文件 {TOKEN_FILE}")
    except OSError:
        pass
    print("完成：", release.get("html_url", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
