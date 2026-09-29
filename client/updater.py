#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查更新与自更新
================

更新源（优先级从高到低）：

1. ~/.potsync/config.json 中 "update_url"：指向一个 version.json 的 http(s) 地址，
   格式：{"version": "1.2.0", "url": "https://…/PotSync.exe", "notes": "更新说明"}
2. ~/.potsync/config.json 中 "update_repo"：GitHub 仓库 "owner/repo"
3. 内置默认仓库 DEFAULT_UPDATE_REPO（官方发布仓库，所有客户端开箱即可检查更新）

GitHub 仓库读取其最新 Release（tag 为版本号，资产中包含 PotSync.exe，正文为更新说明）。

发现新版本后：断点续传下载（复用隧道模块的多镜像下载器）→ 生成替换批处理
→ 重启软件完成自更新（Windows 无法替换运行中的 exe，故由批处理在退出后替换）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

from tunnel import _download_file
from version import APP_NAME, APP_VERSION

log = logging.getLogger("potsync.updater")

# 官方发布仓库：所有客户端默认从这里检查更新（用户配置可覆盖）
DEFAULT_UPDATE_REPO = "aaayeaaa-P1WS/PotSync"

GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
REQUEST_TIMEOUT = 20


class UpdateInfo:
    def __init__(self, version: str, url: str, notes: str = "",
                 sha256: str = "", sha256_url: str = "") -> None:
        self.version = version
        self.url = url
        self.notes = notes
        self.sha256 = sha256            # 期望的 exe SHA-256（小写 hex，可为空）
        self.sha256_url = sha256_url    # 或提供校验和文本的下载地址（可为空）

    def __repr__(self) -> str:
        return f"UpdateInfo(v{self.version}, {self.url})"


def parse_version(s: str) -> tuple:
    """'v1.2.3' / '1.2.3' → (1, 2, 3)；无法解析的部分按 0 处理。"""
    s = (s or "").strip().lstrip("vV")
    parts = []
    for piece in s.split(".")[:4]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(latest: str, current: str = APP_VERSION) -> bool:
    return parse_version(latest) > parse_version(current)


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_version_json(data: dict) -> UpdateInfo:
    if not data.get("version") or not data.get("url"):
        raise ValueError("version.json 缺少 version 或 url 字段")
    return UpdateInfo(str(data["version"]), str(data["url"]),
                      str(data.get("notes", "")),
                      sha256=str(data.get("sha256", "")))


def _parse_github_release(data: dict) -> UpdateInfo:
    version = str(data.get("tag_name") or data.get("name") or "")
    exe_urls, sha256_url = [], ""
    for asset in data.get("assets", []):
        name = str(asset.get("name", "")).lower()
        dl = str(asset.get("browser_download_url", ""))
        if name.endswith(".sha256"):
            sha256_url = dl
        elif name.endswith(".exe"):
            exe_urls.append(dl)
    pot = [u for u in exe_urls if "potsync" in u.lower()]
    url = (pot or exe_urls or [""])[0]      # 优先名字含 potsync 的 exe
    if not version or not url:
        raise ValueError("Release 中未找到版本号或 exe 资产")
    return UpdateInfo(version, url, str(data.get("body", "")),
                      sha256_url=sha256_url)


def configured_source(cfg: dict) -> Optional[str]:
    """返回实际生效的更新源描述（用于 UI 显示），完全无更新源时返回 None。"""
    if cfg.get("update_url"):
        return cfg["update_url"]
    repo = cfg.get("update_repo") or DEFAULT_UPDATE_REPO
    if repo:
        return f"github.com/{repo}"
    return None


def check_for_update(cfg: dict) -> Optional[UpdateInfo]:
    """有更新返回 UpdateInfo；已是最新返回 None；出错抛异常。"""
    if cfg.get("update_url"):
        info = _parse_version_json(_http_json(cfg["update_url"]))
    else:
        repo = cfg.get("update_repo") or DEFAULT_UPDATE_REPO
        if not repo:
            raise ValueError("未配置更新源")
        info = _parse_github_release(
            _http_json(GITHUB_API.format(repo=repo)))
    return info if is_newer(info.version) else None


