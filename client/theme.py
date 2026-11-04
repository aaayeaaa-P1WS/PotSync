#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 11 风格主题
====================

职责：
1. 读取系统"应用模式"（深色/浅色）与系统强调色（设置 → 个性化 → 颜色）；
2. 生成一套 Windows 11 语义色（窗口底色、卡片、描边、文字、强调色…）；
3. 把 ``resources/style.qss``（模板）里的 ``$token`` 替换成实际颜色后应用到 QApplication；
4. 运行中每 1.5 秒跟随系统切换（无需重启），并同步标题栏深浅色。

环境变量 ``POTSYNC_THEME=light|dark`` 可强制主题（测试/截图用）。
"""

import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import QApplication

log = logging.getLogger("potsync.theme")

STYLE_FILE = Path(__file__).resolve().parent / "resources" / "style.qss"


def _style_file() -> Optional[Path]:
    """定位样式模板：源码运行时在 client/resources，onefile 打包后在 _MEIPASS。"""
    here = Path(__file__).resolve().parent
    base = getattr(sys, "_MEIPASS", None)
    candidates = [here / "resources" / "style.qss"]
    if base:
        meipass = Path(base)
        candidates += [meipass / "resources" / "style.qss",
                       meipass / "client" / "resources" / "style.qss"]
    for path in candidates:
        if path.exists():
            return path
    return None

# Windows 11 默认强调色（设置里没取到时的兜底）
DEFAULT_ACCENT = "#0067C0"


# --------------------------------------------------------------------------
# 系统信息
# --------------------------------------------------------------------------

def _reg_int(root: int, path: str, name: str) -> Optional[int]:
    try:
        import winreg
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return int(value)
    except Exception:
        return None


def system_mode() -> str:
    """返回 'light' / 'dark'。"""
    forced = os.environ.get("POTSYNC_THEME", "").strip().lower()
    if forced in ("light", "dark"):
        return forced
    if sys.platform != "win32":
        return "dark"
    import winreg
    value = _reg_int(winreg.HKEY_CURRENT_USER,
                     r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                     "AppsUseLightTheme")
    if value is None:
        return "dark"
    return "light" if value else "dark"


def system_accent() -> str:
    """读取系统强调色（注册表里是 ABGR），失败返回 Win11 默认蓝。

    环境变量 ``POTSYNC_ACCENT=#RRGGBB`` 可强制指定（截图/测试用）。
    """
    forced = os.environ.get("POTSYNC_ACCENT", "").strip()
    if forced:
        color = QColor(forced)
        if color.isValid():
            return color.name(QColor.HexRgb)
    if sys.platform != "win32":
        return DEFAULT_ACCENT
    import winreg
    value = _reg_int(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM", "AccentColor")
    if value is None:
        value = _reg_int(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM",
                         "ColorizationColor")
    if value is None:
        return DEFAULT_ACCENT
    value &= 0xFFFFFFFF
    r = value & 0xFF
    g = (value >> 8) & 0xFF
    b = (value >> 16) & 0xFF
    # 系统颜色设为「自动/灰」时 AccentColor 是中性灰，Win11 控件实际用默认蓝。
    if max(r, g, b) - min(r, g, b) < 28:
        return DEFAULT_ACCENT
    return f"#{r:02x}{g:02x}{b:02x}"


# --------------------------------------------------------------------------
# 颜色工具
# --------------------------------------------------------------------------

def _rgb(color: str):
    c = QColor(color)
    return c.red(), c.green(), c.blue()


def blend(a: str, b: str, t: float) -> str:
    """把 a 往 b 混合 t（0..1）。"""
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    r = round(ar + (br - ar) * t)
    g = round(ag + (bg - ag) * t)
    bl = round(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


def _is_light(color: str) -> bool:
    r, g, b = _rgb(color)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.62


def fit_accent(accent: str, mode: str) -> str:
    """让强调色在深浅背景下都足够清楚（Win11 深色主题会把强调色提亮）。"""
    if mode == "dark":
        return blend(accent, "#ffffff", 0.45) if not _is_light(accent) else accent
    return blend(accent, "#000000", 0.25) if _is_light(accent) else accent


@dataclass(frozen=True)
class Palette:
    mode: str
    accent: str
    translucent: bool

    bg: str
    bg_end: str
    surface: str
    surface_alt: str
    surface_hover: str
    border: str
    border_strong: str
    text: str
    text_dim: str
    text_muted: str
    accent_text: str        # 强调色在浅底上的文字变体
    on_accent: str
    accent_soft: str
    accent_soft_strong: str
    selected: str
    sidebar: str
    log_bg: str
    danger: str
    danger_soft: str
    success: str
    warn: str
    scroll: str
    scroll_hover: str

    def tokens(self) -> dict:
        data = {k: v for k, v in self.__dict__.items()}
        # 半透明版本：有系统背板（Mica/亚克力）时用 alpha，否则退回不透明，
        # 保证任何环境下文字对比度都不低于不透明方案。
        # 半透明但不牺牲可读性：窗口 0.90、卡片 0.94、日志 0.96。
        # 这样即使背后是白底网页，正文对比度也在 10:1 以上。
        a_win = 0.88 if self.translucent else 1.0
        a_side = 0.80 if self.translucent else 1.0
        a_card = 0.93 if self.translucent else 1.0
        a_alt = 0.70 if self.translucent else 1.0
        a_log = 0.95 if self.translucent else 1.0
        a_field = 0.95 if self.translucent else 1.0
        data["bg_a"] = _rgba(self.bg, a_win)
        data["bg_end_a"] = _rgba(self.bg_end, a_win)
        data["window_base"] = "transparent" if self.translucent else self.bg
        data["sidebar_a"] = _rgba(self.sidebar, a_side)
        data["surface_a"] = _rgba(self.surface, a_card)
        data["surface_alt_a"] = _rgba(self.surface_alt, a_alt)
        data["surface_hover_a"] = _rgba(self.surface_hover, 0.82 if self.translucent else 1.0)
        data["log_bg_a"] = _rgba(self.log_bg, a_log)
        data["field_a"] = _rgba(self.surface, a_field)
        data["selected_a"] = _rgba(self.selected, 0.72 if self.translucent else 1.0)
        data["status_a"] = _rgba(self.sidebar, 0.86 if self.translucent else 1.0)
        data["border_a"] = _rgba(self.border, 0.85 if self.translucent else 1.0)
        data["radius"] = "8px"
        data["radius_small"] = "6px"
        data["font_family"] = ('"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", '
                               '"微软雅黑", sans-serif')
        data["mono_family"] = '"Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace'
        return data


def _rgba(color: str, alpha: float) -> str:
    """把 #RRGGBB 转成 QSS 的 rgba(r, g, b, a)，a 取 0..1。"""
    c = QColor(color)
    a = max(0.0, min(1.0, alpha))
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {a:.2f})"


