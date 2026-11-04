"""Neon cyberpunk look, shared with ProtonMD / Ghostwire (same palette, Qt flavour)."""
from __future__ import annotations

INK, PANEL, TERMINAL = "#0D0F14", "#12151C", "#0A0C10"
CYAN, MAGENTA, MINT, AMBER = "#00C8D6", "#FF2BD6", "#2BFFB0", "#FFC145"
TEXT, MUTED = "#E6EBF2", "#A9B4C2"
MONO = '"JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace'


def _a(hex_: str, alpha: float) -> str:
    h = hex_.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


QSS = f"""
* {{ color: {TEXT}; }}
QMainWindow, QWidget {{ background-color: {INK}; }}
QLabel {{ background: transparent; }}
QLabel#neon-title {{
  font-family: {MONO}; font-weight: 800; font-size: 20px; letter-spacing: 6px; color: #F2F4F8;
}}
QLabel#neon-sub {{ color: {CYAN}; font-family: {MONO}; letter-spacing: 2px; }}
QLabel#section {{ color: {CYAN}; font-family: {MONO}; font-weight: 800; letter-spacing: 2px; }}

QMenuBar {{ background: {INK}; border-bottom: 1px solid {_a(CYAN, .55)}; }}
QMenuBar::item {{ padding: 5px 12px; background: transparent; }}
QMenuBar::item:selected {{ background: {_a(CYAN, .18)}; color: {CYAN}; }}
QMenu {{ background: {PANEL}; border: 1px solid {_a(CYAN, .6)}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {_a(CYAN, .20)}; color: #FFFFFF; }}
QMenu::separator {{ height: 1px; background: {_a(CYAN, .25)}; margin: 4px 8px; }}

QListWidget, QPlainTextEdit {{
  background: {PANEL}; border: 1px solid {_a(CYAN, .30)}; border-radius: 10px; padding: 4px;
  selection-background-color: {_a(CYAN, .22)}; selection-color: #FFFFFF;
}}
QPlainTextEdit {{ background: {TERMINAL}; font-family: {MONO}; }}
QListWidget::item {{ border: 1px solid transparent; border-radius: 6px; padding: 6px 8px; margin: 1px 2px; }}
QListWidget::item:hover {{ background: {_a(CYAN, .08)}; border-color: {_a(CYAN, .25)}; }}
QListWidget::item:selected {{
  background: {_a(CYAN, .16)}; border: 1px solid {_a(CYAN, .70)}; border-left: 3px solid {CYAN}; color: #FFFFFF;
}}

QSplitter::handle {{ background: {_a(CYAN, .20)}; }}
QSplitter::handle:horizontal {{ width: 2px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {_a(CYAN, .45)}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {_a(CYAN, .75)}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {_a(CYAN, .45)}; border-radius: 5px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; }}

QPushButton {{
  background: {_a(CYAN, .14)}; color: {CYAN}; border: 1px solid {CYAN}; border-radius: 6px; padding: 6px 14px;
}}
QPushButton:hover {{ background: {_a(CYAN, .30)}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {CYAN}; padding: 4px; }}
QStatusBar, QLabel#status {{ color: {MUTED}; }}
"""


def apply(app) -> None:
    """Dark neon theme. Fusion style + palette so native dialogs (file picker, message boxes) match."""
    from PySide6.QtGui import QColor, QPalette
    app.setStyle("Fusion")
    pal = QPalette()
    roles = {QPalette.Window: INK, QPalette.WindowText: TEXT, QPalette.Base: PANEL, QPalette.AlternateBase: INK,
             QPalette.Text: TEXT, QPalette.Button: PANEL, QPalette.ButtonText: TEXT, QPalette.ToolTipBase: PANEL,
             QPalette.ToolTipText: TEXT, QPalette.Highlight: CYAN, QPalette.HighlightedText: INK,
             QPalette.Link: CYAN, QPalette.BrightText: MAGENTA, QPalette.Mid: "#2a3140"}
    for role, col in roles.items():
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(QSS)
