"""Indexed sprite workbench. Resource reads and writes live in sprite_editor."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QSize, QRect, QPoint, Signal, QTimer, QSignalBlocker
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QColorDialog, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QRadioButton,
    QScrollArea, QSizePolicy, QSlider, QSplitter, QTabWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from toolbox_ui import button, label
from palette_assistant import brightness_order, sorted_act, SheetDialog


def indexed_image(width, height, pixels, palette, transparent_index=0, alpha=None, rgba=None):
    """Copy indexed bytes before the temporary Python buffer leaves scope."""
    if rgba is not None:
        return QImage(rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
    if isinstance(palette, (bytes, bytearray)):
        palette = [tuple(palette[i:i+3]) for i in range(0, 768, 3)]
    image = QImage(bytes(pixels), width, height, width, QImage.Format.Format_Indexed8).copy()
    image.setColorTable([QColor(*rgb, alpha[i] if alpha is not None else 0 if i == transparent_index else 255).rgba()
                         for i, rgb in enumerate(palette)])
    return image


class PixelCanvas(QWidget):
    strokeStarted = Signal()
    pixelRequested = Signal(int, int, int)
    strokeFinished = Signal()
    colorPicked = Signal(int)
    positionChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.pixels = bytearray()
        self.scale = 4
        self.grid = True
        self.highlight = False
        self.tool = 'brush'
        self.color_index = 1
        self.transparent_index = 0
        self._last = None
        self._drawing = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMinimumSize(160, 160)

    def set_sprite(self, width, height, pixels, palette, transparent_index=0, alpha=None, rgba=None):
        self.pixels = bytearray(pixels)
        self.transparent_index = transparent_index
        self.image = indexed_image(width, height, pixels, palette, transparent_index, alpha, rgba)
        self._resize_canvas()
        self.update()

    def set_zoom(self, scale):
        self.scale = max(1, int(scale))
        self._resize_canvas()
        self.update()

    def _resize_canvas(self):
        self.setFixedSize(max(160, self.image.width() * self.scale + 32),
                          max(160, self.image.height() * self.scale + 32))

    def image_rect(self):
        width, height = self.image.width() * self.scale, self.image.height() * self.scale
        return QRect((self.width() - width) // 2, (self.height() - height) // 2, width, height)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#edf0f4'))
        area = self.image_rect()
        painter.save()
        painter.setClipRect(area)
        tile = 10
        for y in range(area.top(), area.bottom() + 1, tile):
            for x in range(area.left(), area.right() + 1, tile):
                color = '#ffffff' if ((x-area.left())//tile + (y-area.top())//tile) % 2 else '#e2e6eb'
                painter.fillRect(x, y, tile, tile, QColor(color))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawImage(area, self.image)
        if self.highlight and not self.image.isNull():
            painter.setPen(QPen(QColor('#ff00aa'), 1))
            for position,value in enumerate(self.pixels):
                if value == self.color_index:
                    x,y=position%self.image.width(),position//self.image.width()
                    painter.drawRect(area.x()+x*self.scale,area.y()+y*self.scale,
                                     max(1,self.scale-1),max(1,self.scale-1))
        if self.grid and self.scale >= 6:
            painter.setPen(QPen(QColor(40, 55, 70, 58), 1))
            for x in range(area.left(), area.right() + 2, self.scale):
                painter.drawLine(x, area.top(), x, area.bottom() + 1)
            for y in range(area.top(), area.bottom() + 2, self.scale):
                painter.drawLine(area.left(), y, area.right() + 1, y)
        painter.restore()
        painter.setPen(QColor('#b4bec9'))
        painter.drawRect(area.adjusted(-1, -1, 0, 0))
        if self.image.isNull():
            painter.setPen(QColor('#656d76'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '选择一张图片')

    def _point(self, event):
        area = self.image_rect()
        point = event.position().toPoint()
        if not self.image.isNull() and area.contains(point):
            return ((point.x()-area.x())//self.scale, (point.y()-area.y())//self.scale)
        return None

    def _draw_to(self, point):
        if point is None:
            self._last = None
            return
        x1, y1 = self._last or point
        x2, y2 = point
        distance = max(abs(x2-x1), abs(y2-y1), 1)
        index = self.transparent_index if self.tool == 'eraser' else self.color_index
        for step in range(distance + 1):
            x = round(x1 + (x2-x1)*step/distance)
            y = round(y1 + (y2-y1)*step/distance)
            offset = y*self.image.width()+x
            if self.pixels[offset] != index:
                self.pixels[offset] = index
                self.image.setPixel(x, y, index)
                self.pixelRequested.emit(x, y, index)
        self._last = point
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.pixels:
            return
        point = self._point(event)
        if point is None:
            return
        if self.tool == 'picker' or event.modifiers() & Qt.KeyboardModifier.AltModifier:
            self.colorPicked.emit(self.pixels[point[1]*self.image.width()+point[0]])
            return
        self._drawing = True
        self._last = None
        self.strokeStarted.emit()
        self._draw_to(point)

    def mouseMoveEvent(self, event):
        point = self._point(event)
        if point is not None and self.pixels:
            index = self.pixels[point[1]*self.image.width()+point[0]]
            self.positionChanged.emit(f'X {point[0]}  Y {point[1]}  ·  色号 {index}')
        else:
            self.positionChanged.emit('')
        if self._drawing:
            self._draw_to(point)

    def mouseReleaseEvent(self, event):
        if self._drawing and event.button() == Qt.MouseButton.LeftButton:
            self._draw_to(self._point(event))
            self._drawing = False
            self._last = None
            self.strokeFinished.emit()


class PaletteGrid(QWidget):
    colorSelected = Signal(int)
    colorActivated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.palette = [(0, 0, 0)] * 256
        self.sort_by_brightness = False
        self.selected = 1
        self.used = set()
        self.setMinimumSize(208, 208)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(224)
        self.setMouseTracking(True)

    def cell_size(self):
        return min(self.width() // 16, self.height() // 16)

    def display_order(self):
        return brightness_order(self.palette) if self.sort_by_brightness else list(range(256))

    def paintEvent(self, event):
        painter = QPainter(self)
        cell = self.cell_size()
        order = self.display_order()
        for position, index in enumerate(order):
            rgb = self.palette[index]
            rect = QRect((position % 16)*cell, (position//16)*cell, cell, cell)
            painter.fillRect(rect, QColor(*rgb))
            painter.setPen(QColor(0, 0, 0, 45))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            if index in self.used:
                painter.setPen(QColor('#ffffff') if sum(rgb) < 384 else QColor('#24292f'))
                painter.drawPoint(rect.bottomRight()-QPoint(3, 3))
        position = order.index(self.selected)
        selected = QRect((position%16)*cell, (position//16)*cell, cell, cell)
        painter.setPen(QPen(QColor('#ffffff'), 3))
        painter.drawRect(selected.adjusted(1, 1, -2, -2))
        painter.setPen(QPen(QColor('#0969da'), 1))
        painter.drawRect(selected.adjusted(0, 0, -1, -1))

    def _index(self, event):
        cell = self.cell_size()
        point = event.position().toPoint()
        if cell and 0 <= point.x() < cell*16 and 0 <= point.y() < cell*16:
            return self.display_order()[point.y()//cell*16 + point.x()//cell]
        return None

    def mousePressEvent(self, event):
        index = self._index(event)
        if index is not None and event.button() == Qt.MouseButton.LeftButton:
            self.selected = index
            self.update()
            self.colorSelected.emit(index)

    def mouseDoubleClickEvent(self, event):
        index = self._index(event)
        if index is not None:
            self.colorActivated.emit(index)

    def mouseMoveEvent(self, event):
        index = self._index(event)
        if index is not None:
            rgb = self.palette[index]
            self.setToolTip(f'色号 {index} · RGB {rgb[0]}, {rgb[1]}, {rgb[2]}')


class SpriteEditorPage(QWidget):
    """Three-pane editor; all disk work is dispatched through the host runner."""

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.document = None
        self.baseline = None
        self.current_entry = None
        self._thumbnail_queue = []
        self._items = {}
        self._shared_items={}
        self._shared_signature=None
        self._shared_affected_ids=set()
        self._saved_colors = [None] * 16
        self._saved_color_slot = 0
        self._saved_colors_ready = False
        self._build()
        self.thumbnail_timer = QTimer(self)
        self.thumbnail_timer.setInterval(12)
        self.thumbnail_timer.timeout.connect(self._thumbnail_batch)
        self.host.busyChanged.connect(self._controls)
        self._controls(getattr(host, 'busy', False))

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        toolbar = QFrame()
        toolbar.setObjectName('Topbar')
        actions = QHBoxLayout(toolbar)
        actions.setContentsMargins(14, 9, 14, 9)
        actions.setSpacing(8)
        actions.addWidget(label('预览配色', 'muted'))
        self.mode_combo = QComboBox()
        self.mode_combo.setMinimumWidth(165)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        actions.addWidget(self.mode_combo)
        self.dirty_label = label('读取后可编辑', 'muted')
        actions.addWidget(self.dirty_label, 1)
        self.reload_button = button('重新读取')
        self.reload_button.clicked.connect(self.reload)
        actions.addWidget(self.reload_button)
        self.backups_button = button('恢复备份')
        self.backups_button.clicked.connect(self.show_backups)
        actions.addWidget(self.backups_button)
        self.save_button = button('预览并保存', True)
        self.save_button.clicked.connect(self.preview_save)
        actions.addWidget(self.save_button)
        outer.addWidget(toolbar)
        self.load_notice = label('', 'muted')
        self.load_notice.setWordWrap(True)
        self.load_notice.setStyleSheet('background:#ddf4ff;color:#0550ae;padding:7px 14px;border-bottom:1px solid #b6e3ff;')
        self.load_notice.hide()
        outer.addWidget(self.load_notice)

        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)
        browser = QWidget()
        browser.setMinimumWidth(155)
        left = QVBoxLayout(browser)
        left.setContentsMargins(12, 12, 10, 8)
        left.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText('图片组, 编号 · 如 19230,0')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        left.addWidget(self.search)
        self.group_filter = QComboBox()
        self.group_filter.addItem('全部图片组', None)
        self.group_filter.currentIndexChanged.connect(self._filter)
        left.addWidget(self.group_filter)
        self.duplicate_only = QCheckBox('只看重复编号')
        self.duplicate_only.toggled.connect(self._filter)
        left.addWidget(self.duplicate_only)
        self.sprite_list = QListWidget()
        self.sprite_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.sprite_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.sprite_list.setMovement(QListWidget.Movement.Static)
        self.sprite_list.setIconSize(QSize(76, 76))
        self.sprite_list.setGridSize(QSize(90, 112))
        self.sprite_list.setSpacing(1)
        self.sprite_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sprite_list.currentItemChanged.connect(self._select_item)
        left.addWidget(self.sprite_list, 1)
        self.count_label = label('尚未读取', 'muted')
        left.addWidget(self.count_label)
        splitter.addWidget(browser)

        center = QWidget()
        center.setMinimumWidth(200)
        middle = QVBoxLayout(center)
        middle.setContentsMargins(10, 12, 10, 8)
        middle.setSpacing(8)
        self.sprite_title = label('选择图片', 'section')
        middle.addWidget(self.sprite_title)
        self.sprite_details = label('缩略图按SFF原顺序排列，重复编号分别保留。', 'muted')
        self.sprite_details.setWordWrap(True)
        middle.addWidget(self.sprite_details)
        tools = QHBoxLayout()
        tools.setSpacing(4)
        self.tool_buttons = []
        tool_group = QButtonGroup(self)
        tool_group.setExclusive(True)
        for title, tool in (('画笔', 'brush'), ('橡皮', 'eraser'), ('吸色', 'picker')):
            control = QToolButton()
            control.setText(title)
            control.setCheckable(True)
            control.setToolTip('按住 Alt 也可临时吸色' if tool == 'picker' else title)
            control.clicked.connect(lambda checked=False, value=tool: self._set_tool(value))
            tool_group.addButton(control)
            self.tool_buttons.append(control)
            tools.addWidget(control)
        self.tool_buttons[0].setChecked(True)
        tools.addStretch()
        self.undo_button = QToolButton()
        self.undo_button.setText('撤销')
        self.undo_button.setToolTip('撤销 · Ctrl+Z')
        self.redo_button = QToolButton()
        self.redo_button.setText('重做')
        self.redo_button.setToolTip('重做 · Ctrl+Y')
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        tools.addWidget(self.undo_button)
        tools.addWidget(self.redo_button)
        middle.addLayout(tools)
        self.canvas = PixelCanvas()
        self.canvas.strokeStarted.connect(self._begin_stroke)
        self.canvas.pixelRequested.connect(self._paint_pixel)
        self.canvas.strokeFinished.connect(self._end_stroke)
        self.canvas.colorPicked.connect(self._select_color)
        self.canvas.positionChanged.connect(lambda text: self.position_label.setText(text))
        self.canvas_scroll = QScrollArea()
        self.canvas_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas_scroll.setWidget(self.canvas)
        self.canvas_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.canvas_scroll.setStyleSheet('QScrollArea { background: #edf0f4; border-radius: 6px; }')
        middle.addWidget(self.canvas_scroll, 1)
        zoom = QHBoxLayout()
        zoom.addWidget(label('缩放', 'muted'))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(1, 16)
        self.zoom_slider.setValue(4)
        self.zoom_slider.valueChanged.connect(self._zoom_changed)
        zoom.addWidget(self.zoom_slider, 1)
        self.zoom_label = label('400%', 'muted')
        self.zoom_label.setMinimumWidth(38)
        zoom.addWidget(self.zoom_label)
        self.grid_check = QCheckBox('像素网格')
        self.grid_check.setChecked(True)
        self.grid_check.setToolTip('放大到600%或以上时显示逐像素网格。')
        self.grid_check.toggled.connect(self._grid_changed)
        zoom.addWidget(self.grid_check)
        middle.addLayout(zoom)
        comparisons=QHBoxLayout()
        self.live_previews=[]
        self.preview_titles=[]
        self.preview_columns=[]
        for text in ('打开时原图','当前配色','其他配色'):
            container = QWidget()
            column = QVBoxLayout(container)
            column.setContentsMargins(0, 0, 0, 0)
            title = label(text,'muted')
            self.preview_titles.append(title)
            column.addWidget(title)
            picture=QLabel()
            picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
            picture.setFixedHeight(86)
            picture.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
            picture.setStyleSheet('background:#edf0f4;border:1px solid #d8dee4;border-radius:4px')
            column.addWidget(picture)
            comparisons.addWidget(container, 1)
            self.preview_columns.append(container)
            self.live_previews.append(picture)
        middle.addLayout(comparisons)
        self.highlight_check=QCheckBox('高亮当前色号在图中的位置')
        self.highlight_check.toggled.connect(self._highlight_changed)
        middle.addWidget(self.highlight_check)
        self.position_label = label('Alt 吸色，双击色块修改 RGB。', 'muted')
        self.position_label.setWordWrap(True)
        middle.addWidget(self.position_label)
        self.shared_title=label('共用这张颜色表的图片：选图后自动显示','section')
        self.shared_title.setWordWrap(True)
        middle.addWidget(self.shared_title)
        self.shared_note=label('色表编号按实际共享关系区分，颜色相同的独立色表不会合并。','muted')
        self.shared_note.setWordWrap(True)
        middle.addWidget(self.shared_note)
        self.shared_list=QListWidget()
        self.shared_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.shared_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.shared_list.setMovement(QListWidget.Movement.Static)
        self.shared_list.setIconSize(QSize(56,56))
        self.shared_list.setGridSize(QSize(88,86))
        self.shared_list.setFixedHeight(112)
        self.shared_list.itemClicked.connect(self._shared_clicked)
        middle.addWidget(self.shared_list)
        splitter.addWidget(center)

        panel = QWidget()
        panel.setMinimumWidth(234)
        right = QVBoxLayout(panel)
        right.setContentsMargins(12, 12, 12, 12)
        right.setSpacing(8)
        right.addWidget(label('颜色编号 0～255', 'section'))
        meaning=label('上方选择角色配色，这里选择图片的色号。','muted')
        meaning.setWordWrap(True)
        right.addWidget(meaning)
        self.palette_source = label('选择图片后显示颜色表来源', 'muted')
        self.palette_source.setWordWrap(True)
        right.addWidget(self.palette_source)
        self.palette_grid = PaletteGrid()
        self.palette_grid.colorSelected.connect(self._select_color)
        self.palette_grid.colorActivated.connect(self.edit_color)
        right.addWidget(self.palette_grid)
        self.brightness = QCheckBox('按亮度排布色块')
        self.brightness.setToolTip('仅改变色块显示顺序，保留像素中的实际色号。')
        self.brightness.toggled.connect(self.change_palette_order)
        right.addWidget(self.brightness)
        helpers = QHBoxLayout()
        self.sort_export = button('导出排序 ACT')
        self.sort_export.clicked.connect(self.export_sorted_act)
        helpers.addWidget(self.sort_export)
        self.sheet_button = button('拼图取色')
        self.sheet_button.clicked.connect(lambda: SheetDialog(self).exec())
        helpers.addWidget(self.sheet_button)
        right.addLayout(helpers)
        selection = QHBoxLayout()
        self.color_swatch = QLabel()
        self.color_swatch.setFixedSize(30, 30)
        selection.addWidget(self.color_swatch)
        self.color_label = label('色号 1 · RGB 0, 0, 0')
        self.color_label.setWordWrap(True)
        selection.addWidget(self.color_label, 1)
        self.color_button = button('改色')
        self.color_button.clicked.connect(lambda: self.edit_color(self.palette_grid.selected))
        selection.addWidget(self.color_button)
        right.addLayout(selection)
        saved_header = QHBoxLayout()
        saved_header.addWidget(label('常用颜色 · 16格', 'section'), 1)
        self.clear_saved_color_button = QToolButton()
        self.clear_saved_color_button.setText('清空此格')
        self.clear_saved_color_button.clicked.connect(self.clear_saved_color)
        saved_header.addWidget(self.clear_saved_color_button)
        right.addLayout(saved_header)
        saved_grid = QGridLayout()
        saved_grid.setSpacing(4)
        self.saved_color_buttons = []
        for slot in range(16):
            control = QToolButton()
            control.setFixedSize(22, 24)
            control.clicked.connect(lambda checked=False, value=slot: self.select_saved_color(value))
            self.saved_color_buttons.append(control)
            saved_grid.addWidget(control, slot // 8, slot % 8)
        right.addLayout(saved_grid)
        self.saved_color_label = label('格1 · 空', 'muted')
        self.saved_color_label.setWordWrap(True)
        right.addWidget(self.saved_color_label)
        saved_actions = QHBoxLayout()
        self.store_saved_color_button = QToolButton()
        self.store_saved_color_button.setText('存入当前色')
        self.store_saved_color_button.setToolTip('把右侧当前色号的RGB存进选定格；不修改角色或草稿。')
        self.store_saved_color_button.clicked.connect(self.store_saved_color)
        saved_actions.addWidget(self.store_saved_color_button)
        self.apply_saved_color_button = QToolButton()
        self.apply_saved_color_button.setText('应用到色号 1')
        self.apply_saved_color_button.clicked.connect(self.apply_saved_color)
        saved_actions.addWidget(self.apply_saved_color_button)
        saved_actions.addStretch()
        right.addLayout(saved_actions)
        saved_note = label('点格只选色；“应用”才按下方范围改草稿。常用色会单独保存，跨图片复用。', 'muted')
        saved_note.setWordWrap(True)
        right.addWidget(saved_note)
        self._refresh_saved_colors()
        right.addSpacing(4)
        right.addWidget(label('这次改色的范围', 'section'))
        self.global_scope = QRadioButton('该配色的全部关联图片')
        self.group_scope = QRadioButton('当前组：原本共用色表的图片')
        self.local_scope = QRadioButton('当前图（包含链接图片）')
        self.global_scope.setChecked(True)
        for scope_button in (self.global_scope, self.group_scope, self.local_scope):
            scope_button.toggled.connect(self._scope_changed)
        right.addWidget(self.global_scope)
        right.addWidget(self.group_scope)
        right.addWidget(self.local_scope)
        self.impact_label = label('尚无影响范围', 'muted')
        self.impact_label.setWordWrap(True)
        right.addWidget(self.impact_label)
        self.impact_button = button('查看受影响图片')
        self.impact_button.clicked.connect(self.show_impact)
        right.addWidget(self.impact_button)
        self.protection_label = label('9000组头像使用独立颜色表。普通人物改色不会合并或覆盖头像颜色。', 'muted')
        self.protection_label.setWordWrap(True)
        right.addWidget(self.protection_label)
        right.addStretch()
        save_note = label('保存会自动备份原文件；外部文件变更时停止保存。', 'muted')
        save_note.setWordWrap(True)
        right.addWidget(save_note)
        inspector_scroll = QScrollArea()
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setFrameShape(QFrame.Shape.NoFrame)
        inspector_scroll.setMinimumWidth(246)
        inspector_scroll.setWidget(panel)
        splitter.addWidget(inspector_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([220, 520, 262])
        for sequence, action in ((QKeySequence.StandardKey.Undo, self.undo),
                                 (QKeySequence.StandardKey.Redo, self.redo),
                                 (QKeySequence.StandardKey.Save, self.preview_save)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)

    @property
    def mode(self):
        return self.mode_combo.currentData()

    @property
    def modes(self):
        return self.document.modes if self.document is not None else ()

    @property
    def scope(self):
        if self.local_scope.isChecked():
            return 'sprite'
        return 'group' if self.group_scope.isChecked() else 'palette'

    def has_unsaved_changes(self):
        return self.document is not None and self.document.dirty

    def confirm_leave(self):
        if not self.has_unsaved_changes():
            return True
        answer = QMessageBox.warning(self, '还有未保存的精灵编辑',
            '仍有未保存的修改。\n选择“保存”会先打开预览，完成后可再次离开。',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            self.preview_save()
            return False
        if answer == QMessageBox.StandardButton.Discard:
            self.document = self.baseline = None
            self.current_entry = None
            self.thumbnail_timer.stop()
            return True
        return False

    def refresh(self):
        if self.has_unsaved_changes() or self.host.busy:
            return
        self._load()

    def reload(self):
        if self.host.busy:
            return
        if self.has_unsaved_changes():
            answer = QMessageBox.question(self, '重新读取并放弃草稿',
                '将放弃当前未保存的像素和颜色修改，重新读取磁盘上的角色文件。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._load()

    def _load(self, preferred=None):
        from mugen_palette import open_palette
        preferred = self.current_entry if preferred is None else preferred
        self.thumbnail_timer.stop()
        def work():
            document = open_palette(self.host.repo_path, self.host.definition)
            try:
                from sprite_color_presets import load_colors
                colors, colors_error = load_colors(self.host.repo_path), None
            except Exception as error:
                colors, colors_error = None, str(error)
            return document, None, colors, colors_error
        def done(result):
            prior = self.mode
            self.document, self.baseline, colors, colors_error = result
            with QSignalBlocker(self.mode_combo):
                self.mode_combo.clear()
                for mode in self.modes:
                    self.mode_combo.addItem(mode, mode)
                self.mode_combo.setCurrentIndex(self.modes.index(prior) if prior in self.modes else 0)
            if colors_error is None:
                self._saved_colors = list(colors)
                self._saved_colors_ready = True
            else:
                self._saved_colors_ready = False
            self._refresh_saved_colors()
            self._shared_signature=None
            self._shared_items={}
            self.shared_list.clear()
            self.current_entry = None
            self.sprite_list.blockSignals(True)
            self.sprite_list.clear()
            self._items.clear()
            groups = set()
            for entry in self.document.entries:
                groups.add(entry['group'])
                suffix = f"\n第{entry['id']+1}条" if entry['duplicate'] else ''
                item = QListWidgetItem(f"{entry['group']}, {entry['index']}{suffix}")
                item.setData(Qt.ItemDataRole.UserRole, entry['id'])
                item.setToolTip(f"图片 {entry['group']}, {entry['index']} · SFF第{entry['id']+1}条\n"
                                f"{entry['width']} × {entry['height']} 像素" +
                                ('\n重复编号：本条可独立选择和编辑' if entry['duplicate'] else ''))
                self.sprite_list.addItem(item)
                self._items[entry['id']] = item
            self.sprite_list.blockSignals(False)
            self.group_filter.blockSignals(True)
            previous_group = self.group_filter.currentData()
            self.group_filter.clear()
            self.group_filter.addItem('全部图片组', None)
            for group in sorted(groups):
                self.group_filter.addItem(str(group), group)
            if previous_group in groups:
                self.group_filter.setCurrentIndex(self.group_filter.findData(previous_group))
            self.group_filter.blockSignals(False)
            self._filter()
            choice = self._items.get(preferred)
            if choice is None or choice.isHidden():
                choice = next((self.sprite_list.item(i) for i in range(self.sprite_list.count())
                               if not self.sprite_list.item(i).isHidden()), None)
            self.sprite_list.setCurrentItem(choice)
            self._queue_thumbnails()
            self._controls(False)
            notice = getattr(self.document, 'external_state_warning', '')
            self.load_notice.setText(notice)
            self.load_notice.setVisible(bool(notice))
            self.host.message(f'已读取全部 {len(self.document.entries)} 条精灵 · ' +
                              (notice if notice else '未修改角色文件'))
            if colors_error is not None:
                QMessageBox.warning(self, '常用颜色未载入',
                    '角色图片已正常读取。常用颜色文件读取失败，原格子没有被覆盖；暂不写入常用颜色。\n\n' + colors_error)
        self.host.run('读取精灵与颜色表', work, done)

    def _controls(self, busy):
        loaded = self.document is not None
        selected = loaded and self.current_entry is not None
        entry = self._entry(self.current_entry) if selected else {}
        indexed = selected and entry.get('indexed', True)
        can_paint = indexed and entry.get('can_paint', True)
        can_isolate = indexed and entry.get('can_isolate', True)
        available = not busy
        self.reload_button.setEnabled(available)
        self.backups_button.setEnabled(available and loaded)
        self.mode_combo.setEnabled(available and loaded)
        self.save_button.setEnabled(available and self.has_unsaved_changes())
        self.undo_button.setEnabled(available and loaded and self.document.undo_available)
        self.redo_button.setEnabled(available and loaded and self.document.redo_available)
        self.canvas.setEnabled(available and can_paint)
        self.brightness.setEnabled(available and indexed)
        self.sort_export.setEnabled(available and indexed)
        self.sheet_button.setEnabled(available)
        for widget in (self.palette_grid, self.color_button, self.global_scope, self.impact_button):
            widget.setEnabled(available and indexed)
        for widget in (self.group_scope, self.local_scope):
            widget.setEnabled(available and can_isolate)
        for control in self.tool_buttons:
            control.setEnabled(available and can_paint)
        self.sprite_list.setEnabled(available and loaded)
        self._saved_color_controls()
        self.dirty_label.setText('待同步Evil播放' if loaded and getattr(self.document,'pending_rebuild',False) else '有未保存编辑' if self.has_unsaved_changes() else
                                 '已读取 · 尚未修改' if loaded else '尚未读取')

    def change_palette_order(self, enabled):
        self.palette_grid.sort_by_brightness = enabled
        self.palette_grid.update()

    def export_sorted_act(self):
        if not self.sort_export.isEnabled():
            return
        choice = QMessageBox.question(self, '导出重新排序的 ACT',
            '导出的新 ACT 按亮度重排色号，0 号保留。现有精灵若要继续使用原颜色，需要同步重映射像素色号。继续导出？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if choice != QMessageBox.StandardButton.Yes:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, '导出排序 ACT', str(self.host.repo_path / '按亮度排序.act'), 'ACT (*.act)')
        if not filename:
            return
        path = Path(filename)
        if not path.suffix:
            path = path.with_suffix('.act')
        try:
            data = sorted_act(self.palette_grid.palette)
            with path.open('xb') as handle:
                handle.write(data)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, '无法导出排序 ACT', str(error))

    def _entry(self, entry_id):
        return next(entry for entry in self.document.entries if entry['id'] == entry_id)

    def _filter(self, *args):
        if self.document is None:
            return
        query = self.search.text().strip().replace('，', ',').replace(' ', '')
        group = self.group_filter.currentData()
        visible = 0
        for entry in self.document.entries:
            title = f"{entry['group']},{entry['index']}"
            matched = (not query or query in title or query == f"#{entry['id']+1}")
            shown = matched and (group is None or entry['group'] == group)
            shown = shown and (not self.duplicate_only.isChecked() or entry['duplicate'])
            self._items[entry['id']].setHidden(not shown)
            visible += int(shown)
        generated=getattr(self.document,'generated_count',0)
        self.count_label.setText(f'{visible} / {len(self.document.entries)} 条原图'+
            (f'\n{generated} 张由旧版配色功能生成的副本已合并显示' if generated else ''))

    def _select_item(self, item, previous=None):
        if item is None or self.document is None:
            self.current_entry = None
            self._controls(self.host.busy)
            return
        self.current_entry = item.data(Qt.ItemDataRole.UserRole)
        self.sprite_list.scrollToItem(item,QAbstractItemView.ScrollHint.PositionAtCenter)
        self._refresh_sprite()

    def _image(self, document, entry_id, mode):
        sprite = self.document.original_sprite(entry_id, mode) if document is None else document.sprite(entry_id, mode)
        return indexed_image(sprite['width'], sprite['height'], sprite['pixels'],
                             sprite['palette'], sprite.get('transparent_index', 0),
                             sprite.get('alpha'), sprite.get('rgba'))

    def _refresh_sprite(self):
        if self.document is None or self.current_entry is None:
            return
        entry = self._entry(self.current_entry)
        if not entry.get('can_isolate', True) and self.scope != 'palette':
            self.global_scope.setChecked(True)
        sprite = self.document.sprite(self.current_entry, self.mode)
        self.canvas.set_sprite(sprite['width'], sprite['height'], sprite['pixels'],
                               sprite['palette'], sprite.get('transparent_index', 0),
                               sprite.get('alpha'), sprite.get('rgba'))
        alternative = next((mode for mode in self.modes if mode != self.mode), None)
        self.preview_titles[1].setText(self.mode + ' 当前')
        self.preview_titles[2].setText(alternative + ' 当前' if alternative else '')
        self.preview_columns[2].setVisible(alternative is not None)
        for preview,document,mode in zip(self.live_previews,
                (self.baseline,self.document,self.document),(self.mode,self.mode,alternative)):
            if mode is None:
                continue
            image=self._image(document,self.current_entry,mode)
            preview.setPixmap(QPixmap.fromImage(image.scaled(
                max(60,preview.width()-8),78,Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)))
        palette = sprite['palette']
        self.palette_grid.palette = [tuple(palette[i:i+3]) for i in range(0, 768, 3)]
        self.palette_grid.used = set(sprite['pixels'])
        self.palette_grid.update()
        self.sprite_title.setText(f"图片 {entry['group']}, {entry['index']}" +
                                 (f" · 重复编号第{entry['id']+1}条" if entry['duplicate'] else ''))
        category = {'body': '人物', 'effect': '独立色表', 'portrait': '独立头像' if entry.get('protected') else '头像',
                    'truecolor': '真彩色'}.get(entry['category'], '精灵')
        behavior = ' · 图案共用，链接图片一起变化' if entry.get('can_paint', True) else ''
        self.sprite_details.setText(f"{sprite['width']} × {sprite['height']} 像素 · {category}{behavior}")
        self._select_color(self.palette_grid.selected)
        self._scope_changed()
        self._controls(self.host.busy)

    def _mode_changed(self, *args):
        if self.document is None:
            return
        self.mode_combo.setToolTip(self.mode or '')
        self._refresh_sprite()
        self._queue_thumbnails()

    def _scope_changed(self, *args):
        if self.document is None or self.current_entry is None:
            return
        affected = self.document.affected(self.current_entry, self.mode, self.scope)
        used = self.document.color_usage(self.current_entry, self.mode, self.palette_grid.selected, self.scope)
        pixel_affected = self.document.affected(self.current_entry, self.mode, 'pixels')
        self.palette_source.setText(self.document.source_summary(self.current_entry, self.mode, self.scope))
        self._refresh_shared(affected,used)
        self.impact_label.setText(f'改色范围：{len(affected)} 张图片\n其中 {len(used)} 张实际用到当前色号 '
                                  f'{self.palette_grid.selected}。\n共用原图：{len(pixel_affected)} 条。')
        entry = self._entry(self.current_entry)
        if self.scope == 'sprite' and entry['category'] == 'body':
            detail = '单图改色会脱离人物换色，成为固定颜色。'
        elif entry.get('protected'):
            detail = '头像使用独立颜色表。'
        else:
            detail = '改色范围以实际关联列表为准。重复编号按条目显示。'
        self.protection_label.setText(detail)

    def _select_color(self, index):
        self.palette_grid.selected = int(index)
        self.palette_grid.update()
        self.canvas.color_index = int(index)
        self.canvas.update()
        rgb = self.palette_grid.palette[index]
        transparent = index == self.canvas.transparent_index
        self.color_label.setText(f'色号 {index}' + (' · 透明' if transparent else '') +
                                f'\nRGB {rgb[0]}, {rgb[1]}, {rgb[2]}')
        self.color_swatch.setStyleSheet(f'background: rgb({rgb[0]}, {rgb[1]}, {rgb[2]}); '
                                       'border: 1px solid #8c959f; border-radius: 4px;')
        self._scope_changed()
        self._saved_color_controls()

    def _refresh_saved_colors(self):
        for slot, control in enumerate(self.saved_color_buttons):
            rgb = self._saved_colors[slot]
            selected = slot == self._saved_color_slot
            border = '2px solid #0969da' if selected else '1px solid #afb8c1'
            background = '#f6f8fa' if rgb is None else f'rgb({rgb[0]},{rgb[1]},{rgb[2]})'
            foreground = '#656d76' if rgb is None else ('#ffffff' if sum(rgb) < 384 else '#24292f')
            control.setText(str(slot + 1))
            control.setStyleSheet(f'background:{background};color:{foreground};border:{border};border-radius:3px;')
            control.setToolTip(f'格{slot + 1} · ' + ('空' if rgb is None else f'RGB {rgb[0]}, {rgb[1]}, {rgb[2]}') +
                               '\n点击只选中，不会修改色表。')
        rgb = self._saved_colors[self._saved_color_slot]
        self.saved_color_label.setText(f'格{self._saved_color_slot + 1} · ' +
            ('空；可存入右侧当前颜色' if rgb is None else f'拟用 RGB {rgb[0]}, {rgb[1]}, {rgb[2]}'))
        self._saved_color_controls()

    def _saved_color_controls(self):
        available = self._saved_colors_ready and not self.host.busy
        selected = (self.document is not None and self.current_entry is not None and
                    self._entry(self.current_entry).get('indexed', True))
        rgb = self._saved_colors[self._saved_color_slot]
        for control in self.saved_color_buttons:
            control.setEnabled(available)
        self.store_saved_color_button.setEnabled(available and selected)
        self.clear_saved_color_button.setEnabled(available and rgb is not None)
        index = self.palette_grid.selected
        self.apply_saved_color_button.setText(f'应用到色号 {index}')
        self.apply_saved_color_button.setEnabled(available and selected and rgb is not None and
                                                index != self.canvas.transparent_index)
        self.apply_saved_color_button.setToolTip(
            '0号透明色受保护，不能应用RGB。' if index == self.canvas.transparent_index else
            f'把选定格的RGB应用到当前 {self.mode} 色号 {index}，沿用下方改色范围；进入草稿后仍需预览保存。')

    def select_saved_color(self, slot):
        if self.host.busy or not self._saved_colors_ready:
            return
        self._saved_color_slot = slot
        self._refresh_saved_colors()

    def _save_color_slot(self, rgb):
        slot = self._saved_color_slot
        def work():
            from sprite_color_presets import save_color
            return save_color(self.host.repo_path, slot, rgb)
        def done(colors):
            self._saved_colors = list(colors)
            self._refresh_saved_colors()
            self.host.message('常用颜色已单独保存；角色图片与编辑草稿未改变')
        self.host.run('保存常用颜色', work, done, write=True)

    def store_saved_color(self):
        if (self.host.busy or not self._saved_colors_ready or
                self.document is None or self.current_entry is None):
            return
        rgb = tuple(self.palette_grid.palette[self.palette_grid.selected])
        previous = self._saved_colors[self._saved_color_slot]
        if previous == rgb:
            self.host.message('选定格已经保存这个颜色；角色草稿未改变')
            return
        if previous is not None:
            answer = QMessageBox.question(self, '替换常用颜色',
                f'格{self._saved_color_slot + 1}：RGB {previous[0]}, {previous[1]}, {previous[2]}\n'
                f'替换为 RGB {rgb[0]}, {rgb[1]}, {rgb[2]}？\n\n只替换常用颜色格，不修改角色。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._save_color_slot(rgb)

    def clear_saved_color(self):
        if self.host.busy or not self._saved_colors_ready:
            return
        rgb = self._saved_colors[self._saved_color_slot]
        if rgb is None:
            return
        answer = QMessageBox.question(self, '清空常用颜色',
            f'清空格{self._saved_color_slot + 1}的 RGB {rgb[0]}, {rgb[1]}, {rgb[2]}？\n\n不会改变已应用到角色的颜色。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self._save_color_slot(None)

    def apply_saved_color(self):
        if self.host.busy or not self._saved_colors_ready:
            return
        rgb = self._saved_colors[self._saved_color_slot]
        if rgb is not None:
            self._apply_color_rgb(self.palette_grid.selected, rgb)

    def _shared_clicked(self,item):
        entry_id=item.data(Qt.ItemDataRole.UserRole)
        if entry_id in self._items:
            self.sprite_list.setCurrentItem(self._items[entry_id])

    def _refresh_shared(self,affected,used):
        if hasattr(self.document,'palette_family'):
            family=self.document.palette_family(self.current_entry,self.mode)
            members=family['entries']
            number=family['number']
        else:
            members=self.document.affected(self.current_entry,self.mode,'palette')
            number=self._entry(self.current_entry)['palette_source']+1
        affected_ids={e['id'] for e in affected}
        changed_scope=affected_ids!=self._shared_affected_ids
        self._shared_affected_ids=affected_ids
        used_ids={e['id'] for e in self.document.color_usage(
            self.current_entry,self.mode,self.palette_grid.selected,'palette')}
        self.shared_title.setText(f'共用色表 #{number} · {len(members)} 张图片（自动关联）')
        self.shared_note.setText(f'蓝色：本次改色范围；'
                                f'★：使用当前色号 {self.palette_grid.selected}。点击缩略图可切换。')
        signature=(self.mode,number,tuple(e['id'] for e in members))
        if signature!=self._shared_signature:
            self._shared_signature=signature
            self.shared_list.clear();self._shared_items={}
            for entry in members:
                item=QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole,entry['id'])
                item.setIcon(self._items[entry['id']].icon())
                self.shared_list.addItem(item)
                self._shared_items[entry['id']]=item
            self._queue_thumbnails([e['id'] for e in members])
        for entry in members:
            item=self._shared_items[entry['id']]
            star=' ★' if entry['id'] in used_ids else ''
            item.setText(f"{entry['group']},{entry['index']}{star}")
            item.setToolTip(f"图片 {entry['group']},{entry['index']} · 第{entry['id']+1}条\n共用色表 #{number}"+
                            ('\n本次会受改色影响' if entry['id'] in affected_ids else '\n本次局部改色不影响此图'))
            item.setBackground(QColor('#ddedff' if entry['id'] in affected_ids else '#f4f6f8'))
            if changed_scope:
                item.setIcon(self._shared_icon(entry['id'],self._items[entry['id']].icon()))
            item.setSelected(entry['id']==self.current_entry)

    def _shared_icon(self,entry_id,icon):
        pixmap=icon.pixmap(56,56)
        if not pixmap.isNull() and entry_id in self._shared_affected_ids:
            painter=QPainter(pixmap)
            painter.setPen(QPen(QColor('#0969da'),2))
            painter.drawRect(1,1,pixmap.width()-3,pixmap.height()-3)
            painter.end()
        return QIcon(pixmap)

    def _set_tool(self, value):
        self.canvas.tool = value

    def _zoom_changed(self, value):
        self.canvas.set_zoom(value)
        self.zoom_label.setText(f'{value*100}%')

    def _grid_changed(self, value):
        self.canvas.grid = value
        self.canvas.update()

    def _highlight_changed(self,value):
        self.canvas.highlight=value
        self.canvas.update()

    def _begin_stroke(self):
        if self.document is not None:
            self.document.begin_stroke()

    def _paint_pixel(self, x, y, index):
        if self.document is not None and self.current_entry is not None:
            self.document.paint(self.current_entry, x, y, index)

    def _end_stroke(self):
        if self.document is not None:
            self.document.end_stroke()
            self._refresh_sprite()
            affected = self.document.affected(self.current_entry, self.mode, 'pixels')
            self._queue_thumbnails([entry['id'] for entry in affected])

    def edit_color(self, index):
        if self.document is None or self.current_entry is None or self.host.busy:
            return
        self._select_color(index)
        if index == self.canvas.transparent_index:
            QMessageBox.information(self, '透明色保持不变', '0号保留为透明色。要擦除像素，请使用橡皮。')
            return
        rgb = self.palette_grid.palette[index]
        color = QColorDialog.getColor(QColor(*rgb), self,
            f'修改 {self.mode} 色号 {index} · ' +
            {'sprite': '当前图及链接', 'group': '当前图片组', 'palette': '当前颜色表'}[self.scope])
        if not color.isValid():
            return
        self._apply_color_rgb(index, (color.red(), color.green(), color.blue()))

    def _apply_color_rgb(self, index, rgb):
        if self.document is None or self.current_entry is None or self.host.busy:
            return False
        if index == self.canvas.transparent_index:
            QMessageBox.information(self, '透明色保持不变', '0号保留为透明色。要擦除像素，请使用橡皮。')
            return False
        try:
            affected = [entry['id'] for entry in self.document.affected(self.current_entry, self.mode, self.scope)]
            self.document.set_color(self.current_entry, self.mode, index, tuple(rgb), self.scope)
        except Exception as error:
            QMessageBox.warning(self, '无法修改这个颜色', str(error))
            return False
        self._refresh_sprite()
        self._queue_thumbnails(affected)
        return True

    def undo(self):
        if self.document is not None and not self.host.busy and self.document.undo_available:
            self.document.undo()
            self._refresh_sprite()
            self._queue_thumbnails()

    def redo(self):
        if self.document is not None and not self.host.busy and self.document.redo_available:
            self.document.redo()
            self._refresh_sprite()
            self._queue_thumbnails()

    def _queue_thumbnails(self, ids=None):
        if self.document is None:
            return
        self._thumbnail_queue = list(self._items) if ids is None else list(dict.fromkeys(self._thumbnail_queue + list(ids)))
        self.thumbnail_timer.start()

    def _thumbnail_batch(self):
        if self.document is None or self.host.busy:
            return
        for _ in range(min(10, len(self._thumbnail_queue))):
            entry_id = self._thumbnail_queue.pop(0)
            if entry_id not in self._items:
                continue
            image = self._image(self.document, entry_id, self.mode)
            pixmap = QPixmap(76, 76)
            pixmap.fill(QColor('#edf0f4'))
            painter = QPainter(pixmap)
            scaled = image.scaled(72, 72, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.FastTransformation)
            painter.drawImage((76-scaled.width())//2, (76-scaled.height())//2, scaled)
            painter.end()
            self._items[entry_id].setIcon(QIcon(pixmap))
            if entry_id in self._shared_items:
                self._shared_items[entry_id].setIcon(self._shared_icon(entry_id,QIcon(pixmap)))
        if not self._thumbnail_queue:
            self.thumbnail_timer.stop()
            if self.sprite_list.currentItem() is not None:
                self.sprite_list.scrollToItem(self.sprite_list.currentItem(),QAbstractItemView.ScrollHint.PositionAtCenter)

    def show_impact(self):
        if self.document is None or self.current_entry is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('当前改色范围与实际用色')
        dialog.resize(620, 540)
        layout = QVBoxLayout(dialog)
        source = label(self.document.source_summary(self.current_entry, self.mode, self.scope), 'section')
        source.setWordWrap(True)
        layout.addWidget(source)
        used = {entry['id'] for entry in self.document.color_usage(
            self.current_entry, self.mode, self.palette_grid.selected, self.scope)}
        affected = self.document.affected(self.current_entry, self.mode, self.scope)
        layout.addWidget(label(f'共 {len(affected)} 张关联图片；{len(used)} 张实际使用色号 '
                               f'{self.palette_grid.selected}。', 'muted'))
        tabs = QTabWidget()
        listing = QListWidget()
        listing.setViewMode(QListWidget.ViewMode.IconMode)
        listing.setResizeMode(QListWidget.ResizeMode.Adjust)
        listing.setMovement(QListWidget.Movement.Static)
        listing.setIconSize(QSize(76,76))
        listing.setGridSize(QSize(140,120))
        for entry in affected:
            suffix = ' · 使用当前色号' if entry['id'] in used else ''
            item=QListWidgetItem(f"{entry['group']}, {entry['index']}\n第{entry['id']+1}条{suffix}")
            image=self._image(self.document,entry['id'],self.mode)
            item.setIcon(QIcon(QPixmap.fromImage(image.scaled(76,76,
                Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.FastTransformation))))
            listing.addItem(item)
        tabs.addTab(listing, f'改色影响 · {len(affected)} 张')
        pixels = self.document.affected(self.current_entry, self.mode, 'pixels')
        pixel_list = QListWidget()
        for entry in pixels:
            pixel_list.addItem(f"{entry['group']}, {entry['index']} · SFF第{entry['id']+1}条 · 原图共用")
        tabs.addTab(pixel_list, f'画笔影响 · {len(pixels)} 张')
        layout.addWidget(tabs, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def preview_save(self):
        if not self.has_unsaved_changes() or self.host.busy:
            return
        document, baseline = self.document, self.baseline
        def work():
            plan = document.plan_save()
            changed = []
            for entry in document.entries:
                entry_id = entry['id']
                for mode in document.modes:
                    old = document.original_sprite(entry_id, mode) if baseline is None else baseline.sprite(entry_id, mode)
                    new = document.sprite(entry_id, mode)
                    if old['pixels'] != new['pixels'] or old['palette'] != new['palette']:
                        changed.append(entry_id)
                        break
            return plan, changed
        def done(result):
            plan, changed = result
            if self._save_dialog(plan, changed):
                self.save_plan(plan)
        self.host.run('生成保存前后预览', work, done)

    def _save_dialog(self, plan, changed):
        dialog = QDialog(self)
        dialog.setWindowTitle('确认保存精灵与颜色表')
        dialog.resize(800, 630)
        layout = QVBoxLayout(dialog)
        heading = label(plan['summary'], 'section')
        heading.setWordWrap(True)
        layout.addWidget(heading)
        note = label('将写入：' + '、'.join(plan['files']) +
                     '\n保存前会备份相关源文件。', 'muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        if plan.get('warnings'):
            warning = label('\n'.join(plan['warnings']))
            warning.setWordWrap(True)
            warning.setStyleSheet('color: #9a6700; background: #fff8c5; padding: 8px; border-radius: 5px;')
            layout.addWidget(warning)
        selectors = QHBoxLayout()
        selector = QComboBox()
        preview_ids=changed or ([self.current_entry] if self.current_entry is not None else [self.document.entries[0]['id']])
        for entry_id in preview_ids:
            entry = self._entry(entry_id)
            selector.addItem(f"{entry['group']}, {entry['index']} · 第{entry_id+1}条", entry_id)
        if self.current_entry in preview_ids:
            selector.setCurrentIndex(preview_ids.index(self.current_entry))
        selectors.addWidget(label(f'前后预览 · {len(changed)} 张变化' if changed else '仅同步播放 · 图片与RGB保持原样', 'muted'))
        selectors.addWidget(selector, 1)
        mode = QComboBox()
        mode.addItems(self.modes)
        mode.setCurrentText(self.mode)
        selectors.addWidget(mode)
        layout.addLayout(selectors)
        pictures = QHBoxLayout()
        previews = []
        for title in ('磁盘原图', '保存后的效果'):
            column = QVBoxLayout()
            column.addWidget(label(title, 'muted'))
            preview = QLabel()
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setMinimumSize(240, 220)
            preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
            preview.setStyleSheet('background: #edf0f4; border: 1px solid #d8dee4; border-radius: 5px;')
            column.addWidget(preview, 1)
            pictures.addLayout(column, 1)
            previews.append(preview)
        layout.addLayout(pictures, 1)
        def show_images(*args):
            entry_id = selector.currentData()
            if entry_id is None:
                return
            for preview, document in zip(previews, (self.baseline, self.document)):
                image = self._image(document, entry_id, mode.currentText())
                image = image.scaled(340, 340, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.FastTransformation)
                preview.setPixmap(QPixmap.fromImage(image))
        selector.currentIndexChanged.connect(show_images)
        mode.currentIndexChanged.connect(show_images)
        show_images()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('备份并保存')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def save_plan(self, plan):
        if self.document is None or self.host.busy:
            return
        document = self.document
        def work():
            with self.host.operation_lock():
                return document.save(plan)
        def done(result):
            self.host.message(result.get('summary', '精灵与颜色表已保存'))
            QMessageBox.information(self, '保存完成', result.get('summary', '保存完成') +
                '\n\n自动备份：\n' + str(result['backup']))
            self._load()
        self.host.run('备份并保存精灵与颜色表', work, done, write=True)

    def show_backups(self):
        if self.document is None or self.host.busy:
            return
        if self.has_unsaved_changes():
            QMessageBox.information(self, '请先处理未保存编辑',
                '恢复备份前，请先保存当前草稿，或重新读取并放弃草稿。')
            return
        self.host.run('读取精灵备份', self.document.list_backups, self._backup_dialog)

    def _backup_dialog(self, backups):
        if not backups:
            QMessageBox.information(self, '尚无精灵备份', '第一次保存后会自动生成备份，可从这里恢复。')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('恢复精灵与颜色表备份')
        dialog.resize(640, 440)
        layout = QVBoxLayout(dialog)
        introduction = label('选择之前保存的备份。恢复前会保留当前文件；发现外部改动时停止操作。', 'muted')
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        listing = QListWidget()
        for backup in backups:
            item = QListWidgetItem(backup['label'])
            item.setData(Qt.ItemDataRole.UserRole, backup)
            item.setToolTip(str(backup['path']))
            listing.addItem(item)
        layout.addWidget(listing, 1)
        details = label('', 'muted')
        details.setWordWrap(True)
        layout.addWidget(details)
        def selected(item, previous=None):
            backup = item.data(Qt.ItemDataRole.UserRole)
            files = backup.get('files', [])
            details.setText('将恢复：' + ('、'.join(files) if files else '该备份保存的精灵与ACT文件') +
                            '\n备份位置：' + str(backup['path']))
        listing.currentItemChanged.connect(selected)
        listing.setCurrentRow(0)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.RestoreDefaults | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText('恢复所选备份')
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        backup = listing.currentItem().data(Qt.ItemDataRole.UserRole)
        answer = QMessageBox.question(self, '确认恢复所选备份', details.text() +
            '\n\n将先备份当前文件，再恢复所选文件。继续？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.restore_backup(backup['path'])

    def restore_backup(self, path):
        if self.document is None or self.host.busy or self.has_unsaved_changes():
            return
        document = self.document
        def work():
            with self.host.operation_lock():
                return document.restore_backup(Path(path))
        def done(result):
            self.host.message(result.get('summary', '已恢复精灵备份'))
            self._load()
        self.host.run('备份当前文件并恢复所选精灵备份', work, done, write=True)