def dialog_background(palette: Optional[Palette] = None) -> str:
    """对话框底色的 QSS 值。

    半透明模式下仍保留接近不透明的表面色：Mica 只在边缘透出一点点，
    这样无论壁纸是纯黑还是浅色，对话框里的文字对比度都是稳定的。
    """
    pal = palette or current()
    return _rgba(pal.surface, 0.96 if pal.translucent else 1.0)


# --------------------------------------------------------------------------
# 系统背板（Mica / 亚克力）：半透明窗口的基础
# --------------------------------------------------------------------------

# DWMWA_SYSTEMBACKDROP_TYPE = 38（Win11 22H2+）
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMSBT_MICA = 2
_DWMSBT_ACRYLIC = 3


def _win_build() -> int:
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def backdrop_available() -> bool:
    """当前环境是否支持系统背板（可用 POTSYNC_OPAQUE=1 强制关闭）。"""
    if os.environ.get("POTSYNC_OPAQUE", "").strip() not in ("", "0", "false", "False"):
        return False
    if sys.platform != "win32":
        return False
    return _win_build() >= 22621      # Win11 22H2+ 才有 DWMWA_SYSTEMBACKDROP_TYPE


def enable_backdrop(widget, palette: Optional[Palette] = None) -> bool:
    """给窗口开 Mica 背板；失败返回 False（调用方据此回退不透明底色）。"""
    if widget is None or sys.platform != "win32":
        return False
    if not backdrop_available():
        return False
    palette = palette or current()
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return False
        handle = ctypes.windll.dwmapi
        ok = False
        # 默认用 Acrylic（模糊窗口后面的内容，半透明感最明显）；
        # POTSYNC_BACKDROP=mica 可切到跟随壁纸的 Mica。
        kind = os.environ.get("POTSYNC_BACKDROP", "mica").strip().lower()
        target = _DWMSBT_MICA if kind == "mica" else _DWMSBT_ACRYLIC
        try:
            value = ctypes.c_int(target)
            result = handle.DwmSetWindowAttribute(
                hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(value),
                ctypes.sizeof(value))
            ok = result == 0
        except Exception:
            ok = False
        if not ok:
            # 老一点儿的 Win11 只有“亚克力/云母”旧接口，失败就算了
            try:
                value = ctypes.c_int(1)   # DWMSBT_MAINWINDOW（旧名 Mica）
                result = handle.DwmSetWindowAttribute(
                    hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(value),
                    ctypes.sizeof(value))
                ok = result == 0
            except Exception:
                ok = False
        # 深浅色标题栏与背板一起设置（20 是新版，19 兼容旧版）
        try:
            dark = ctypes.c_int(1 if palette.mode == "dark" else 0)
            for attribute in (_DWMWA_USE_IMMERSIVE_DARK_MODE, 19):
                if handle.DwmSetWindowAttribute(
                        hwnd, attribute, ctypes.byref(dark),
                        ctypes.sizeof(dark)) == 0:
                    break
        except Exception:
            pass
        return ok
    except Exception as exc:
        log.debug("开启系统背板失败：%s", exc)
        return False


