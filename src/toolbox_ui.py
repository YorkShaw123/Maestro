"""Qt-native typography and restrained workbench styling."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap, QIcon, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QPushButton

BG = '#f7f8fa'
TEXT = '#24292f'
MUTED = '#656d76'
ACCENT = '#0969da'
BORDER = '#d8dee4'


def apply_theme(app):
    app.setStyle('Fusion')
    font = QFont('Microsoft YaHei UI', 10)
    app.setFont(font)
    app.setStyleSheet("""
        QMainWindow, QWidget#Workspace { background: #ffffff; color: #24292f; }
        QWidget { color: #24292f; }
        QFrame#Sidebar { background: #f6f8fa; border-right: 1px solid #d8dee4; }
        QFrame#Topbar { background: #ffffff; border-bottom: 1px solid #d8dee4; }
        QLabel[role="muted"] { color: #656d76; }
        QLabel[role="badge"] { color: #0969da; background: #ddf4ff; border-radius: 5px; padding: 4px 9px; }
        QPushButton { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 5px; padding: 6px 12px; min-height: 20px; }
        QPushButton:hover { background: #eef1f4; border-color: #afb8c1; }
        QPushButton:pressed { background: #e8edf2; }
        QPushButton:disabled { color: #8c959f; background: #f6f8fa; border-color: #e6e9ed; }
        QPushButton[primary="true"] { background: #0969da; border-color: #0969da; color: white; }
        QPushButton[primary="true"]:hover { background: #075dbf; }
        QPushButton[primary="true"]:disabled { background: #a8c9ef; border-color: #a8c9ef; color: #fff; }
        QPushButton[nav="true"] { text-align: left; border: 0; background: transparent; padding: 8px 12px; border-radius: 5px; }
        QPushButton[nav="true"]:hover { background: #eaeef2; }
        QPushButton[nav="true"]:checked { background: #dceafb; color: #0550ae; }
        QPushButton[nav="true"]:disabled { color: #8c959f; }
        QLineEdit, QPlainTextEdit, QTextBrowser { background: #ffffff; border: 1px solid #d0d7de; border-radius: 5px; padding: 7px; selection-background-color: #cce2ff; selection-color: #24292f; }
        QLineEdit:focus, QPlainTextEdit:focus { border-color: #0969da; }
        QPlainTextEdit#Diff { border: 0; border-radius: 0; padding: 12px; }
        QTreeWidget, QListWidget { background: #ffffff; alternate-background-color: #f8fafc; border: 0; outline: 0; }
        QTreeWidget::item { padding: 6px 8px; border-bottom: 1px solid #f0f2f4; }
        QListWidget::item { border-bottom: 1px solid #edf0f3; }
        QListWidget::item:selected { background: #dceafb; color: #0550ae; }
        QListWidget::item:hover:!selected { background: #f3f6fa; }
        QTreeWidget::item:selected { background: #dceafb; color: #0550ae; }
        QTreeWidget::item:hover:!selected { background: #f3f6fa; }
        QHeaderView::section { background: #f6f8fa; color: #656d76; border: 0; border-bottom: 1px solid #d8dee4; padding: 7px; }
        QTabWidget::pane { border: 0; border-top: 1px solid #d8dee4; }
        QTabBar::tab { background: transparent; color: #656d76; padding: 10px 15px; border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { color: #0969da; border-bottom-color: #0969da; }
        QSplitter::handle { background: #d8dee4; width: 1px; height: 1px; }
        QStatusBar { background: #f6f8fa; border-top: 1px solid #d8dee4; color: #656d76; }
        QStatusBar::item { border: 0; }
        QScrollBar:vertical { background: transparent; width: 11px; margin: 0; }
        QScrollBar::handle:vertical { background: #c4cbd3; min-height: 30px; border-radius: 5px; margin: 2px; }
        QScrollBar:horizontal { background: transparent; height: 11px; margin: 0; }
        QScrollBar::handle:horizontal { background: #c4cbd3; min-width: 30px; border-radius: 5px; margin: 2px; }
        QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
        QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
        QToolTip { color: #24292f; background: #ffffff; border: 1px solid #d8dee4; padding: 5px; }
        QGroupBox { border: 1px solid #d8dee4; border-radius: 5px; margin-top: 10px; padding-top: 10px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
    """)


def label(text, role='body'):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setProperty('role', role)
    if role in ('title', 'section'):
        font = widget.font()
        font.setPointSize(14 if role == 'title' else 10)
        font.setWeight(QFont.Weight.DemiBold)
        widget.setFont(font)
    return widget


def button(text, primary=False):
    widget = QPushButton(text)
    widget.setProperty('primary', primary)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


def line_icon(kind, color='#57606a'):
    """Original simple line icons, rendered separately at common native DPRs."""
    result = QIcon()
    for ratio in (1, 1.25, 1.5, 1.75, 2, 3):
        pixmap = QPixmap(round(20 * ratio), round(20 * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(color))
        if kind == 'git':
            painter.drawLine(6, 5, 6, 15)
            painter.drawLine(6, 10, 14, 5)
            for x, y in ((6, 4), (6, 16), (15, 4)):
                painter.drawEllipse(x - 2, y - 2, 4, 4)
        elif kind == 'package':
            painter.drawRect(3, 5, 14, 12)
            painter.drawLine(3, 8, 17, 8)
            painter.drawLine(10, 5, 10, 11)
        elif kind == 'help':
            painter.drawEllipse(2, 2, 16, 16)
            painter.drawText(2, 1, 16, 17, Qt.AlignmentFlag.AlignCenter, '?')
        elif kind == 'sprite-creation':
            painter.drawRect(3, 3, 14, 14)
            painter.drawLine(5, 15, 14, 6)
            painter.drawLine(13, 5, 15, 7)
            painter.drawLine(5, 15, 8, 14)
        elif kind == 'simple-ai':
            painter.drawRoundedRect(3, 6, 14, 11, 3, 3)
            painter.drawLine(10, 6, 10, 3)
            painter.drawEllipse(9, 1, 2, 2)
            painter.drawLine(1, 10, 3, 10)
            painter.drawLine(17, 10, 19, 10)
            painter.drawEllipse(6, 9, 2, 2)
            painter.drawEllipse(12, 9, 2, 2)
            painter.drawLine(8, 14, 12, 14)
        elif kind == 'dockyard':
            painter.drawLine(2, 16, 18, 16)
            painter.drawLine(4, 16, 4, 7)
            painter.drawLine(16, 16, 16, 7)
            painter.drawLine(2, 8, 10, 3)
            painter.drawLine(10, 3, 18, 8)
            painter.drawLine(7, 12, 13, 12)
            painter.drawLine(7, 14, 13, 14)
        elif kind == 'sprites':
            painter.drawRect(2, 3, 16, 14)
            painter.drawLine(2, 12, 7, 7)
            painter.drawLine(7, 7, 12, 12)
            painter.drawLine(12, 12, 15, 9)
            painter.drawLine(15, 9, 18, 12)
            painter.drawEllipse(12, 5, 2, 2)
        else:
            painter.drawRect(4, 3, 12, 14)
        painter.end()
        result.addPixmap(pixmap)
    return result


class DiffHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        spec = None
        if text.startswith(('diff --git ', '+++ ', '--- ')):
            spec = ('#0969da', None)
        elif text.startswith('+'):
            spec = ('#116329', '#e6ffec')
        elif text.startswith('-'):
            spec = ('#a40e26', '#ffebe9')
        elif text.startswith('@@'):
            spec = ('#0550ae', '#ddf4ff')
        if spec:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(spec[0]))
            if spec[1]:
                fmt.setBackground(QColor(spec[1]))
            self.setFormat(0, len(text), fmt)


class DiffView(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('Diff')
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont('Consolas', 10)
        font.setFamilies(['Consolas', 'Microsoft YaHei UI'])
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self.highlighter = DiffHighlighter(self.document())
