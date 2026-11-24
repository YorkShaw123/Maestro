"""Palette utilities inspired by the supplied standalone assistant."""
from pathlib import Path
import re

from PySide6.QtCore import QBuffer, QIODevice, QPoint, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QMessageBox, QSpinBox, QVBoxLayout)

from toolbox_ui import button


def brightness_order(colors):
    return [0, *sorted(range(1, len(colors)), key=lambda i:
                      (2126 * colors[i][0] + 7152 * colors[i][1] + 722 * colors[i][2], i))]


def sorted_act(colors):
    if len(colors) != 256:
        raise ValueError('ACT 导出需要 256 色。')
    return b''.join(bytes(colors[i]) for i in reversed(brightness_order(colors)))


def _natural(name):
    return tuple((0, int(part)) if part.isdecimal() else (1, part.casefold())
                 for part in re.split(r'(\d+)', name))


def _crop_alpha(image):
    raw = image.constBits()
    left, top, right, bottom = image.width(), image.height(), -1, -1
    for y in range(image.height()):
        row = bytes(raw[y * image.bytesPerLine():(y + 1) * image.bytesPerLine()])[3::4]
        visible = row.lstrip(b'\0')
        if visible:
            left = min(left, len(row) - len(visible))
            right = max(right, len(row.rstrip(b'\0')) - 1)
            top, bottom = min(top, y), y
    return image.copy(left, top, right - left + 1, bottom - top + 1) if right >= 0 else QImage()


def build_sheet(folder, columns, trim):
    allowed = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'}
    sources = sorted((path for path in Path(folder).iterdir() if path.is_file()
                      and path.suffix.lower() in allowed), key=lambda path: (_natural(path.name), path.name))
    if not sources or len(sources) > 256:
        raise ValueError('请选择包含 1～256 张图片的目录。')
    images, total = [], 0
    for path in sources:
        reader = QImageReader(str(path))
        size = reader.size()
        total += size.width() * size.height()
        if not size.isValid() or total > 16_777_216:
            raise ValueError('图片格式无效，或原图总尺寸超过 1677 万像素：' + path.name)
        image = reader.read().convertToFormat(QImage.Format.Format_RGBA8888)
        if image.isNull():
            raise ValueError('无法读取：' + path.name)
        if trim:
            image = _crop_alpha(image)
        if not image.isNull():
            images.append(image)
    if not images:
        raise ValueError('裁剪后没有可见像素。')
    columns = min(columns, len(images))
    rows = [images[i:i+columns] for i in range(0, len(images), columns)]
    width = max(sum(image.width() for image in row) for row in rows)
    heights = [max(image.height() for image in row) for row in rows]
    if width * sum(heights) > 16_777_216:
        raise ValueError('拼图尺寸超过 1677 万像素，请更换列数或减少图片。')
    sheet = QImage(width, sum(heights), QImage.Format.Format_RGBA8888)
    sheet.fill(Qt.GlobalColor.transparent)
    painter = QPainter(sheet)
    y = 0
    for row, height in zip(rows, heights):
        x = 0
        for image in row:
            painter.drawImage(x, y, image)
            x += image.width()
        y += height
    painter.end()
    return sheet, len(images)