def translucent_enabled() -> bool:
    if os.environ.get("POTSYNC_OPAQUE", "").strip() not in ("", "0", "false", "False"):
        return False
    return backdrop_available()


def build_palette(mode: Optional[str] = None, accent: Optional[str] = None,
                  translucent: Optional[bool] = None) -> Palette:
    mode = mode or system_mode()
    accent = fit_accent(accent or system_accent(), mode)
    if translucent is None:
        translucent = backdrop_available()

    if mode == "light":
        bg, bg_end = "#f4f5f7", "#e9edf3"
        surface, surface_alt, surface_hover = "#ffffff", "#f6f6f8", "#eeeef2"
        border, border_strong = "#e2e2e6", "#cfcfd6"
        text, text_dim, text_muted = "#1b1b1f", "#5c5c66", "#8a8a94"
        selected, sidebar, log_bg = "#e8e8ee", "#eef0f4", "#fafbfc"
        danger, danger_soft = "#c42b1c", "#fde7e9"
        success, warn = "#0f7b0f", "#9d5d00"
        scroll, scroll_hover = "#cfcfd6", "#b4b4bd"
        on_accent = "#ffffff"
        accent_text = accent
    else:
        bg, bg_end = "#1f1f23", "#262a31"
        surface, surface_alt, surface_hover = "#2b2b31", "#323238", "#3a3a42"
        border, border_strong = "#3d3d45", "#4d4d57"
        text, text_dim, text_muted = "#f2f2f5", "#a8a8b3", "#7c7c88"
        selected, sidebar, log_bg = "#383840", "#242428", "#232327"
        danger, danger_soft = "#ff99a4", "#4a2327"
        success, warn = "#6ccb5f", "#fce100"
        scroll, scroll_hover = "#4a4a54", "#5c5c68"
        on_accent = "#15161a"
        accent_text = accent

    accent_soft = blend(accent, surface, 0.86 if mode == "light" else 0.80)
    accent_soft_strong = blend(accent, surface, 0.68 if mode == "light" else 0.60)

    return Palette(
        mode=mode, accent=accent, translucent=bool(translucent),
        bg=bg, bg_end=bg_end,
        surface=surface, surface_alt=surface_alt, surface_hover=surface_hover,
        border=border, border_strong=border_strong,
        text=text, text_dim=text_dim, text_muted=text_muted,
        accent_text=accent_text, on_accent=on_accent,
        accent_soft=accent_soft, accent_soft_strong=accent_soft_strong,
        selected=selected, sidebar=sidebar, log_bg=log_bg,
        danger=danger, danger_soft=danger_soft,
        success=success, warn=warn,
        scroll=scroll, scroll_hover=scroll_hover,
    )


