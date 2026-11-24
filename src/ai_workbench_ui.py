"""Branch browser, source diff, and native test controls for existing AI."""
from pathlib import Path
import re

from PySide6.QtCore import QProcess, QSignalBlocker, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

from ai_workbench import AIDocument, AIError, CONDITION, numeric_atoms
from ai_workbench_graph import Branch, BranchGraph, Lane
from ai_workbench_runner import prepare_run
from toolbox_ui import DiffView, button, label


class AIPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.doc = None
        self.rule = None
        self.origin = None
        self.roots, self.route, self.levels = {}, [], []
        self.confirmed = {}
        self.sandbox = None
        self.process = QProcess(self)
        self.process.finished.connect(self.test_finished)
        self.process.errorOccurred.connect(self.test_error)
        main = QVBoxLayout(self)
        main.setContentsMargins(18, 12, 18, 12)
        top = QHBoxLayout()
        main.addLayout(top)
        self.category = QComboBox()
        self.category.addItems(('全部规则', '回合开场', '对峙 / 起手', '连段 / 接续', '大招', '手动 / 公用', '其他 / 待确认'))
        self.category.currentIndexChanged.connect(self.populate)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索分支名、状态号、招式或源码')
        self.search.textChanged.connect(self.populate)
        self.scan_button = self.action('重新扫描', self.rescan)
        self.dynamic_button = self.action('动态目标 (0)', self.show_dynamic_targets)
        self.dynamic_button.setToolTip('查看目标状态需在运行时确定的分支')
        for widget in (self.category, self.search, self.dynamic_button, self.scan_button):
            top.addWidget(widget, 1 if widget is self.search else 0)
        canvas_tools = QHBoxLayout()
        main.addLayout(canvas_tools)
        self.entries = QComboBox()
        self.entries.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.entries.setMinimumContentsLength(12)
        self.entries.setAccessibleName('链路入口')
        self.entries.currentIndexChanged.connect(self.select_origin)
        canvas_tools.addWidget(label('入口', 'section'))
        canvas_tools.addWidget(self.entries)
        self.back_button = self.action('退回一层', self.back)
        canvas_tools.addWidget(self.back_button)
        canvas_tools.addStretch()
        self.graph = BranchGraph()
        self.graph.branch_selected.connect(self.select_branch, Qt.ConnectionType.QueuedConnection)
        self.graph.entry_selected.connect(self.select_origin, Qt.ConnectionType.QueuedConnection)
        self.graph.state_name_requested.connect(self.edit_state_name, Qt.ConnectionType.QueuedConnection)
        self.zoom_level = self.action('100%', self.graph.reset_zoom)
        self.zoom_level.setToolTip('恢复原始缩放')
        self.graph.zoom_changed.connect(lambda percent: self.zoom_level.setText(f'{percent}%'))
        for widget in (self.action('−', lambda: self.graph.zoom(1/1.15)), self.zoom_level,
                       self.action('+', lambda: self.graph.zoom(1.15)),
                       self.action('适应画布', self.graph.fit_route)):
            canvas_tools.addWidget(widget)
        panes = QSplitter()
        main.addWidget(panes, 1)
        panes.addWidget(self.graph)
        self.detail = QWidget()
        panes.addWidget(self.detail)
        panes.setStretchFactor(0, 1)
        panes.setStretchFactor(1, 0)
        panes.setCollapsible(0, False)
        self.detail.setMinimumWidth(272)
        self.detail.setMaximumWidth(414)
        side = QVBoxLayout(self.detail)
        side.setContentsMargins(13, 8, 0, 2)
        heading = QHBoxLayout()
        side.addLayout(heading)
        heading.addWidget(label('分支规则', 'section'), 1)
        close = self.action('×', self.detail.hide)
        close.setAccessibleName('关闭分支规则')
        heading.addWidget(close)
        self.location = label('选择分支查看规则', 'section')
        self.location.setWordWrap(True)
        side.addWidget(self.location)
        tools = QHBoxLayout()
        side.addLayout(tools)
        self.source_button = self.action('在原文定位', self.open_source)
        self.name_button = self.action('命名此分支', self.edit_name)
        tools.addWidget(self.source_button)
        tools.addWidget(self.name_button)
        tools.addStretch()
        self.source = QPlainTextEdit()
        self.source.setReadOnly(True)
        side.addWidget(self.source, 1)
        self.fields = QListWidget()
        self.fields.setAccessibleName('可编辑条件')
        self.fields.setMaximumHeight(123)
        self.fields.currentItemChanged.connect(self.update_actions)
        self.fields.itemDoubleClicked.connect(lambda _item: self.edit())
        side.addWidget(self.fields)
        line = QHBoxLayout()
        side.addLayout(line)
        self.edit_button = self.action('修改所选项', self.edit)
        self.undo_button = self.action('撤销所选项', self.undo)
        self.confirm_button = self.action('确认是 AI 规则', self.confirm_rule)
        line.addWidget(self.edit_button)
        line.addWidget(self.undo_button)
        line.addWidget(self.confirm_button)
        line.addStretch()
        bottom = QHBoxLayout()
        main.addLayout(bottom)
        self.status = label('', 'muted')
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        self.test_button = self.action('原生试跑', self.configure_test)
        self.stop_button = self.action('结束试跑', self.stop_test)
        self.diff_button = self.action('预览差异 / 保存', self.preview, True)
        for widget in (self.test_button, self.stop_button, self.diff_button):
            bottom.addWidget(widget)
        self.detail.hide()
        self.host.busyChanged.connect(self.update_actions)
        self.update_actions()

    @staticmethod
    def action(text, fn, primary=False):
        widget = button(text, primary)
        widget.clicked.connect(fn)
        return widget

    def refresh(self):
        if self.doc is None:
            self.rescan()

    def rescan(self):
        if not self.confirm_draft():
            return
        root, definition = self.host.repo_path, self.host.definition
        self.host.run('扫描 DEF 加载的 AI 规则', lambda: AIDocument(root, definition), self.loaded)

    def loaded(self, doc):
        self.doc = doc
        self.dynamic_button.setText(f'动态目标 ({sum(rule.target is None for rule in doc.rules)})')
        self.confirmed.clear()
        self.populate()
        self.host.message(f'已扫描 {len(doc.raw)} 份源码 · {len(doc.rules)} 条分支规则')

    def confirm_draft(self):
        return (self.doc is None or not self.doc.dirty or
                QMessageBox.question(self, '草稿尚未保存', '丢弃本页 AI 草稿？',
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes)

    def confirm_leave(self):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(self, '测试运行中', '请先结束本页试跑。')
            return False
        if self.confirm_draft():
            if self.doc is not None:
                self.doc.clear()
                self.refresh_edge(None)
            return True
        return False

    def populate(self, *_):
        if self.doc is None:
            return
        previous = self.entries.currentData()
        self.roots.clear()
        query = self.search.text().strip().casefold()
        category = self.category.currentText()
        for rule in self.doc.rules:
            if category != '全部规则' and category != rule.category:
                continue
            origins = rule.sources or (None,)
            for origin in origins:
                branch = self.doc.branch_name(rule, origin)
                haystack = ' '.join((branch, self.doc.name(self.target(rule)), self.doc.name(origin),
                                     rule.block.section.name, rule.block.tag,
                                     *(value for _, value, _ in rule.block.triggers))).casefold()
                if query and query not in haystack:
                    continue
                key = origin if origin is not None else rule.category
                self.roots.setdefault(key, []).append(rule)
        self.entries.blockSignals(True)
        self.entries.clear()
        for key in self.roots:
            title = self.doc.name(key) if isinstance(key, int) else key
            self.entries.addItem(title, key)
        index = max(0, self.entries.findData(previous))
        self.entries.setCurrentIndex(index)
        self.entries.blockSignals(False)
        self.select_origin()

    def select_origin(self, *_):
        self.route.clear()
        self.rule = None
        self.origin = None
        self.detail.hide()
        self.render_graph(reset=True)
        self.show_rule()

    def target(self, rule):
        rule = self.confirmed.get(rule.selection, rule)
        for field, (key, _) in self.doc.fields(rule).items():
            if key == 'value' and field in self.doc.pending:
                return int(self.doc.pending[field])
        return rule.target

    def show_dynamic_targets(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('动态目标')
        dialog.resize(780, 470)
        layout = QVBoxLayout(dialog)
        rules = [rule for rule in self.doc.rules if rule.target is None]
        if not rules:
            layout.addWidget(label('当前角色没有目标待解析的分支。'))
            layout.addWidget(self.action('关闭', dialog.reject))
            dialog.exec()
            return
        text = label('这些分支的目标无法静态确定。选中一项可查看表达式和来源。', 'muted')
        text.setWordWrap(True)
        layout.addWidget(text)
        items = QListWidget()
        items.setAccessibleName('动态目标分支列表')
        layout.addWidget(items, 2)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        layout.addWidget(details, 1)
        for rule in rules:
            section = rule.destination.section
            origin = ' / '.join(map(str, rule.sources)) or rule.category
            expression = rule.target_expression or '未声明目标'
            title = f'{section.path}:{section.line}  ·  {origin}  →  {expression}'
            item = QListWidgetItem(title, items)
            item.setToolTip(title)

        def show(row):
            if row < 0:
                return
            rule = rules[row]
            section = rule.destination.section
            context = [f'{"满足" if condition else "否则"}：{expr}' for condition, expr in rule.tests]
            details.setPlainText('\n'.join([f'{section.path}:{section.line}',
                                            f'{rule.target_key} = {rule.target_expression or "未声明目标"}', *context]))

        items.currentRowChanged.connect(show)
        items.itemDoubleClicked.connect(lambda _item: dialog.accept())
        items.setCurrentRow(0)
        layout.addWidget(self.action('在画布定位', dialog.accept, True))
        if dialog.exec() == QDialog.DialogCode.Accepted and items.currentRow() >= 0:
            self.locate_rule(rules[items.currentRow()])

    def locate_rule(self, rule):
        self.search.clear()
        self.category.setCurrentText(rule.category)
        origin = rule.sources[0] if rule.sources else rule.category
        self.entries.setCurrentIndex(self.entries.findData(origin))
        if self.levels:
            _, candidates = self.levels[0]
            index = next((i for i, candidate in enumerate(candidates) if candidate.selection == rule.selection), None)
            if index is not None:
                self.select_branch(0, index)

    def branch(self, rule, origin, visited):
        path = rule.block.section.path
        target = self.target(rule)
        name = self.doc.branch_name(rule, origin)
        state = self.doc.name(target)
        controller = rule.block.section.name.partition(',')[2].strip()
        details = [(key, self.doc.line_value(path, line, value))
                   for key, value, line in self.doc.visible_triggers(rule, origin)]
        displayed = ' '.join(value for _, value in details)
        # A condition tag summarizes the source. Each numbered group remains visible in the rule pane.
        tests = ['命中后切换'] if rule.target_key == 'p1stateno' else []
        if rule.manual and rule.dependencies:
            tests.append('指令缓冲')
        for word, title in (('MoveHit', '实中'), ('MoveGuarded', '被防'), ('MoveContact', '接触')):
            match = re.search(rf'(?<!\w)(!?\s*){word}\b\s*(=?\s*0\b)?', displayed, re.I)
            if match:
                tests.append(('未' if '!' in match[1] or match[2] else '') + title)
        numbers = []
        for _, value in details:
            for _, _, atom in numeric_atoms(value):
                atom = atom.strip().lower()
                for term, title in (('time', '状态时间'), ('power', '能量'), ('p2bodydist x', '横距'), ('random', '随机值')):
                    if atom.startswith(term):
                        numbers.append(f'{title} {atom[len(term):].strip()}')
                        break
        has_followup = target is not None and target not in visited and bool(self.doc.by_source.get(target))
        title = name or state
        subtitle = state if name else controller
        if target is None or target in visited:
            kind = '动态目标' if target is None else '回到已走过的状态'
            subtitle = f'{state} · {kind}' if name else kind
        arms = [f'{"满足" if yes else "否则"}：{value}' for yes, value in rule.tests]
        full = '\n'.join([f'{path}:{rule.block.section.line}', *arms,
                         *([f'{rule.target_key} = {rule.target_expression}'] if target is None else []),
                         *(f'变量写入候选 {writer.section.path}:{writer.section.line}' for writer in rule.provenance[:5]),
                         *(f'指令来源 {writer.section.path}:{writer.section.line}' for writer in rule.dependencies[:5]),
                         *(f'{key} = {value[:320]}' for key, value in details[:12])])
        tag = arms[-1] if arms else ' · '.join((tests + numbers)[:2])
        if rule.inferred_origin:
            tag = f'变量候选 · {tag}' if tag else '变量候选'
        fallback = '触发条件' if details else '直接切换'
        state_id = target if target in self.doc.state_locations else None
        return Branch(title, subtitle or rule.evidence, tag or fallback, full, has_followup, state_id)

    def render_graph(self, *, reset=False, focus=False):
        key = self.entries.currentData()
        origin = key if isinstance(key, int) else None
        choices = self.roots.get(key, [])
        self.levels = []
        lanes = []
        visited = {origin}
        reveal = None
        while choices:
            depth = len(lanes)
            selected = self.route[depth] if depth < len(self.route) else None
            index = next((i for i, rule in enumerate(choices) if rule.selection == selected), None)
            self.levels.append((origin, choices))
            lanes.append(Lane(tuple(self.branch(rule, origin, visited) for rule in choices), index))
            if index is None:
                break
            reveal = (depth, index)
            origin = self.target(choices[index])
            if origin is None or origin in visited:
                break
            visited.add(origin)
            choices = [rule for rule in self.doc.by_source.get(origin, []) if not rule.manual or choices[index].manual]
        root = self.entries.currentText()
        root_state = key if self.doc and key in self.doc.state_locations else None
        self.graph.set_route(root, '链路入口', lanes, reset=reset, reveal=reveal if focus else None, root_state=root_state)

    def edit_state_name(self, state):
        if self.doc is None or self.host.busy:
            return
        locations = self.doc.state_locations.get(state, ())
        if not locations:
            return
        location = locations[0]
        if len(locations) > 1:
            options = {f'{path}:{line} · {self.doc.state_name_at((path, line)) or "未命名"}': (path, line)
                       for path, line in locations}
            chosen, ok = QInputDialog.getItem(self, f'状态 {state} 有多处定义', '选择要命名的定义',
                                              list(options), 0, False)
            if not ok:
                return
            location = options[chosen]
        path, line = location
        title, ok = QInputDialog.getText(self, f'命名状态 {state}', f'{path}:{line}\n状态名（留空删除）',
                                         text=self.doc.state_name_at(location))
        if not ok:
            return
        try:
            self.doc.rename_state(state, location, title)
        except AIError as error:
            QMessageBox.warning(self, '无法命名', str(error))
            return
        with QSignalBlocker(self.entries):
            index = self.entries.findData(state)
            if index >= 0:
                self.entries.setItemText(index, self.doc.name(state))
        if self.rule is None:
            self.render_graph()
            self.update_actions()
        else:
            self.refresh_edge(self.selected_field())

    def select_branch(self, depth, index):
        if self.host.busy or not 0 <= depth < len(self.levels):
            return
        origin, choices = self.levels[depth]
        if not 0 <= index < len(choices):
            return
        rule = choices[index]
        self.route[depth:] = [rule.selection]
        self.origin = origin
        self.rule = self.confirmed.get(rule.selection, rule)
        self.detail.show()
        self.show_rule()
        self.render_graph(focus=True)

    def back(self):
        if len(self.route) <= 1:
            self.select_origin()
            return
        _, choices = self.levels[len(self.route)-2]
        selected = self.route[-2]
        index = next(i for i, rule in enumerate(choices) if rule.selection == selected)
        self.select_branch(len(self.route)-2, index)

    def selected_field(self):
        item = self.fields.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def show_rule(self):
        self.fields.clear()
        if self.rule is None:
            self.location.setText('选择分支查看规则')
            self.source.clear()
            self.update_actions()
            return
        rule = self.rule
        self.location.setText(f'{rule.evidence} · 代码顺序 {rule.block.order}\n'
                              f'{rule.block.section.path}:{rule.block.section.line}' + (
                                  '\n动态目标 · 由引擎运行时确定' if rule.target is None and rule.target_expression else
                                  '\n目标未声明' if rule.target is None else ''))
        code = []
        if rule.tests:
            code.extend([*(f'; {"满足" if yes else "否则"}：{value}' for yes, value in rule.tests), ''])
        if rule.provenance:
            code.extend([*(f'; 变量写入候选 {writer.section.path}:{writer.section.line}'
                           for writer in rule.provenance), ''])
        if rule.dependencies:
            code.extend([*(f'; 指令来源 {writer.section.path}:{writer.section.line}'
                           for writer in rule.dependencies), ''])
        active = {line for _, _, line in self.doc.visible_triggers(rule, self.origin)}
        for block in (rule.block,) if rule.block is rule.destination else (rule.block, rule.destination):
            code.append(f'[{block.section.name}]  ; {block.section.path}:{block.section.line}')
            code.extend(f'{key} = {self.doc.line_value(block.section.path, line, value)}' + (
                        '  ; 其他来源状态' if block is rule.block and key.startswith('trigger') and line not in active else '')
                        for key, value, line in block.section.entries)
            code.append('')
        self.source.setPlainText('\n'.join(code))
        for field, (key, value) in self.doc.fields(rule).items():
            current = self.doc.pending.get(field, value)
            title = f'目标招式 · {self.doc.name(int(current))}' if key == 'value' else f'{key} · {current}'
            item = QListWidgetItem(title, self.fields)
            item.setData(Qt.ItemDataRole.UserRole, field)
            item.setToolTip(title)
        if not self.fields.count():
            self.fields.addItem('这条规则供查看')
        self.fields.setCurrentRow(0)
        self.update_actions()

    def open_source(self):
        blocks = [self.rule.block]
        if self.rule.block is not self.rule.destination:
            blocks.append(self.rule.destination)
        blocks.extend(self.rule.provenance)
        blocks.extend(self.rule.dependencies)
        popup = QDialog(self)
        popup.setWindowTitle('定位 AI 原文')
        popup.resize(900, 610)
        layout = QVBoxLayout(popup)
        locations = QComboBox()
        view = QPlainTextEdit()
        view.setReadOnly(True)
        for block in blocks:
            locations.addItem(f'{block.section.path}:{block.section.line}')

        def point(index):
            section = blocks[index].section
            view.setPlainText(self.doc.text[section.path])
            cursor = QTextCursor(view.document().findBlockByNumber(section.line - 1))
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            view.setTextCursor(cursor)
            view.centerCursor()

        locations.currentIndexChanged.connect(point)
        layout.addWidget(locations)
        layout.addWidget(view, 1)
        point(0)
        popup.exec()

    def update_actions(self, *_):
        ready = self.doc is not None and not self.host.busy
        running = self.process.state() != QProcess.ProcessState.NotRunning
        field = self.selected_field()
        self.edit_button.setEnabled(ready and field is not None)
        self.undo_button.setEnabled(ready and field is not None and field in self.doc.pending)
        self.source_button.setEnabled(ready and self.rule is not None)
        self.name_button.setEnabled(ready and self.rule is not None)
        self.back_button.setEnabled(ready and bool(self.route))
        self.scan_button.setEnabled(not self.host.busy)
        self.dynamic_button.setEnabled(ready)
        can_confirm = (ready and self.rule is not None and self.rule.block.state < 0 and
                       '待确认' in self.rule.evidence)
        self.confirm_button.setVisible(can_confirm)
        self.diff_button.setEnabled(ready and self.doc.dirty)
        self.test_button.setEnabled(ready and not running)
        self.stop_button.setEnabled(running)
        if ready:
            self.status.setText(f'{len(self.doc.rules)} 条规则 · {self.doc.change_count} 处草稿')

    def edit(self):
        field = self.selected_field()
        if self.rule is None or field is None:
            return
        key, old = self.doc.fields(self.rule)[field]
        current = self.doc.pending.get(field, old)
        if key == 'value':
            options = {self.doc.name(n): n for n in sorted(self.doc.states)
                       if n >= 0 and n not in self.doc.duplicates}
            labels = list(options)
            picked, ok = QInputDialog.getItem(self, '替换目标招式', '角色已有且唯一定义的状态',
                                              labels, labels.index(self.doc.name(int(current))), True)
            value = options.get(picked)
            if ok and value is None:
                QMessageBox.warning(self, '招式无效', '请选择列表中的现有状态。')
                return
        else:
            match = CONDITION.fullmatch(current)
            value, ok = QInputDialog.getInt(self, '修改数值', match[1].strip(), int(match[2]), -999999, 999999)
        if ok:
            try:
                self.doc.stage(self.rule, field, value)
            except AIError as error:
                QMessageBox.warning(self, '无法暂存', str(error))
                return
            self.refresh_edge(field)

    def undo(self):
        field = self.selected_field()
        self.doc.pending.pop(field, None)
        self.refresh_edge(field)

    def confirm_rule(self):
        if QMessageBox.question(self, '确认 AI 归属', '确认这条规则由角色 AI 使用，可以编辑？',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            confirmed = self.doc.confirm_rule(self.rule)
            self.confirmed[confirmed.selection] = confirmed
            self.refresh_edge(None)

    def edit_name(self):
        previous = self.doc.branch_name(self.rule, self.origin)
        name, accepted = QInputDialog.getText(self, '命名此分支', '分支名称（留空可移除）', text=previous)
        if accepted:
            try:
                self.doc.name_branch(self.rule, self.origin, name)
            except AIError as error:
                QMessageBox.warning(self, '无法命名', str(error))
                return
            self.refresh_edge(None)

    def refresh_edge(self, field):
        if self.rule is None:
            self.update_actions()
            return
        self.rule = self.confirmed.get(self.rule.selection, self.rule)
        self.render_graph()
        self.show_rule()
        if field is not None:
            for row in range(self.fields.count()):
                if self.fields.item(row).data(Qt.ItemDataRole.UserRole) == field:
                    self.fields.setCurrentRow(row)
                    break

    def preview(self):
        try:
            text = self.doc.diff()
        except AIError as error:
            QMessageBox.warning(self, '无法预览', str(error))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('核对 AI 源码差异')
        dialog.resize(900, 580)
        layout = QVBoxLayout(dialog)
        diff = DiffView()
        diff.setPlainText(text)
        layout.addWidget(diff, 1)
        layout.addWidget(self.action('保存这些修改', dialog.accept, True))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        doc = self.doc

        def save():
            with self.host.operation_lock():
                backup = doc.save()
            return backup, AIDocument(doc.root, doc.character.definition)

        def done(result):
            backup, updated = result
            self.loaded(updated)
            QMessageBox.information(self, 'AI 已保存', '原件备份\n' + str(backup))

        self.host.run('备份并保存 AI 修改', save, done, write=True)

    def configure_test(self):
        popup = QDialog(self)
        popup.setWindowTitle('原生 MUGEN 试跑')
        form = QFormLayout(popup)
        engine = QLineEdit()
        stage = QLineEdit()
        engines = QComboBox()
        engines.addItems(('MUGEN 1.1', 'MUGEN 1.0'))
        form.addRow('版本', engines)

        def version(_index=None):
            folder = Path('D:/1mugen1.1' if engines.currentIndex() == 0 else 'D:/全N格斗2/mugen1.0(格斗2020版)')
            exe = folder / 'mugen.exe'
            default_stage = Path('D:/1mugen1.1/stages/DonjonTX.def')
            engine.setText(str(exe) if exe.is_file() else '')
            stage.setText(str(default_stage) if default_stage.is_file() else '')

        for title, field, file_filter in (('引擎', engine, 'EXE (*.exe)'), ('舞台', stage, 'DEF (*.def)')):
            line = QHBoxLayout()
            line.addWidget(field, 1)

            def browse(field=field, file_filter=file_filter):
                chosen, _ = QFileDialog.getOpenFileName(popup, '选择文件', field.text(), file_filter)
                if chosen:
                    field.setText(chosen)

            line.addWidget(self.action('选择', browse))
            form.addRow(title, line)
        engines.currentIndexChanged.connect(version)
        version()
        posture = QComboBox()
        for name, data in (('站立靶', 'stand'), ('蹲姿靶', 'crouch'), ('站防靶', 'guard'),
                           ('蹲防靶', 'low_guard'), ('周期起跳靶', 'jump'), ('镜像 AI', 'mirror')):
            posture.addItem(name, data)
        form.addRow('对手', posture)
        power, distance = QSpinBox(), QSpinBox()
        power.setRange(0, 99999)
        power.setValue(3000)
        distance.setRange(0, 1000)
        distance.setValue(50)
        form.addRow('初始能量', power)
        form.addRow('起始距离', distance)
        mode = QComboBox()
        mode.addItem('AI 自主选招', None)
        target = self.target(self.rule) if self.rule else None
        if target is not None and target in self.doc.states and target >= 0 and target not in self.doc.duplicates:
            mode.addItem('强制试招 · ' + self.doc.name(target), target)
        form.addRow('模式', mode)
        hint = QLabel('使用当前草稿与独立运行副本。白字显示状态、计时、命中、防御及能量。')
        hint.setWordWrap(True)
        form.addRow(hint)
        form.addRow(self.action('启动测试', popup.accept, True))
        if popup.exec() != QDialog.DialogCode.Accepted:
            return
        doc = self.doc
        args = engine.text(), stage.text(), posture.currentData(), power.value(), distance.value(), mode.currentData()
        self.host.run('准备原生测试副本', lambda: prepare_run(doc, *args), self.launch)

    def launch(self, run):
        self.sandbox = run.root
        self.process.setWorkingDirectory(str(run.root))
        self.process.setProgram(str(run.root / 'mugen.exe'))
        self.process.setArguments(list(run.args))
        self.process.setStandardOutputFile(str(run.root / 'stdout.txt'))
        self.process.setStandardErrorFile(str(run.root / 'stderr.txt'))
        self.process.start()
        self.host.message('原生测试已启动 · ' + str(run.root))
        self.update_actions()

    def stop_test(self):
        self.process.kill()

    def test_finished(self, code, _status):
        self.host.message(f'测试结束（退出码 {code}） · {self.sandbox}')
        self.update_actions()

    def test_error(self, _error):
        self.host.message('测试进程 · ' + self.process.errorString())
        self.update_actions()
