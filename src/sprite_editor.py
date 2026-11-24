"""Indexed sprite editing with explicit palette ownership and checked transactions.

Opening and previewing never write character files. Runtime-specific Evil
effects are compiled only while preparing a reviewable save plan.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import base64
import hashlib
import json
import os
import re
import stat
import struct
import uuid

from port_reference_assets import parse_sff, rle_encode
from normalize_character_palettes import palette_sources, pixel_payload, read_act

STATE_FILE = 'sprite-editor.json'
MODES = ('Azure', 'Evil')


class SpriteEditorError(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf8')


def packed(data):
    return base64.b64encode(data).decode('ascii')


def unpacked(value):
    return base64.b64decode(value, validate=True)


def _safe_path(root, name):
    path = root / name
    if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(root):
        raise SpriteEditorError('文件路径越出当前工程：' + str(name))
    cursor = path
    while cursor != root:
        if cursor.exists() and (cursor.is_symlink() or
                getattr(cursor.lstat(), 'st_file_attributes', 0) & 0x400):
            raise SpriteEditorError('不通过链接写入角色文件：' + str(cursor))
        cursor = cursor.parent
    return path


def _read(root, name):
    path = _safe_path(root, name)
    return path.read_bytes() if path.is_file() else None


def _act_bytes(palette, original):
    return b''.join(palette[i:i+3] for i in range(765, -1, -3)) + original[768:]


def _paint_payload(sprite, pixels, palette):
    image = sprite.image
    stride = struct.unpack_from('<H', image.header, 66)[0]
    rows = []
    for y in range(image.height):
        row = pixels[y*image.width:(y+1)*image.width] + bytes(stride-image.width)
        rows.append(rle_encode(row) if image.header[2] else row)
    return image.header + b''.join(rows) + b'\x0c' + palette


def _sff_bytes(prefix, sprites, palettes, pixels):
    """Preserve original encoding unless that image or inheritance must change."""
    output = bytearray(prefix)
    struct.pack_into('<III', output, 16, len({s.group for s in sprites}), len(sprites), len(prefix))
    inherited = None
    for s in sprites:
        desired = palettes[s.ordinal]
        if not s.raw_payload:
            payload, shared, link = b'', s.shared, s.link
        else:
            shared, link = s.shared, s.link
            if s.ordinal in pixels:
                payload = _paint_payload(s, pixels[s.ordinal], desired)
                if shared and desired == inherited:
                    payload = payload[:-769] + b'\x0c'
                else:
                    shared = 0
            elif shared and desired != inherited:
                payload = pixel_payload(s) + b'\x0c' + desired
                shared = 0
            elif not shared and desired != s.palette:
                payload = pixel_payload(s) + b'\x0c' + desired
            else:
                payload = s.raw_payload
            if not shared:
                inherited = desired
        nxt = len(output) + 32 + len(payload) if s.ordinal + 1 < len(sprites) else 0
        output.extend(struct.pack('<IIhhHHHB', nxt, len(payload), s.x, s.y,
                                  s.group, s.number, link, shared))
        output.extend(s.reserved)
        output.extend(payload)
    return bytes(output)


class SpriteDocument:
    modes = MODES

    @classmethod
    def open(cls, repo_path):
        return cls(repo_path)

    def __init__(self, repo_path):
        self.root = Path(repo_path).resolve()
        if not (self.root / '1Shun_OB.def').is_file():
            raise SpriteEditorError('请选择含 1Shun_OB.def 的本角色工程。')
        names = ['1Shun_OB.def', 'Shun.sff', 'Shun.air', 'Shun-Azure.act', 'Shun-Evil.act',
                 'Shun.cmd', 'AI/ai_main.cmd']
        in_files=False
        for raw in (self.root/'1Shun_OB.def').read_text('utf-8-sig').splitlines():
            line=raw.split(';',1)[0].strip()
            if line.startswith('['):
                in_files=line.lower()=='[files]'
            elif in_files and '=' in line:
                key,value=(part.strip() for part in line.split('=',1))
                if re.fullmatch(r'cns|st\d*|cmd',key,re.I):
                    names.extend(part.strip().strip('"').replace('\\','/')
                                 for part in value.split(',') if part.strip())
        names=list(dict.fromkeys(names))
        self._original_files = {name: _read(self.root, name) for name in names}
        if any(v is None for v in self._original_files.values()):
            raise SpriteEditorError('角色图片、动作或两份配色文件不完整。')
        self._original_state = _read(self.root, STATE_FILE)
        if self._original_state is None and any(b'SPRITE_EDITOR_EVIL_V1' in value
                for name,value in self._original_files.items() if name.endswith(('.air','.cns','.cmd'))):
            raise SpriteEditorError('检测到Evil专用特效，但配套编辑记录丢失。请恢复工具箱完整备份，不能删掉记录后继续覆盖。')
        self._state = json.loads(self._original_state.decode('utf8')) if self._original_state else {}
        if not isinstance(self._state,dict) or (self._state and self._state.get('schema') != 1):
            raise SpriteEditorError('编辑记录版本不受支持，未修改角色文件。')
        self.external_state_warning = ''
        if self._state:
            if any(n not in self._original_files or not n.endswith(('.air','.cns','.cmd'))
                   for n in self._state.get('script_originals',{})):
                raise SpriteEditorError('编辑记录包含当前工程之外的播放脚本，未使用。')
            mismatches = [n for n, h in self._state.get('files', {}).items()
                          if n not in self._original_files or sha(self._original_files[n]) != h]
            if mismatches:
                from sprite_editor_reconcile import reconcile_state,ReconcileError
                try:
                    self._state,details=reconcile_state(self.root,self._state,self._original_files)
                except ReconcileError as error:
                    raise SpriteEditorError('当前文件与配色记录有暂时不能安全合并的差异：'+
                        str(error)+'\n现有文件未被覆盖；不需要直接回退整个人物。') from error
                self.external_state_warning=(
                    '已核对换行格式差异，不是图片或颜色变化；下次保存会按工程规范统一换行。'
                    if all(kind=='line_endings' for kind in details['kinds'].values()) else
                    '已核对并保留后来修改的动作脚本：'+
                    '、'.join(details['files'])+'。配色生成区保持完整，下次保存时同步编辑记录。')
        self._pending_bank_rebuild=bool(self._state.get('pending_rebuild'))
        if self._pending_bank_rebuild:
            self.external_state_warning=('已保留后来的动作或特效入口修改。现有颜色没有丢失，'
                '请点击“预览并保存”同步Evil派生动作与播放分支。')
        self._prefix, all_sprites = parse_sff(self._original_files['Shun.sff'])
        self.generated_count = len(all_sprites) - self._state.get('base_count', len(all_sprites))
        base_count = self._state.get('base_count', len(all_sprites))
        if not 0 < base_count <= len(all_sprites):
            raise SpriteEditorError('精灵记录的基础图片数量无效。')
        self._sprites = all_sprites[:base_count]
        actual_sources = palette_sources(self._sprites)
        self._groups = self._state.get('palette_groups', actual_sources)
        if len(self._groups) != base_count or any(not isinstance(n, int) or not 0 <= n < base_count for n in self._groups):
            raise SpriteEditorError('编辑记录中的颜色表归属无效。')
        self._owners = []
        for s in self._sprites:
            self._owners.append(s.ordinal if s.raw_payload else self._owners[s.link])
        self._fixed = set(self._state.get('fixed_owners', []))
        self._broken_portraits = [s.ordinal for s in self._sprites
                                 if s.group == 9000 and self._groups[s.ordinal] == 0]
        self._palettes = {}
        for mode in MODES:
            for source in set(self._groups):
                palette = (read_act(self._original_files['Shun-' + mode + '.act'])
                           if source == 0 else self._sprites[source].palette)
                saved = self._state.get('palettes', {}).get(mode, {}).get(str(source))
                self._palettes[mode, source] = unpacked(saved) if saved else palette
        self._single = {}
        self._mode_single = {(mode,int(owner)):unpacked(value)
            for mode,values in self._state.get('single_modes',{}).items()
            for owner,value in values.items()}
        if any(mode not in MODES or owner not in self._owners or len(value)!=768
               for (mode,owner),value in self._mode_single.items()) or any(
                   len(value)!=768 for value in self._palettes.values()):
            raise SpriteEditorError('编辑记录中的独立颜色表不完整。')
        self._pixels = {}
        self._undo, self._redo = [], []
        self._stroke = None
        self._revision = 0
        self._cached_plan = None
        counts = Counter(s.key for s in self._sprites)
        self.entries = []
        for s in self._sprites:
            category = 'portrait' if s.group == 9000 else ('body' if self._groups[s.ordinal] == 0 else 'effect')
            self.entries.append(dict(id=s.ordinal, group=s.group, index=s.number,
                width=s.image.width, height=s.image.height, duplicate=counts[s.key] > 1,
                palette_source=self._groups[s.ordinal], category=category,
                protected=category == 'portrait'))
        self._initial = self._snapshot()

    @property
    def dirty(self):
        return self._pending_bank_rebuild or self._snapshot() != self._initial

    @property
    def pending_rebuild(self):
        return self._pending_bank_rebuild

    @property
    def undo_available(self):
        return bool(self._undo)

    @property
    def redo_available(self):
        return bool(self._redo)

    def _snapshot(self):
        return (dict(self._palettes), dict(self._single), dict(self._pixels), set(self._fixed), dict(self._mode_single))

    def _restore(self, snap):
        self._palettes, self._single, self._pixels, self._fixed, self._mode_single = (
            dict(snap[0]), dict(snap[1]), dict(snap[2]), set(snap[3]), dict(snap[4]))
        self._revision += 1
        self._cached_plan = None

    def _record(self):
        if self._stroke is None:
            self._undo.append(self._snapshot())
            self._undo = self._undo[-100:]
            self._redo.clear()
        self._revision += 1
        self._cached_plan = None

    def begin_stroke(self):
        if self._stroke is None:
            self._stroke = self._snapshot()

    def end_stroke(self):
        before, self._stroke = self._stroke, None
        if before is not None and before != self._snapshot():
            self._undo.append(before)
            self._undo = self._undo[-100:]
            self._redo.clear()

    def undo(self):
        self.end_stroke()
        if self._undo:
            self._redo.append(self._snapshot())
            self._restore(self._undo.pop())

    def redo(self):
        self.end_stroke()
        if self._redo:
            self._undo.append(self._snapshot())
            self._restore(self._redo.pop())

    def _entry(self, entry_id):
        if not isinstance(entry_id, int) or not 0 <= entry_id < len(self.entries):
            raise SpriteEditorError('请选择有效精灵。')
        return self._sprites[entry_id]

    def _palette(self, entry_id, mode, original=False):
        self._entry(entry_id)
        if mode not in MODES:
            raise SpriteEditorError('未知配色：' + str(mode))
        palettes, single, pixels, fixed, mode_single = self._initial if original else self._snapshot()
        owner = self._owners[entry_id]
        if owner in single:
            return single[owner]
        if (mode,owner) in mode_single:
            return mode_single[mode,owner]
        source = self._groups[entry_id]
        return palettes['Azure' if owner in fixed or (self.entries[entry_id]['protected'] and source != 0) else mode, source]

    def sprite(self, entry_id, mode='Azure'):
        s = self._entry(entry_id)
        return dict(pixels=self._pixels.get(self._owners[entry_id], s.image.pixels),
                    width=s.image.width, height=s.image.height, palette=self._palette(entry_id, mode),
                    transparent_index=0, palette_source=self._groups[entry_id],
                    affected_ids=[e['id'] for e in self.affected(entry_id, mode)],
                    scope_label=self.source_summary(entry_id, mode))

    def original_sprite(self, entry_id, mode='Azure'):
        s = self._entry(entry_id)
        return dict(pixels=s.image.pixels, width=s.image.width, height=s.image.height,
                    palette=self._palette(entry_id, mode, original=True), transparent_index=0,
                    palette_source=self._groups[entry_id], affected_ids=[], scope_label='打开工程时')

    def affected(self, entry_id, mode='Azure', scope='palette'):
        self._entry(entry_id)
        owner, source = self._owners[entry_id], self._groups[entry_id]
        if scope in ('sprite', 'pixels'):
            return [dict(e) for e in self.entries if self._owners[e['id']] == owner]
        if scope == 'group':
            return [dict(e) for e in self.entries if e['group'] == self.entries[entry_id]['group']
                    and e['palette_source'] == source]
        if owner in self._single or (mode,owner) in self._mode_single:
            return [dict(e) for e in self.entries if self._owners[e['id']] == owner]
        return [dict(e) for e in self.entries if self._groups[e['id']] == source
                and self._owners[e['id']] not in self._single
                and (mode,self._owners[e['id']]) not in self._mode_single]

    def color_usage(self, entry_id, mode, color_index, scope='palette'):
        return [e for e in self.affected(entry_id, mode, scope)
                if color_index in self._pixels.get(self._owners[e['id']],self._sprites[e['id']].image.pixels)]

    def palette_family(self,entry_id,mode='Azure'):
        self._entry(entry_id)
        source,owner=self._groups[entry_id],self._owners[entry_id]
        keys=[('source',n) for n in sorted(set(self._groups))]
        keys += [('single',n) for n in sorted(self._single)]
        keys += [(m,n) for m,n in sorted(self._mode_single)]
        key=(('single',owner) if owner in self._single else
             (mode,owner) if (mode,owner) in self._mode_single else ('source',source))
        members=self.affected(entry_id,mode,'palette')
        return dict(number=keys.index(key)+1,total=len(keys),entries=members,
            source_ordinal=source,source_key=list(self._sprites[source].key),
            numbering='本工具按当前实际共享关系编号；颜色相同的独立色表仍分开')

    def source_summary(self, entry_id, mode='Azure', scope='palette'):
        entry = self.entries[entry_id]
        owner, source = self._owners[entry_id], self._groups[entry_id]
        count = len(self.affected(entry_id, mode, scope))
        origin=self._sprites[source]
        provenance=f'来源图片 {origin.group},{origin.number}（第{source+1}条）'
        if scope == 'pixels':
            return f'共用图案：Azure / Evil 都改变；包含 {count} 张链接图片'
        if entry['protected']:
            if source == 0:
                return '头像颜色表异常：正在跟随人物ACT；为保护头像，保存前须先恢复独立色表'
            return f'固定头像色表 · {provenance}；Azure / Evil 共用，影响 {count} 张'
        if scope == 'group':
            return f'{mode} 当前图片组 {entry["group"]} · 共 {count} 张，包含本组已单独改色的图片；另一套保持原样'
        if entry['category'] == 'effect' and (scope == 'sprite' or (mode,owner) in self._mode_single):
            return f'{mode} 仅该特效图及其链接，共 {count} 张；另一套配色保持原样'
        if scope == 'sprite' or owner in self._single or owner in self._fixed:
            return f'仅该图及其链接，共 {count} 张；固定配色，Azure / Evil 共用'
        if source == 0:
            return f'{1 if mode=="Azure" else 2}号 {mode} 人物配色（ACT）· {provenance} · 影响 {count} 张；另一套保持原样'
        return f'{1 if mode=="Azure" else 2}号 {mode} 特效配色 · {provenance} · 影响 {count} 张；另一套保持原样'

    def set_color(self, entry_id, mode, index, rgb, scope='palette'):
        self._entry(entry_id)
        if mode not in MODES or scope not in ('palette', 'sprite', 'group'):
            raise SpriteEditorError('请选择有效配色和修改范围。')
        if not isinstance(index, int) or not 0 <= index <= 255 or len(rgb) != 3 or any(
                not isinstance(v, int) or not 0 <= v <= 255 for v in rgb):
            raise SpriteEditorError('颜色必须为0～255的RGB及色号。')
        if index == 0:
            raise SpriteEditorError('0号是透明色，默认保护；请用橡皮改变透明区域。')
        if scope == 'group':
            ids = sorted({self._owners[e['id']] for e in self.affected(entry_id,mode,'group')})
            if self.entries[entry_id]['category'] == 'body':
                raise SpriteEditorError('人物组局部配色会脱离ACT换色；请选人物共用色表，或明确仅当前图固定配色。')
            self.begin_stroke()
            try:
                for owner in ids:
                    self.set_color(owner,mode,index,rgb,'sprite')
            finally:
                self.end_stroke()
            return
        current = self._palette(entry_id, mode)
        new = current[:index*3] + bytes(rgb) + current[index*3+3:]
        if new == current:
            return
        owner, source = self._owners[entry_id], self._groups[entry_id]
        if scope == 'sprite' and owner == 0:
            raise SpriteEditorError('第一张图承载人物ACT色表，不能单独隔离；请选择人物配色或使用画笔已有颜色。')
        self._record()
        if (scope == 'sprite' or (mode,owner) in self._mode_single) and self.entries[entry_id]['category'] == 'effect':
            self._mode_single[mode,owner] = new
        elif scope == 'sprite' or owner in self._single:
            self._single[owner] = new
            self._fixed.add(owner)
        elif self.entries[entry_id]['protected'] or owner in self._fixed:
            self._palettes['Azure', source] = new
            self._palettes['Evil', source] = new
        else:
            self._palettes[mode, source] = new

    def paint(self, entry_id, x, y, index):
        s = self._entry(entry_id)
        if not all(isinstance(v, int) for v in (x,y,index)) or not (
                0 <= x < s.image.width and 0 <= y < s.image.height and 0 <= index <= 255):
            raise SpriteEditorError('像素坐标或色号超出范围。')
        owner = self._owners[entry_id]
        current = self._pixels.get(owner, s.image.pixels)
        pos = y * s.image.width + x
        if current[pos] == index:
            return
        self._record()
        self._pixels[owner] = current[:pos] + bytes([index]) + current[pos+1:]

    def _check_external(self):
        for name, original in self._original_files.items():
            if _read(self.root, name) != original:
                raise SpriteEditorError('检测到外部修改，未覆盖文件：' + name + '。请先重新载入并核对。')
        if _read(self.root, STATE_FILE) != self._original_state:
            raise SpriteEditorError('配色编辑记录已被外部修改，未覆盖。')

    def _base_inputs(self):
        result = dict(self._original_files)
        for name, value in self._state.get('script_originals', {}).items():
            result[name] = unpacked(value)
        return result

    def plan_save(self):
        if not self.dirty:
            raise SpriteEditorError('没有待保存的修改。')
        if self._broken_portraits:
            raise SpriteEditorError('发现头像正在跟随人物主色表，未写入任何文件。请先恢复头像独立色表再保存配色；受影响条目：'+
                                    '、'.join(str(n+1) for n in self._broken_portraits))
        self._check_external()
        desired = [s.palette if self._groups[s.ordinal] == 0 and
                   self._owners[s.ordinal] not in self._fixed and
                   self._palettes['Azure', 0] == self._initial[0]['Azure', 0]
                   else self._palette(s.ordinal, 'Azure') for s in self._sprites]
        raw = _sff_bytes(self._prefix, self._sprites, desired, self._pixels)
        prefix, base = parse_sff(raw)
        # Validate the complete current user image set, not an old generator list.
        for a, b in zip(self._sprites, base):
            if (a.key,a.x,a.y,a.link,a.reserved,a.image.header) != (b.key,b.x,b.y,b.link,b.reserved,b.image.header):
                raise SpriteEditorError('图片身份或头部保护核对失败。')
            expected = self._pixels.get(self._owners[a.ordinal], a.image.pixels)
            if b.image.pixels != expected or b.palette != desired[a.ordinal]:
                raise SpriteEditorError('像素或颜色表保存核对失败：' + str(a.key))
        evil = {s.ordinal: self._palette(s.ordinal,'Evil') for s in base
                if self._palette(s.ordinal,'Evil') != desired[s.ordinal] and self._groups[s.ordinal] != 0}
        inputs = {name:(data.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
                       if name.lower().endswith(('.air','.cns','.cmd','.def')) else data)
                  for name,data in self._base_inputs().items()}
        output = {name: value for name,value in inputs.items()}
        output['Shun.sff'] = raw
        warnings = []
        if self._pending_bank_rebuild:
            warnings.append('本次会同步后来变化的动作或特效入口，重建Evil副本；已有RGB颜色保持不变。')
        bank = {}
        if evil:
            from sprite_editor_effects import build_effect_variant
            result = build_effect_variant(self.root, raw, evil, base, source_files=inputs)
            if result.get('blocked'):
                meanings={
                    'duplicate_sprite_key_select_all_occurrences':'同一图片编号有重复条目，请选当前组或该色表范围，包含全部重复项后再改色',
                    'body_or_helper_animation_reference':'这张图片还用于尚未核实的本体或召唤物动作',
                    'helper_anim_comparison_dependency':'脚本依赖原动作编号，当前不能安全改写播放',
                    'unbounded_animation_expression':'播放方式由动态表达式决定，当前不能安全推断',
                    'body_palette_uses_act':'人物主色请通过Azure/Evil人物配色编辑',
                    'portrait_is_fixed':'头像保持独立固定颜色，不生成Evil特效副本',
                    'generated_baseline':'当前文件已有生成内容且缺少匹配编辑记录，需要从工具箱完整备份恢复'}
                details = '\n'.join(
                    (('图片 '+','.join(map(str,item['sprite_key']))+'：') if 'sprite_key' in item else '')+
                    meanings.get(item.get('reason'),item.get('message',str(item.get('reason'))))+
                    (f"（{item['file']}:{item['line']}）" if 'file' in item else '')
                    for item in result['blocked'][:12])
                raise SpriteEditorError('以下Evil独立配色尚不能安全接入实际播放，未保存任何文件：\n' + details)
            output['Shun.sff'], output['Shun.air'] = result['sff'], result['air']
            output.update(result['cns'])
            bank, details = result['metadata'], result.get('warnings', [])
            bank['warnings']=details
            unused_images=len(bank.get('unused_ordinals',[]))
            unused_actions=len(bank.get('unused_actions',[]))
            if unused_images or unused_actions:
                warnings.append(f'{unused_images} 张图片、{unused_actions} 个动作当前没有播放入口；可以保存配色副本，但不将它们计作游戏中已显示的效果。')
            if any('fightfx' in line for line in details):
                warnings.append('游戏公共特效由公共资源提供，本角色编辑不会改动公共特效。')
        for mode in MODES:
            name = 'Shun-' + mode + '.act'
            output[name] = _act_bytes(self._palettes[mode,0], inputs[name])
        groups = list(self._groups)
        for owner in self._single:
            for e in self.entries:
                if self._owners[e['id']] == owner:
                    groups[e['id']] = owner
        palettes = {mode:{str(source):packed(self._palettes[mode,source]) for source in set(self._groups)}
                    for mode in MODES}
        for owner,palette in self._single.items():
            palettes['Azure'][str(owner)] = palettes['Evil'][str(owner)] = packed(palette)
        state = dict(schema=1, base_count=len(base), palette_groups=groups,
            fixed_owners=sorted(self._fixed), palettes=palettes, effect_bank=bank,
            single_modes={mode:{str(owner):packed(value) for (m,owner),value in self._mode_single.items() if m==mode} for mode in MODES},
            script_originals={name:packed(data) for name,data in inputs.items()
                              if name.endswith(('.air','.cns','.cmd')) and output[name] != data},
            script_outputs={name:packed(output[name]) for name,data in inputs.items()
                            if name.endswith(('.air','.cns','.cmd')) and output[name] != data},
            files={name:sha(data) for name,data in output.items()})
        output[STATE_FILE] = json_bytes(state)
        changed = {name:data for name,data in output.items()
                   if data != (self._original_state if name==STATE_FILE else self._original_files.get(name))}
        changed_ids = [e['id'] for e in self.entries if
                       self.sprite(e['id'],'Azure')['pixels'] != self.original_sprite(e['id'],'Azure')['pixels']
                       or any(self._palette(e['id'],mode) != self._palette(e['id'],mode,True) for mode in MODES)]
        if self._single:
            warnings.append('单图颜色已隔离为固定色表；这些图片的Azure/Evil将使用同一颜色。')
        if any(e['protected'] and e['id'] in changed_ids for e in self.entries):
            warnings.append('本次明确编辑了固定头像；人物配色仍与头像分开。')
        public = dict(summary=f'{len(changed_ids)} 张图片、{len(changed)} 个文件将更新；先自动备份，再写入并重读核对。',
            files=list(changed), changed_sprites=len(changed_ids),
            changed_palettes=sum(self._palettes[k]!=self._initial[0].get(k) for k in self._palettes)+len(self._single)+sum(v!=self._initial[4].get(k) for k,v in self._mode_single.items()),
            warnings=warnings, changed_ids=changed_ids, token=uuid.uuid4().hex)
        self._cached_plan = (public, changed, self._revision)
        return public

    def save(self, plan):
        if not self._cached_plan or plan.get('token') != self._cached_plan[0]['token'] or self._revision != self._cached_plan[2]:
            raise SpriteEditorError('预览后内容已变化，请重新检查保存预览。')
        self._check_external()
        result = self._commit(self._cached_plan[1], '保存精灵与配色')
        self._reopen()
        return result

    def _commit(self, changed, label):
        self._check_external()
        backup_root = self._backup_root()
        backup_root.mkdir(parents=True,exist_ok=True)
        folder = backup_root / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
        folder.mkdir()
        manifest = dict(schema=1,label=label,completed=False,backup_complete=True,files={})
        originals = {}
        # A palette restore must restore the matching AIR/CNS too. A delta-only
        # backup could leave new animation calls pointing at removed sprites.
        complete = {name: changed.get(name,_read(self.root,name))
                    for name in self._backup_files() | set(changed)}
        for name,data in complete.items():
            _safe_path(self.root,name)
            originals[name] = _read(self.root,name)
            if originals[name] is not None:
                dest=folder/'original'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(originals[name])
            stage=folder/'candidate'/name;stage.parent.mkdir(parents=True,exist_ok=True);stage.write_bytes(data) if data is not None else None
            manifest['files'][name] = dict(existed=originals[name] is not None,changed=name in changed,
                before_sha256=sha(originals[name]) if originals[name] is not None else None,
                after_sha256=sha(data) if data is not None else None)
        (folder/'manifest.json').write_bytes(json_bytes(manifest))
        written=[]
        try:
            self._check_external()
            for name,data in changed.items():
                path=_safe_path(self.root,name)
                if _read(self.root,name) != originals[name]:
                    raise SpriteEditorError('保存期间检测到外部改动：'+name)
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    temp=path.with_name(path.name+'.sprite-editor-'+uuid.uuid4().hex)
                    try:
                        temp.write_bytes(data);os.replace(temp,path)
                    finally:
                        temp.unlink(missing_ok=True)
                written.append(name)
            for name,data in changed.items():
                if _read(self.root,name)!=data:
                    raise SpriteEditorError('保存后重读核对失败：'+name)
            manifest['completed']=True
            (folder/'manifest.json').write_bytes(json_bytes(manifest))
        except Exception as error:
            rollback_errors=[]
            for name in reversed(written):
                try:
                    if _read(self.root,name)!=changed[name]:
                        rollback_errors.append(name+' 已由外部程序改变，保留外部版本')
                        continue
                    path=_safe_path(self.root,name);original=originals[name]
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        temp=path.with_name(path.name+'.sprite-editor-rollback')
                        temp.write_bytes(original);os.replace(temp,path)
                except OSError as rollback_error:
                    rollback_errors.append(name+'：'+str(rollback_error))
            if rollback_errors:
                raise SpriteEditorError('保存未完成，已保护外部修改；请从完整备份核对恢复：'+str(folder)+'\n'+'\n'.join(rollback_errors)) from error
            raise
        return dict(backup=str(folder),summary=f'{len(changed)} 个文件已保存并重新读取核对；原件已备份。')

    def _backup_root(self):
        return _safe_path(self.root, '.recon/sprite_editor_backups')

    def _backup_files(self):
        return set(self._original_files) | {STATE_FILE}

    def _reopen(self):
        self.__init__(self.root)

    def list_backups(self):
        root=self._backup_root()
        if not root.is_dir():
            return []
        result=[]
        for path in sorted(root.iterdir(),reverse=True):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                data=json.loads((path/'manifest.json').read_text('utf8'))
                if data.get('schema')==1 and (data.get('completed') or data.get('backup_complete')):
                    label=path.name+' · '+data['label']+('' if data.get('completed') else '（保存未完成的原件）')
                    result.append(dict(id=path.name,label=label,path=str(path),files=list(data['files'])))
            except (OSError,ValueError,KeyError):
                continue
        return result

    def restore_backup(self, backup_path):
        self._check_external()
        allowed={row['path'] for row in self.list_backups()}
        folder=Path(backup_path).resolve()
        if str(folder) not in allowed:
            raise SpriteEditorError('只能恢复当前工程工具箱生成的完整备份。')
        manifest=json.loads((folder/'manifest.json').read_text('utf8'));changed={}
        valid=self._backup_files()
        for name,info in manifest['files'].items():
            if name not in valid:
                raise SpriteEditorError('备份包含未授权文件：'+name)
            if info['existed']:
                path=(folder/'original'/name)
                if not path.resolve().is_relative_to(folder) or path.is_symlink():
                    raise SpriteEditorError('备份路径无效。')
                raw=path.read_bytes()
                if sha(raw)!=info['before_sha256']:
                    raise SpriteEditorError('备份校验失败：'+name)
                changed[name]=raw
            else:
                changed[name]=None
        result=self._commit(changed,'恢复前自动备份')
        self._reopen()
        return result
