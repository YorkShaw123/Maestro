#!/usr/bin/env python3
"""Read the selected character's real CNS/AIR/CMD without changing game files.

Examples (redirect stdout explicitly if a saved report is wanted):
    python -B tools/inspect_ai_moves.py --format markdown
    python -B tools/inspect_ai_moves.py --format json --states 1100,1120,111001

This is a source inspector, not a MUGEN interpreter. Time is the local action
tick, before hitpause/superpause. Conditional state changes, inherited animation
positions, and opponent collision boxes must not be replaced with guessed facts.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sys


PRIMARY = (
    200, 210, 225, 230, 240, 400, 410, 430, 440, 600, 610, 630, 640,
    245, 800, 810, 821, 1000, 1002, 1330, 1340, 1100, 1102, 1101,
    1103, 1104, 1107, 1109, 1120, 1115, 1132, 1050, 1055,
    3600, 10020, 1006, 320001, 3050, 3110, 3200,
)
RELATED = (
    40, 50, 52, 820, 1051, 1052, 1056, 1130, 110100, 111001,
    21000, 21001, 3051, 3111, 3112, 3605, 3606,
    3610, 3611, 3612, 3613, 3614, 3620, 3630, 3650, 3651, 3652,
    3680, 3690, 10021, 10022, 10023, 10024, 11150,
    25111, 25112, 25113, 30000, 30001, 30002, 30004, 40001,
    1005, 3800, 100501, 2100, 2102, 2200, 2202, 305001, 311001, 1110,
)
LABELS = {
    200: '站轻拳', 210: '站中拳', 225: '肘击', 230: '站轻腿',
    240: '站重腿', 400: '蹲轻拳', 410: '蹲中拳', 430: '蹲轻腿',
    440: '扫堂腿', 600: '空轻拳', 610: '空重拳', 630: '空轻腿',
    640: '空重腿', 245: '防御反击', 800: '普通投抓取',
    810: '普通投演出', 821: '被投释放', 1000: '普通慢波',
    1002: '普通快波', 1330: '青焰慢波', 1340: '青焰快波',
    1100: '轻升龙', 1102: '轻旋风起手', 1101: '轻旋风攻击',
    1103: '重旋风起手', 1104: '重旋风前段', 1107: '重旋风后段',
    1109: '轻升龙蹲底恢复', 1120: '付费升龙', 1115: '瞬移连击',
    1132: '分身', 1050: '膝顶', 1055: '膝顶追踢',
    3600: '援助合击', 10020: '乱舞', 1006: '极波动拳',
    320001: '远距真波', 3050: '猛击升龙', 3110: '真旋风',
    3200: '缓慢真波', 110100: '升龙K旋转派生', 111001: '轻旋风K砸地',
}
COMBAT_TYPES = {
    'hitdef', 'projectile', 'velset', 'veladd', 'velmul', 'posset',
    'posadd', 'width', 'playerpush', 'ctrlset', 'changestate', 'selfstate',
    'targetstate', 'targetbind', 'targetvelset', 'targetveladd', 'targetlifeadd',
    'hitfallset', 'changeanim', 'changeanim2', 'statetypeset',
    'superpause', 'pause', 'helper', 'assertspecial', 'nothitby', 'hitby',
}
DETAIL_KEYS = (
    'attr', 'damage', 'hitflag', 'guardflag', 'priority', 'pausetime',
    'air.juggle', 'ground.hittime', 'air.hittime', 'ground.velocity',
    'air.velocity', 'yaccel', 'fall', 'air.fall', 'fall.recover',
    'fall.recovertime', 'fall.xvelocity', 'fall.yvelocity',
    'ground.cornerpush.veloff', 'air.cornerpush.veloff',
    'guard.cornerpush.veloff', 'p1stateno', 'p2stateno', 'p2getp1state',
    'id', 'chainid', 'nochainid', 'hitonce', 'kill', 'getpower',
    'projid', 'projanim', 'projhitanim', 'offset', 'velocity', 'accel',
    'projhits', 'projmisstime', 'projremove', 'projremovetime', 'projscale',
)


def read_source(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() == '.def':
        try:
            return data.decode('utf-8-sig')
        except UnicodeDecodeError:
            return data.decode('cp936')
    if path.suffix.lower() == '.air':
        return data.decode('utf-8-sig')
    return data.decode('cp936')


def integer(value: str | None) -> int | None:
    return int(value) if value and re.fullmatch(r'-?\d+', value) else None


def clean(line: str) -> str:
    return line.split(';', 1)[0].strip()


@dataclass
class Section:
    name: str
    line: int
    path: str
    entries: list[tuple[str, str, int]] = field(default_factory=list)

    def values(self, key: str) -> list[str]:
        return [v for k, v, _ in self.entries if k == key.lower()]

    def get(self, key: str, default: str | None = None) -> str | None:
        found = self.values(key)
        return found[-1] if found else default

    def fields(self) -> dict[str, str]:
        return {k: v for k, v, _ in self.entries}

    def triggers(self) -> list[str]:
        return [f'{k}: {v}' for k, v, _ in self.entries if k.startswith('trigger')]

    def as_dict(self) -> dict:
        return {'title': self.name, 'line': self.line, 'path': self.path,
                'entries': [{'key': k, 'value': v, 'line': n}
                            for k, v, n in self.entries]}


def sections(path: Path, root: Path) -> list[Section]:
    return source_sections(read_source(path), path.relative_to(root).as_posix())


def source_sections(text: str, name: str) -> list[Section]:
    result: list[Section] = []
    current = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = re.split(r';(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', raw, maxsplit=1)[0].strip() if '"' in raw else clean(raw)
        match = re.fullmatch(r'\[(.+)\]', line)
        if match:
            current = Section(match.group(1), n, name)
            result.append(current)
        elif current is not None and '=' in line:
            key, value = line.split('=', 1)
            current.entries.append((key.strip().lower(), value.strip(), n))
    return result


def parse_cns(paths: list[Path], root: Path) -> dict[int, list[dict]]:
    states: dict[int, list[dict]] = defaultdict(list)
    for path in paths:
        current = None
        for section in sections(path, root):
            match = re.fullmatch(r'Statedef\s+(-?\d+)', section.name, re.I)
            if match:
                current = {'id': int(match.group(1)), 'definition': section,
                           'controllers': []}
                states[current['id']].append(current)
            elif current is not None and re.match(r'^State\s', section.name, re.I):
                current['controllers'].append(section)
    return states


def parse_air(path: Path, root: Path) -> dict[int, list[dict]]:
    actions: dict[int, list[dict]] = defaultdict(list)
    current = None
    defaults = {1: [], 2: []}
    pending: dict[int, list] = {}
    declaration = None
    elapsed = 0
    for n, raw in enumerate(read_source(path).splitlines(), 1):
        line = clean(raw)
        match = re.fullmatch(r'\[Begin\s+Action\s+(\d+)\]', line, re.I)
        if match:
            current = {'id': int(match.group(1)), 'line': n,
                       'path': path.relative_to(root).as_posix(), 'frames': [],
                       'loop_start': None}
            actions[current['id']].append(current)
            defaults, pending, declaration, elapsed = {1: [], 2: []}, {}, None, 0
            continue
        if current is None:
            continue
        if line.lower() == 'loopstart':
            current['loop_start'] = elapsed
            continue
        match = re.fullmatch(r'Clsn([12])(Default)?\s*:\s*(\d+)', line, re.I)
        if match:
            kind, is_default = int(match.group(1)), bool(match.group(2))
            declaration = (kind, is_default)
            if is_default:
                defaults[kind] = []
            else:
                pending[kind] = []
            continue
        match = re.fullmatch(
            r'Clsn([12])\[\d+\]\s*=\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)',
            line, re.I)
        if match and declaration:
            kind = int(match.group(1))
            box = [int(v) for v in match.groups()[1:]]
            destination = defaults[kind] if declaration == (kind, True) else pending.setdefault(kind, [])
            destination.append(box)
            continue
        fields = [part.strip() for part in line.split(',')]
        if len(fields) >= 5 and all(integer(v) is not None for v in fields[:5]):
            duration = int(fields[4])
            frame = {'element': len(current['frames']) + 1, 'line': n,
                     'start': elapsed, 'duration': duration,
                     'end': elapsed + duration if duration >= 0 else None,
                     'sprite': fields[:2], 'sprite_offset': fields[2:4],
                     'frame_options': fields[5:],
                     'clsn1': [b[:] for b in pending.get(1, defaults[1])],
                     'clsn2': [b[:] for b in pending.get(2, defaults[2])]}
            current['frames'].append(frame)
            pending, declaration = {}, None
            if duration >= 0:
                elapsed += duration
            current['duration'] = elapsed
            current['infinite'] = duration < 0
    return actions


def trigger_times(controller: Section, action: dict | None) -> list[int]:
    """Recognize literal event timing only; other clauses remain in the report."""
    result = []
    for key, value, _ in controller.entries:
        if not re.fullmatch(r'trigger\d+', key):
            continue
        match = re.fullmatch(r'Time\s*=\s*(\d+)', value, re.I)
        if match:
            result.append(int(match.group(1)))
            continue
        match = re.fullmatch(r'AnimElem\s*=\s*(\d+)(?:\s*,\s*(-?\d+))?', value, re.I)
        if match and action:
            element = int(match.group(1))
            if 1 <= element <= len(action['frames']):
                result.append(action['frames'][element - 1]['start'] + int(match.group(2) or 0))
    return sorted(set(result))


def animation_refs(state: dict) -> list[int]:
    refs = []
    for value in [state['definition'].get('anim')]:
        if integer(value) is not None:
            refs.append(int(value))
    for controller in state['controllers']:
        typ = (controller.get('type') or '').lower()
        key = 'projanim' if typ == 'projectile' else 'value' if typ == 'changeanim' else None
        value = controller.get(key) if key else None
        if integer(value) is not None and int(value) not in refs:
            refs.append(int(value))
    return refs


def initial_action(state: dict, actions: dict) -> dict | None:
    number = integer(state['definition'].get('anim'))
    candidates = actions.get(number, [])
    return candidates[0] if len(candidates) == 1 else None


def simple_end(state: dict, action: dict | None) -> int | None:
    """Unconditional local-tick ChangeState; excludes hit/position-gated exits."""
    endings = []
    for controller in state['controllers']:
        if (controller.get('type') or '').lower() != 'changestate':
            continue
        triggers = [v for k, v, _ in controller.entries if k.startswith('trigger')]
        if len(triggers) != 1:
            continue
        expression = triggers[0]
        match = re.fullmatch(r'Time\s*(?:=|>=)\s*(\d+)', expression, re.I)
        if match:
            endings.append(int(match.group(1)))
        elif re.fullmatch(r'AnimTime\s*=\s*0', expression, re.I) and action and not action['infinite']:
            endings.append(action['duration'])
    return min(endings) if endings else None


def red_windows(action: dict, start: int = 0, end: int | None = None) -> list[dict]:
    windows = []
    for frame in action['frames']:
        boxes = [box for box in frame['clsn1'] if box[0] <= box[2] and box[1] <= box[3]]
        if not boxes:
            continue
        low = max(start, frame['start'])
        high = frame['end']
        if end is not None:
            high = min(high, end) if high is not None else end
        if high is not None and low >= high:
            continue
        windows.append({'start': low, 'end': high, 'element': frame['element'],
                        'line': frame['line'], 'boxes': boxes})
    return windows


def animation_segments(state: dict, actions: dict, inherited: dict | None = None) -> list[dict]:
    """Resolve only literal Time=N ChangeAnim events; preserve ambiguity."""
    action = initial_action(state, actions)
    offset = 0
    if action is None and inherited:
        variants = actions.get(inherited['action'], [])
        if len(variants) == 1:
            action, offset = variants[0], inherited['animation_tick_at_entry']
    if action is None:
        return []
    segments = [{'state_start': 0, 'state_end': None, 'action': action,
                 'animation_start': offset}]
    for controller in state['controllers']:
        if (controller.get('type') or '').lower() != 'changeanim':
            continue
        number = integer(controller.get('value'))
        variants = actions.get(number, [])
        triggers = [v for k, v, _ in controller.entries if k.startswith('trigger')]
        if len(triggers) != 1 or len(variants) != 1:
            return []
        match = re.fullmatch(r'Time\s*=\s*(\d+)', triggers[0], re.I)
        if not match:
            return []
        moment = int(match.group(1))
        segments[-1]['state_end'] = moment
        segments.append({'state_start': moment, 'state_end': None,
                         'action': variants[0], 'animation_start': 0})
    return segments


def segment_event_times(controller: Section, segments: list[dict]) -> list[int]:
    times = []
    for key, value, _ in controller.entries:
        if not re.fullmatch(r'trigger\d+', key):
            continue
        match = re.fullmatch(r'Time\s*=\s*(\d+)', value, re.I)
        if match:
            times.append(int(match.group(1)))
            continue
        match = re.fullmatch(r'AnimElem\s*=\s*(\d+)(?:\s*,\s*(-?\d+))?', value, re.I)
        if not match:
            continue
        element = int(match.group(1))
        for segment in segments:
            frames = segment['action']['frames']
            if element > len(frames):
                continue
            moment = (segment['state_start'] + frames[element - 1]['start']
                      - segment['animation_start'] + int(match.group(2) or 0))
            if moment >= segment['state_start'] and (segment['state_end'] is None or moment < segment['state_end']):
                times.append(moment)
    return sorted(set(times))


def segment_red_windows(segments: list[dict], start: int, end: int | None) -> list[dict]:
    result = []
    for segment in segments:
        shift = segment['state_start'] - segment['animation_start']
        low = max(start, segment['state_start'])
        ends = [v for v in [end, segment['state_end']] if v is not None]
        high = min(ends) if ends else None
        for window in red_windows(segment['action']):
            a = max(low, window['start'] + shift)
            b = window['end'] + shift if window['end'] is not None else None
            b = min(b, high) if b is not None and high is not None else high if high is not None else b
            if b is not None and a >= b:
                continue
            result.append({**window, 'start': a, 'end': b, 'action': segment['action']['id']})
    return result


def attack_summary(state: dict, actions: dict, inherited: dict | None = None) -> list[dict]:
    action = initial_action(state, actions)
    segments = animation_segments(state, actions, inherited)
    end = simple_end(state, action)
    attacks = [c for c in state['controllers'] if (c.get('type') or '').lower() in {'hitdef', 'projectile'}]
    result = []
    for idx, controller in enumerate(attacks):
        typ = controller.get('type').lower()
        times = segment_event_times(controller, segments)
        entry = {'controller': controller.as_dict(), 'type': typ,
                 'literal_event_ticks': times, 'effective_red_windows': None,
                 'notes': []}
        if typ == 'hitdef' and segments and times:
            next_times = [t for later in attacks[idx + 1:]
                          if (later.get('type') or '').lower() == 'hitdef'
                          for t in segment_event_times(later, segments) if t >= min(times)]
            stop = min(next_times + ([end] if end is not None else []), default=None)
            entry['effective_red_windows'] = segment_red_windows(segments, min(times), stop)
            if not entry['effective_red_windows']:
                entry['notes'].append('此局部动画/固定退出路径内，HitDef生效后没有有效红框。')
        elif typ == 'hitdef':
            entry['notes'].append('动画继承、换动画或动态事件：只列实际定义，不伪造有效命中帧。')
        if controller.values('triggerall'):
            entry['notes'].append('另有triggerall门槛，列出的时刻并非无条件命中。')
        result.append(entry)
    return result


def inherited_animations(number: int, states: dict, actions: dict) -> list[dict]:
    candidates = []
    for predecessor, variants in states.items():
        for state in variants:
            action = initial_action(state, actions)
            end = simple_end(state, action)
            if action is None or end is None or any((c.get('type') or '').lower() == 'changeanim' for c in state['controllers']):
                continue
            for controller in state['controllers']:
                if (controller.get('type') or '').lower() != 'changestate' or integer(controller.get('value')) != number:
                    continue
                # Only an unconditional fixed local exit is an identified inheritance path.
                triggers = [v for k, v, _ in controller.entries if k.startswith('trigger')]
                if len(triggers) != 1:
                    continue
                if re.fullmatch(r'Time\s*(?:=|>=)\s*' + str(end), triggers[0], re.I) or (re.fullmatch(r'AnimTime\s*=\s*0', triggers[0], re.I) and end == action['duration']):
                    candidates.append({'from_state': predecessor, 'controller_line': controller.line,
                                       'action': action['id'], 'animation_tick_at_entry': end})
    return candidates


def command_refs(path: Path, root: Path, chosen: set[int]) -> dict[int, list[dict]]:
    refs: dict[int, list[dict]] = defaultdict(list)
    for controller in sections(path, root):
        if (controller.get('type') or '').lower() != 'changestate':
            continue
        target = integer(controller.get('value'))
        if target in chosen:
            refs[target].append(controller.as_dict())
    return refs


def make_report(root: Path, chosen: tuple[int, ...]) -> dict:
    definition = root / '1Shun_OB.def'
    manifest = next(section for section in sections(definition, root) if section.name.lower() == 'files')
    cns_paths = []
    for key, value, _ in manifest.entries:
        if key in {'cns', 'st'} or re.fullmatch(r'st\d+', key):
            path = root / value.replace('\\', '/')
            if path not in cns_paths:
                cns_paths.append(path)
    air_path = root / manifest.get('anim')
    cmd_path = root / manifest.get('cmd')
    player_path = root / 'Shun.cmd'
    states = parse_cns(cns_paths, root)
    actions = parse_air(air_path, root)
    cpu_refs = command_refs(cmd_path, root, set(chosen))
    player_refs = command_refs(player_path, root, set(chosen)) if player_path.exists() else {}
    files = [definition, *cns_paths, air_path, cmd_path]
    if player_path not in files and player_path.exists():
        files.append(player_path)
    report = {
        'schema': 1, 'active_cmd': cmd_path.relative_to(root).as_posix(),
        'sources': [{'path': path.relative_to(root).as_posix(),
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in files],
        'missing_states': [number for number in chosen if number not in states],
        'duplicate_states': {str(number): [item['definition'].line for item in definitions]
                             for number, definitions in states.items() if len(definitions) > 1},
        'duplicate_actions': {str(number): [item['line'] for item in definitions]
                              for number, definitions in actions.items() if len(definitions) > 1},
        'states': [],
        'constants': {section.name: section.fields() for path in cns_paths
                      for section in sections(path, root)
                      if section.name.lower() in {'data', 'size', 'velocity', 'movement'}},
        'global_juggle_overrides': [controller.as_dict()
            for number in (-2, -3) for variant in states.get(number, [])
            for controller in variant['controllers']
            if (controller.get('type') or '').lower() == 'assertspecial'
            and any(value.lower() == 'nojugglecheck' for key, value, _ in controller.entries if key.startswith('flag'))],
    }
    for number in chosen:
        for variant, state in enumerate(states.get(number, []), 1):
            inherited = inherited_animations(number, states, actions) if integer(state['definition'].get('anim')) is None else []
            state_actions = [action for ref in animation_refs(state) for action in actions.get(ref, [])]
            for candidate in inherited:
                for action in actions.get(candidate['action'], []):
                    if action not in state_actions:
                        state_actions.append(action)
            report['states'].append({
                'id': number, 'name': LABELS.get(number, '关联状态'), 'variant': variant,
                'definition': state['definition'].as_dict(),
                'controllers': [c.as_dict() for c in state['controllers']
                                if (c.get('type') or '').lower() in COMBAT_TYPES],
                'attacks': attack_summary(state, actions, inherited[0] if len(inherited) == 1 else None),
                'actions': state_actions,
                'inherited_animation_candidates': inherited,
                'animation_segments': [
                    {'action': segment['action']['id'],
                     'state_start': segment['state_start'],
                     'state_end': segment['state_end'],
                     'animation_tick_at_segment_start': segment['animation_start']}
                    for segment in animation_segments(state, actions, inherited[0] if len(inherited) == 1 else None)],
                'cpu_change_states_into_move': cpu_refs.get(number, []),
                'player_change_states_into_move': player_refs.get(number, []),
                'simple_end_tick': simple_end(state, initial_action(state, actions)),
            })
    return report


def fields(section: dict) -> dict[str, str]:
    return {entry['key']: entry['value'] for entry in section['entries']}


def expression(section: dict) -> str:
    entries = section['entries']
    common = [e['value'] for e in entries if e['key'] == 'triggerall']
    branches: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        if re.fullmatch(r'trigger\d+', entry['key']):
            branches[entry['key']].append(entry['value'])
    common_text = ' AND '.join(f'({value})' for value in common)
    branch_text = ' OR '.join(' AND '.join(f'({v})' for v in values) for values in branches.values())
    return (common_text + (' AND [' + branch_text + ']' if branch_text else '')) if common_text else branch_text


def esc(value: object) -> str:
    return str(value).replace('|', '\\|').replace('\n', ' ')


def markdown(report: dict) -> str:
    out = [
        '# AI 招式物理模型 v0.08.7', '',
        '本页由 `tools/inspect_ai_moves.py` 从当前 DEF 指定的 CNS、AIR、AI CMD 和玩家 CMD 只读抽取。'
        '它记录真实状态、动画和触发式，供连段引擎核对；没有把源码静态检查当成实机连段成功。', '',
        '复现：`python -B tools/inspect_ai_moves.py --format markdown`。JSON 可用 `--format json`；'
        '单独检查可用 `--states 1100,1120,111001`。工具仅向标准输出写报告，不修改游戏文件。', '',
        f'当前生效 CMD：`{report["active_cmd"]}`。以下帧区间一律为 **[起点,终点)**，'
        '从状态或动画本地 Time=0 计数；区间 [7,10) 指第 7、8、9 个行动 tick。', '',
        '## 必须保留的模型边界', '',
        '- startup 是有 HitDef 的身体红框首次相交机会；Projectile 的事件帧只是出波帧，仍要另加飞行交会时间。',
        '- 行动 tick 不包含 hitpause、Pause、SuperPause 的墙钟时间。双方暂停不同，必须分别推进位置。',
        '- 同一 triggerN 的多行是 AND，不同 triggerN 之间是 OR；triggerall 对每组共同生效。',
        '- AIR 红框按原始坐标列出，不能把每帧精灵 offset 加到红框上。'
        '保留帧翻转/缩放参数供核对，不从图片位置猜碰撞范围。',
        '- 敌人的 size.air.front、head.pos、mid.pos 只是作者常量，不能代替敌人当前动画的 Clsn2。'
        '不同体型只能保守筛选，最终仍要在引擎中验证。',
        '- 省略 HitDef hitflag 的默认值是 MAF；省略 air.juggle 为 0，省略 yaccel 为 .35（240p）。'
        'StateDef juggle 省略时继承前一攻击的追打结果，不能擅自当作 0。',
        '- 定义了 HitDef 不等于存在有效红框。分阶段攻击继承的动画位置、换动画、固定退出同帧均须考虑。',
        '- 存在重复状态或 Action 时工具全部报告，不猜测当前引擎究竟取哪份；'
        '不同结果要通过实际 MUGEN 版本确认。',
        '- CMD 下面列的是进入该招的全部真实控制器。玩家取消限制不自动限制 AI；'
        '必须检查 AI 自己的 ChangeState 条件。', '',
        '## 源文件快照', '', '| 文件 | SHA-256 |', '|---|---|',
    ]
    out.extend(f'| `{source["path"]}` | `{source["sha256"]}` |' for source in report['sources'])
    out.extend(['', '## 重复定义与未加载状态', ''])
    out.append('CNS 重复：' + (json.dumps(report['duplicate_states'], ensure_ascii=False) or '无'))
    out.append('AIR 重复：' + (json.dumps(report['duplicate_actions'], ensure_ascii=False) or '无'))
    out.append('DEF 的自定义 CNS 未定义：' + ', '.join(map(str, report['missing_states'])) +
               '。其中通用状态 50/52 由 stcommon 提供，本工具没有擅自用另一个 common 文件替代它。')
    out.extend(['', '## 全局物理与追打规则', ''])
    for name, values in report['constants'].items():
        out.append(f'- {name}：`' + '; '.join(f'{key}={value}' for key, value in values.items()) + '`。')
    for controller in report['global_juggle_overrides']:
        out.append(f'- NoJuggleCheck @ {controller["path"]}:{controller["line"]}：`{esc(expression(controller))}`。')
    out.extend(['', '## 静态交叉核对中必须处理的空窗口', '',
                '下列情况在明确的局部动画及固定退出路径上，HitDef 后没有任何有效红框。'
                '这不是靠距离阈值能修复的问题。若后续修改了游戏物理，重新运行即可自动更新。', ''])
    empty = [(state['id'], attack['controller']['line']) for state in report['states']
             for attack in state['attacks'] if attack['effective_red_windows'] == []]
    if empty:
        out.extend(f'- State {number}，HitDef @ CNS:{line}。' for number, line in empty)
    else:
        out.append('本次可静态决定的路径中，没有发现此类空窗口；动态路径仍需实机验证。')
    out.extend(['', '## 全招式总表', '',
                '| 状态 | 名称 | 初始动画 | 本地身体首红框 / 出波事件 | juggle | 固定退出 / 恢复 |',
                '|---:|---|---|---|---|---|'])
    for state in report['states']:
        parameters = fields(state['definition'])
        opportunity = []
        for attack in state['attacks']:
            if attack['type'] == 'projectile':
                opportunity.append('波 ' + '/'.join(map(str, attack['literal_event_ticks']))
                                   if attack['literal_event_ticks'] else '波 动态')
            elif attack['effective_red_windows']:
                opportunity.append('体 ' + str(attack['effective_red_windows'][0]['start']))
            elif attack['effective_red_windows'] == []:
                opportunity.append('该 HitDef 无有效红框')
            else:
                opportunity.append('体 动态/继承')
        end = state['simple_end_tick']
        out.append(f'| {state["id"]} | {state["name"]} | {esc(parameters.get("anim", "继承"))} | '
                   f'{esc("；".join(opportunity) or "无本体攻击")} | '
                   f'{esc(parameters.get("juggle", "继承/未显式设置"))} | '
                   f'{str(end) + " tick 切状态" if end is not None else "见位置/命中/落地条件"} |')
    out.extend(['', '## 每招逐段证据', ''])
    for state in report['states']:
        number = state['id']
        definition = state['definition']
        parameters = fields(definition)
        out.extend([f'### {number} {state["name"]}（CNS:{definition["line"]}，定义 {state["variant"]}）', '',
                    '`' + '; '.join(f'{k}={v}' for k, v in parameters.items()) + '`', ''])
        if state['animation_segments']:
            out.append('静态动画时间轴：`' + json.dumps(state['animation_segments'], ensure_ascii=False) + '`。')
            out.append('')
        if state['inherited_animation_candidates']:
            out.append('固定前驱继承候选：`' + json.dumps(state['inherited_animation_candidates'], ensure_ascii=False) +
                       '`。涉及切状态同帧的边界，实机应保留一 tick 容差。')
            out.append('')
        for action in state['actions']:
            out.append(f'**Action {action["id"]} @ {action["path"]}:{action["line"]}**：'
                       f'{"末帧无限" if action.get("infinite") else str(action.get("duration", 0)) + " tick"}'
                       f'；LoopStart={action["loop_start"]}。')
            windows = red_windows(action)
            if windows:
                out.extend(['', '| 元素 | 动画 tick | 原始有效 Clsn1 | AIR 行 |', '|---:|---|---|---:|'])
                for window in windows:
                    out.append(f'| {window["element"]} | [{window["start"]},{window["end"] if window["end"] is not None else "∞"}) | '
                               f'`{window["boxes"]}` | {window["line"]} |')
            else:
                out.append('无有效 Clsn1。')
            out.append('')
        if not state['actions']:
            out.extend(['没有可静态确定的独立 Action；继承上一状态动画或使用动态表达式。', ''])
        for attack in state['attacks']:
            controller = attack['controller']
            attack_fields = fields(controller)
            out.extend([f'**{attack["type"]} @ CNS:{controller["line"]}**：`{esc(expression(controller))}`。', ''])
            values = [f'{key}={attack_fields[key]}' for key in DETAIL_KEYS if key in attack_fields]
            values.append('hitflag=MAF(默认)' if 'hitflag' not in attack_fields else '')
            values.append('air.juggle=0(默认)' if 'air.juggle' not in attack_fields else '')
            values.append('yaccel=.35(240p默认)' if 'yaccel' not in attack_fields else '')
            out.append('`' + '; '.join(value for value in values if value) + '`')
            if attack['literal_event_ticks']:
                out.append('字面事件 tick：' + ', '.join(map(str, attack['literal_event_ticks'])) + '。')
            if attack['effective_red_windows'] is not None:
                windows = attack['effective_red_windows']
                out.append('该 HitDef 的局部有效红框：' +
                           ('；'.join(f'[{w["start"]},{w["end"]}) AIR:{w["line"]}' for w in windows)
                            if windows else '**没有**') + '。')
            out.extend(attack['notes'])
            out.append('')
        out.append('**运动、暂停、恢复、强制受击与局部取消**：')
        out.append('')
        for controller in state['controllers']:
            values = fields(controller)
            typ = values.get('type', '').lower()
            if typ in {'hitdef', 'projectile'}:
                continue
            properties = [f'{k}={v}' for k, v in values.items()
                          if not k.startswith('trigger') and k not in {'type', 'ignorehitpause', 'persistent'}]
            out.append(f'- CNS:{controller["line"]} `{typ}` `{esc("; ".join(properties))}`；'
                       f'条件 `{esc(expression(controller))}`；'
                       f'ignorehitpause={values.get("ignorehitpause", "0")}。')
        out.append('')
        # Entry controllers are kept exact because they are the actual cancel contract.
        for label, key in [('生效 AI 的进入/取消', 'cpu_change_states_into_move'),
                           ('玩家 CMD 的进入/取消（AI 不自动继承）', 'player_change_states_into_move')]:
            refs = state[key]
            out.append(f'**{label}**：')
            out.append('')
            if not refs:
                out.append('无直接 ChangeState；可由动作内部、命中、Helper 或通用引擎状态进入。')
            for controller in refs:
                out.append(f'- {controller["path"]}:{controller["line"]} `{esc(expression(controller))}`。')
            out.append('')
    return '\n'.join(out) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--format', choices=('json', 'markdown'), default='json')
    parser.add_argument('--states', help='comma-separated explicit state numbers')
    args = parser.parse_args()
    chosen = tuple(int(value) for value in args.states.split(',')) if args.states else tuple(dict.fromkeys(PRIMARY + RELATED))
    report = make_report(args.root.resolve(), chosen)
    output = markdown(report) if args.format == 'markdown' else json.dumps(report, ensure_ascii=False, indent=2)
    sys.stdout.reconfigure(encoding='utf-8')
    print(output, end='' if output.endswith('\n') else '\n')


if __name__ == '__main__':
    main()
