#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
首次启动防火墙权限申请
======================

问题：Windows 防火墙的"是否允许访问网络"弹窗只在程序**第一次监听端口**时出现
（创建房间当主机那一刻），一旦被忽略/拒绝，异地好友就永远连不进来。

方案：首次启动时一次性主动申请全部所需网络权限——弹出**一次** UAC 授权，
由管理员权限的 netsh 写入以下防火墙规则（之后运行再不打扰）：

- PotSync.exe（本程序）TCP 入站 / 出站放行
- cloudflared.exe / bore.exe（隧道组件，下载前预登记路径）TCP 入站 / 出站放行
- TCP 8765-8775 端口段入站放行（内嵌中继端口自动重试的范围兜底）

所有规则幂等（先删同名旧规则再添加）。用户拒绝 UAC 时不影响客户端加入功能，
仅主机模式可能在建房时被系统防火墙弹窗拦截（届时日志会提示）。
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("potsync.firewall")

# 内嵌中继服务器端口自动重试范围（与 main_window 的 _try_ports 对应）
PORT_RANGE = "8765-8775"
BIN_DIR = Path.home() / ".potsync" / "bin"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _rule_specs(app_exe: Path) -> list:
    """(规则名, 方向, 程序路径|None, 端口|None)"""
    return [
        ("PotSync (TCP-In)", "in", str(app_exe), None),
        ("PotSync (TCP-Out)", "out", str(app_exe), None),
        ("PotSync Tunnel cloudflared (TCP-In)", "in", str(BIN_DIR / "cloudflared.exe"), None),
        ("PotSync Tunnel cloudflared (TCP-Out)", "out", str(BIN_DIR / "cloudflared.exe"), None),
        ("PotSync Tunnel bore (TCP-In)", "in", str(BIN_DIR / "bore.exe"), None),
        ("PotSync Tunnel bore (TCP-Out)", "out", str(BIN_DIR / "bore.exe"), None),
        ("PotSync Relay Ports (TCP-In)", "in", None, PORT_RANGE),
    ]


def build_bat(app_exe: Path, remove_only: bool = False) -> str:
    """生成幂等的防火墙规则安装/移除批处理。"""
    lines = ["@echo off"]
    for name, direction, program, ports in _rule_specs(app_exe):
        # 先删同名旧规则，保证重复执行不产生重复规则
        lines.append(f'netsh advfirewall firewall delete rule name="{name}" >nul 2>&1')
        if remove_only:
            continue
        cmd = (f'netsh advfirewall firewall add rule name="{name}" '
               f'dir={direction} action=allow enable=yes profile=any')
        if program:
            cmd += f' program="{program}"'
        if ports:
            cmd += f' protocol=TCP localport={ports}'
        lines.append(cmd)
    return "\r\n".join(lines) + "\r\n"


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def rules_installed(app_exe: Optional[Path] = None) -> bool:
    """检查主规则是否已存在（查询无需管理员权限）。"""
    names = ["PotSync (TCP-In)", "PotSync Relay Ports (TCP-In)"]
    for name in names:
        try:
            r = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                capture_output=True, text=True, encoding="gbk", errors="replace",
                timeout=15, creationflags=CREATE_NO_WINDOW)
        except Exception:
            return False
        if name not in r.stdout:   # 无匹配规则的提示信息中不含规则名本身
            return False
    return True


# ---------- 提权执行 ----------

class _SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE), ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
ERROR_CANCELLED = 1223


def _run_elevated(bat_path: Path, timeout_s: int = 120) -> Tuple[bool, str]:
    """以管理员身份运行批处理（触发一次 UAC），等待结束。"""
    if is_elevated():
        r = subprocess.run(["cmd", "/c", str(bat_path)], capture_output=True,
                           timeout=timeout_s, creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0, ""

    info = _SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFO)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = "cmd.exe"
    info.lpParameters = f'/c "{bat_path}"'
    info.lpDirectory = None
    info.nShow = SW_HIDE

    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        err = ctypes.GetLastError()
        if err == ERROR_CANCELLED:
            return False, "用户取消了授权"
        return False, f"提权启动失败（错误码 {err}）"
    if info.hProcess:
        ctypes.windll.kernel32.WaitForSingleObject(info.hProcess,
                                                   int(timeout_s * 1000))
        ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return True, ""


def install_rules(app_exe: Path, remove_only: bool = False) -> Tuple[bool, str]:
    """安装（或 remove_only=True 时移除）全部防火墙规则。

    返回 (是否生效, 说明)。是否生效以规则实际写入结果为准。
    """
    bat = build_bat(app_exe, remove_only=remove_only)
    bat_path = Path(tempfile.gettempdir()) / "potsync_firewall.bat"
    bat_path.write_text(bat, encoding="gbk", errors="replace")

    ok, msg = _run_elevated(bat_path)
    if not ok:
        return False, msg

    time.sleep(0.5)   # 等规则落库
    if remove_only:
        return (not rules_installed(app_exe)), ""
    if rules_installed(app_exe):
        return True, ""
    return False, "规则未生效（可能被安全软件拦截）"


if __name__ == "__main__":
    # 手动维护：python firewall.py install|remove|status [exe路径]
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    exe = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(sys.executable)

    if action == "status":
        print("已安装" if rules_installed(exe) else "未安装")
    elif action == "install":
        ok, msg = install_rules(exe)
        print("安装成功" if ok else f"安装失败: {msg}")
    elif action == "remove":
        ok, msg = install_rules(exe, remove_only=True)
        print("已移除" if ok else f"移除失败: {msg}")
    else:
        print("用法: firewall.py install|remove|status [exe路径]")
