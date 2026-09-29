#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查更新 / 自更新模块测试
========================

- 版本号解析与比较（v 前缀 / 缺段补零 / 数值比较）
- version.json 与 GitHub Release 两种响应解析
- 本地 HTTP 服务器实测 check_for_update（有更新 / 已最新 / 未配置 / GitHub API 路径）
- download_update 下载落地（复用隧道模块下载器）
- make_update_bat 自更新批处理内容

运行：
    python tests/test_updater.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 本机开启系统代理时，强制本地回环地址直连（urllib 读取该变量）
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

import updater  # noqa: E402
from version import APP_VERSION  # noqa: E402

ROUTES: dict = {}
RANGE_ROUTES: set = set()   # 这些路径支持 Range（206 / 416），模拟真实镜像


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = ROUTES.get(self.path)
        if body is None:
            self.send_error(404)
            return
        rng = self.headers.get("Range")
        if rng and self.path in RANGE_ROUTES:
            try:
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
            except (IndexError, ValueError):
                self.send_error(400)
                return
            if start >= len(body):
                self.send_error(416)        # 续传起点超出文件大小
                return
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            self.wfile.write(body[start:])
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静默
        pass


def main() -> int:
    # ---------- 版本号解析与比较 ----------
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.2") == (1, 2, 0)
    assert updater.parse_version("") == (0, 0, 0)
    assert updater.parse_version("V2.0.1") == (2, 0, 1)
    assert updater.is_newer("v99.0.0")
    assert not updater.is_newer(APP_VERSION)
    assert not updater.is_newer("0.0.1")
    assert updater.is_newer("1.10.0", "1.9.0")      # 数值比较而非字典序
    assert not updater.is_newer("1.9.0", "1.10.0")
    print("PASS  版本号解析与比较（v 前缀 / 缺段补零 / 数值比较）")

    # ---------- 响应解析 ----------
    info = updater._parse_version_json(
        {"version": "2.0.0", "url": "http://x/PotSync.exe", "notes": "n"})
    assert info.version == "2.0.0" and info.url.endswith("PotSync.exe")
    assert info.notes == "n"
    try:
        updater._parse_version_json({"version": "2.0.0"})
        raise AssertionError("缺少 url 字段应抛 ValueError")
    except ValueError:
        pass

    rel = {"tag_name": "v2.1.0", "body": "修复若干问题",
           "assets": [
               {"name": "other-tool.exe",
                "browser_download_url": "http://x/other.exe"},
               {"name": "PotSync.exe",
                "browser_download_url": "http://x/PotSync.exe"},
               {"name": "PotSync.exe.sha256",
                "browser_download_url": "http://x/PotSync.exe.sha256"}]}
    info = updater._parse_github_release(rel)
    assert info.version == "v2.1.0"
    assert info.url.endswith("PotSync.exe")          # 优先名字含 potsync 的资产
    assert info.notes == "修复若干问题"
    assert info.sha256_url.endswith("PotSync.exe.sha256")   # 识别校验和资产
    rel2 = dict(rel, assets=[{"name": "other-tool.exe",
                              "browser_download_url": "http://x/other.exe"}])
    info2 = updater._parse_github_release(rel2)
    assert info2.url.endswith("other.exe")
    assert info2.sha256_url == ""
    try:
        updater._parse_github_release({"tag_name": "v1", "assets": []})
        raise AssertionError("无 exe 资产应抛 ValueError")
    except ValueError:
        pass
    # version.json 可内联 sha256
    info3 = updater._parse_version_json(
        {"version": "2.0.0", "url": "http://x/PotSync.exe",
         "sha256": "ab" * 32})
    assert info3.sha256 == "ab" * 32
    print("PASS  version.json / GitHub Release 响应解析（缺字段报错、优先 PotSync.exe 资产、识别校验和）")

    # ---------- 本地 HTTP 服务器实测 ----------
    fake_exe = b"MZ" + b"\0" * 1_200_000            # >1MB，满足 expected_min
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    ROUTES.update({
        "/version.json": json.dumps({
            "version": "99.0.0", "url": f"{base}/PotSync.exe",
            "notes": "新功能与修复"}).encode(),
        "/version-old.json": json.dumps({
            "version": "0.0.1", "url": f"{base}/PotSync.exe"}).encode(),
        "/repos/o/r/releases/latest": json.dumps({
            "tag_name": "v98.0.0", "body": "github 更新说明",
            "assets": [{"name": "PotSync.exe",
                        "browser_download_url": f"{base}/PotSync.exe"}]}).encode(),
        "/PotSync.exe": fake_exe,
    })
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 有新版本（version.json 直链）
        info = updater.check_for_update({"update_url": f"{base}/version.json"})
        assert info is not None and info.version == "99.0.0"
        assert info.url == f"{base}/PotSync.exe" and info.notes == "新功能与修复"

        # 已是最新
        assert updater.check_for_update(
            {"update_url": f"{base}/version-old.json"}) is None

        # 未配置更新源（默认仓库也置空时才报错）
        orig_repo = updater.DEFAULT_UPDATE_REPO
        updater.DEFAULT_UPDATE_REPO = ""
        try:
            updater.check_for_update({})
            raise AssertionError("未配置更新源应抛 ValueError")
        except ValueError as exc:
            assert "未配置更新源" in str(exc)
        finally:
            updater.DEFAULT_UPDATE_REPO = orig_repo

        # GitHub Release 路径（API 地址指向本地服务器）
        orig_api = updater.GITHUB_API
        updater.GITHUB_API = base + "/repos/{repo}/releases/latest"
        updater.DEFAULT_UPDATE_REPO = "o/r"
        try:
            info = updater.check_for_update({})           # 走内置默认仓库
            assert info is not None and info.version == "v98.0.0"
            assert info.notes == "github 更新说明"
            info = updater.check_for_update({"update_repo": "o/r"})  # cfg 覆盖
            assert info is not None and info.version == "v98.0.0"
        finally:
            updater.GITHUB_API = orig_api
            updater.DEFAULT_UPDATE_REPO = orig_repo

        # configured_source 描述（默认仓库兜底）
        assert updater.configured_source({}) == f"github.com/{orig_repo}"
        updater.DEFAULT_UPDATE_REPO = ""
        assert updater.configured_source({}) is None
        updater.DEFAULT_UPDATE_REPO = orig_repo
        assert updater.configured_source(
            {"update_repo": "o/r"}) == "github.com/o/r"
        assert updater.configured_source(
            {"update_url": "http://x/v.json"}) == "http://x/v.json"
        print("PASS  check_for_update（有更新 / 已最新 / 未配置 / GitHub API 路径）")

        # ---------- 下载落地 ----------
        dst = ROOT / "tests" / "_tmp_PotSync-new.exe"
        dst.unlink(missing_ok=True)
        seen = []
        updater.download_update(
            updater.UpdateInfo("99.0.0", f"{base}/PotSync.exe"),
            dst, status=seen.append)
        assert dst.read_bytes() == fake_exe
        assert seen, "下载进度回调应被调用"
        dst.unlink()
        print("PASS  download_update 下载落地（1.2MB 内容一致 + 进度回调）")

        # ---------- 完整性校验 ----------
        import hashlib
        good_hash = hashlib.sha256(fake_exe).hexdigest()
        bad_exe = b"MZ" + b"\xFF" * 1_200_000
        ROUTES["/PotSync.exe.sha256"] = f"{good_hash}  PotSync.exe\n".encode()
        ROUTES["/bad.exe"] = bad_exe
        RANGE_ROUTES.update({"/PotSync.exe", "/bad.exe"})

        # 1) 带 sha256 的正常下载：校验通过
        dst.unlink(missing_ok=True)
        updater.download_update(
            updater.UpdateInfo("99.0.0", f"{base}/PotSync.exe",
                               sha256_url=f"{base}/PotSync.exe.sha256"), dst)
        assert dst.read_bytes() == fake_exe
        dst.unlink()
        print("PASS  校验通过：sha256 与文件一致")

        # 2) 旧版本脏残留自愈：残留头部是错误字节，续传拼接后校验失败
        #    → 自动删除并从头完整下载，最终内容必须完好
        dst.write_bytes(b"\x00" * 600_000)       # 错误头部（假残留）
        updater.download_update(
            updater.UpdateInfo("99.0.0", f"{base}/PotSync.exe",
                               sha256_url=f"{base}/PotSync.exe.sha256"), dst)
        assert dst.read_bytes() == fake_exe, "脏残留拼接后应自愈为完整文件"
        dst.unlink()
        print("PASS  脏残留自愈：续传拼接损坏 → 校验失败 → 重新完整下载")

        # 3) 持续损坏（镜像劫持/文件被改）：重试后仍不一致 → 报错且不替换
        dst.unlink(missing_ok=True)
        try:
            updater.download_update(
                updater.UpdateInfo("99.0.0", f"{base}/bad.exe",
                                   sha256=good_hash), dst)
            raise AssertionError("持续损坏应抛 RuntimeError")
        except RuntimeError as exc:
            assert "校验失败" in str(exc)
        assert not dst.exists(), "校验失败的文件必须删除，不得进入替换流程"
        print("PASS  持续损坏防护：重试仍不一致则报错，残留已删除")

        # 4) 残留比新文件还大：服务器 416 → 自动从头完整下载
        dst.write_bytes(b"\x00" * (len(fake_exe) + 5000))
        updater.download_update(
            updater.UpdateInfo("99.0.0", f"{base}/PotSync.exe"), dst)
        assert dst.read_bytes() == fake_exe, "416 后应重新完整下载"
        dst.unlink()
        print("PASS  416 处理：续传起点超出文件大小 → 从头重新下载")
    finally:
        srv.shutdown()
        srv.server_close()

    # ---------- 自更新批处理 ----------
    bat_path = updater.make_update_bat(Path(r"C:\app\PotSync.exe"),
                                       Path(r"C:\app\PotSync-new.exe"))
    try:
        bat = bat_path.read_text(encoding="gbk")
        assert 'tasklist /FI "IMAGENAME eq PotSync.exe"' in bat   # 等待旧进程退出
        assert 'move /y "%NEW%" "%OLD%"' in bat                    # 替换 exe
        assert 'start "" "%OLD%"' in bat                           # 重启
        assert 'del "%~f0"' in bat                                 # 自删除
        assert r"C:\app\PotSync.exe" in bat
        assert r"C:\app\PotSync-new.exe" in bat
    finally:
        bat_path.unlink(missing_ok=True)
    print("PASS  make_update_bat（等待退出→替换→重启→自删除）")

    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