class ImagePreview(QLabel):
    def __init__(self, select):
        super().__init__()
        self.select = select
        self.source = QImage()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(420, 280)
        self.setText('选择图片目录后生成预览。')

    def display(self, source):
        self.source = source
        self.update_image()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_image()

    def update_image(self):
        if not self.source.isNull():
            self.setPixmap(QPixmap.fromImage(self.source.scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)))

    def mousePressEvent(self, event):
        preview = self.pixmap()
        if event.button() != Qt.MouseButton.LeftButton or preview is None or preview.isNull():
            return
        pos = event.position().toPoint() - QPoint((self.width() - preview.width()) // 2,
                                                  (self.height() - preview.height()) // 2)
        if 0 <= pos.x() < preview.width() and 0 <= pos.y() < preview.height():
            self.select(self.source.pixelColor(pos.x()*self.source.width()//preview.width(),
                                               pos.y()*self.source.height()//preview.height()))


class SheetDialog(QDialog):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.folder = None
        self.rgb = None
        self.resize(760, 530)
        self.setWindowTitle('拼图取色')
        layout = QVBoxLayout(self)
        actions = QHBoxLayout()
        choose = button('图片目录')
        choose.clicked.connect(self.choose)
        actions.addWidget(choose)
        actions.addWidget(QLabel('每行列数'))
        self.columns = QSpinBox()
        self.columns.setRange(1, 32)
        self.columns.setValue(8)
        actions.addWidget(self.columns)
        self.trim = QCheckBox('裁剪外围透明像素')
        self.trim.setChecked(True)
        actions.addWidget(self.trim)
        actions.addStretch()
        build = button('生成预览', True)
        build.clicked.connect(self.build)
        actions.addWidget(build)
        layout.addLayout(actions)
        self.preview = ImagePreview(self.select_color)
        layout.addWidget(self.preview, 1)
        footer = QHBoxLayout()
        self.description = QLabel('点击拼图上的像素取色。')
        self.description.setWordWrap(True)
        footer.addWidget(self.description, 1)
        self.copy = button('复制 RGB')
        self.copy.setEnabled(False)
        self.copy.clicked.connect(lambda: QApplication.clipboard().setText(', '.join(map(str, self.rgb))))
        footer.addWidget(self.copy)
        self.apply = button('应用到当前色号')
        self.apply.setEnabled(False)
        self.apply.clicked.connect(self.apply_color)
        footer.addWidget(self.apply)
        self.export = button('导出 PNG')
        self.export.setEnabled(False)
        self.export.clicked.connect(self.export_sheet)
        footer.addWidget(self.export)
        layout.addLayout(footer)

    def choose(self):
        path = QFileDialog.getExistingDirectory(self, '选择图片目录', str(self.folder or self.editor.host.repo_path))
        if path:
            self.folder = Path(path)
            self.description.setText(str(self.folder))

    def build(self):
        if self.folder is None:
            self.choose()
            if self.folder is None:
                return
        folder, columns, trim = self.folder, self.columns.value(), self.trim.isChecked()
        def done(result):
            image, count = result
            self.rgb = None
            self.apply.setEnabled(False)
            self.copy.setEnabled(False)
            self.preview.display(image)
            self.description.setText(f'{count} 张 · {image.width()} × {image.height()} · 点击像素取色')
            self.export.setEnabled(True)
        self.editor.host.run('拼接图片', lambda: build_sheet(folder, columns, trim), done)

    def select_color(self, color):
        self.rgb = (color.red(), color.green(), color.blue()) if color.alpha() else None
        self.description.setText('RGB ' + ', '.join(map(str, self.rgb)) if self.rgb else '透明像素')
        self.copy.setEnabled(self.rgb is not None)
        self.apply.setEnabled(self.rgb is not None and self.editor.color_button.isEnabled()
                              and self.editor.palette_grid.selected != 0)

    def apply_color(self):
        if self.rgb is not None and self.editor._apply_color_rgb(self.editor.palette_grid.selected, self.rgb):
            self.accept()

    def export_sheet(self):
        filename, _ = QFileDialog.getSaveFileName(self, '导出拼图', str(self.folder / '拼接图片.png'), 'PNG (*.png)')
        if filename:
            path = Path(filename)
            if not path.suffix:
                path = path.with_suffix('.png')
            try:
                buffer = QBuffer()
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                if not self.preview.source.save(buffer, 'PNG'):
                    raise ValueError('PNG 编码失败。')
                with path.open('xb') as stream:
                    stream.write(bytes(buffer.data()))
            except (OSError, ValueError):
                QMessageBox.warning(self, '无法导出', '请使用一个可写且尚不存在的 PNG 文件名。')
