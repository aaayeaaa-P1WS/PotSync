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

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSlider, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from net_client import NetClient
from pot_bridge import STATUS_RUNNING, STATUS_TEXT, PotPlayerBridge, Snapshot
from relay import EmbeddedServer
from sync_engine import SyncEngine
from tunnel import PublicEndpoint, TunnelManager
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
        self.resize(560, 640)

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

        self._build_ui()
        self._wire()

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

    # ---------- 检查更新 / 自更新 ----------

    def _check_update(self, silent: bool) -> None:
        self._persist()
        if not updater.configured_source(self.cfg):
            if not silent:
                QMessageBox.information(
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
            if not silent:
                QMessageBox.warning(self, "检查更新", f"检查更新失败：\n{error}")
            return
        if info is None:
            if not silent:
                QMessageBox.information(self, "检查更新",
                                        f"当前已是最新版本（v{APP_VERSION}）。")
            return
        # 发现新版本
        notes = (info.notes or "").strip()[:500]
        ver = info.version.lstrip("vV")
        text = f"发现新版本 v{ver}（当前 v{APP_VERSION}）。"
        if notes:
            text += f"\n\n更新说明：\n{notes}"
        text += "\n\n是否下载并更新？（下载完成后重启软件生效）"
        if QMessageBox.question(self, "发现新版本", text,
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._download_and_apply(info)

    def _download_and_apply(self, info: "updater.UpdateInfo") -> None:
        import threading
        # 临时文件名带版本号：避免续传拼接到旧版本残留的临时文件（会损坏新包）
        safe_ver = "".join(c if (c.isalnum() or c in ".-") else "_"
                           for c in info.version)
        new_exe = Path(sys.executable).with_name(f"PotSync-{safe_ver}.exe") \
            if getattr(sys, "frozen", False) \
            else Path.home() / ".potsync" / f"PotSync-{safe_ver}.exe"
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
            QMessageBox.warning(self, "下载更新", f"下载失败：\n{error}")
            return
        if getattr(sys, "frozen", False):
            if QMessageBox.question(
                    self, "下载完成",
                    "新版本已下载完成，现在重启并更新？") == QMessageBox.Yes:
                updater.apply_and_restart(Path(sys.executable), Path(new_exe))
                QApplication.instance().quit()
        else:
            QMessageBox.information(self, "下载完成",
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
        self.stack = QStackedWidget(self)
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self._build_home())
        self.stack.addWidget(self._build_room())

        self.status = QStatusBar(self)
        self.setStatusBar(self.status)
        self.lblConn = QLabel("● 未连接")
        self.lblConn.setObjectName("connLabel")
        self.lblRtt = QLabel("")
        self.status.addPermanentWidget(self.lblRtt)
        self.status.addPermanentWidget(self.lblConn)

    def _build_home(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(36, 30, 36, 24)
        v.setSpacing(14)

        title = QLabel("PotSync 一起看")
        title.setObjectName("title")
        subtitle = QLabel("在多台电脑之间同步 PotPlayer 的播放、暂停与进度")
        subtitle.setObjectName("subtitle")
        v.addWidget(title)
        v.addWidget(subtitle)
        v.addSpacing(10)

        # 昵称
        row = QHBoxLayout()
        row.addWidget(QLabel("昵　称"))
        self.editNickname = QLineEdit()
        self.editNickname.setPlaceholderText("给自己起个名字（房间成员可见）")
        self.editNickname.setMaxLength(24)
        self.btnSaveNick = QPushButton("保存")
        self.btnSaveNick.setObjectName("smallBtn")
        self.btnSaveNick.clicked.connect(self._save_nickname)
        row.addWidget(self.editNickname, 1)
        row.addWidget(self.btnSaveNick)
        v.addLayout(row)

        # 服务器
        row = QHBoxLayout()
        row.addWidget(QLabel("服务器"))
        self.editServer = QLineEdit()
        self.editServer.setPlaceholderText("中继服务器地址，如 1.2.3.4:8765")
        row.addWidget(self.editServer, 1)
        v.addLayout(row)

        # 主机模式：本机内嵌服务器
        self.chkHost = QCheckBox("在本机启动服务器（我当主机，好友直连我的电脑）")
        self.chkHost.setToolTip("无需单独部署服务器：建房时自动在本机启动内置中继。\n"
                                "勾选下方内网穿透即可让异地好友加入。")
        self.chkHost.toggled.connect(self._toggle_host_mode)
        v.addWidget(self.chkHost)

        # 内网穿透
        row = QHBoxLayout()
        row.addSpacing(22)
        self.chkTunnel = QCheckBox("内网穿透（自动生成公网地址，异地好友可直接加入）")
        self.chkTunnel.setToolTip("通过 Cloudflare 免费隧道把本机服务器暴露到公网，\n"
                                  "首次使用自动下载组件（约 30MB），无需注册。")
        row.addWidget(self.chkTunnel, 1)
        v.addLayout(row)

        v.addSpacing(8)
        self.btnCreate = QPushButton("创 建 房 间")
        self.btnCreate.setObjectName("primaryBtn")
        self.btnCreate.clicked.connect(self._create_room)
        v.addWidget(self.btnCreate)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("divider")
        v.addWidget(line)

        v.addWidget(QLabel("加入好友的房间："))
        self.editJoin = QLineEdit()
        self.editJoin.setPlaceholderText("输入房间号 或 邀请链接 potsync://…")
        v.addWidget(self.editJoin)

        row = QHBoxLayout()
        self.btnJoin = QPushButton("加 入 房 间")
        self.btnJoin.setObjectName("primaryBtn")
        self.btnJoin.clicked.connect(self._join_from_input)
        self.btnJoinClip = QPushButton("📋 从剪贴板读取链接并加入")
        self.btnJoinClip.clicked.connect(self._join_from_clipboard)
        row.addWidget(self.btnJoin, 1)
        row.addWidget(self.btnJoinClip, 1)
        v.addLayout(row)

        tip = QLabel("提示：创建房间后把邀请链接发给好友，对方复制链接后点\n"
                     "「从剪贴板读取链接并加入」即可。双方需打开同一个视频文件。")
        tip.setObjectName("tip")
        tip.setWordWrap(True)
        v.addSpacing(6)
        v.addWidget(tip)
        v.addStretch(1)

        # 版本 + 检查更新
        row = QHBoxLayout()
        lblVer = QLabel(f"v{APP_VERSION}")
        lblVer.setObjectName("tip")
        row.addWidget(lblVer)
        row.addStretch(1)
        self.btnUpdate = QPushButton("检查更新")
        self.btnUpdate.setObjectName("smallBtn")
        self.btnUpdate.setFlat(True)
        self.btnUpdate.clicked.connect(lambda: self._check_update(silent=False))
        row.addWidget(self.btnUpdate)
        v.addLayout(row)
        return page

    def _build_room(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 22, 28, 18)
        v.setSpacing(10)

        # 房间号 + 复制链接
        row = QHBoxLayout()
        col = QVBoxLayout()
        lab = QLabel("房间号")
        lab.setObjectName("subtitle")
        self.lblRoomCode = QLabel("——————")
        self.lblRoomCode.setObjectName("roomCode")
        self.lblRoomCode.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(lab)
        col.addWidget(self.lblRoomCode)
        row.addLayout(col, 1)
        self.btnCopyLink = QPushButton("🔗 复制邀请链接")
        self.btnCopyLink.clicked.connect(self._copy_invite_link)
        row.addWidget(self.btnCopyLink, 0, Qt.AlignBottom)
        v.addLayout(row)

        # 成员 + 媒体信息
        row = QHBoxLayout()
        self.listMembers = QListWidget()
        self.listMembers.setObjectName("memberList")
        self.listMembers.setMaximumWidth(190)
        row.addWidget(self.listMembers)

        info = QVBoxLayout()
        self.lblMedia = QLabel("媒体：—")
        self.lblMedia.setWordWrap(True)
        self.lblStatus = QLabel("状态：—")
        self.lblSync = QLabel("")
        self.lblSync.setObjectName("syncLabel")
        info.addWidget(self.lblMedia)
        info.addWidget(self.lblStatus)
        info.addWidget(self.lblSync)
        info.addStretch(1)
        row.addLayout(info, 1)
        v.addLayout(row)

        # 进度条
        prow = QHBoxLayout()
        self.lblCur = QLabel("00:00")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(self._seek_start)
        self.slider.sliderReleased.connect(self._seek_commit)
        self.lblDur = QLabel("00:00")
        prow.addWidget(self.lblCur)
        prow.addWidget(self.slider, 1)
        prow.addWidget(self.lblDur)
        v.addLayout(prow)

        # 控制按钮
        brow = QHBoxLayout()
        self.btnPrev = QPushButton("⏮ 上一集")
        self.btnPrev.setToolTip("切换到播放列表上一个（全房间同步）")
        self.btnPrev.clicked.connect(lambda: self.engine.user_episode(-1))
        self.btnToggle = QPushButton("▶ 播放")
        self.btnToggle.setObjectName("primaryBtn")
        self.btnToggle.clicked.connect(self.engine.user_toggle_play_pause)
        self.btnNext = QPushButton("⏭ 下一集")
        self.btnNext.setToolTip("切换到播放列表下一个（全房间同步）")
        self.btnNext.clicked.connect(lambda: self.engine.user_episode(+1))
        self.btnSyncMe = QPushButton("⟳ 以我的进度为准")
        self.btnSyncMe.clicked.connect(self.engine.user_force_sync_to_me)
        self.btnLeave = QPushButton("离开房间")
        self.btnLeave.setObjectName("dangerBtn")
        self.btnLeave.clicked.connect(self._leave_room)
        brow.addWidget(self.btnPrev, 2)
        brow.addWidget(self.btnToggle, 2)
        brow.addWidget(self.btnNext, 2)
        brow.addWidget(self.btnSyncMe, 2)
        brow.addWidget(self.btnLeave, 1)
        v.addLayout(brow)

        # 日志
        self.txtLog = QPlainTextEdit()
        self.txtLog.setReadOnly(True)
        self.txtLog.setObjectName("logView")
        self.txtLog.setMaximumBlockCount(300)
        v.addWidget(self.txtLog, 1)
        return page

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
        n.sigConnected.connect(self._on_connected)
        n.sigDisconnected.connect(self._on_disconnected)
        n.sigError.connect(self._on_error)
        n.sigRoomCreated.connect(self._on_room_created)
        n.sigRoomJoined.connect(self._on_room_joined)
        n.sigState.connect(self._on_remote_state)
        n.sigMembers.connect(self._on_members)
        n.sigNotice.connect(self._log)
        n.sigRtt.connect(lambda ms: self.lblRtt.setText(f"延迟 {ms} ms"))

        e = self.engine
        e.sigLocalState.connect(self._on_local_state)
        e.sigSyncInfo.connect(self.lblSync.setText)
        e.sigLog.connect(self._log)
        e.sigBroadcastState.connect(self._broadcast_state)

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
            QMessageBox.warning(self, "内置服务器启动失败",
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
            QMessageBox.warning(self, "内置服务器启动失败",
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
        QMessageBox.warning(
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
        self.stack.setCurrentIndex(0)
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

    def _on_connected(self) -> None:
        self.lblConn.setText("● 已连接")
        self.lblConn.setStyleSheet("color:#3dd68c;")
        self.status.showMessage("已连接服务器", 2000)
        if self._pending:
            pending, self._pending = self._pending, None
            self._do_pending(pending)

    def _on_disconnected(self, reason: str) -> None:
        self.lblConn.setText("● 未连接")
        self.lblConn.setStyleSheet("color:#e5534b;")
        self.lblRtt.setText("")
        if self.stack.currentIndex() == 1:
            self.engine.leave_room()
            self.room_code = ""
            self.stack.setCurrentIndex(0)
            self.status.showMessage("与服务器断开，已退出房间", 4000)

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
        self.stack.setCurrentIndex(1)
        self.engine.enter_room(state)
        if state:
            self._log(f"同步房间状态：{'暂停' if state.get('paused') else '播放中'} "
                      f"@ {fmt_time(state.get('position', 0))}")

    def _on_remote_state(self, s: dict) -> None:
        self.engine.apply_remote_state(s)

    def _on_members(self, members: list) -> None:
        self.listMembers.clear()
        for m in members:
            item = QListWidgetItem(f"👤 {m.get('name', '匿名')}")
            self.listMembers.addItem(item)

    def _broadcast_state(self, state: dict) -> None:
        self.net.send_state(
            paused=state["paused"], position=state["position"],
            media=state.get("media", ""), duration=state.get("duration", 0),
            action=state.get("action", "state"))

    # ================= 本地状态显示 =================

    def _on_local_state(self, snap: Optional[Snapshot]) -> None:
        if snap is None:
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

    def closeEvent(self, event) -> None:
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
