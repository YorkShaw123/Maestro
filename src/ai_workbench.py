"""Read-only AI graph and narrow byte-preserving edits for resolved rules."""
from collections import defaultdict
from dataclasses import dataclass, replace
from difflib import unified_diff
from decimal import Decimal
import json
from pathlib import Path
import os
import re
from uuid import uuid4

from inspect_ai_moves import source_sections
from ai_workbench_expression import (AI_COMMAND, AI_LEVEL, NUM, boolean_atoms, guarantees, number, positive_variable,
                                     state_groups, trigger_groups, trigger_guarantees, variants)
from mugen_character import local_path, read_character


STATEDEF = re.compile(r'Statedef\s+(' + NUM + r')', re.I)
CONDITION = re.compile(
    r'(\s*(?:time|movehit|moveguarded|movecontact|animelemtime\s*\(\s*[0-9]+\s*\)|'
    r'power|p2bodydist\s+[xy]|p2dist\s+[xy]|random|roundno)\s*(?:<=|>=|!=|=|<|>)\s*)(' + NUM +
    r')(\s*)', re.I)
ASSIGN = re.compile(r'\bvar\s*\(\s*(\d+)\s*\)\s*:=\s*(' + NUM + r')\b', re.I)
CONSUME = re.compile(r'\s*var\s*\(\s*(\d+)\s*\)\s*=\s*(' + NUM + r')\s*', re.I)
KEY_LINE = re.compile(r'([ \t]*[A-Za-z][\w.]*[ \t]*=[ \t]*)([^;\r\n]*?)([ \t]*(?:;[^\r\n]*)?(?:\r\n|\n|\r)?)')
BRANCH = re.compile(r'\s*;\s*@ai-branch\s+(entry|' + NUM + r')(?:\.(\d+))?\s+(.+?)\s*')
STATE_NAME = re.compile(r'\s*;\s*@toolbox-state-name(?:\s+(.*))?')
MANUAL_AI = re.compile(r'(?:!\s*AILevel|AILevel\s*=\s*0)', re.I)
LOCAL_VAR = re.compile(r'var\s*\(\s*(\d+)\s*\)', re.I)
HELPER_VAR = re.compile(r'Helper\s*\(\s*(\d+)\s*\)\s*,\s*var\s*\(\s*(\d+)\s*\)', re.I)
HELPER_GATE = re.compile(r'Helper\s*\([^)]*\)\s*,\s*var\s*\(\s*(\d+)\s*\)(?:\s*(?:>\s*0|>=\s*1|!=\s*0))?', re.I)
BUFFER_TEST = re.compile(HELPER_VAR.pattern + r'\s*>\s*0', re.I)
COMMAND = re.compile(r'command\s*=\s*"[^"]+"', re.I)
PHASE_REGISTER = re.compile(r'(f?var)\s*\(\s*(\d+)\s*\)', re.I)
PHASE_TEST = re.compile(PHASE_REGISTER.pattern + r'\s*=\s*([0-9]+(?:\.[0-9]+)?)', re.I)
NUMBER_VALUE = re.compile(r'[0-9]+(?:\.[0-9]+)?')


class AIError(ValueError):
    pass


def numeric_atoms(value):
    """Expose only standalone comparisons, preserving enclosing boolean logic."""
    if '"' in value or ':=' in value:
        return
    depth = start = 0
    for token in re.finditer(r'&&|\|\||[()]|$', value):
        word = token[0]
        if word == '(':
            depth += 1
        elif word == ')':
            depth -= 1
        elif depth == 0:
            end = token.start()
            part = value[start:end]
            if CONDITION.fullmatch(part):
                yield start, end, part
            start = token.end()


def source_text(raw):
    encodings = ('utf-16', 'utf-16-be') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else (
        'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8', 'cp936', 'cp932', 'latin1')
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            if text.encode(encoding) == raw:
                return text, encoding
        except UnicodeError:
            continue
    raise AIError('无法无损读取源码的文本编码。')


