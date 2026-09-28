#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PotSync 中继服务器 —— 独立部署入口
==================================

服务器核心逻辑在 client/relay.py（客户端 exe 内嵌的同一份代码），
本文件只是便于在 VPS / 独立机器上运行的薄壳：

    python server.py --host 0.0.0.0 --port 8765
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))

from relay import main  # noqa: E402

if __name__ == "__main__":
    main()