# --------------------------------------------------------------------------
# 应用主题
# --------------------------------------------------------------------------

_CURRENT: Optional[Palette] = None


def current() -> Palette:
    """当前主题（未初始化时按系统现算一份）。"""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = build_palette()
    return _CURRENT


CACHE_DIR = Path.home() / ".potsync" / "cache"


def _check_asset(palette: Palette) -> str:
    """生成「勾选」小图（按主题配色缓存），返回 QSS 里可用的路径。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"check-{palette.mode}-{palette.on_accent.lstrip('#')}.png"
        if not path.exists():
            from PyQt5.QtCore import QPointF, Qt
            from PyQt5.QtGui import QPainter, QPen, QPixmap, QPolygonF
            pm = QPixmap(32, 32)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(QColor(palette.on_accent))
            pen.setWidthF(3.4)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(QPolygonF([QPointF(8, 17), QPointF(14, 23), QPointF(24, 10)]))
            p.end()
            pm.save(str(path), "PNG")
        return path.as_posix()
    except Exception as exc:  # 图标生成失败不影响主题
        log.warning("生成勾选图标失败：%s", exc)
        return ""


def _handle_asset(palette: Palette) -> str:
    """生成进度条手柄（圆点，按主题配色缓存），返回 QSS 里可用的路径。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tag = f"{palette.mode}-{palette.accent.lstrip('#')}-{palette.surface.lstrip('#')}"
        path = CACHE_DIR / f"handle-{tag}.png"
        if not path.exists():
            from PyQt5.QtCore import QRectF, Qt
            from PyQt5.QtGui import QPainter, QPen, QPixmap
            size, ratio = 16, 2.0
            pm = QPixmap(int(size * ratio), int(size * ratio))
            pm.setDevicePixelRatio(ratio)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(palette.accent), 2.0))
            p.setBrush(QColor(palette.surface))
            p.drawEllipse(QRectF(1.0, 1.0, size - 2.0, size - 2.0))
            p.end()
            pm.save(str(path), "PNG")
        return path.as_posix()
    except Exception as exc:  # 图标生成失败不影响主题
        log.warning("生成进度条手柄失败：%s", exc)
        return ""


def _qss(palette: Palette) -> str:
    tokens = palette.tokens()
    check = _check_asset(palette)
    if check:
        tokens["check_on"] = check
    else:
        tokens["check_on"] = ""
    handle = _handle_asset(palette)
    if handle:
        tokens["slider_handle"] = handle
    else:
        tokens["slider_handle"] = ""
    style = _style_file()
    if style is not None:
        template = Template(style.read_text(encoding="utf-8"))
        try:
            return template.substitute(tokens)
        except KeyError as exc:  # 模板里写了未定义的 token
            log.error("样式模板缺少变量 %s，退回内置样式", exc)
    else:
        log.warning("未找到样式模板 style.qss，使用内置简化样式")
    return _FALLBACK_QSS.substitute(tokens)