@dataclass(frozen=True)
class Block:
    state: int
    order: int
    section: object
    tag: str

    @property
    def triggers(self):
        return [(key, value, line) for key, value, line in self.section.entries
                if key == 'triggerall' or re.fullmatch(r'trigger[0-9]+', key)]


@dataclass(frozen=True)
class Rule:
    block: Block
    destination: Block
    target: int | None
    sources: tuple[int, ...]
    category: str
    evidence: str
    editable: bool
    retarget: bool
    arm: int = 0
    tests: tuple = ()
    manual: bool = False
    target_key: str = 'value'
    provenance: tuple[Block, ...] = ()
    dependencies: tuple[Block, ...] = ()
    inferred_origin: bool = False

    @property
    def key(self):
        return self.block.section.path, self.block.section.line

    @property
    def selection(self):
        return (*self.key, self.arm, self.target_key)

    @property
    def target_expression(self):
        return self.destination.section.get(self.target_key, '')


def variable_writes(section):
    writes = [(number(section.get('v')), section.get('value'))]
    writes.extend((int(match[1]), value) for key, value, _ in section.entries
                  if (match := LOCAL_VAR.fullmatch(key)))
    return [(slot, value) for slot, value in writes if slot is not None and value is not None]


def legacy_ai(blocks):
    """Anchor old CPU switches in activation code. Resolve literal-ID Helper copies as candidates."""
    root, inferred = set(), set()
    local, helpers, copies, writes_by_state = defaultdict(set), defaultdict(set), [], defaultdict(list)
    for block in blocks:
        section = block.section
        kind = section.get('type', '').casefold()
        if kind == 'helper':
            helper_id, state = number(section.get('id')), number(section.get('stateno'))
            if helper_id is not None and state is not None:
                helpers[helper_id].add(state)
        if kind != 'varset':
            continue
        writes = variable_writes(section)
        cpu = lambda atom: bool(AI_COMMAND.fullmatch(atom))
        common, alternatives = trigger_groups(block.triggers)
        groups = [common, *alternatives]
        dedicated = trigger_guarantees(block.triggers, cpu)
        cpu_activation = any(guarantees(value, cpu) for group in groups for _, value, _ in group)
        for var, value in writes:
            if block.state >= 0 and number(value) != 0:
                writes_by_state[block.state, var].append(block)
            def helper_activation(atom):
                match = HELPER_GATE.fullmatch(atom)
                return bool(match and int(match[1]) == var)

            if (number(value) or 0) > 0 and block.state < 0 and cpu_activation and (
                    dedicated or trigger_guarantees(block.triggers, lambda atom: cpu(atom) or helper_activation(atom))):
                root.add(var)
            elif (number(value) or 0) != 0 and block.state >= 0 and cpu_activation:
                local[block.state].add(var)
            match = HELPER_VAR.fullmatch(value)
            if block.state < 0 and match:
                copies.append((var, int(match[1]), int(match[2])))
    for var, helper, source in copies:
        states = helpers[helper]
        if len(states) == 1 and source in local[next(iter(states))]:
            inferred.add(var)
    buffers = {}
    ids_by_state = defaultdict(list)
    for helper, states in helpers.items():
        if len(states) == 1:
            ids_by_state[next(iter(states))].append(helper)
    for (state, slot), writers in writes_by_state.items():
        if state in ids_by_state and all(trigger_guarantees(
                writer.triggers, lambda atom: bool(COMMAND.fullmatch(atom) and not AI_COMMAND.fullmatch(atom)))
                for writer in writers):
            for helper in ids_by_state[state]:
                buffers[helper, slot] = tuple(writers)
    return root, inferred, buffers


