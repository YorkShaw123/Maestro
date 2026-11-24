"""Qt 6 workbench for MUGEN characters."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
import traceback

from PySide6.QtGui import QIcon
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot, Qt, QUrl, QSignalBlocker
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame,
    QHBoxLayout, QVBoxLayout, QGridLayout, QStackedWidget, QTreeWidget,
    QTreeWidgetItem, QLineEdit, QPlainTextEdit, QMessageBox, QTabWidget,
    QTextBrowser, QSplitter, QFileDialog, QComboBox, QDialog, QDialogButtonBox,
    QListWidget, QAbstractItemView)

from character_package import inspect_package, package_character
from git_helper_core import GitError, GitRepo, workspace_write_lock
from mugen_character import choices, local_path, preferred_definition, role_files
from toolbox_ui import apply_theme, label, button, line_icon

PROJECT_ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', PROJECT_ROOT))


class JobSignals(QObject):
    completed = Signal(object, object)


class Job(QRunnable):
    def __init__(self, work):
        super().__init__()
        self.work = work
        self.signals = JobSignals()

    def run(self):
        try:
            result = self.work()
        except Exception as error:
            self.signals.completed.emit(None, error)
        else:
            self.signals.completed.emit(result, None)


class PackagePage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.info = None
        self.extras = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)
        fields = QGridLayout()
        fields.setHorizontalSpacing(18)
        fields.setVerticalSpacing(10)
        fields.addWidget(label('源工程', 'muted'), 0, 0)
        source = QLineEdit(str(host.repo_path))
        source.setReadOnly(True)
        fields.addWidget(source, 0, 1)
        fields.addWidget(label('输出位置', 'muted'), 1, 0)
        self.destination = QLineEdit()
        self.destination.setReadOnly(True)
        fields.addWidget(self.destination, 1, 1)
        fields.setColumnStretch(1, 1)
        layout.addLayout(fields)
        actions = QHBoxLayout()
        self.summary = label('正在检查角色文件……', 'muted')
        actions.addWidget(self.summary, 1)
        self.refresh_button = button('重新检查')
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        self.add_button = button('添加文件')
        self.add_button.clicked.connect(self.add_files)
        actions.addWidget(self.add_button)
        self.find_button = button('查找文件')
        self.find_button.clicked.connect(self.find_files)
        actions.addWidget(self.find_button)
        self.remove_button = button('移除附加文件')
        self.remove_button.clicked.connect(self.remove_file)
        actions.addWidget(self.remove_button)
        self.pack_button = button('开始打包', True)
        self.pack_button.clicked.connect(self.start_package)
        actions.addWidget(self.pack_button)
        layout.addLayout(actions)
        self.note = label('打包前会先确认输出位置。', 'muted')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.files = QTreeWidget()
        self.files.setHeaderLabels(['角色文件', '来源'])
        self.files.currentItemChanged.connect(lambda *_: self._controls(self.host.busy))
        self.files.setRootIsDecorated(False)
        self.files.setColumnWidth(0, 500)
        splitter.addWidget(self.files)
        output_area = QWidget()
        output_layout = QVBoxLayout(output_area)
        output_layout.setContentsMargins(0, 10, 0, 0)
        output_layout.addWidget(label('执行结果', 'section'))
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText('点击“开始打包”并确认后，这里会显示执行结果。')
        output_layout.addWidget(self.output)
        splitter.addWidget(output_area)
        splitter.setSizes([360, 150])
        layout.addWidget(splitter, 1)
        host.busyChanged.connect(self._controls)
        self._controls(False)

    def _controls(self, busy):
        self.refresh_button.setEnabled(not busy)
        self.add_button.setEnabled(not busy)
        self.find_button.setEnabled(not busy)
        item = self.files.currentItem()
        self.remove_button.setEnabled(not busy and item is not None and item.text(0) in self.extras)
        self.pack_button.setEnabled(not busy and self.info is not None)

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '添加角色目录内的文件', str(self.host.repo_path))
        try:
            names = {Path(path).relative_to(self.host.repo_path).as_posix() for path in paths}
        except ValueError as error:
            QMessageBox.warning(self, '无法添加文件', str(error))
            return
        self.add_names(names)

    def find_files(self):
        def show(names):
            dialog = QDialog(self)
            dialog.setWindowTitle('查找角色目录内的文件')
            dialog.resize(620, 520)
            layout = QVBoxLayout(dialog)
            filter_box = QLineEdit()
            filter_box.setPlaceholderText('搜索文件名或子目录')
            layout.addWidget(filter_box)
            listing = QListWidget()
            listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            listing.addItems(names)
            layout.addWidget(listing)

            def filter_files(value):
                value = value.casefold().replace('\\', '/')
                for i, name in enumerate(names):
                    listing.item(i).setHidden(value not in name.casefold())

            filter_box.textChanged.connect(filter_files)
            commands = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            commands.button(QDialogButtonBox.StandardButton.Ok).setText('添加所选文件')
            commands.accepted.connect(dialog.accept)
            commands.rejected.connect(dialog.reject)
            layout.addWidget(commands)
            filter_box.setFocus()
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.add_names({item.text() for item in listing.selectedItems() if not item.isHidden()})

        selected = set(self.info.files if self.info else ())
        destination = self.info.destination if self.info else None
        root = self.host.repo_path
        self.host.run('查找角色文件', lambda: [
            name for name in role_files(root)
            if name not in selected and (destination is None or not (root / name).is_relative_to(destination))
        ], show)

    def add_names(self, names):
        additions = names - set(self.info.files if self.info else ())
        if not additions:
            return
        try:
            inspect_package(self.host.repo_path, definition=self.host.definition, extras=self.extras | additions)
        except (ValueError, RuntimeError) as error:
            QMessageBox.warning(self, '无法添加文件', str(error))
            return
        self.extras.update(additions)
        self.refresh()

    def remove_file(self):
        item = self.files.currentItem()
        if item is not None:
            self.extras.discard(item.text(0))
            self.refresh()

    def refresh(self):
        self.info = None
        self._controls(self.host.busy)
        if not self.host.definition:
            self.destination.clear()
            self.files.clear()
            self.summary.setText('请先打开角色 DEF。')
            self.note.setText('选择角色后会自动检查打包文件。')
            return
        def done(info):
            self.info = info
            self.destination.setText(str(info.destination))
            self.summary.setText(f'{len(info.files)} 个角色文件 · {info.total_bytes / 1048576:.1f} MB')
            notice = '现有导出会先移入备份目录。' if info.export_exists else '系统推荐清单来自当前 DEF 的文件引用。'
            if info.missing_palettes:
                notice += ' 以下 DEF 配色缺失，将跳过：' + '、'.join(info.missing_palettes)
            self.note.setText(notice)
            self.files.clear()
            for name in info.files:
                self.files.addTopLevelItem(QTreeWidgetItem([name, '手动添加' if name in self.extras else '系统推荐']))
            self._controls(False)
            self.host.message('角色文件检查完成')
        extras = tuple(sorted(self.extras))
        self.host.run('检查角色文件', lambda: inspect_package(
            self.host.repo_path, definition=self.host.definition, extras=extras), done)

    def start_package(self):
        if self.host.busy or self.info is None:
            return
        missing = ('\n缺少以下 DEF 配色，将跳过：' + '、'.join(self.info.missing_palettes) + '\n'
                   if self.info.missing_palettes else '')
        answer = QMessageBox.question(self, '确认打包角色',
            f'输出位置：\n{self.info.destination}\n\n'
            f'将复制当前 {len(self.info.files)} 个角色文件。已有的导出会先备份。\n{missing}\n开始打包？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        extras, preview = tuple(sorted(self.extras)), self.info
        def work():
            with self.host.operation_lock():
                return self.host.package_runner(self.host.repo_path, definition=self.host.definition,
                                                extras=extras, preview=preview)
        def done(result):
            self.info = result.info
            self.output.setPlainText(f'打包成功\n输出：{result.info.destination}\n\n{len(result.info.files)} 个文件已逐项核对 SHA256。\n\n' + result.output)
            self.note.setText('已有导出文件夹；下次打包会备份旧导出。')
            self.host.message(f'打包完成 · {len(result.info.files)} 个文件已核对')
        self.host.run('正在打包并核对文件', work, done, write=True)


class HelpPage(QWidget):
    def __init__(self, host):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 18)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        for title, name, legacy_name in (('工具箱', 'user-guide.md', '角色工具箱.md'), ('Git 入门', 'git-guide.md', 'Git使用与小工具.md'), ('打包安装', 'installation.md', '安装到MUGEN.md')):
            browser = QTextBrowser()
            browser.setOpenExternalLinks(True)
            path = host.repo_path / 'docs' / '维护' / legacy_name
            if not path.is_file():
                path = RESOURCE_ROOT / 'docs' / name
            try:
                content = path.read_text('utf-8-sig')
            except OSError as error:
                content = f'无法读取说明：{error}'
            browser.document().setBaseUrl(QUrl.fromLocalFile(str(path.parent) + '/'))
            browser.setMarkdown(content)
            self.tabs.addTab(browser, title)


@dataclass(frozen=True)
class PageSpec:
    key: str
    title: str
    factory: object


def make_git(host):
    from git_helper import GitPage
    return GitPage(host)


def make_sprites(host):
    from sprite_editor_ui import SpriteEditorPage
    return SpriteEditorPage(host)


def make_sprite_creation(host):
    from sprite_creation_ui import SpriteCreationPage
    return SpriteCreationPage(host)


def make_ai(host):
    from ai_workbench_ui import AIPage
    return AIPage(host)


def make_dockyard(host):
    from dockyard import DockyardPage
    return DockyardPage(host)


PAGE_REGISTRY = (
    PageSpec('git', 'Git 存档', make_git),
    PageSpec('package', '角色打包', PackagePage),
    PageSpec('sprites', '调色板', make_sprites),
    PageSpec('sprite-creation', '精灵图制作', make_sprite_creation),
    PageSpec('simple-ai', '简易AI修改', make_ai),
    PageSpec('dockyard', '船坞搭建', make_dockyard),
    PageSpec('help', '使用说明', HelpPage),
)


class ToolboxApp(QMainWindow):
    busyChanged = Signal(bool)
    tools_root = PROJECT_ROOT

    def __init__(self, repo_path=PROJECT_ROOT, *, page='git', definition=None, git_factory=GitRepo,
                 package_runner=package_character):
        super().__init__()
        self.repo_path = Path(repo_path).resolve()
        self.definition = definition
        self.git_factory, self.package_runner = git_factory, package_runner
        self.busy = self.writing = self._closed = False
        self._jobs = []
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.pages, self.nav = {}, {}
        self.active_page = None
        self.setWindowTitle('Maestro · MUGEN 角色工具箱')
        area = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1280, area.width() - 64), min(820, area.height() - 80))
        self.setMinimumSize(min(900, area.width() - 32), min(580, area.height() - 48))
        central = QWidget()
        central.setObjectName('Workspace')
        self.setCentralWidget(central)
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName('Sidebar')
        sidebar.setFixedWidth(172)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 22, 12, 16)
        side.setSpacing(5)
        side.addWidget(label('角色工具箱', 'section'))
        side.addSpacing(25)
        for spec in PAGE_REGISTRY:
            nav = button(spec.title)
            nav.setProperty('nav', True)
            nav.setCheckable(True)
            nav.setIcon(line_icon(spec.key))
            nav.clicked.connect(lambda checked=False, key=spec.key: self.show_page(key))
            self.nav[spec.key] = nav
            side.addWidget(nav)
        side.addStretch()
        side.addWidget(label('当前工程', 'muted'))
        self.project = label(self.repo_path.name)
        side.addWidget(self.project)
        row.addWidget(sidebar)
        main = QWidget()
        column = QVBoxLayout(main)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        top = QFrame()
        top.setObjectName('Topbar')
        top.setFixedHeight(60)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(22, 0, 22, 0)
        self.title = label('', 'section')
        top_layout.addWidget(self.title)
        top_layout.addStretch()
        self.def_combo = QComboBox()
        self.def_combo.currentIndexChanged.connect(self.change_definition)
        top_layout.addWidget(self.def_combo)
        self.open_button = button('打开角色 DEF')
        self.open_button.clicked.connect(self.open_character)
        top_layout.addWidget(self.open_button)
        column.addWidget(top)
        self.stack = QStackedWidget()
        column.addWidget(self.stack, 1)
        row.addWidget(main, 1)
        self.statusBar().setSizeGripEnabled(False)
        self.busyChanged.connect(lambda busy: [item.setEnabled(not busy) for item in self.nav.values()])
        self.busyChanged.connect(lambda busy: self.def_combo.setEnabled(not busy))
        self.busyChanged.connect(lambda busy: self.open_button.setEnabled(not busy))
        self._populate_definitions()
        if page == 'git' and not self.has_git():
            page = 'package'
        self.show_page(page)

    def _populate_definitions(self):
        options = choices(self.repo_path) if self.repo_path.is_dir() else ()
        if self.definition not in options:
            self.definition = preferred_definition(self.repo_path, options)
        with QSignalBlocker(self.def_combo):
            self.def_combo.clear()
            self.def_combo.addItem('选择角色 DEF', None)
            for name in options:
                self.def_combo.addItem(name, name)
            self.def_combo.setCurrentIndex(self.def_combo.findData(self.definition))
        self.project.setText(self.repo_path.name)
        self.project.setToolTip(str(self.repo_path))

    def _switch_character(self, path, definition):
        for controller in self.pages.values():
            if hasattr(controller, 'confirm_leave') and not controller.confirm_leave():
                self._populate_definitions()
                return
        key = self.active_page or 'package'
        for controller in self.pages.values():
            self.stack.removeWidget(controller)
            controller.deleteLater()
        self.pages.clear()
        self.active_page = None
        self.repo_path, self.definition = path, definition
        self._populate_definitions()
        self.show_page('package' if key == 'git' and not self.has_git() else key)

    def change_definition(self, _index):
        if not self.busy:
            self._switch_character(self.repo_path, self.def_combo.currentData())

    def open_character(self):
        filename, _ = QFileDialog.getOpenFileName(self, '打开角色 DEF', str(self.repo_path), 'MUGEN DEF (*.def)')
        if filename:
            path = Path(filename).resolve()
            root = self.repo_path if path.is_relative_to(self.repo_path) else path.parent
            self._switch_character(root, path.relative_to(root).as_posix())

    def git_path(self):
        """Look up the selected DEF's repository without leaving the workspace."""
        if not self.definition:
            return self.repo_path
        current = (self.repo_path / self.definition).parent
        while current != self.repo_path:
            if (current / '.git').exists():
                return current
            current = current.parent
        return self.repo_path

    def has_git(self):
        return (self.git_path() / '.git').exists()

    def open_git(self):
        target = self.git_path()
        repo = self.git_factory(target)
        if repo.root != target:
            raise GitError('请选择角色自己的 Git 仓库，避免影响上级目录。')
        return repo

    def operation_lock(self):
        if self.has_git():
            return self.open_git().operation_lock()
        folder = local_path(self.repo_path, '.recon')
        folder.mkdir(exist_ok=True)
        return workspace_write_lock(folder / 'mugen-toolbox.write.lock')

    def message(self, text):
        if not self._closed:
            self.statusBar().showMessage(text)

    def run(self, text, work, done, write=False):
        if self.busy or self._closed:
            return False
        self.busy, self.writing = True, write
        self.busyChanged.emit(True)
        self.message(text + '……')
        job = Job(work)
        self._jobs.append(job)
        self._current_job, self._current_done = job, done
        job.signals.completed.connect(self._finish_job, Qt.ConnectionType.QueuedConnection)
        self.pool.start(job)
        return True

    @Slot(object, object)
    def _finish_job(self, result, error):
        # This QObject slot has the main window's GUI-thread affinity.
        job, done = self._current_job, self._current_done
        self._current_job = self._current_done = None
        if job in self._jobs:
            self._jobs.remove(job)
        self.busy = self.writing = False
        if self._closed:
            return
        self.busyChanged.emit(False)
        if error is not None:
            self.message('操作未完成：' + str(error))
            QMessageBox.critical(self, '操作未完成', str(error))
            return
        try:
            done(result)
        except Exception as callback_error:
            self.message('界面刷新未完成：' + str(callback_error))
            QMessageBox.warning(self, '刷新未完成', str(callback_error))

    def show_page(self, key):
        if self.busy or self._closed:
            return
        previous = self.pages.get(self.active_page)
        if key != self.active_page and previous is not None and hasattr(previous, 'confirm_leave'):
            if not previous.confirm_leave():
                for name, nav in self.nav.items():
                    nav.setChecked(name == self.active_page)
                return
        specs = {spec.key: spec for spec in PAGE_REGISTRY}
        if key not in specs:
            raise ValueError('未知工具页面：' + key)
        if key not in self.pages:
            controller = specs[key].factory(self)
            self.pages[key] = controller
            self.stack.addWidget(controller)
        controller = self.pages[key]
        self.stack.setCurrentWidget(controller)
        self.active_page = key
        self.title.setText(specs[key].title)
        for name, nav in self.nav.items():
            nav.setChecked(name == key)
        if hasattr(controller, 'refresh'):
            controller.refresh()

    def closeEvent(self, event):
        if self.writing:
            QMessageBox.information(self, '正在保存，请稍候', '操作还未完成，请等待完成后再关闭工具箱。')
            event.ignore()
            return
        for page in self.pages.values():
            if hasattr(page, 'confirm_leave') and not page.confirm_leave():
                event.ignore()
                return
        self._closed = True
        event.accept()


def create_application(argv=None):
    application = QApplication.instance() or QApplication(argv or [])
    application.setApplicationName('Maestro')
    application.setWindowIcon(QIcon(str(RESOURCE_ROOT / 'assets' / 'maestro.ico')))
    apply_theme(application)
    return application


def main(argv=None):
    parser = argparse.ArgumentParser(description='适用于 MUGEN 1.0／1.1 的角色工具箱。')
    parser.add_argument('--repo', type=Path, default=PROJECT_ROOT)
    parser.add_argument('--def', dest='definition', help='角色目录内的 DEF 文件名')
    parser.add_argument('--page', choices=[spec.key for spec in PAGE_REGISTRY], default='git')
    args = parser.parse_args(argv)
    app = create_application(sys.argv[:1])
    window = ToolboxApp(args.repo, page=args.page, definition=args.definition)
    def error_handler(kind, error, trace):
        message = ''.join(traceback.format_exception(kind, error, trace))
        QMessageBox.critical(window, '工具箱遇到错误', message)
    sys.excepthook = error_handler
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