def _http_text(url: str) -> str:
    """下载小文本文件（校验和等）；github.com 地址自动尝试镜像。"""
    from tunnel import _candidate_urls
    last: Optional[Exception] = None
    for candidate in _candidate_urls(url):
        try:
            req = urllib.request.Request(
                candidate, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:            # 换下一个镜像
            last = exc
    raise RuntimeError(f"校验和下载失败: {last}")


def _parse_sha256_text(text: str) -> str:
    """从 sha256sum 格式文本（'<hash>  <文件名>'）提取 64 位 hex 校验和。"""
    token = (text or "").strip().split()[0].strip().lower() if text.strip() else ""
    if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
        return token
    raise ValueError("校验和文件格式异常")


def _file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _expected_sha256(info: UpdateInfo) -> str:
    """解析期望校验和：优先内联字段，其次 sha256_url 文本；拿不到返回 ""。"""
    direct = (info.sha256 or "").strip().lower()
    if direct:
        return direct
    if info.sha256_url:
        try:
            return _parse_sha256_text(_http_text(info.sha256_url))
        except Exception as exc:
            log.warning("获取校验和失败，本次跳过校验: %s", exc)
    return ""


def download_update(info: UpdateInfo, dst: Path,
                    status: Optional[Callable[[str], None]] = None) -> Path:
    """断点续传下载新版本 exe（复用隧道模块的多镜像下载器）。

    完整性保障：发布方附带 .sha256 时，下载后必校验；校验失败删除残留
    从头重新下载一次，仍失败则抛错——绝不让损坏的包进入替换重启流程
    （历史上"续传拼接了旧版本残留临时文件"会导致新 exe 启动即报
    Failed to extract / decompression return code -3）。
    """
    expected = _expected_sha256(info)
    _download_file(info.url, dst, status=status, expected_min=1_000_000)
    if expected and _file_sha256(dst) != expected:
        log.warning("更新包校验失败，删除残留并重新完整下载")
        if status:
            status("校验失败，重新完整下载…")
        dst.unlink(missing_ok=True)
        _download_file(info.url, dst, status=status, expected_min=1_000_000)
        if _file_sha256(dst) != expected:
            dst.unlink(missing_ok=True)
            raise RuntimeError("更新包校验失败（重试后仍不一致），"
                               "请稍后再试或到发布页手动下载")
    return dst


def make_update_bat(current_exe: Path, new_exe: Path) -> Path:
    """生成"退出后替换并重启"的批处理，返回路径。"""
    bat = f"""@echo off
rem PotSync 自更新：等待旧进程退出 → 替换 exe → 重启
set OLD={current_exe}
set NEW={new_exe}
:wait
tasklist /FI "IMAGENAME eq {current_exe.name}" | findstr /I "{current_exe.name}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
move /y "%NEW%" "%OLD%" >nul
rem 等待杀毒软件/索引服务对新文件扫描就绪，降低重启时 DLL 加载竞争
timeout /t 2 /nobreak >nul
start "" "%OLD%"
del "%~f0" >nul 2>&1
"""
    bat_path = Path(tempfile.gettempdir()) / "potsync_update.bat"
    bat_path.write_text(bat, encoding="gbk", errors="replace")
    return bat_path


def apply_and_restart(current_exe: Path, new_exe: Path) -> None:
    """启动替换批处理（调用方随后应立即退出本程序）。"""
    bat = make_update_bat(current_exe, new_exe)
    subprocess.Popen(["cmd", "/c", str(bat)],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     close_fds=True)


if __name__ == "__main__":
    print(f"{APP_NAME} {APP_VERSION}")
    print("当前版本元组:", parse_version(APP_VERSION))