def _app_font() -> QFont:
    for family in ("Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "微软雅黑"):
        font = QFont(family)
        if font.exactMatch() or family in QFont().defaultFamily():
            break
    font.setPointSizeF(9.75)   # 13px @96dpi，Win11 正文档位
    font.setStyleStrategy(QFont.PreferAntialias)
    return font


def _apply_palette(app: QApplication, palette: Palette) -> None:
    qp = QPalette()
    qp.setColor(QPalette.Window, QColor(palette.bg))
    qp.setColor(QPalette.WindowText, QColor(palette.text))
    qp.setColor(QPalette.Base, QColor(palette.surface))
    qp.setColor(QPalette.AlternateBase, QColor(palette.surface_alt))
    qp.setColor(QPalette.Text, QColor(palette.text))
    qp.setColor(QPalette.PlaceholderText, QColor(palette.text_muted))
    qp.setColor(QPalette.Button, QColor(palette.surface_alt))
    qp.setColor(QPalette.ButtonText, QColor(palette.text))
    qp.setColor(QPalette.Highlight, QColor(palette.accent))
    qp.setColor(QPalette.HighlightedText, QColor(palette.on_accent))
    qp.setColor(QPalette.ToolTipBase, QColor(palette.surface))
    qp.setColor(QPalette.ToolTipText, QColor(palette.text))
    qp.setColor(QPalette.Link, QColor(palette.accent))
    qp.setColor(QPalette.Disabled, QPalette.Text, QColor(palette.text_muted))
    qp.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(palette.text_muted))
    app.setPalette(qp)


def apply(app: Optional[QApplication], mode: Optional[str] = None,
          accent: Optional[str] = None) -> Palette:
    global _CURRENT
    app = app or QApplication.instance()
    palette = build_palette(mode or system_mode(), accent)
    _CURRENT = palette
    if app is not None:
        app.setStyle("Fusion")
        app.setFont(_app_font())
        _apply_palette(app, palette)
        app.setStyleSheet(_qss(palette))
        for widget in app.topLevelWidgets():
            apply_titlebar(widget, palette.mode == "dark")
            if palette.translucent:
                enable_backdrop(widget, palette)
    return palette


def install(app: QApplication) -> Palette:
    """应用主题并开始跟随系统（返回首次配色）。"""
    palette = apply(app)
    log.info("主题：%s，强调色 %s", palette.mode, palette.accent)
    watcher = Watcher(app)
    watcher.start()
    app._potsync_theme_watcher = watcher  # 持有引用，避免被回收
    # 退出时在主线程停掉轮询，避免 Qt 在别的线程析构 QTimer 时报警告
    app.aboutToQuit.connect(watcher.stop)
    return palette


# --------------------------------------------------------------------------
# 标题栏 / 跟随系统
# --------------------------------------------------------------------------

def apply_titlebar(widget, dark: bool) -> None:
    """Windows 10/11 深色标题栏（失败静默，非 Windows 平台忽略）。"""
    if sys.platform != "win32" or widget is None:
        return
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):   # Win11 DWMWA_USE_IMMERSIVE_DARK_MODE
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
            if result == 0:
                break
    except Exception:
        pass


class Watcher(QObject):
    """轮询系统深浅色与强调色，变化时热替换样式。"""

    changed = pyqtSignal(object)   # Palette

    def __init__(self, app: QApplication, interval_ms: int = 1500) -> None:
        super().__init__(app)
        self._app = app
        self._mode = current().mode
        self._accent = current().accent
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _poll(self) -> None:
        mode = system_mode()
        accent = fit_accent(system_accent(), mode)
        if mode == self._mode and accent == self._accent:
            return
        self._mode, self._accent = mode, accent
        palette = apply(self._app, mode, accent)
        log.info("主题切换：%s（强调色 %s）", palette.mode, palette.accent)
        self.changed.emit(palette)


# 模板文件缺失时的内置样式（与 resources/style.qss 保持同步的最小子集）
_FALLBACK_QSS = Template("""
QWidget { background: $bg; color: $text; font-family: $font_family; }
QPushButton { background: $surface_alt; border: 1px solid $border; border-radius: $radius_small;
              padding: 6px 14px; color: $text; }
QPushButton:hover { background: $surface_hover; }
QPushButton#primaryBtn { background: $accent; color: $on_accent; border: none; }
QLineEdit, QPlainTextEdit, QListWidget { background: $surface; border: 1px solid $border;
                                         border-radius: $radius_small; padding: 6px 8px; }
""")
