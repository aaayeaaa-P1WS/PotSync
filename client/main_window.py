#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotSync 客户端主窗口（PyQt5）

主页：修改昵称、设置服务器、创建房间、输入房间号/链接加入、从剪贴板一键加入
房间页：房间号与一键复制邀请链接、成员列表、媒体与同步状态、进度条（可拖动广播）、
        播放/暂停、事件日志
"""

from __future__ import annotations

import json
import logging
import re
import socket
import sys
from pathlib import Path
from typing import Optional, Tuple

from PyQt5.QtCore import QUrl, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QGuiApplication, QIcon
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidgetItem, QMainWindow,
    QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

import diagnostics
import theme
import whatsnew
from net_client import NetClient
from pot_bridge import STATUS_RUNNING, STATUS_TEXT, PotPlayerBridge, Snapshot
from relay import EmbeddedServer
from sync_engine import SyncEngine
from tunnel import PublicEndpoint, TunnelManager
from ui import appicon
from ui import message as ui_message
from ui.home_page import build_home
from ui.notes_dialog import ask_update, show_notes
from ui.room_page import build_room
from ui.settings_page import build_settings
from ui.sidebar import Sidebar
from version import APP_NAME, APP_VERSION
import firewall
import updater

log = logging.getLogger("potsync.ui")

CONFIG_DIR = Path.home() / ".potsync"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_SERVER = "127.0.0.1:8765"
DEFAULT_PORT = 8765

LINK_RE = re.compile(r"^\s*potsync://([^/:\s]+)(?::(\d+))?/([A-Za-z0-9]{4,10})\s*$")
CODE_RE = re.compile(r"^\s*([A-Za-z0-9]{4,10})\s*$")


def fmt_time(ms: int) -> str:
    ms = max(0, int(ms))
    total = ms // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_invite(text: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """解析邀请链接或纯房间号，返回 (host, port, room)。

    端口缺省规则：IP/localhost → 8765（ws）；域名 → 443（wss，隧道/反代场景）。
    """
    if not text:
        return None, None, None
    m = LINK_RE.match(text)
    if m:
        host, port, code = m.group(1), m.group(2), m.group(3)
        if port:
            port = int(port)
        else:
            port = 8765 if _is_ip_host(host) else 443
        return host, port, code.upper()
    m = CODE_RE.match(text)
    if m:
        return None, None, m.group(1).upper()
    return None, None, None


def _is_ip_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    return re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host) is not None


def detect_lan_ip() -> str:
    """探测本机局域网 IP（用于主机模式生成邀请链接）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as exc:
        log.warning("保存配置失败: %s", exc)