class AIDocument:
    def __init__(self, root, definition=None):
        self.character = read_character(root, definition)
        self.root = self.character.root
        self.def_bytes = local_path(self.root, self.character.definition).read_bytes()
        self.raw, self.text, self.encoding = {}, {}, {}
        self.branch_names, self.pending_names = {}, {}
        self.state_locations, self.state_names, self.pending_state_names = defaultdict(list), {}, {}
        self.states, self.hyper = {}, set()
        duplicates, blocks = set(), []
        for name in self.character.files:
            if Path(name).suffix.casefold() in ('.def', '.air', '.sff', '.snd', '.act', '.pal'):
                continue
            raw = local_path(self.root, name).read_bytes()
            text, encoding = source_text(raw)
            sections = source_sections(text.lstrip('\ufeff'), name)
            if not any(STATEDEF.fullmatch(s.name) for s in sections):
                continue
            self.raw[name], self.text[name], self.encoding[name] = raw, text, encoding
            state = None
            lines = text.splitlines()
            for section in sections:
                match = STATEDEF.fullmatch(section.name)
                if match:
                    state = int(match[1])
                    if state in self.states and state >= 0:
                        duplicates.add(state)
                    _, _, comment = lines[section.line-1].partition(';')
                    self.states.setdefault(state, comment.strip())
                    if state >= 0:
                        location = (name, section.line)
                        self.state_locations[state].append(location)
                        marks, title = [], ''
                        for index in range(section.line-2, -1, -1):
                            mark = STATE_NAME.fullmatch(lines[index].lstrip('\ufeff'))
                            if mark is None:
                                break
                            if not marks:
                                title = (mark[1] or '').strip()
                            marks.append(index + 1)
                        if marks:
                            self.state_names[location] = title, tuple(marks)
                    cost = number(section.get('poweradd'))
                    if cost is not None and cost <= -1000:
                        self.hyper.add(state)
                elif state is not None and re.match(r'^State\s', section.name, re.I):
                    tag = ''
                    for index in range(section.line, len(lines)):
                        line = lines[index].lstrip()
                        if not line.startswith(';'):
                            break
                        role = re.match(r';\s*@role\s+(.+)', line)
                        if role:
                            tag = role[1]
                        branch = BRANCH.fullmatch(line)
                        if branch:
                            try:
                                title = json.loads(branch[3])
                            except ValueError:
                                continue
                            if isinstance(title, str):
                                name_key = (name, section.line, number(branch[1]), int(branch[2] or 0))
                                self.branch_names.setdefault(name_key, (title, index+1))
                    block = Block(state, len(blocks)+1, section, tag)
                    blocks.append(block)
                    if section.get('type', '').casefold() == 'superpause':
                        self.hyper.add(state)
                    if section.get('type', '').casefold() == 'poweradd' and (number(section.get('value')) or 0) <= -1000:
                        self.hyper.add(state)
        self.duplicates = duplicates
        self.blocks = blocks
        self._source_groups = {}
        self.legacy_vars, self.helper_vars, self.input_buffers = legacy_ai(blocks)
        self.phase_writers = self.index_phase_writers(blocks)
        self.rules = self._rules(blocks)
        self.by_source = defaultdict(list)
        for rule in self.rules:
            for source in rule.sources:
                self.by_source[source].append(rule)
        self.pending = {}

    @property
    def dirty(self):
        return bool(self.change_count)

    @property
    def change_count(self):
        return len(self.pending) + len(self.pending_names) + len(self.pending_state_names)

    def clear(self):
        self.pending.clear()
        self.pending_names.clear()
        self.pending_state_names.clear()

    def state_name_at(self, location):
        return self.pending_state_names.get(location, self.state_names.get(location, ('', ()))[0])

    def rename_state(self, state, location, title):
        if location not in self.state_locations.get(state, ()):
            raise AIError('请选择此状态在源码中的定义位置。')
        if len(title) > 80 or any(not char.isprintable() for char in title):
            raise AIError('状态名须为一行，最多 80 个字符。')
        title = title.strip()
        try:
            title.encode(self.encoding[location[0]])
        except UnicodeEncodeError as error:
            raise AIError('当前源码编码无法保存该名称，请换成能用该编码表示的文字。') from error
        if title == self.state_names.get(location, ('', ()))[0]:
            self.pending_state_names.pop(location, None)
        else:
            self.pending_state_names[location] = title

    def branch_name(self, rule, origin):
        key = (*rule.key, origin, rule.arm)
        return self.pending_names.get(key, self.branch_names.get(key, ('', None))[0])

    def name_branch(self, rule, origin, title):
        if origin not in (rule.sources or (None,)):
            raise AIError('分支起点不属于此规则。')
        if len(title) > 80 or any(not char.isprintable() for char in title):
            raise AIError('分支名须为一行，最多 80 个字符。')
        title = title.strip()
        key = (*rule.key, origin, rule.arm)
        if title == self.branch_names.get(key, ('', None))[0]:
            self.pending_names.pop(key, None)
        else:
            self.pending_names[key] = title

    def _rules(self, blocks):
        jumps = [block for block in blocks if block.section.get('type', '').casefold() in ('changestate','selfstate')]
        jump_orders = {block.order for block in jumps}
        consumers = defaultdict(list)
        for block in jumps:
            for key, value, _ in block.triggers:
                match = CONSUME.fullmatch(value)
                if match and key == 'triggerall':
                    consumers[(block.section.path, block.state, *map(int, match.groups()))].append(block)
        linked, rules = set(), []
        for block in blocks:
            if block.order in jump_orders or block.state >= 0:
                continue
            assignments = {tuple(map(int, match.groups())) for _, value, _ in block.triggers
                           for match in ASSIGN.finditer(value)}
            destinations = consumers.get((block.section.path, block.state, *next(iter(assignments)))) if len(assignments) == 1 else None
            if not destinations or len(destinations) != 1:
                continue
            destination = destinations[0]
            linked.add(destination.order)
            evidence = '候选联动 · 已标注 AI' if block.tag else '候选联动 · 人工确认'
            if block.tag and re.search(r'\bpropose\b', block.tag, re.I):
                rules.extend(self._rule(block, destination, evidence, True, False))
            else:
                rules.extend(self._rule(block, destination, evidence, False, False))
        for block in jumps:
            if block.order in linked:
                continue
            explicit = trigger_guarantees(block.triggers, lambda atom: bool(AI_LEVEL.fullmatch(atom)))
            legacy = block.state < 0 and trigger_guarantees(block.triggers, lambda atom: positive_variable(atom, self.legacy_vars))
            helper = block.state < 0 and trigger_guarantees(
                block.triggers, lambda atom: positive_variable(atom, self.helper_vars, allow_nonzero=False))
            role = bool(re.search(r'\b(?:N[0-9]+|AI)\b', block.tag, re.I))
            dependencies = self.buffer_dependencies(block)
            manual = block.state < 0 and (trigger_guarantees(block.triggers, self.manual_gate) or
                       not (explicit or legacy or helper or role) and trigger_guarantees(
                           block.triggers, lambda atom: bool(COMMAND.fullmatch(atom) and not AI_COMMAND.fullmatch(atom)
                                                            or self.buffer_gate(atom))))
            evidence = ('指令缓冲 / Helper' if manual and dependencies else '指令输入' if manual else
                        'AI 条件' if explicit else '源码标注 AI' if role else
                        '旧式 AI 开关' if legacy else 'Helper AI 开关待确认' if helper else
                        '变量 / Helper 待确认' if block.state < 0 and any(
                            re.search(r'\b(?:var|fvar|helper)\s*\(', value, re.I) for _, value, _ in block.triggers)
                        else '来源待确认' if block.state < 0 else '招式内部状态切换')
            editable = (explicit or role or legacy) and not manual
            rules.extend(self._rule(block, block, evidence, editable, editable, manual=manual,
                                    dependencies=dependencies))
        for block in blocks:
            if (block.state >= 0 and block.section.get('type', '').casefold() in ('hitdef', 'projectile')
                    and block.section.get('p1stateno') is not None):
                rules.extend(self._rule(block, block, '命中后自身转状态', False, False, target_key='p1stateno'))
        return sorted(rules, key=lambda rule: rule.block.order)

    def manual_gate(self, atom):
        if MANUAL_AI.fullmatch(atom):
            return True
        match = re.fullmatch(r'!\s*var\(\s*(\d+)\s*\)|var\(\s*(\d+)\s*\)\s*=\s*0', atom, re.I)
        return bool(match and int(match[1] or match[2]) in self.legacy_vars)

    def buffer_gate(self, atom):
        match = BUFFER_TEST.fullmatch(atom)
        return self.input_buffers.get(tuple(map(int, match.groups())), ()) if match else ()

    def buffer_dependencies(self, block):
        if block.state >= 0 or not self.input_buffers:
            return ()
        return tuple({writer.order: writer for _, value, _ in block.triggers for atom in boolean_atoms(value)
                      for writer in self.buffer_gate(atom)}.values())

    def _rule(self, block, destination, evidence, editable, retarget, *, manual=False, target_key='value', dependencies=()):
        expression = destination.section.get(target_key)
        targets = variants(expression, source=destination.state if destination.state >= 0 else None)
        text = ' '.join(value for _, value, _ in block.triggers)
        if block.state < 0:
            _, groups = self.source_groups(block)
            found = tuple(sorted(set().union(*(facts.anchors for facts, _ in groups))))
        else:
            found = (block.state,)
        provenance = self.phase_origins(block) if not manual and block.state < 0 else ()
        inferred_origin = not found and bool(provenance)
        if inferred_origin:
            found = tuple(sorted({state for writer in provenance for state in self.anchors(writer)}))
            evidence += ' · 变量来源候选'
        for index, (target, tests) in enumerate(targets, 1):
            if manual:
                category = '手动 / 公用'
            elif target in self.hyper:
                category = '大招'
            elif editable and re.search(r'\b(?:RoundNo|RoundsExisted)\b', text, re.I):
                category = '回合开场'
            elif target_key == 'p1stateno' or set(found) - {0, 10, 11, 12, 20, 21, 100, 101} or re.search(
                    r'\b(?:MoveHit|MoveContact|MoveGuarded|PrevStateNo)\b', text, re.I):
                category = '连段 / 接续'
            elif editable or 'AI 开关' in evidence:
                category = '对峙 / 起手'
            else:
                category = '其他 / 待确认'
            yield Rule(block, destination, target, found, category, evidence, editable,
                       retarget and target in self.states and target not in self.duplicates and
                       block.state < 0 and number(expression) is not None and
                       len(destination.section.values(target_key)) == 1,
                       index if tests else 0, tests, manual, target_key, provenance, dependencies, inferred_origin)

    def anchors(self, block):
        _, groups = self.source_groups(block)
        # A writer that can run from an unbounded state has no reliable source anchor.
        return set().union(*(facts.anchors for facts, _ in groups)) if all(facts.allowed for facts, _ in groups) else set()

    def index_phase_writers(self, blocks):
        writes = defaultdict(list)
        for block in blocks:
            section = block.section
            if (block.state >= 0 or section.get('type', '').casefold() != 'varset' or
                    not re.search(r'\b(?:MoveHit|MoveGuarded|MoveContact|ProjHitTime|ProjGuardedTime)\b',
                                  ' '.join(value for _, value, _ in block.triggers), re.I)):
                continue
            assignments = []
            for key, value, _ in section.entries:
                if match := PHASE_REGISTER.fullmatch(key):
                    assignments.append((match[1], int(match[2]), value))
                elif key in ('v', 'fv') and number(value) is not None:
                    assignments.append(('fvar' if key == 'fv' else 'var', int(value), section.get('value', '')))
            for kind, slot, value in assignments:
                if NUMBER_VALUE.fullmatch(value) and Decimal(value) > 0 and self.anchors(block):
                    writes[kind.casefold(), slot, Decimal(value)].append(block)
        return writes

    def phase_origins(self, block):
        writers = {}
        for _, value, _ in block.triggers:
            for atom in boolean_atoms(value):
                match = PHASE_TEST.fullmatch(atom)
                if match and Decimal(match[3]) > 0:
                    for writer in self.phase_writers[(match[1].casefold(), int(match[2]), Decimal(match[3]))]:
                        if writer.order != block.order:
                            writers[writer.order] = writer
        return tuple(writers.values())

    def visible_triggers(self, rule, origin):
        if origin is None or rule.block.state >= 0:
            return rule.block.triggers
        common, groups = self.source_groups(rule.block)
        return common + [entry for facts, group in groups if facts.permits(origin) for entry in group]

    def source_groups(self, block):
        if block.order not in self._source_groups:
            self._source_groups[block.order] = state_groups(block.triggers, self.states)
        return self._source_groups[block.order]

    def confirm_rule(self, rule):
        if rule.editable or rule.block.state >= 0 or '待确认' not in rule.evidence:
            raise AIError('这条规则不能手动确认。')
        destination = rule.destination.section
        return replace(rule, evidence='人工已确认 AI', editable=True,
                       retarget=rule.block is rule.destination and not rule.tests and rule.target in self.states
                       and rule.target not in self.duplicates and len(destination.values('value')) == 1)

    def name(self, state):
        if state is None:
            return '动态目标'
        locations = self.state_locations.get(state, ())
        titles = dict.fromkeys(title for loc in locations if (title := self.state_name_at(loc)))
        if len(locations) > 1:
            suffix = ' / '.join(titles)
            return f'{suffix} · {state}（多处定义）' if suffix else f'状态 {state}（多处定义）'
        return f'{next(iter(titles))} · {state}' if titles else f'状态 {state}'

    def fields(self, rule):
        result = {}
        if rule.retarget:
            destination = rule.destination.section
            value, line = next((value, line) for key, value, line in destination.entries if key == 'value')
            result[(destination.path, line)] = ('value', value)
        if rule.editable:
            for key, value, line in rule.block.triggers:
                for start, end, part in numeric_atoms(value):
                    field = (rule.block.section.path, line)
                    if start or end != len(value):
                        field += (start, end)
                    result[field] = (key, part)
        return result

    def stage(self, rule, field, value):
        fields = self.fields(rule)
        if field not in fields:
            raise AIError('此条件需要在源码中人工确认。')
        key, old = fields[field]
        if key == 'value':
            if value < 0 or value not in self.states or value in self.duplicates:
                raise AIError('请选择有唯一定义的现有招式状态。')
            current = str(value)
        else:
            if not -999999 <= value <= 999999:
                raise AIError('数值超出编辑范围。')
            match = CONDITION.fullmatch(old)
            current = old if int(match[2]) == value else match[1] + str(value) + match[3]
        if current != old:
            self.pending[field] = current
        else:
            self.pending.pop(field, None)

    def assert_current(self):
        if local_path(self.root, self.character.definition).read_bytes() != self.def_bytes:
            raise AIError('DEF 已改变，请重新扫描。')
        for name, raw in self.raw.items():
            if local_path(self.root, name).read_bytes() != raw:
                raise AIError('源码已由其他程序修改，请重新扫描：' + name)

    def line_value(self, path, line, original):
        field = (path, line)
        if field in self.pending:
            return self.pending[field]
        patches = [(key[2], key[3], value) for key, value in self.pending.items()
                   if len(key) == 4 and key[:2] == field]
        for start, end, value in sorted(patches, reverse=True):
            original = original[:start] + value + original[end:]
        return original

    def candidates(self):
        changed = defaultdict(dict)
        for path, line, *_ in self.pending:
            changed[path][line] = None
        annotations = defaultdict(list)
        for key, title in self.pending_names.items():
            annotations[key[0]].append((key, title))
            changed.setdefault(key[0], {})
        state_annotations = defaultdict(list)
        for location, title in self.pending_state_names.items():
            state_annotations[location[0]].append((location, title))
            changed.setdefault(location[0], {})
        result = {}
        for path, replacements in changed.items():
            lines = self.text[path].splitlines(keepends=True)
            bom = '\ufeff' if lines and lines[0].startswith('\ufeff') else ''
            if bom:
                lines[0] = lines[0][1:]
            original_lines = tuple(lines)
            for line in replacements:
                match = KEY_LINE.fullmatch(lines[line-1])
                if match is None:
                    raise AIError(f'无法安全定位原行 {path}:{line}')
                value = self.line_value(path, line, match[2])
                lines[line-1] = match[1] + value + match[3]
            insertions = defaultdict(list)
            for key, title in annotations[path]:
                _, header, origin, arm = key
                _, previous = self.branch_names.get(key, ('', None))
                newline = '\r\n' if lines[header-1].endswith('\r\n') else '\n'
                comment = ''
                if title:
                    try:
                        title.encode(self.encoding[path])
                        escaped = False
                    except UnicodeEncodeError:
                        escaped = True
                    scope = 'entry' if origin is None else str(origin)
                    if arm:
                        scope += f'.{arm}'
                    comment = f'; @ai-branch {scope} {json.dumps(title, ensure_ascii=escaped)}{newline}'
                if previous is not None:
                    lines[previous-1] = comment
                elif comment:
                    insert_at = header
                    while insert_at < len(original_lines) and original_lines[insert_at].lstrip().startswith(';'):
                        insert_at += 1
                    insertions[insert_at].append(comment)
            for location, title in state_annotations[path]:
                _, header = location
                _, previous = self.state_names.get(location, ('', ()))
                for line in previous:
                    lines[line-1] = ''
                if title:
                    ending = re.search(r'(\r\n|\n|\r)$', original_lines[header-1])
                    newline = ending[1] if ending else '\r\n'
                    insertions[header-1].append(f'; @toolbox-state-name {title}{newline}')
            for header in sorted(insertions, reverse=True):
                lines[header:header] = insertions[header]
            result[path] = (bom + ''.join(lines)).encode(self.encoding[path])
        return result

    def diff(self):
        return '\n'.join(line for path, data in self.candidates().items() for line in unified_diff(
            self.text[path].splitlines(), data.decode(self.encoding[path]).splitlines(),
            '原文件/' + path, '草稿/' + path, lineterm=''))

    def save(self):
        self.assert_current()
        result = self.candidates()
        if not result:
            raise AIError('没有待保存的规则。')
        backup = local_path(self.root, f'.recon/ai_workbench_backups/{uuid4().hex}')
        backup.mkdir(parents=True)
        for path, data in result.items():
            for kind, raw in (('original', self.raw[path]), ('candidate', data)):
                copy = backup / kind / path
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(raw)
        self.assert_current()
        written = []
        try:
            for name, data in result.items():
                path = local_path(self.root, name)
                if path.read_bytes() != self.raw[name]:
                    raise AIError('保存前检测到外部改动：' + name)
                atomic_write(path, data)
                written.append(name)
                if path.read_bytes() != data:
                    raise AIError('写回核对失败：' + name)
        except Exception as error:
            for name in reversed(written):
                path = local_path(self.root, name)
                if path.read_bytes() == result[name]:
                    atomic_write(path, self.raw[name])
            raise AIError(f'{error}\n原件备份：{backup}') from error
        self.clear()
        return backup


