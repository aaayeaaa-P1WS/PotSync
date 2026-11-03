"""客户端界面构件（2.0 起从 main_window.py 拆出，便于单独修改与测试）。"""

from .home_page import build_home
from .room_page import build_room

__all__ = ["build_home", "build_room"]