class MainWindow(QMainWindow):
    _sigServerStarted = pyqtSignal(bool, int, str)  # 内嵌服务器启动结果（线程转投）
    _sigTunnelReady = pyqtSignal(object)            # PublicEndpoint（线程转投）
    _sigTunnelFailed = pyqtSignal(str)
    _sigTunnelStatus = pyqtSignal(str)
    _sigFirewall = pyqtSignal(bool, str)            # 防火墙权限申请结果（线程转投）
    _sigUpdateResult = pyqtSignal(bool, object, str)  # 检查更新结果(静默?, UpdateInfo|None, 错误)（线程转投）
    _sigUpdateReady = pyqtSignal(str, bool, str)      # 更新包下载结果(新exe路径, 成功?, 错误)（线程转投）

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} 一起看 v{APP_VERSION} · PotPlayer 同步播放")
        self.resize(1080, 720)
        self.setMinimumSize(920, 600)
        # 半透明窗口：Win11 22H2+ 有 Mica 背板时才开，避免在旧系统上变成
        # “透出桌面、文字发虚”的廉价效果。
        self._translucent = theme.translucent_enabled()
        if self._translucent:
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_NoSystemBackground, True)

        self.cfg = load_config()
        self.bridge = PotPlayerBridge()
        self.net = NetClient(self)
        self.engine = SyncEngine(self.bridge, self.net, self)
        self.embedded: Optional[EmbeddedServer] = None
        self.tunnel: Optional[TunnelManager] = None
        self._server_before_host = ""
        self._host_port = DEFAULT_PORT
        self._try_ports: list = []

        self.room_code = ""
        self._pending: Optional[tuple] = None   # ("create",) / ("join", code)
        self._seeking = False
        self._last_media_shown = None
        self._closing = False
        self._resuming = False
        self._members: list = []

        self._build_ui()
        self._wire()

        app = QApplication.instance()
        watcher = getattr(app, "_potsync_theme_watcher", None) if app else None
        if watcher is not None:
            watcher.changed.connect(self._on_theme_changed)
        self._on_theme_changed(theme.current())

        self.editNickname.setText(self.cfg.get("nickname", ""))
        self.editServer.setText(self.cfg.get("server", DEFAULT_SERVER))
        self.chkTunnel.setChecked(bool(self.cfg.get("tunnel", True)))
        self.chkHost.setChecked(bool(self.cfg.get("host_mode", False)))
        self.chkTunnel.setEnabled(self.chkHost.isChecked())

        # 首次启动：一次性申请全部防火墙网络权限
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(600, self._maybe_setup_firewall)
        # 启动后静默检查更新（仅在配置了更新源时）
        QTimer.singleShot(3000, lambda: self._check_update(silent=True))
        # 更新完成后第一次启动：把这次的变化亮一次
        QTimer.singleShot(1600, self._maybe_show_whatsnew)

    # ---------- 检查更新 / 自更新 ----------

    def _check_update(self, silent: bool) -> None:
        self._persist()
        if not updater.configured_source(self.cfg):
            if not silent:
                ui_message.info(
                    self, "检查更新",
                    "尚未配置更新源。\n\n请在配置文件中添加更新源（详见 README「检查更新」一节）：\n"
                    f"{CONFIG_FILE}\n\n"
                    '  "update_repo": "你的GitHub用户名/仓库名"\n'
                    '或\n'
                    '  "update_url": "https://你的服务器/version.json"')
            return
        if not silent:
            self.status.showMessage("正在检查更新…", 5000)
        self.btnUpdate.setEnabled(False)
        self.lblUpdateHint.setText("正在检查…")
        import threading

        def work() -> None:
            try:
                info = updater.check_for_update(self.cfg)
                self._sigUpdateResult.emit(silent, info, "")
            except Exception as exc:
                self._sigUpdateResult.emit(silent, None, str(exc))

        threading.Thread(target=work, daemon=True,
                         name="potsync-updater").start()

    def _on_update_result(self, silent: bool,
                          info: Optional[updater.UpdateInfo], error: str) -> None:
        self.btnUpdate.setEnabled(True)
        if error:
            self.lblUpdateHint.setText("检查失败")
            if not silent:
                ui_message.warn(self, "检查更新", f"检查更新失败：\n{error}")
            return
        if info is None:
            self.lblUpdateHint.setText(f"已是最新版本（v{APP_VERSION}）")
            if not silent:
                ui_message.info(self, "检查更新",
                                        f"当前已是最新版本（v{APP_VERSION}）。")
            return
        # 发现新版本：用可滚动的更新说明对话框，完整展示最近几个版本的改动
        self.lblUpdateHint.setText(f"发现新版本 v{info.version.lstrip('vV')}")
        if silent:
            self.status.showMessage(
                f"发现新版本 v{info.version.lstrip('vV')}，可在「设置 → 关于」中更新", 8000)
            return
        if not self._ask_update(info):
            return
        self._download_and_apply(info)

    def _ask_update(self, info: "updater.UpdateInfo") -> bool:
        """展示「更新内容」并询问是否更新；返回用户是否确认。"""
        ver = info.version.lstrip("vV")
        section = whatsnew.section_for(ver)
        if section is not None:
            sections = [section]
        else:
            # 更新源里的说明（Markdown）：压成纯文本行，避免符号干扰阅读
            lines = []
            for raw in (info.notes or "").splitlines():
                line = raw.strip().lstrip("#").strip()
                if not line or set(line) <= set("-|= "):
                    continue
                lines.append(line)
            sections = [(ver, lines)] if lines else []
        return ask_update(self, ver, sections,
                          subtitle=f"当前 v{APP_VERSION}，可更新到 v{ver}")

    def _show_notes(self) -> None:
        """设置页「更新内容」：最近几个版本的变化。"""
        sections = whatsnew.recent_sections(5)
        if not sections:
            ui_message.info(self, "更新内容", "暂时没有可显示的更新说明。")
            return
        show_notes(self, sections, subtitle="最近几个版本的变化")

    def _maybe_show_whatsnew(self) -> None:
        """更新后第一次启动：把这次的变化亮一次。"""
        seen = str(self.cfg.get("last_seen_version", "") or "")
        if seen == APP_VERSION:
            return
        self.cfg["last_seen_version"] = APP_VERSION
        save_config(self.cfg)
        if not seen:
            return           # 全新安装，不打扰
        section = whatsnew.section_for(APP_VERSION)
        sections = [section] if section is not None else whatsnew.recent_sections(2)
        if sections:
            show_notes(self, sections,
                       subtitle=f"已更新到 v{APP_VERSION}，本次的变化")

    def _download_and_apply(self, info: "updater.UpdateInfo") -> None:
        import threading
        # 临时文件名带版本号：避免续传拼接到旧版本残留的临时文件（会损坏新包）
        safe_ver = "".join(c if (c.isalnum() or c in ".-") else "_"
                           for c in info.version)
        # 固定落在 ~/.potsync/updates/：即便替换失败，安装包也还在，可手动运行
        new_exe = updater.updates_dir() / f"PotSync-{safe_ver}.exe"
        if new_exe.exists() and new_exe.stat().st_size > 1_000_000:
            new_exe.unlink()        # 上一轮残留，重新下载避免拼接到坏文件
        self.status.showMessage("正在下载新版本…", 10000)
        self.btnUpdate.setEnabled(False)

        def status(msg: str) -> None:
            self._sigTunnelStatus.emit(f"下载更新：{msg}")

        def work() -> None:
            try:
                updater.download_update(info, new_exe, status=status)
                self._sigUpdateReady.emit(str(new_exe), True, "")
            except Exception as exc:
                self._sigUpdateReady.emit(str(new_exe), False, str(exc))

        threading.Thread(target=work, daemon=True,
                         name="potsync-updl").start()

    def _on_update_ready(self, new_exe: str, ok: bool, error: str) -> None:
        self.btnUpdate.setEnabled(True)
        if not ok:
            ui_message.warn(self, "下载更新", f"下载失败：\n{error}")
            return
        if getattr(sys, "frozen", False):
            hint = ("更新时会自动退出并重启；如果重启失败，安装包仍保留在：\n"
                    f"{Path(new_exe).parent}\n可随时手动运行它。")
            if ui_message.ask(
                    self, "下载完成",
                    f"新版本已下载完成，现在重启并更新？\n\n{hint}",
                    ok_text="重启并更新"):
                import os
                updater.apply_and_restart(Path(sys.executable), Path(new_exe),
                                          old_pid=os.getpid())
                QApplication.instance().quit()
        else:
            ui_message.info(self, "下载完成",
                                    f"新版本已下载到：\n{new_exe}\n（源码模式运行，"
                                    "请手动替换或直接用新版本启动）")

    # ---------- 防火墙权限（首次启动一次性申请） ----------

    def _maybe_setup_firewall(self) -> None:
        import threading
        frozen = getattr(sys, "frozen", False)
        if not frozen and "--setup-firewall" not in sys.argv:
            return
        app_exe = Path(sys.executable)
        if firewall.rules_installed(app_exe):
            return
        self.status.showMessage(
            "首次运行：正在申请网络权限（将弹出一次系统授权，请点击「是」）…", 10000)

        def work() -> None:
            ok, msg = firewall.install_rules(app_exe)
            self._sigFirewall.emit(ok, msg)

        threading.Thread(target=work, daemon=True,
                         name="potsync-firewall").start()

    def _on_firewall_done(self, ok: bool, msg: str) -> None:
        if ok:
            self.status.showMessage("网络权限已就绪：以后当主机不会再被防火墙拦截", 6000)
            self._log("防火墙权限已配置完成")
        else:
            self.status.showMessage("网络权限申请未完成", 6000)
            self._log(f"⚠ 防火墙权限未配置（{msg}）：加入别人房间不受影响；"
                      "当主机时如弹出 Windows 防火墙提示，请点击「允许」")

    # ================= UI 构建 =================

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("root")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar(root)
        self.sidebar.navigate.connect(self._navigate)
        layout.addWidget(self.sidebar)

        content = QWidget(root)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self.stack = QStackedWidget(content)
        self.stack.addWidget(self._build_home())        # 0 主页
        self.stack.addWidget(self._build_room())        # 1 房间
        self.settings_page = self._build_settings()
        self.stack.addWidget(self.settings_page)       # 2 设置
        self.stack.currentChanged.connect(lambda _i: self._update_nav())
        content_layout.addWidget(self.stack, 1)
        layout.addWidget(content, 1)
        self.setCentralWidget(root)

        self.status = QStatusBar(self)
        self.setStatusBar(self.status)
        self.lblConn = QLabel("● 未连接")
        self.lblConn.setObjectName("connLabel")
        self.lblConn.setProperty("state", "idle")
        self.lblRtt = QLabel("")
        self.status.addPermanentWidget(self.lblRtt)
        self.status.addPermanentWidget(self.lblConn)
        self._update_nav()

    def _build_home(self) -> QWidget:
        return build_home(self)

    def _build_room(self) -> QWidget:
        return build_room(self)

    def _build_settings(self) -> QWidget:
        return build_settings(self)

    # ================= 导航 / 主题 / 连接状态 =================

    def _navigate(self, index: int) -> None:
        if index == 1 and not self.room_code:
            self.sidebar.set_current(self.stack.currentIndex())
            return
        self.stack.setCurrentIndex(index)

    def _update_nav(self) -> None:
        self.sidebar.set_page_enabled(1, bool(self.room_code))
        self.sidebar.set_current(self.stack.currentIndex())

    def _go_home(self) -> None:
        if self.stack.currentIndex() != 0:
            self.stack.setCurrentIndex(0)
        self._update_nav()

    def _go_room(self) -> None:
        if self.stack.currentIndex() != 1:
            self.stack.setCurrentIndex(1)
        self._update_nav()

    def _set_conn(self, state: str, text: str) -> None:
        """状态栏连接指示：用动态属性驱动配色，避免到处写死样式表。"""
        self.lblConn.setText(text)
        if self.lblConn.property("state") != state:
            self.lblConn.setProperty("state", state)
            style = self.lblConn.style()
            style.unpolish(self.lblConn)
            style.polish(self.lblConn)

    def _set_badge(self, text: str, kind: str = "muted") -> None:
        badge = getattr(self, "lblBadge", None)
        if badge is None:
            return
        badge.setText(text)
        if badge.property("kind") != kind:
            badge.setProperty("kind", kind)
            style = badge.style()
            style.unpolish(badge)
            style.polish(badge)

    def _on_theme_changed(self, pal) -> None:
        if pal is None:
            pal = theme.current()
        self.sidebar.refresh(pal, pal.mode, pal.accent)
        self._apply_window_icon(pal)
        label = getattr(self, "lblThemeMode", None)
        if label is not None:
            label.setText(f"跟随系统 · {'深色' if pal.mode == 'dark' else '浅色'}")
        accent_label = getattr(self, "lblAccent", None)
        if accent_label is not None:
            accent_label.setText(f"跟随系统 · {pal.accent.upper()}")
        glass_label = getattr(self, "lblTranslucent", None)
        if glass_label is not None:
            glass_label.setText("已启用（Mica）" if pal.translucent else "已关闭")
        if getattr(self, "_members", None):
            self._on_members(self._members)
        self.update()

    def _apply_window_icon(self, pal=None) -> None:
        pal = pal or theme.current()
        try:
            ic = appicon.icon(pal.accent)
            self.setWindowIcon(ic)
            app = QApplication.instance()
            if app is not None:
                app.setWindowIcon(ic)
        except Exception:  # 图标失败不影响主流程
            log.debug("设置窗口图标失败", exc_info=True)

    def _open_log_dir(self) -> None:
        try:
            folder = diagnostics.log_path().parent
            folder.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except Exception as exc:
            self._log(f"打开日志目录失败：{exc}")

    # ================= 信号接线 =================

    def _wire(self) -> None:
        self._sigServerStarted.connect(self._on_server_started)
        self._sigTunnelReady.connect(self._on_tunnel_ready)
        self._sigTunnelFailed.connect(self._on_tunnel_failed)
        self._sigTunnelStatus.connect(
            lambda m: self.status.showMessage(m, 8000))
        self._sigFirewall.connect(self._on_firewall_done)
        self._sigUpdateResult.connect(self._on_update_result)
        self._sigUpdateReady.connect(self._on_update_ready)
        n = self.net
        n.sigConnecting.connect(self._on_connecting)
        n.sigConnected.connect(self._on_connected)
        n.sigConnectionLost.connect(self._on_connection_lost)
        n.sigDisconnected.connect(self._on_disconnected)
        n.sigError.connect(self._on_error)
        n.sigRoomCreated.connect(self._on_room_created)
        n.sigRoomJoined.connect(self._on_room_joined)
        n.sigRoomResumed.connect(self._on_room_resumed)
        n.sigResumeFailed.connect(self._on_resume_failed)
        n.sigState.connect(self._on_remote_state)
        n.sigMembers.connect(self._on_members)
        n.sigNotice.connect(self._log)
        n.sigRtt.connect(lambda ms: self.lblRtt.setText(f"延迟 {ms} ms"))

        e = self.engine
        e.sigLocalState.connect(self._on_local_state)
        e.sigSyncInfo.connect(self._on_sync_info)
        e.sigLog.connect(self._log)
        e.sigBroadcastState.connect(self._broadcast_state)

    def _on_sync_info(self, text: str) -> None:
        self.lblSync.setText(text or "")
        state = "ok"
        if text.startswith("⏳"):
            state = "wait"
        elif text.startswith("⚠") or text.startswith("偏差"):
            state = "warn"
        elif not text:
            state = "ok"
        if self.lblSync.property("state") != state:
            self.lblSync.setProperty("state", state)
            style = self.lblSync.style()
            style.unpolish(self.lblSync)
            style.polish(self.lblSync)

    # ================= 配置 / 昵称 =================

    def _nickname(self) -> str:
        name = self.editNickname.text().strip()
        return name or "匿名"

    def _server(self) -> Tuple[str, int]:
        text = self.editServer.text().strip() or DEFAULT_SERVER
        host, _, port = text.partition(":")
        try:
            return host.strip(), int(port) if port else 8765
        except ValueError:
            return host.strip(), 8765

    def _persist(self) -> None:
        self.cfg["nickname"] = self.editNickname.text().strip()
        self.cfg["server"] = self.editServer.text().strip() or DEFAULT_SERVER
        self.cfg["host_mode"] = self.chkHost.isChecked()
        self.cfg["tunnel"] = self.chkTunnel.isChecked()
        save_config(self.cfg)

    def _save_nickname(self) -> None:
        self._persist()
        if self.net.connected and self.stack.currentIndex() == 1:
            self.net.rename(self._nickname())
        self.status.showMessage("昵称已保存", 2000)

    # ================= 房间进出 =================

    def _ensure_connected(self, pending: tuple) -> None:
        self._persist()
        if self.net.connected:
            self._pending = None
            self._do_pending(pending)
        else:
            self._pending = pending
            host, port = self._server()
            self.status.showMessage(f"正在连接 {host}:{port} …")
            self.net.connect_to(host, port)

    def _do_pending(self, pending: tuple) -> None:
        if pending[0] == "create":
            self.net.create_room(self._nickname())
        else:
            self.net.join_room(pending[1], self._nickname())

    def _create_room(self) -> None:
        if self.chkHost.isChecked() and not (self.embedded and self.embedded.running):
            self._start_embedded_then_create()
        else:
            self._ensure_connected(("create",))

    # ---------- 主机模式（内嵌服务器 + 内网穿透） ----------

    def _toggle_host_mode(self, checked: bool) -> None:
        self.chkTunnel.setEnabled(checked)
        if checked:
            self._server_before_host = self.editServer.text()
            self.editServer.setText(f"{detect_lan_ip()}:{DEFAULT_PORT}")
            if self.chkTunnel.isChecked():
                self.status.showMessage("主机模式：创建房间时将自动建立公网隧道", 5000)
            else:
                self.status.showMessage("主机模式（未开内网穿透）：仅同一网络的好友可加入", 5000)
        else:
            if self._server_before_host:
                self.editServer.setText(self._server_before_host)
            self._stop_tunnel()
            self._stop_embedded()

    def _start_embedded_then_create(self) -> None:
        base_port = self._server()[1]
        self._try_ports = list(range(base_port, base_port + 10))
        self._persist()
        self._try_next_port()

    def _try_next_port(self) -> None:
        if not self._try_ports:
            ui_message.warn(self, "内置服务器启动失败",
                                "本机 10 个连续端口均被占用，请在服务器栏更换端口后重试。")
            return
        port = self._try_ports.pop(0)
        self.status.showMessage(f"正在本机启动内置服务器（端口 {port}）…", 4000)
        self.embedded = EmbeddedServer(
            "0.0.0.0", port,
            on_ready=lambda ok, err, p=port: self._sigServerStarted.emit(ok, p, err))
        self.embedded.start()

    def _on_server_started(self, ok: bool, port: int, err: str) -> None:
        if not ok:
            # 端口被占用则自动尝试下一个
            if self._try_ports and ("address" in err.lower() or "10048" in err
                                    or "占用" in err or "bind" in err.lower()):
                self._log(f"端口 {port} 被占用，尝试下一个端口…")
                self._try_next_port()
                return
            ui_message.warn(self, "内置服务器启动失败",
                                f"无法在本机启动服务器：\n{err}")
            return
        self._host_port = port
        if self.chkHost.isChecked() and self.chkTunnel.isChecked():
            self._start_tunnel(port)
        else:
            self.editServer.setText(f"{detect_lan_ip()}:{port}")
            self._persist()
            self._ensure_connected(("create",))

    def _start_tunnel(self, local_port: int) -> None:
        self._stop_tunnel()
        self.btnCreate.setEnabled(False)
        self.tunnel = TunnelManager(
            on_ready=lambda ep: self._sigTunnelReady.emit(ep),
            on_error=lambda e: self._sigTunnelFailed.emit(e),
            on_status=lambda s: self._sigTunnelStatus.emit(s))
        self.tunnel.start(local_port)

    def _on_tunnel_ready(self, ep: PublicEndpoint) -> None:
        self.btnCreate.setEnabled(True)
        self.editServer.setText(f"{ep.host}:{ep.port}")
        self._persist()
        self._log(f"公网隧道已建立（{ep.provider}）：{ep.display}")
        self.status.showMessage("公网隧道已建立，正在创建房间…", 4000)
        self._ensure_connected(("create",))

    def _on_tunnel_failed(self, msg: str) -> None:
        self.btnCreate.setEnabled(True)
        self.editServer.setText(f"{detect_lan_ip()}:{self._host_port}")
        self._persist()
        ui_message.warn(
            self, "内网穿透失败",
            f"无法建立公网隧道（{msg}）。\n\n"
            f"将改用局域网地址 {self.editServer.text()} 创建房间，"
            "仅同一网络内的好友可加入。\n如需异地联机，请检查网络后重试。")
        self._ensure_connected(("create",))

    def _stop_tunnel(self) -> None:
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None
        self.btnCreate.setEnabled(True)

    def _stop_embedded(self) -> None:
        if self.embedded is not None:
            self.embedded.stop()
            self.embedded.wait_stopped(2)
            self.embedded = None

    def _join_from_input(self) -> None:
        text = self.editJoin.text()
        host, port, code = parse_invite(text)
        if not code:
            self.status.showMessage("无法识别的房间号或邀请链接", 3000)
            return
        if host:
            self.editServer.setText(f"{host}:{port or 8765}")
        self._ensure_connected(("join", code))

    def _join_from_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text()
        host, port, code = parse_invite(text or "")
        if not code:
            self.status.showMessage("剪贴板里没有有效的邀请链接/房间号", 3000)
            self._log("剪贴板内容不是有效的邀请链接")
            return
        if host:
            self.editServer.setText(f"{host}:{port or 8765}")
        self.editJoin.setText(code)
        self._log(f"已从剪贴板读取邀请链接，加入房间 {code}")
        self._ensure_connected(("join", code))

    def _leave_room(self) -> None:
        self.net.leave_room()
        self.engine.leave_room()
        self.room_code = ""
        self._go_home()
        self._set_badge("未连接", "bad")
        self._log("已离开房间")

    # ================= 邀请链接 =================

    def _copy_invite_link(self) -> None:
        if not self.room_code:
            return
        host, port = self._server()
        link = f"potsync://{host}:{port}/{self.room_code}"
        QGuiApplication.clipboard().setText(link)
        self.status.showMessage("邀请链接已复制到剪贴板", 2500)
        self._log(f"邀请链接已复制：{link}")

    # ================= 网络回调 =================

    def _on_connecting(self, attempt: int, delay_ms: int) -> None:
        if attempt <= 1:
            self._set_conn("warn", "● 连接中…")
        else:
            self._set_conn("warn", f"● 重连中（第 {attempt} 次）")
        if self.stack.currentIndex() == 1 and attempt in (2, 4, 6, 8):
            self._log(f"正在重连（第 {attempt} 次，{delay_ms / 1000:.1f}s 后重试）…")

    def _on_connected(self) -> None:
        self._set_conn("ok", "● 已连接")
        self._set_badge("已连接", "ok")
        if self.stack.currentIndex() == 1 and self.room_code:
            self._resuming = True
            self.status.showMessage("已重新连上服务器，正在恢复房间…", 4000)
            self._log("已重新连上服务器，正在恢复房间…")
        else:
            self.status.showMessage("已连接服务器", 2000)
        if self._pending:
            pending, self._pending = self._pending, None
            self._do_pending(pending)

    def _on_connection_lost(self, reason: str) -> None:
        """意外掉线：房间与成员身份都保留，只显示重连状态，绝不主动退房。"""
        self._set_conn("warn", "● 重连中…")
        self._set_badge("重连中", "warn")
        self.lblRtt.setText("")
        if self.stack.currentIndex() == 1:
            self._log(f"⚠ 连接中断（{reason}），正在自动重连；房间与播放器状态都保留")
            self.status.showMessage("连接中断，正在自动重连…", 8000)
        else:
            self.status.showMessage(f"连接中断：{reason}", 4000)

    def _on_disconnected(self, reason: str) -> None:
        """连接线程彻底结束（主动关闭或已无法恢复）时的兜底。"""
        if self._closing:
            return
        self._set_conn("bad", "● 未连接")
        self._set_badge("未连接", "bad")
        self.lblRtt.setText("")
        if self.stack.currentIndex() == 1:
            self.engine.leave_room()
            self.room_code = ""
            self._go_home()
            self.status.showMessage("与服务器断开，已退出房间", 4000)

    def _on_room_resumed(self, room: str, state: Optional[dict], members: list) -> None:
        """重连后服务器原地恢复了成员身份（房间还在、宽限期内）。"""
        self._resuming = False
        if self.stack.currentIndex() != 1 or self.room_code != room:
            self._enter_room(room, state, members)
            self._log(f"已恢复房间 {room}")
            return
        self._on_members(members)
        self.engine.enter_room(state)   # 重新对齐进度与播放/暂停
        self._set_badge("已恢复", "ok")
        self._log("✓ 已恢复房间，正在按服务器时间对齐进度…")
        self.status.showMessage("已恢复房间", 3000)

    def _on_resume_failed(self, message: str) -> None:
        """房间已解散/身份过期：这时才真正回主页。"""
        self._resuming = False
        self._log(f"⚠ 房间无法恢复：{message}")
        if self.stack.currentIndex() == 1:
            self.engine.leave_room()
            self.room_code = ""
            self._go_home()
            self.status.showMessage(f"房间已不存在（{message}），请重新创建或加入", 6000)

    def _on_error(self, message: str) -> None:
        self._log(f"⚠ {message}")
        self.status.showMessage(message, 4000)

    def _on_room_created(self, room: str, state: Optional[dict], members: list) -> None:
        self._enter_room(room, state, members)
        self._log(f"房间 {room} 已创建，把邀请链接发给好友吧")
        if self.chkHost.isChecked():
            self._log("你是主机：好友通过你的 IP 直连；关闭本程序会断开所有人")

    def _on_room_joined(self, room: str, state: Optional[dict], members: list) -> None:
        self._enter_room(room, state, members)
        self._log(f"已加入房间 {room}")

    def _enter_room(self, room: str, state: Optional[dict], members: list) -> None:
        self.room_code = room
        self.lblRoomCode.setText(room)
        self._on_members(members)
        self.txtLog.clear()
        self._go_room()
        self._set_badge("已连接", "ok")
        self.engine.enter_room(state)
        if state:
            self._log(f"同步房间状态：{'暂停' if state.get('paused') else '播放中'} "
                      f"@ {fmt_time(state.get('position', 0))}")

    def _on_remote_state(self, s: dict) -> None:
        self.engine.apply_remote_state(s)

    def _on_members(self, members: list) -> None:
        self._members = list(members or [])
        self.listMembers.clear()
        muted = theme.current().text_muted if theme.current() else "#8b949e"
        for m in self._members:
            name = m.get("name", "匿名")
            online = bool(m.get("online", True))
            item = QListWidgetItem(name if online else f"{name}（离线，等待重连）")
            if not online:
                item.setForeground(QColor(muted))
            self.listMembers.addItem(item)

    def _broadcast_state(self, state: dict) -> None:
        self.net.send_state(
            paused=state["paused"], position=state["position"],
            media=state.get("media", ""), duration=state.get("duration", 0),
            action=state.get("action", "state"))

    # ================= 本地状态显示 =================

    def _on_local_state(self, snap: Optional[Snapshot]) -> None:
        if snap is None:
            if self.stack.currentIndex() == 1:
                self.lblStatus.setText("状态：PotPlayer 未运行")
            else:
                self.lblStatus.setText("状态：未检测到 PotPlayer")
            return
        self.lblStatus.setText(f"状态：{STATUS_TEXT.get(snap.status, '未知')}")
        self.btnToggle.setText("⏸ 暂停" if snap.status == STATUS_RUNNING else "▶ 播放")
        media = snap.media or "（未打开文件）"
        if media != self._last_media_shown:
            self._last_media_shown = media
            dur = f"（{fmt_time(snap.duration)}）" if snap.duration else ""
            self.lblMedia.setText(f"媒体：{media} {dur}")
        if snap.duration > 0:
            if self.slider.maximum() != snap.duration:
                self.slider.setRange(0, snap.duration)
            if not self._seeking:
                self.slider.setValue(snap.position)
            self.lblCur.setText(fmt_time(self.slider.value() if self._seeking
                                         else snap.position))
            self.lblDur.setText(fmt_time(snap.duration))
        else:
            self.lblCur.setText("00:00")
            self.lblDur.setText("00:00")

    # ================= 进度条 =================

    def _seek_start(self) -> None:
        self._seeking = True

    def _seek_commit(self) -> None:
        self._seeking = False
        self.engine.user_seek(self.slider.value())

    # ================= 日志 / 关闭 =================

    def _log(self, text: str) -> None:
        if not text:
            return
        from datetime import datetime
        self.txtLog.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        pal = theme.current()
        theme.apply_titlebar(self, pal.mode == "dark")
        if getattr(self, "_translucent", False):
            if not theme.enable_backdrop(self, pal):
                # 背板没开起来：退回不透明底色，保证可读性
                self._translucent = False
                fallback = theme.build_palette(pal.mode, pal.accent, translucent=False)
                theme.apply(QApplication.instance(), fallback.mode, fallback.accent)

    def closeEvent(self, event) -> None:
        self._closing = True
        self._persist()
        self.net.leave_room()
        self.net.shutdown()
        self._stop_tunnel()
        self._stop_embedded()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())
