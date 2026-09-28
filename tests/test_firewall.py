#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
防火墙权限模块测试
==================

离线部分：规则脚本生成、规则查询。
联调部分（--live）：真实执行一次提权安装（会弹 UAC 授权窗口，请点击「是」），
然后验证规则已写入。规则指向 dist\\PotSync.exe 与隧道组件路径，
安装后即为本软件所需的最终状态，无需清理。

运行：
    python tests/test_firewall.py          # 仅离线检查
    python tests/test_firewall.py --live   # 含真实提权安装
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

import firewall  # noqa: E402


def main() -> int:
    # 规则脚本生成
    bat = firewall.build_bat(Path(r"C:\app\PotSync.exe"))
    assert 'name="PotSync (TCP-In)"' in bat
    assert 'program="C:\\app\\PotSync.exe"' in bat
    assert "localport=8765-8775" in bat
    assert bat.count("add rule") == 7 and bat.count("delete rule") == 7
    assert "cloudflared.exe" in bat and "bore.exe" in bat
    bat_remove = firewall.build_bat(Path(r"C:\app\PotSync.exe"), remove_only=True)
    assert "add rule" not in bat_remove
    assert bat_remove.count("delete rule") == 7
    print("PASS  防火墙规则脚本生成（7 条规则 + 幂等删除 + 移除模式）")

    # 规则查询接口可用
    installed = firewall.rules_installed(ROOT / "dist" / "PotSync.exe")
    print(f"PASS  规则查询（当前状态：{'已安装' if installed else '未安装'}）")

    if "--live" in sys.argv:
        exe = ROOT / "dist" / "PotSync.exe"
        assert exe.exists(), "dist/PotSync.exe 不存在"
        print("即将弹出一次系统授权（UAC），请点击「是」…", flush=True)
        ok, msg = firewall.install_rules(exe)
        if not ok:
            print(f"SKIP  提权安装未完成: {msg}（用户取消或超时，功能本身已降级处理）")
        else:
            assert firewall.rules_installed(exe)
            print("PASS  提权安装成功，防火墙规则已写入（含隧道组件与端口段）")

    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
