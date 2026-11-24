"""Qt workbench for composing indexed MUGEN sprites."""
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QShortcut, QTransform
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog, QGridLayout,
    QHBoxLayout, QInputDialog, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QStyle, QStyleOptionButton, QToolButton, QVBoxLayout, QWidget,
)

from sprite_creation import MAX_SIZE, SpriteSources, adapt_colors, indexed_preview
from toolbox_ui import button, label


def blank(width, height):
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)
    return image


def action(name, callback, primary=False):
    widget = button(name, primary)
    widget.clicked.connect(callback)
    return widget


def row(layout, *widgets):
    line = QHBoxLayout()
    for widget in widgets:
        line.addWidget(widget)
    line.addStretch()
    layout.addLayout(line)


def scroll(surface):
    area = QScrollArea()
    area.setAlignment(Qt.AlignmentFlag.AlignCenter)
    area.setStyleSheet('QScrollArea { background: #edf1f5; border: 1px solid #d8dee4; }')
    area.setWidget(surface)
    return area


def zoom_picker(surface):
    zoom = QComboBox()
    for percent in (50, 100, 200, 400, 800, 1200, 1600):
        zoom.addItem(f'{percent}%', percent)
    zoom.setCurrentIndex(1)
    zoom.currentIndexChanged.connect(lambda _: surface.set_zoom(zoom.currentData()))
    return zoom


