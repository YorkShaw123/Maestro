"""Preview and install the reusable character maintenance documents."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QComboBox, QMessageBox, QTextBrowser, QVBoxLayout, QWidget

from mugen_character import local_path
from toolbox_ui import button, label


FOLDER = 'MUGEN维护船坞'
DOCUMENTS = ('README.md', '状态与资源.md', 'AI与连段.md', '调色板与精灵.md', '测试与性能.md')
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
TEMPLATES = RESOURCE_ROOT / 'assets' / 'dockyard_templates'
TEMPLATE_NAMES = {'状态与资源.md': 'states-and-resources.md', 'AI与连段.md': 'ai-and-combos.md', '调色板与精灵.md': 'palettes-and-sprites.md', '测试与性能.md': 'testing-and-performance.md'}


def install_dockyard(root, definition):
    entry = local_path(root, definition)
    if not entry.is_file():
        raise ValueError('找不到当前角色 DEF。')
    dest = local_path(root, (entry.parent / FOLDER).relative_to(root).as_posix())
    documents = {name: (TEMPLATES / TEMPLATE_NAMES.get(name, name)).read_bytes() for name in DOCUMENTS}
    if dest.exists():
        raise FileExistsError(f'文档目录已存在：{dest}')
    with TemporaryDirectory(prefix='.dockyard-', dir=entry.parent) as temporary:
        staged = Path(temporary) / FOLDER
        staged.mkdir()
        for name, data in documents.items():
            (staged / name).write_bytes(data)
        if dest.exists():
            raise FileExistsError(f'文档目录已存在：{dest}')
        staged.rename(dest)
    return dest


class DockyardPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.addWidget(label('把角色维护与测试手册复制到当前 DEF 所在目录。', 'section'))
        self.target = label('', 'muted')
        self.target.setWordWrap(True)
        layout.addWidget(self.target)
        self.topics = QComboBox()
        self.topics.addItems(DOCUMENTS)
        layout.addWidget(self.topics)
        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        self.preview.anchorClicked.connect(self.open_topic)
        layout.addWidget(self.preview, 1)
        self.install_button = button('搭建船坞', True)
        self.install_button.clicked.connect(self.install)
        layout.addWidget(self.install_button)
        self.topics.currentTextChanged.connect(self.read_topic)
        host.busyChanged.connect(self.refresh)
        self.read_topic(DOCUMENTS[0])

    def refresh(self, *_):
        definition = self.host.definition
        if definition:
            dest = self.host.repo_path / Path(definition).parent / FOLDER
            self.target.setText(f'目标目录：{dest}' + ('（已搭建）' if dest.exists() else ''))
        else:
            self.target.setText('先在顶部选择要维护的角色 DEF。')
        self.install_button.setEnabled(bool(definition) and not self.host.busy)

    def read_topic(self, name):
        self.preview.setMarkdown((TEMPLATES / TEMPLATE_NAMES.get(name, name)).read_text(encoding='utf-8'))
        self.preview.document().setBaseUrl(QUrl.fromLocalFile(str(TEMPLATES) + '/'))

    def open_topic(self, url):
        if url.fileName() in DOCUMENTS:
            self.topics.setCurrentText(url.fileName())

    def install(self):
        root, definition = self.host.repo_path, self.host.definition
        if not definition:
            return

        def work():
            with self.host.operation_lock():
                return install_dockyard(root, definition)

        def done(dest):
            self.refresh()
            self.host.message(f'船坞已搭建 · {dest}')
            QMessageBox.information(self, '船坞已搭建', f'文档已写入 {dest}\n让 AI 从 README.md 开始读取。')

        self.host.run('复制船坞文档', work, done, write=True)
