"""Qt Git workbench; repository operations stay in git_helper_core."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import sys

from PySide6.QtCore import Qt, QSize, QSignalBlocker
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QSizePolicy, QSplitter, QStackedWidget,
    QTabBar, QVBoxLayout, QWidget,
)

from toolbox_ui import DiffView, button, label
from git_helper_core import GitRepo


def _plain_label(text, role='body'):
    widget = label(text, role)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def _display_time(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return value


def _change_kind(status):
    for marker, title in (('?', '新增'), ('U', '冲突'), ('R', '重命名'),
                          ('D', '删除'), ('A', '新增'), ('C', '复制'), ('M', '修改')):
        if marker in status:
            return title
    return status.strip() or '修改'


def _readable_details(raw):
    """Keep commit prose, file statistics and patch; omit raw Git metadata."""
    match = re.search(r'(?m)^diff --', raw)
    before, patch = (raw[:match.start()], raw[match.start():]) if match else (raw, '')
    message, started = [], False
    for line in before.splitlines():
        if line.startswith('    '):
            started = True
            message.append(line[4:])
        elif started and not line.strip():
            message.append('')
        elif started:
            break
    prose = '\n'.join(message).strip().partition('\n')[2].strip()
    statistics = '\n'.join(line for line in before.splitlines()
                           if ' | ' in line or re.match(r'^\s*\d+ files? changed', line)
                           or re.match(r'^\s*(create|delete|rename|copy) mode', line))
    parts = [part for part in (prose, statistics, patch) if part.strip()]
    return '\n\n'.join(parts) if parts else '这个提交没有可显示的文本差异。'


class GitPage(QWidget):
    """One Git page hosted by the toolbox's shared background-job runner."""

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.repo = None
        self.changes = []
        self.commits = {}
        self.author = ('', '')
        self.current_diff = ''
        self._details_oid = None
        self._details_raw = ''
        self._busy = False
        self._build()
        host.busyChanged.connect(self._busy_changed)
        self._update_controls()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        toolbar = QFrame()
        toolbar.setObjectName('Topbar')
        actions = QHBoxLayout(toolbar)
        actions.setContentsMargins(16, 0, 16, 0)
        actions.setSpacing(10)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.addTab('当前改动')
        self.tabs.addTab('提交历史')
        self.tabs.currentChanged.connect(self._mode_changed)
        actions.addWidget(self.tabs)
        actions.addStretch()
        self.branch_label = _plain_label('正在读取仓库', 'muted')
        actions.addWidget(self.branch_label)
        self.init_button = button('初始化角色 Git')
        self.init_button.clicked.connect(self.initialize_repo)
        actions.addWidget(self.init_button)
        self.identity_button = button('设置署名')
        self.identity_button.clicked.connect(self.edit_identity)
        actions.addWidget(self.identity_button)
        self.refresh_button = button('刷新')
        self.refresh_button.clicked.connect(lambda: self.refresh())
        actions.addWidget(self.refresh_button)
        outer.addWidget(toolbar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        outer.addWidget(self.splitter, 1)

        left = QFrame()
        left.setMinimumWidth(290)
        left.setObjectName('GitSidebar')
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self.list_summary = _plain_label('正在读取文件', 'muted')
        self.list_summary.setContentsMargins(16, 12, 16, 10)
        left_layout.addWidget(self.list_summary)
        self.lists = QStackedWidget()
        self.file_list = QListWidget()
        self.file_list.setObjectName('GitFiles')
        self.file_list.setSpacing(0)
        self.file_list.currentItemChanged.connect(self._file_selected)
        self.history_list = QListWidget()
        self.history_list.setObjectName('GitHistory')
        self.history_list.setSpacing(0)
        self.history_list.currentItemChanged.connect(self._history_selected)
        self.lists.addWidget(self.file_list)
        self.lists.addWidget(self.history_list)
        left_layout.addWidget(self.lists, 1)
        self.composer = QFrame()
        self.composer.setObjectName('CommitComposer')
        compose = QVBoxLayout(self.composer)
        compose.setContentsMargins(16, 14, 16, 16)
        compose.setSpacing(8)
        compose.addWidget(_plain_label('保存当前改动', 'section'))
        self.message_edit = QPlainTextEdit()
        self.message_edit.setPlaceholderText('写一句这次改了什么')
        self.message_edit.setMinimumHeight(72)
        self.message_edit.setMaximumHeight(96)
        self.message_edit.textChanged.connect(self._update_controls)
        compose.addWidget(self.message_edit)
        self.submit_button = button('提交全部改动', primary=True)
        self.submit_button.clicked.connect(self.commit_all)
        compose.addWidget(self.submit_button)
        compose.addWidget(_plain_label('全部文件 · 保存在本机', 'muted'))
        left_layout.addWidget(self.composer)
        self.splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        summary = QWidget()
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(20, 15, 20, 14)
        summary_layout.setSpacing(7)
        summary_actions = QHBoxLayout()
        summary_actions.setSpacing(8)
        self.summary_title = _plain_label('当前工作区', 'section')
        self.summary_title.setWordWrap(True)
        summary_actions.addWidget(self.summary_title, 1)
        self.copy_button = button('复制号码')
        self.copy_button.clicked.connect(self.copy_oid)
        summary_actions.addWidget(self.copy_button)
        self.favorite_button = button('收藏')
        self.favorite_button.clicked.connect(self.toggle_favorite)
        summary_actions.addWidget(self.favorite_button)
        self.restore_button = button('回档到此版本')
        self.restore_button.clicked.connect(self.preview_restore)
        summary_actions.addWidget(self.restore_button)
        summary_layout.addLayout(summary_actions)
        self.summary_meta = _plain_label('读取完成后显示文件变化。', 'muted')
        self.summary_meta.setWordWrap(True)
        summary_layout.addWidget(self.summary_meta)
        right_layout.addWidget(summary)
        self.diff_view = DiffView()
        right_layout.addWidget(self.diff_view, 1)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 35)
        self.splitter.setStretchFactor(1, 65)
        self.splitter.setSizes([350, 650])
        self._mode_changed(0)

    @staticmethod
    def _list_row(list_widget, title, subtitle, key):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, title + ' ' + subtitle)
        item.setToolTip(title + '\n' + subtitle)
        item.setSizeHint(QSize(0, 78))
        list_widget.addItem(item)
        row = QWidget()
        row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(row)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(4)
        title_widget = _plain_label(title, 'section')
        title_widget.setWordWrap(True)
        title_widget.setMaximumHeight(title_widget.fontMetrics().lineSpacing() * 2 + 2)
        title_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_widget.setToolTip(title)
        layout.addWidget(title_widget)
        subtitle_widget = _plain_label(subtitle, 'muted')
        subtitle_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(subtitle_widget)
        list_widget.setItemWidget(item, row)
        return item

    def _busy_changed(self, busy):
        self._busy = bool(busy)
        self._update_controls()

    def _update_controls(self):
        active = not self._busy
        ready = active and self.repo is not None
        self.refresh_button.setEnabled(active)
        self.tabs.setEnabled(active)
        self.file_list.setEnabled(ready)
        self.history_list.setEnabled(ready)
        self.identity_button.setEnabled(ready)
        self.init_button.setVisible(not self.host.has_git())
        self.init_button.setEnabled(active)
        self.message_edit.setReadOnly(not active)
        self.submit_button.setEnabled(ready and bool(self.changes)
                                      and bool(self.message_edit.toPlainText().strip()))
        selected = self.selected_oid() is not None
        self.copy_button.setEnabled(ready and selected)
        self.restore_button.setEnabled(ready and selected)
        favorites = getattr(self, 'favorites', frozenset())
        self.favorite_button.setEnabled(ready and selected)
        self.favorite_button.setText('取消收藏' if self.selected_oid() in favorites else '收藏')

    def toggle_favorite(self):
        oid = self.selected_oid()
        if self.repo is not None and oid is not None:
            self.host.run('更新收藏', lambda: self.repo.toggle_favorite(oid),
                          lambda _: self.refresh(oid), write=True)

    def selected_oid(self):
        item = self.history_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _snapshot(self, repo, selected=None):
        commits, favorites = repo.history()
        oids = {commit.oid for commit in commits}
        selected = selected if selected in oids else commits[0].oid if commits else None
        return {
            'repo': repo, 'branch': repo.branch(), 'changes': repo.status(),
            'commits': commits, 'favorites': favorites, 'author': repo.identity(), 'diff': repo.diff(),
            'selected': selected, 'details': repo.details(selected) if selected else '',
        }

    def refresh(self, selected=None):
        if self._busy:
            return False
        if not self.host.has_git():
            self.repo = None
            self.branch_label.setText('尚无仓库')
            self.list_summary.setText('当前角色目录没有独立 Git 仓库')
            self.summary_meta.setText('可初始化这个角色目录的 Git。打包和调色板可以直接使用。')
            self._update_controls()
            return False
        selected = selected or self.selected_oid()
        repo = self.repo
        return self.host.run('读取 Git 存档',
                             lambda: self._snapshot(repo or self.host.open_git(), selected),
                             self._render_snapshot)

    def initialize_repo(self):
        target = self.host.repo_path
        answer = QMessageBox.question(self, '初始化角色 Git',
                                      f'在此目录建立独立 Git 仓库？\n\n{target}',
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.host.run('初始化角色 Git', lambda: GitRepo.initialize(target),
                          lambda _: self.refresh(), write=True)

    def _render_snapshot(self, data):
        self.repo = data['repo']
        self.changes, self.author = data['changes'], data['author']
        self.commits = {commit.oid: commit for commit in data['commits']}
        self.favorites = data['favorites']
        self.current_diff = data['diff']
        self.branch_label.setText('分支：' + data['branch'])
        self.tabs.setTabText(0, f'当前改动 ({len(self.changes)})')
        with QSignalBlocker(self.file_list), QSignalBlocker(self.history_list):
            self.file_list.clear()
            for change in self.changes:
                path = Path(change.path)
                location = path.parent.as_posix() if path.parent.as_posix() != '.' else '工程根目录'
                self._list_row(self.file_list, path.name,
                               _change_kind(change.status) + ' · ' + location, change.path)
            self.history_list.clear()
            selected_item = None
            for commit in data['commits']:
                title = '★ ' + commit.subject if commit.oid in self.favorites else commit.subject
                item = self._list_row(self.history_list, title,
                                      _display_time(commit.time) + '  ·  ' + commit.short, commit.oid)
                if commit.oid == data['selected']:
                    selected_item = item
            if selected_item is not None:
                self.history_list.setCurrentItem(selected_item)
        self._details_oid = data['selected']
        self._details_raw = data['details']
        self._mode_changed(self.tabs.currentIndex())
        self._update_controls()
        self.host.message(f'{len(self.changes)} 个文件有未提交改动' if self.changes else '工作区干净，当前内容已保存')

    def _mode_changed(self, index):
        self.lists.setCurrentIndex(index)
        history = index == 1
        self.composer.setVisible(not history)
        self.copy_button.setVisible(history)
        self.favorite_button.setVisible(history)
        self.restore_button.setVisible(history)
        self.list_summary.setText(f'最近 30 条及收藏 · {len(getattr(self, "favorites", ()))} / 10 已收藏'
                                  if history else f'{len(self.changes)} 个文件有变化')
        if history:
            oid = self.selected_oid()
            if oid == self._details_oid and oid in self.commits:
                self._show_commit(self.commits[oid], self._details_raw)
            elif oid is not None:
                self._history_selected()
            else:
                self.summary_title.setText('还没有提交记录')
                self.summary_meta.setText('完成第一次提交后，可以在这里查看历史和回档。')
                self.diff_view.setPlainText('')
        else:
            self.summary_title.setText('当前工作区的文件变化')
            self.summary_meta.setText(f'{len(self.changes)} 个文件 · 新增、修改和删除将一起保存'
                                      if self.changes else '当前内容已保存，没有未提交的改动。')
            self.diff_view.setPlainText(self.current_diff if self.changes else '工作区干净。修改角色后，点击“刷新”查看变化。')
        self._update_controls()

    def _file_selected(self, current=None, _previous=None):
        if self._busy or current is None or self.tabs.currentIndex() != 0:
            return
        path = current.data(Qt.ItemDataRole.UserRole)
        self.diff_view.moveCursor(QTextCursor.MoveOperation.Start)
        self.diff_view.find(path)

    def _history_selected(self, _current=None, _previous=None):
        if self._busy or self.repo is None or self.tabs.currentIndex() != 1:
            return
        oid = self.selected_oid()
        if oid is None:
            return
        self._update_controls()
        commit = self.commits[oid]

        def done(raw):
            self._details_oid, self._details_raw = oid, raw
            self._show_commit(commit, raw)
            self.host.message('所选版本：' + commit.short)

        return self.host.run('读取版本详情', lambda: self.repo.details(oid), done)

    def _show_commit(self, commit, raw):
        self.summary_title.setText(commit.subject)
        self.summary_meta.setText(f'{_display_time(commit.time)}  ·  {commit.author}  ·  {commit.short}')
        self.summary_meta.setToolTip(commit.oid)
        self.diff_view.setPlainText(_readable_details(raw))

    def copy_oid(self):
        oid = self.selected_oid()
        if oid and not self._busy:
            QApplication.clipboard().setText(oid)
            self.host.message('已复制完整提交号码')

    def _after_write(self, result, selected):
        # Snapshot failure must not disguise an already successful write.
        try:
            snapshot = self._snapshot(self.repo, selected)
        except Exception as error:
            return result, None, str(error)
        return result, snapshot, None

    def commit_all(self):
        if self._busy or self.repo is None:
            return False
        message = self.message_edit.toPlainText().strip()
        if not message or not self.changes:
            return False

        def work():
            oid = self.repo.commit(message)
            return self._after_write(oid, oid)

        def done(data):
            oid, snapshot, refresh_error = data
            self.message_edit.clear()
            if snapshot is not None:
                self._render_snapshot(snapshot)
            self.host.message('已保存版本：' + oid[:12])
            if refresh_error:
                self._warning('提交已完成', f'新提交：{oid}\n\n刷新界面失败，请点击刷新：\n{refresh_error}')

        return self.host.run('提交全部改动', work, done, write=True)

    def preview_restore(self):
        oid = self.selected_oid()
        if self._busy or self.repo is None or oid is None:
            return False
        commit = self.commits[oid]

        def done(data):
            preview, changes = data
            if self._restore_dialog(commit, preview, changes):
                self.restore_to(commit.oid)

        return self.host.run('读取回档预览',
                             lambda: (self.repo.restore_preview(oid), self.repo.status()), done)

    def _restore_dialog(self, commit, preview, changes):
        dialog = QDialog(self.host)
        dialog.setWindowTitle('确认回档')
        dialog.resize(860, 620)
        screen = dialog.screen().availableGeometry()
        dialog.resize(min(860, screen.width() - 60), min(620, screen.height() - 60))
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        title = _plain_label('恢复到：' + commit.subject, 'section')
        title.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(_plain_label('提交号码：' + commit.oid, 'muted'))
        explanation = _plain_label(
            '将源码、图片、声音和文档恢复为此版本，并新建一条回档提交。\n'
            '原有历史和当前工具箱继续保留；较新版本中的角色修复也会随之回档。')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        note = (f'当前有 {len(changes)} 项未提交改动，执行前会先自动建立备份提交。'
                if changes else '当前没有未提交改动；确认后仍会重新检查。')
        note_widget = _plain_label(note, 'muted')
        note_widget.setWordWrap(True)
        layout.addWidget(note_widget)
        view = DiffView()
        view.setPlainText(preview)
        layout.addWidget(view, 1)
        buttons = QDialogButtonBox()
        cancel = buttons.addButton('取消', QDialogButtonBox.ButtonRole.RejectRole)
        confirm = buttons.addButton('确认回档', QDialogButtonBox.ButtonRole.AcceptRole)
        confirm.setProperty('primary', True)
        cancel.setDefault(True)
        confirm.setAutoDefault(False)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def restore_to(self, oid):
        if self._busy or self.repo is None or oid not in self.commits:
            return False

        def work():
            result = self.repo.restore(oid)
            return self._after_write(result, result.oid)

        def done(data):
            result, snapshot, refresh_error = data
            if snapshot is not None:
                self._render_snapshot(snapshot)
            notice = ('回档完成，已保存为新版本：' + result.oid[:12]
                      if result.changed else '项目文件已经与所选版本一致。')
            if result.backup_oid:
                notice += '  ·  改动备份：' + result.backup_oid[:12]
            self.host.message(notice)
            if refresh_error:
                self._warning('回档已完成', notice + '\n\n刷新界面失败，请点击刷新：\n' + refresh_error)

        return self.host.run('备份并回档', work, done, write=True)

    def edit_identity(self):
        if self._busy or self.repo is None:
            return False
        dialog = QDialog(self.host)
        dialog.setWindowTitle('设置当前仓库署名')
        dialog.resize(440, 200)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        hint = _plain_label('只保存到当前仓库，不更改电脑的全局 Git 设置。', 'muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        name, email = QLineEdit(self.author[0]), QLineEdit(self.author[1])
        form.addRow('姓名', name)
        form.addRow('邮箱', email)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        buttons.addButton('取消', QDialogButtonBox.ButtonRole.RejectRole)
        save = buttons.addButton('保存署名', QDialogButtonBox.ButtonRole.AcceptRole)
        save.setProperty('primary', True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def valid():
            save.setEnabled(bool(name.text().strip() and email.text().strip()))

        name.textChanged.connect(valid)
        email.textChanged.connect(valid)
        valid()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        author_name, author_email = name.text().strip(), email.text().strip()

        def work():
            self.repo.set_identity(author_name, author_email)
            return self.repo.identity()

        def done(identity):
            self.author = identity
            self.host.message('当前仓库署名已保存：' + identity[0])

        return self.host.run('保存仓库署名', work, done, write=True)

    def _warning(self, title, text):
        notice = QMessageBox(self.host)
        notice.setIcon(QMessageBox.Icon.Warning)
        notice.setWindowTitle(title)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setText(text)
        notice.exec()


def main(argv=None):
    from character_toolbox import main as toolbox_main
    args = list(sys.argv[1:] if argv is None else argv)
    if not any(value == '--page' or value.startswith('--page=') for value in args):
        args[:0] = ['--page', 'git']
    return toolbox_main(args)


if __name__ == '__main__':
    raise SystemExit(main())