class PixelSurface(QWidget):
    pressed = Signal(QPoint, object)
    moved = Signal(QPoint)
    released = Signal(QPoint)

    def __init__(self, image):
        super().__init__()
        self.image = image
        self.axis = None
        self.ghost = None
        self.selection = QRect()
        self.fragment = None
        self.percent = 100
        self.down = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.set_zoom(100)

    def set_zoom(self, percent):
        self.percent = percent
        self.sync()

    def sync(self):
        self.setFixedSize(max(1, round(self.image.width()*self.percent/100)),
                          max(1, round(self.image.height()*self.percent/100)))
        self.update()

    def paintEvent(self, event):
        with QPainter(self) as painter:
            color = (QColor('#f5f7fa'), QColor('#d8e0e8'))
            rect = event.rect()
            for y in range(rect.top()//8, rect.bottom()//8+1):
                for x in range(rect.left()//8, rect.right()//8+1):
                    painter.fillRect(x*8, y*8, 8, 8, color[(x+y)%2])
            scale = self.percent/100
            painter.scale(scale, scale)
            if self.ghost:
                painter.setOpacity(0.35)
                painter.drawImage(*self.ghost)
                painter.setOpacity(1)
            painter.drawImage(0, 0, self.image)
            if self.fragment:
                pos, image = self.fragment
                painter.drawImage(pos, image)
            if scale >= 8:
                painter.setPen(QColor(50, 65, 80, 40))
                for x in range(self.image.width()):
                    painter.drawLine(x, 0, x, self.image.height())
                for y in range(self.image.height()):
                    painter.drawLine(0, y, self.image.width(), y)
            if self.axis is not None:
                painter.setPen(QColor('#ed4581'))
                x, y = self.axis.x(), self.axis.y()
                painter.drawLine(x-5, y, x+5, y)
                painter.drawLine(x, y-5, x, y+5)
            if self.fragment or not self.selection.isEmpty():
                bounds = QRect(pos, image.size()) if self.fragment else self.selection
                for color, style in ((Qt.GlobalColor.white, Qt.PenStyle.SolidLine),
                                     (Qt.GlobalColor.black, Qt.PenStyle.DashLine)):
                    painter.setPen(color)
                    pen = painter.pen()
                    pen.setStyle(style)
                    pen.setCosmetic(True)
                    painter.setPen(pen)
                    painter.drawRect(bounds)

    def pixel(self, event):
        point = event.position()
        return QPoint(int(point.x()*100//self.percent), int(point.y()*100//self.percent))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self.down = True
            self.pressed.emit(self.pixel(event), event.modifiers())

    def mouseMoveEvent(self, event):
        if self.down:
            self.moved.emit(self.pixel(event))

    def mouseReleaseEvent(self, event):
        if self.down and event.button() == Qt.MouseButton.LeftButton:
            self.down = False
            self.released.emit(self.pixel(event))


def select(surface, start, end):
    surface.selection = QRect(start, end).normalized().intersected(surface.image.rect())
    surface.update()


class ReferenceDialog(QDialog):
    def __init__(self, page, entry):
        super().__init__(page)
        self.page, self.entry = page, entry
        self.image = page.sources.image(entry, page.mode.currentText())
        self.setWindowTitle('%d,%d · 参考精灵' % page.sources.key(entry))
        self.resize(580, 475)
        self.start = None
        self.surface = PixelSurface(self.image)
        self.surface.axis = QPoint(*page.sources.axis(entry))
        self.surface.pressed.connect(self.press)
        self.surface.moved.connect(self.pick)
        self.surface.released.connect(self.pick)
        layout = QVBoxLayout(self)
        layout.addWidget(label('拖出矩形裁剪范围。直接粘贴整张时可跳过选区。', 'muted'))
        row(layout, label('缩放'), zoom_picker(self.surface),
            action('粘到画布', self.paste, True), action('作为底图', self.base),
            action('叠影对齐', self.ghost))
        if page.sources.entries[entry]['indexed']:
            row(layout, action('使用这张的色表', self.use_palette))
        layout.addWidget(scroll(self.surface), 1)
        QShortcut(QKeySequence.StandardKey.Copy, self, activated=self.paste)

    def press(self, point, _modifiers):
        self.start = point
        self.pick(point)

    def pick(self, point):
        if self.start is not None:
            select(self.surface, self.start, point)

    def paste(self):
        rect = self.surface.selection
        part = self.image if rect.isEmpty() else self.image.copy(rect)
        if self.page.begin_paste(part):
            self.accept()

    def base(self):
        if self.page.use_base(self.entry, self.image):
            self.accept()

    def ghost(self):
        self.page.ghost = self.image, QPoint(*self.page.sources.axis(self.entry))
        self.page.sync()
        self.accept()

    def use_palette(self):
        self.page.choose_palette(self.entry)
        self.accept()


@dataclass
class Workspace:
    image: QImage
    axis: QPoint


class ReferenceTile(QPushButton):
    def __init__(self):
        super().__init__()
        self.image = QImage()
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        if self.image.isNull():
            super().paintEvent(event)
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ''
        with QPainter(self) as painter:
            self.style().drawControl(QStyle.ControlElement.CE_PushButton, option, painter, self)
            text_width = self.fontMetrics().horizontalAdvance(self.text())
            scale = min(3, (self.width() - text_width - 32) / self.image.width(),
                        (self.height() - 10) / self.image.height())
            width = max(1, round(self.image.width()*scale))
            height = max(1, round(self.image.height()*scale))
            gap = 12
            left = (self.width() - width - gap - text_width)//2
            painter.drawImage(QRect(left, (self.height()-height)//2, width, height), self.image)
            painter.setPen(self.palette().buttonText().color())
            painter.drawText(QRect(left+width+gap, 0, text_width, self.height()),
                             Qt.AlignmentFlag.AlignVCenter, self.text())


class SpriteCreationPage(QWidget):
    page_size = 4

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.sources = None
        self.base = Workspace(blank(128, 128), QPoint(64, 113))
        self.past, self.future = [], []
        self.saved = Workspace(self.base.image.copy(), self.base.axis)
        self.fragment = self.ghost = None
        self.reference = None
        self.color = QColor('#24292f')
        self.page_number = 0
        self.drag = self.start = self.last = None
        self._build()
        host.busyChanged.connect(self.busy_changed)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        work = QHBoxLayout()
        layout.addLayout(work, 1)
        editor = QVBoxLayout()
        work.addLayout(editor, 1)
        tools = QHBoxLayout()
        editor.addLayout(tools)
        self.tool = QComboBox()
        self.tool.addItems(('画笔', '橡皮', '吸管', '矩形选区', '轴点'))
        self.tool.setToolTip('按住 Alt 临时吸取画布颜色')
        self.ink = action('颜色', self.pick_color)
        self.ink.setToolTip('画布可用 Alt 吸色。')
        self.swatches = action('色号', self.pick_swatch)
        self.swatches.setToolTip('从目标色表直接选择画笔颜色')
        self.pen = self.spin(1, 16, 1, '笔宽（像素）')
        self.surface = PixelSurface(self.base.image)
        for widget in (self.tool, self.ink, self.swatches, self.pen, zoom_picker(self.surface)):
            tools.addWidget(widget)
        tools.addStretch()
        self.overlay = QCheckBox('轴点')
        self.overlay.setChecked(True)
        self.overlay.toggled.connect(self.sync)
        tools.addWidget(self.overlay)
        self.surface.pressed.connect(self.press)
        self.surface.moved.connect(self.move)
        self.surface.released.connect(self.release)
        self.undo_button = action('撤销', self.undo)
        self.redo_button = action('重做', self.redo)
        self.ghost_button = action('关闭叠影', self.clear_ghost)
        self.clear_button = action('清空', self.clear_canvas)
        row(editor, self.undo_button, self.redo_button, self.clear_button, action('画布尺寸', self.resize_canvas),
            action('裁掉透明边', self.trim), self.ghost_button)
        editor.addWidget(scroll(self.surface), 1)
        self.paste_bar = QWidget()
        paste = QHBoxLayout(self.paste_bar)
        paste.setContentsMargins(0, 0, 0, 0)
        for text, callback in (
            ('左转', lambda: self.rotate(-90)), ('右转', lambda: self.rotate(90)),
            ('任意角', self.angle), ('水平翻转', self.flip),
            ('放置', self.settle), ('取消', self.cancel_paste),
        ):
            paste.addWidget(action(text, callback, text == '放置'))
        editor.addWidget(self.paste_bar)
        self.hint = label('选区可复制。粘贴后拖动定位，Enter 放置，Esc 取消。', 'muted')
        self.hint.setWordWrap(True)
        editor.addWidget(self.hint)
        self._reference_panel(work)
        self._output_bar(layout)
        for key, handler in (
            ('Ctrl+C', self.copy), ('Ctrl+X', lambda: self.copy(cut=True)), ('Ctrl+V', self.paste_clipboard),
            ('Ctrl+Z', self.undo), ('Ctrl+Shift+Z', self.redo),
            ('Return', self.settle), ('Escape', self.cancel_paste),
            ('Left', lambda: self.nudge(-1, 0)), ('Right', lambda: self.nudge(1, 0)),
            ('Up', lambda: self.nudge(0, -1)), ('Down', lambda: self.nudge(0, 1)),
        ):
            QShortcut(QKeySequence(key), self.surface, context=Qt.ShortcutContext.WidgetShortcut, activated=handler)
        self.sync()

    def _reference_panel(self, work):
        panel = QWidget()
        panel.setMinimumWidth(215)
        panel.setMaximumWidth(250)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        refs = QVBoxLayout(panel)
        refs.setContentsMargins(8, 0, 0, 0)
        refs.setSpacing(6)
        work.addWidget(panel)
        refs.addWidget(label('参考精灵', 'section'))
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索 Group 或 Group,Index')
        self.search.textChanged.connect(lambda _: self.refresh_results(0))
        refs.addWidget(self.search)
        self.reference_groups = QComboBox()
        self.reference_groups.setAccessibleName('参考精灵 Group')
        self.reference_groups.setMaxVisibleItems(12)
        self.reference_groups.addItem('全部 Group', None)
        self.reference_groups.activated.connect(self.select_reference_group)
        refs.addWidget(self.reference_groups)
        self.tiles = []
        for _ in range(self.page_size):
            tile = ReferenceTile()
            tile.clicked.connect(lambda checked=False, tile=tile: self.open_reference(tile.property('entry')))
            refs.addWidget(tile, 1)
            self.tiles.append(tile)
        self.results = label('', 'muted')
        refs.addWidget(self.results)
        self.previous = action('上一页', lambda: self.refresh_results(self.page_number-1))
        self.following = action('下一页', lambda: self.refresh_results(self.page_number+1))
        row(refs, self.previous, self.following)

    def _output_bar(self, layout):
        self.reference_label = label('尚未载入色表', 'muted')
        self.mode = QComboBox()
        self.mode.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.mode.setMinimumContentsLength(21)
        self.mode.currentIndexChanged.connect(self.change_mode)
        row(layout, self.reference_label, self.mode, action('刷新参考', lambda: self.refresh(force=True)))
        self.group = self.spin(0, 65535, 0, 'Group')
        self.index = self.spin(0, 65535, 0, 'Index')
        self.x = self.spin(-32768, 32767, 64, '轴点 X')
        self.y = self.spin(-32768, 32767, 113, '轴点 Y')
        for widget in (self.group, self.index):
            widget.valueChanged.connect(self.controls)
        for widget in (self.x, self.y):
            widget.editingFinished.connect(self.read_axis)
        self.preview_button = action('预览色表 / 插入', self.preview, True)
        line = QHBoxLayout()
        layout.addLayout(line)
        for text, widget in (('Group', self.group), ('Index', self.index), ('轴 X', self.x), ('轴 Y', self.y)):
            line.addWidget(label(text))
            line.addWidget(widget)
        line.addStretch()
        line.addWidget(self.preview_button)

    @staticmethod
    def spin(low, high, value, tip):
        field = QSpinBox()
        field.setRange(low, high)
        field.setValue(value)
        field.setToolTip(tip)
        field.setKeyboardTracking(False)
        return field

    def refresh(self, force=False):
        if self.sources is not None and not force:
            return
        self.host.run('读取参考精灵', lambda: SpriteSources(self.host.repo_path, self.host.definition), self.loaded)

    def loaded(self, sources, suggest_slot=False):
        current = self.mode.currentText()
        new_session = self.sources is None
        reference = sources.default
        if self.sources is not None and self.reference is not None:
            key = self.sources.key(self.reference)
            rank = sum(e['indexed'] and self.sources.key(e['id']) == key
                       for e in self.sources.entries[:self.reference])
            matches = [e['id'] for e in sources.entries if e['indexed'] and sources.key(e['id']) == key]
            if rank < len(matches):
                reference = matches[rank]
        self.sources = sources
        self.reference_groups.clear()
        self.reference_groups.addItem('全部 Group', None)
        for group in sorted({entry['group'] for entry in sources.entries}):
            self.reference_groups.addItem(f'Group {group}', group)
        self.mode.blockSignals(True)
        self.mode.clear()
        self.mode.addItems(sources.modes)
        self.mode.setCurrentIndex(max(0, self.mode.findText(current)))
        self.mode.blockSignals(False)
        self.choose_palette(reference)
        if new_session or suggest_slot:
            self.next_slot()
        self.change_mode()
        self.host.message(f'已载入 {len(sources.entries)} 张参考精灵 · SFF v{sources.version}')

    def change_mode(self, *_):
        self.mode.setToolTip(self.mode.currentText())
        self.refresh_results(self.page_number)

    def next_slot(self):
        if self.sources is None:
            return
        group, index = self.key()
        for value in range(index, 65536):
            if (group, value) not in self.sources.keys:
                self.index.setValue(value)
                break
        self.controls()

    def select_reference_group(self, index):
        group = self.reference_groups.itemData(index)
        query = '' if group is None else str(group)
        if query == self.search.text():
            self.refresh_results(0)
        else:
            self.search.setText(query)

    def refresh_results(self, page_number):
        matches = []
        group = None
        if self.sources is not None:
            query = self.search.text().strip().replace('，', ',').replace(' ', ',')
            parts = [p for p in query.split(',') if p]
            if not parts:
                matches = self.sources.entries
            elif len(parts) in (1, 2) and all(p.isdecimal() and len(p) <= 5 for p in parts):
                group = int(parts[0])
                number = int(parts[1]) if len(parts) == 2 else None
                matches = [e for e in self.sources.entries
                           if e['group'] == group and (number is None or number == e['index'])]
        self.reference_groups.setCurrentIndex(max(0, self.reference_groups.findData(group)))
        end = max(0, (len(matches)-1)//self.page_size)
        self.page_number = max(0, min(page_number, end))
        start = self.page_number*self.page_size
        visible = matches[start:start+self.page_size]
        for i, tile in enumerate(self.tiles):
            tile.setProperty('entry', None)
            tile.setEnabled(i < len(visible))
            if i >= len(visible):
                tile.setText('' if matches or i else '没有匹配的精灵')
                tile.image = QImage()
                tile.setToolTip('')
            else:
                entry = visible[i]
                tile.setProperty('entry', entry['id'])
                tile.setText(f"{entry['group']},{entry['index']}")
                tile.image = self.sources.image(entry['id'], self.mode.currentText())
                tile.setToolTip('点击以 100% 打开并裁剪')
            tile.update()
        self.results.setText(f'{len(matches)} 张 · 第 {self.page_number+1}/{end+1} 页' if matches else '搜索例：200 或 200,0')
        self.previous.setEnabled(self.page_number > 0)
        self.following.setEnabled(self.page_number < end)

    def open_reference(self, entry):
        if entry is not None and self.sources is not None:
            ReferenceDialog(self, entry).exec()
            self.surface.setFocus()

    def choose_palette(self, entry):
        self.reference = entry
        self.reference_label.setText('无索引色表可用' if entry is None else
                                     '色表取自 %d,%d' % self.sources.key(entry))
        self.controls()

    def key(self):
        return self.group.value(), self.index.value()

    def controls(self, *_):
        ready = self.sources is not None and self.reference is not None and not self.host.busy
        self.swatches.setEnabled(ready)
        self.preview_button.setEnabled(ready and self.fragment is None)
        occupied = self.sources is not None and self.key() in self.sources.keys
        self.preview_button.setToolTip('此编号已占用，可预览或导出 PNG。' if occupied else
                                       '预览索引色，再确认插入空编号。')

    def busy_changed(self, busy):
        self.setEnabled(not busy)
        self.controls()

    def sync(self, fields=True):
        self.surface.image = self.base.image
        self.surface.axis = self.base.axis if self.overlay.isChecked() else None
        self.surface.fragment = self.fragment
        self.surface.ghost = (self.base.axis - self.ghost[1], self.ghost[0]) if self.ghost else None
        if fields:
            self.x.setValue(self.base.axis.x())
            self.y.setValue(self.base.axis.y())
        self.surface.sync()
        self.undo_button.setEnabled(bool(self.past))
        self.redo_button.setEnabled(bool(self.future))
        self.ghost_button.setEnabled(self.ghost is not None)
        self.clear_button.setEnabled(self.fragment is None)
        self.paste_bar.setVisible(self.fragment is not None)
        self.update_hint()
        self.ink.setStyleSheet(f'border-left: 6px solid {self.color.name()};')
        self.controls()

    def update_hint(self):
        if self.fragment is None:
            self.hint.setText('选区可复制。粘贴后拖动定位，Enter 放置，Esc 取消。')
            return
        pos, image = self.fragment
        outside = not self.base.image.rect().contains(QRect(pos, image.size()))
        self.hint.setText('画布外的片段会被裁掉。可拖动或用方向键调整。' if outside else
                          '拖动或用方向键定位片段，Enter 放置，Esc 取消。')

    def remember(self):
        self.past.append(Workspace(self.base.image.copy(), QPoint(self.base.axis)))
        self.past = self.past[-32:]
        self.future.clear()

    def undo(self):
        if self.fragment:
            self.cancel_paste()
        elif self.past:
            self.future.append(self.base)
            self.base = self.past.pop()
            self.surface.selection = QRect()
            self.sync()

    def redo(self):
        if self.future and not self.fragment:
            self.past.append(self.base)
            self.base = self.future.pop()
            self.surface.selection = QRect()
            self.sync()

    def clear_ghost(self):
        self.ghost = None
        self.sync()

    def clear_canvas(self):
        if self.fragment is None:
            self.remember()
            self.base.image = blank(self.base.image.width(), self.base.image.height())
            self.surface.selection = QRect()
            self.sync()

    def set_axis(self, point):
        if self.base.axis != point:
            self.remember()
            self.base.axis = point
            self.sync()

    def read_axis(self):
        self.set_axis(QPoint(self.x.value(), self.y.value()))

    def pick_color(self):
        color = QColorDialog.getColor(self.color, self, '选择画笔颜色')
        if color.isValid():
            self.color = color
            self.sync()

    def pick_swatch(self):
        palette, alpha, count = self.sources.colors(self.reference, self.mode.currentText())
        dialog = QDialog(self)
        dialog.setWindowTitle('从目标色表取色')
        layout = QVBoxLayout(dialog)
        layout.addWidget(label('点击色块取色。0 号透明色请用橡皮。', 'muted'))
        grid = QGridLayout()
        grid.setSpacing(3)
        layout.addLayout(grid)

        def choose(color):
            self.color = color
            self.sync()
            dialog.accept()

        for index in range(count):
            color = QColor(*palette[index*3:index*3+3])
            swatch = QToolButton()
            swatch.setFixedSize(22, 22)
            swatch.setEnabled(index != 0 and alpha[index] != 0)
            swatch.setToolTip(f'{index} · {color.name()}')
            swatch.setAccessibleName(f'色号 {index}')
            swatch.setStyleSheet(f'QToolButton {{ background: {color.name()}; border: 1px solid #afb8c1; }}'
                                 'QToolButton:disabled { background: #edf1f5; }'
                                 'QToolButton:hover, QToolButton:focus { border: 2px solid #0969da; }')
            swatch.clicked.connect(lambda checked=False, color=color: choose(color))
            grid.addWidget(swatch, index//16, index%16)
        dialog.exec()
        self.surface.setFocus()

    def press(self, point, modifiers):
        if self.fragment:
            if QRect(self.fragment[0], self.fragment[1].size()).contains(point):
                self.drag = point - self.fragment[0]
            return
        if modifiers & Qt.KeyboardModifier.AltModifier or self.tool.currentIndex() == 2:
            if self.base.image.rect().contains(point):
                color = self.base.image.pixelColor(point)
                if color.alpha():
                    self.color = color
                    self.sync()
        elif self.tool.currentIndex() == 3:
            self.start = point
            select(self.surface, point, point)
        elif self.tool.currentIndex() == 4:
            self.set_axis(point)
        else:
            self.remember()
            self.last = point
            self.paint(point)

    def move(self, point):
        if self.drag is not None and self.fragment:
            self.position_fragment(point - self.drag)
        elif self.start is not None:
            select(self.surface, self.start, point)
        elif self.last is not None:
            self.paint(point)

    def release(self, point):
        self.move(point)
        self.drag = self.start = self.last = None
        self.sync()

    def paint(self, point):
        with QPainter(self.base.image) as painter:
            if self.tool.currentIndex() == 1:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            pen = painter.pen()
            pen.setColor(self.color)
            pen.setWidth(self.pen.value())
            pen.setCapStyle(Qt.PenCapStyle.SquareCap)
            painter.setPen(pen)
            if self.last == point:
                painter.drawPoint(point)
            else:
                painter.drawLine(self.last, point)
        self.last = point
        self.surface.update()

    def use_base(self, entry, image):
        if max(image.width(), image.height()) > MAX_SIZE:
            QMessageBox.information(self, '底图超过画布上限', f'请先裁剪到 {MAX_SIZE} 像素以内，再粘贴。')
            return False
        if self.fragment:
            QMessageBox.information(self, '有待放置的片段', '请先放置或取消当前片段。')
            return False
        self.remember()
        self.base = Workspace(image.copy(), QPoint(*self.sources.axis(entry)))
        self.surface.selection = QRect()
        if self.sources.entries[entry]['indexed']:
            self.choose_palette(entry)
        self.sync()
        return True

    def resize_canvas(self):
        if self.fragment:
            QMessageBox.information(self, '有待放置的片段', '请先放置或取消当前片段。')
            return
        text, ok = QInputDialog.getText(self, '画布尺寸', '宽 × 高（保留左上角位置）',
                                       text=f'{self.base.image.width()}×{self.base.image.height()}')
        if not ok:
            return
        parts = text.lower().replace('×', 'x').split('x')
        if len(parts) != 2 or not all(p.strip().isdecimal() and len(p.strip()) <= 3 for p in parts):
            QMessageBox.warning(self, '尺寸无效', '请输入宽×高，例如 128×128。')
            return
        width, height = map(int, parts)
        if not (1 <= width <= MAX_SIZE and 1 <= height <= MAX_SIZE):
            QMessageBox.warning(self, '尺寸无效', f'宽和高须为 1～{MAX_SIZE}。')
            return
        if (width, height) == (self.base.image.width(), self.base.image.height()):
            return
        self.remember()
        image = blank(width, height)
        with QPainter(image) as painter:
            painter.drawImage(0, 0, self.base.image)
        self.base.image = image
        self.surface.selection = QRect()
        self.sync()

    def trim(self):
        if self.fragment:
            return
        image = self.base.image
        width, height = image.width(), image.height()
        pixels = image.constBits()
        xs, ys = [], []
        for y in range(height):
            visible = [x for x in range(width) if pixels[y*image.bytesPerLine()+4*x+3]]
            if visible:
                xs.extend((visible[0], visible[-1]))
                ys.append(y)
        if not xs:
            return
        crop = QRect(QPoint(min(xs), ys[0]), QPoint(max(xs), ys[-1]))
        if crop == image.rect():
            return
        new_axis = self.base.axis - crop.topLeft()
        if any(not -32768 <= value <= 32767 for value in (new_axis.x(), new_axis.y())):
            QMessageBox.warning(self, '轴点超出范围', '请先调整轴点再裁掉透明边。')
            return
        self.remember()
        self.base = Workspace(image.copy(crop), new_axis)
        self.surface.selection = QRect()
        self.sync()

    def copy(self, cut=False):
        if self.fragment:
            QApplication.clipboard().setImage(self.fragment[1])
            if cut:
                self.cancel_paste()
            return
        bounds = self.surface.selection
        QApplication.clipboard().setImage(self.base.image if bounds.isEmpty() else self.base.image.copy(bounds))
        if cut and not bounds.isEmpty():
            self.remember()
            with QPainter(self.base.image) as painter:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                painter.fillRect(bounds, Qt.GlobalColor.black)
            self.sync()

    def paste_clipboard(self):
        self.begin_paste(QApplication.clipboard().image())

    def begin_paste(self, image):
        if self.fragment:
            QMessageBox.information(self, '有待放置的片段', '请先放置或取消当前片段。')
            return False
        if image.isNull() or max(image.width(), image.height()) > MAX_SIZE:
            QMessageBox.information(self, '无法粘贴', f'请选择宽高在 {MAX_SIZE} 像素以内的非空图像。')
            return False
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        point = QPoint(max(0, (self.base.image.width()-image.width())//2),
                       max(0, (self.base.image.height()-image.height())//2))
        self.fragment = (point, image)
        self.surface.selection = QRect()
        self.sync()
        self.surface.setFocus()
        return True

    def position_fragment(self, point):
        _, image = self.fragment
        x = max(1-image.width(), min(self.base.image.width()-1, point.x()))
        y = max(1-image.height(), min(self.base.image.height()-1, point.y()))
        self.fragment = QPoint(x, y), image
        self.surface.fragment = self.fragment
        self.update_hint()
        self.surface.update()

    def nudge(self, x, y):
        if self.fragment:
            self.position_fragment(self.fragment[0] + QPoint(x, y))

    def rotate(self, degrees):
        if self.fragment:
            point, image = self.fragment
            turned = image.transformed(QTransform().rotate(degrees), Qt.TransformationMode.FastTransformation)
            if max(turned.width(), turned.height()) > MAX_SIZE:
                QMessageBox.information(self, '旋转后的尺寸过大', f'请先裁剪片段，边长上限为 {MAX_SIZE} 像素。')
                return
            center = point + QPoint((image.width()-turned.width())//2, (image.height()-turned.height())//2)
            self.fragment = center, turned.convertToFormat(QImage.Format.Format_RGBA8888)
            self.position_fragment(center)
            self.surface.setFocus()

    def angle(self):
        degrees, ok = QInputDialog.getInt(self, '旋转片段', '顺时针角度', 0, -360, 360)
        if ok:
            self.rotate(degrees)

    def flip(self):
        if self.fragment:
            point, image = self.fragment
            self.fragment = point, image.transformed(QTransform().scale(-1, 1))
            self.sync()
            self.surface.setFocus()

    def cancel_paste(self):
        if self.fragment:
            self.fragment = None
            self.sync()

    def settle(self):
        if self.fragment:
            self.remember()
            with QPainter(self.base.image) as painter:
                painter.drawImage(*self.fragment)
            self.cancel_paste()

    def confirm_leave(self):
        if self.fragment or self.base.axis != self.saved.axis or self.base.image != self.saved.image:
            return QMessageBox.question(
                self, '精灵草稿尚未插入', '切换工具时草稿会保留。关闭窗口或切换角色会丢失草稿。仍要继续？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        return True

    def preview(self):
        if self.fragment or self.reference is None or self.host.busy:
            return
        self.read_axis()
        mode = self.mode.currentText()
        canvas = self.base.image.copy()
        colors = self.sources.colors(self.reference, mode)
        self.host.run('适配目标色表', lambda: adapt_colors(canvas, *colors),
                      lambda draft: self.show_preview(draft, mode))

    def show_preview(self, draft, mode):
        dialog = QDialog(self)
        dialog.setWindowTitle('精灵色表预览')
        dialog.resize(730, 420)
        layout = QVBoxLayout(dialog)
        counts = [f'换到近似色 {draft.adjusted} 像素', f'透明度调整 {draft.clipped_alpha} 像素']
        if draft.ambiguous:
            counts.append(f'同色多索引 {draft.ambiguous} 像素，优先较小色号')
        if self.key() in self.sources.keys:
            counts.append('此编号已占用')
        info = label(' · '.join(counts), 'muted')
        info.setWordWrap(True)
        layout.addWidget(info)
        images = QHBoxLayout()
        before = PixelSurface(self.base.image)
        after = PixelSurface(self.base.image)
        look = QComboBox()
        look.addItems(self.sources.modes)
        look.setCurrentText(mode)

        def palette_image():
            rgb, alpha, _ = self.sources.colors(self.reference, look.currentText())
            return indexed_preview(draft, rgb, alpha)

        def update_palette(_index=None):
            after.image = palette_image()
            after.sync()

        update_palette()
        look.currentIndexChanged.connect(update_palette)
        layout.addLayout(images, 1)
        for title, surface in (('原画', before), ('同一批索引在所选色表中的显示', after)):
            side = QVBoxLayout()
            row(side, label(title), zoom_picker(surface))
            side.addWidget(scroll(surface), 1)
            images.addLayout(side, 1)
        insert = action('确认插入', dialog.accept, True)
        insert.setEnabled(self.key() not in self.sources.keys)
        row(layout, look, action('导出当前预览 PNG', lambda: self.export_png(palette_image())),
            insert, action('返回编辑', dialog.reject))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.insert(draft)
        else:
            self.host.message('已完成色表预览')

    def export_png(self, image):
        filename, _ = QFileDialog.getSaveFileName(self, '导出索引 PNG',
                                                  '%d,%d.png' % self.key(), 'PNG 图像 (*.png)')
        if not filename:
            return
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, 'PNG'):
            QMessageBox.warning(self, '导出失败', '无法编码 PNG。')
            return
        try:
            with open(filename, 'xb') as file:
                file.write(payload.data())
        except OSError as error:
            QMessageBox.warning(self, '导出失败', str(error))

    def insert(self, draft):
        sources, key, reference = self.sources, self.key(), self.reference
        axis = self.base.axis.x(), self.base.axis.y()
        saved = Workspace(self.base.image.copy(), QPoint(self.base.axis))
        root, definition = self.host.repo_path, self.host.definition

        def write():
            with self.host.operation_lock():
                backup = sources.insert(key, axis, draft, reference)
            return backup

        def done(backup):
            self.saved = saved
            QMessageBox.information(self, '已插入精灵',
                                    f"编号 {key[0]},{key[1]} 已写入 SFF。\n备份：{backup['backup']}")
            self.host.run('重新读取参考精灵', lambda: SpriteSources(root, definition),
                          lambda refreshed: self.loaded(refreshed, suggest_slot=True))

        self.host.run('备份并插入精灵', write, done, write=True)