def atomic_write(path, data):
    temporary = path.with_name(path.name + '.ai-' + uuid4().hex)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def main():
    """Print the same source index used by the workbench, without Qt or source writes."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--def', dest='definition')
    parser.add_argument('--state', type=int, help='Only rules entering or leaving this state')
    args = parser.parse_args()
    try:
        document = AIDocument(args.repo, args.definition)
    except (ValueError, OSError) as error:
        parser.exit(1, f'{error}\n')

    def location(block):
        return {'file': block.section.path, 'line': block.section.line}

    rules = []
    for rule in document.rules:
        if args.state is not None and args.state != rule.target and args.state not in rule.sources:
            continue
        rules.append({
            'source_states': rule.sources, 'target': rule.target,
            'target_name': document.name(rule.target), 'target_expression': rule.target_expression,
            'category': rule.category, 'evidence': rule.evidence, 'order': rule.block.order,
            'source': location(rule.block), 'destination': location(rule.destination),
            'trigger_lines': rule.block.triggers, 'destination_trigger_lines': rule.destination.triggers,
            'target_conditions': rule.tests,
            'candidate_sources': [location(block) for block in rule.provenance],
            'input_sources': [location(block) for block in rule.dependencies],
            'branch_names': {str(origin) if origin is not None else 'entry': name
                             for origin in rule.sources or (None,)
                             if (name := document.branch_name(rule, origin))},
        })
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps({'definition': document.character.definition, 'files': list(document.raw), 'rules': rules},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
