"""DEF-driven palette document using the existing editor draft and backup flow."""
from collections import Counter
from pathlib import Path
import uuid

from mugen_character import read_character
from normalize_character_palettes import palette_sources, read_act
from port_reference_assets import parse_sff
from sff_v2 import read_sff_v2, replace_palettes
from sprite_editor import (SpriteDocument, SpriteEditorError, STATE_FILE,
                           _act_bytes, _read, _safe_path, _sff_bytes, sha)


def open_palette(root, definition=None):
    character = read_character(root, definition)
    if not character.sprite:
        raise SpriteEditorError('当前 DEF 缺少可读取的 sprite 文件。')
    # Preserve previously managed effect variants when their baseline can be read safely.
    if character.definition.casefold() == '1shun_ob.def' and (character.root / STATE_FILE).is_file():
        try:
            return SpriteDocument.open(character.root)
        except (ValueError, OSError):
            document = PaletteDocument(character)
            document.external_state_warning = (
                '已按外部修改后的 SFF 与实际色表重新载入。旧特效记录保留；本次保存仅编辑当前文件里的色表或像素。')
            return document
    return PaletteDocument(character)


class PaletteDocument(SpriteDocument):
    def __init__(self, character):
        self.character = character
        self.root = character.root
        self._original_files = {name: _read(self.root, name)
                                for name in (character.definition, character.sprite, *(path for _, path in character.palettes))}
        self._original_state = _read(self.root, STATE_FILE)
        raw = self._original_files[character.sprite]
        if raw is None or len(raw) < 16:
            raise SpriteEditorError('SFF 文件缺失或不完整。')
        self._version = raw[15]
        self.generated_count = 0
        self.external_state_warning = ''
        self._pending_bank_rebuild = False
        self._mode_single = {}
        self._fixed = set()
        self._single = {}
        self._pixels = {}
        self._palettes = {}
        self._undo, self._redo = [], []
        self._stroke = None
        self._revision = 0
        self._cached_plan = None
        self._preview_targets = {}
        self._rgba = {}
        if self._version == 1:
            self._prefix, sprites = parse_sff(raw)
            self._sprites = sprites
            self._groups = palette_sources(sprites)
            self._owners = []
            for sprite in sprites:
                self._owners.append(sprite.ordinal if sprite.raw_payload else self._owners[sprite.link])
            self._shapes = [(s.image.width, s.image.height, s.image.pixels) for s in sprites]
            self._palettes = {('v1', group): sprites[group].palette for group in set(self._groups)}
            for slot, name in character.palettes:
                data = self._original_files[name]
                if data is not None and len(data) in (768, 772):
                    target = ('act', name)
                    self._palettes[target] = read_act(data)
                    self._preview_targets[f'pal{slot} · {Path(name).name}'] = target
            ignored = [name for _, name in character.palettes
                       if len(self._original_files[name] or b'') not in (768, 772)]
            if ignored:
                self.external_state_warning = '无法读取以下 ACT，已载入其余色表：' + '、'.join(ignored)
            self._body_source = 0
        elif self._version == 2:
            sprites, self._v2_palettes = read_sff_v2(raw)
            self._groups = [self._v2_palettes[s.palette].owner if s.palette is not None else None for s in sprites]
            self._owners = [s.owner for s in sprites]
            self._shapes = [(s.width, s.height, s.pixels) for s in sprites]
            self._rgba = {i: s.rgba for i, s in enumerate(sprites) if s.rgba is not None}
            self._palettes = {('v2', p.owner): p.rgb for p in self._v2_palettes}
            self._body_slot = next((i for i, p in enumerate(self._v2_palettes) if p.key == (1, 1)), None)
            self._body_source = self._v2_palettes[self._body_slot].owner if self._body_slot is not None else None
            for palette in sorted(self._v2_palettes, key=lambda p: p.key):
                if palette.key[0] == 1 and palette.key[1] > 0:
                    self._preview_targets.setdefault(f'内置 1,{palette.key[1]}', ('v2', palette.owner))
        else:
            raise SpriteEditorError(f'不支持 SFF 版本 {self._version}。')
        self._keys = [s.key for s in sprites]
        self.modes = tuple(self._preview_targets) or ('SFF 内置色表',)
        counts = Counter(self._keys)
        self.entries = []
        for i, (group, number) in enumerate(self._keys):
            indexed = self._groups[i] is not None
            width, height, _ = self._shapes[i]
            self.entries.append(dict(id=i, group=group, index=number, width=width, height=height,
                palette_source=self._groups[i], duplicate=counts[group, number] > 1,
                category='truecolor' if not indexed else 'portrait' if group == 9000 else
                'body' if self._uses_character_palette(i) else 'effect',
                protected=False, indexed=indexed, can_paint=indexed and self._version == 1,
                can_isolate=indexed and self._version == 1))
        self._initial = self._snapshot()

    def _entry(self, entry_id):
        if not isinstance(entry_id, int) or not 0 <= entry_id < len(self.entries):
            raise SpriteEditorError('请选择有效精灵。')
        return self.entries[entry_id]

    def _uses_character_palette(self, entry_id):
        return self._body_source is not None and self._groups[entry_id] == self._body_source

    def _target(self, entry_id, mode, single):
        source, owner = self._groups[entry_id], self._owners[entry_id]
        if source is None:
            return None
        if owner in single:
            return ('single', owner)
        if self._uses_character_palette(entry_id) and mode in self._preview_targets:
            return self._preview_targets[mode]
        return ('v' + str(self._version), source)

    def _color_table(self, target, original=False):
        palettes, single = self._initial[:2] if original else (self._palettes, self._single)
        if target is None:
            return bytes(768)
        return single[target[1]] if target[0] == 'single' else palettes[target]

    def sprite(self, entry_id, mode=None, *, original=False):
        self._entry(entry_id)
        mode = mode or self.modes[0]
        width, height, source_pixels = self._shapes[entry_id]
        target = self._target(entry_id, mode, self._initial[1] if original else self._single)
        alpha = self._v2_palettes[target[1]].alpha if target and target[0] == 'v2' else None
        return dict(width=width, height=height, pixels=source_pixels if original else
                    self._pixels.get(self._owners[entry_id], source_pixels),
                    rgba=self._rgba.get(entry_id), palette=self._color_table(target, original), alpha=alpha,
                    transparent_index=0, palette_source=self._groups[entry_id])

    def original_sprite(self, entry_id, mode=None):
        return self.sprite(entry_id, mode, original=True)

    def affected(self, entry_id, mode=None, scope='palette'):
        self._entry(entry_id)
        source, owner = self._groups[entry_id], self._owners[entry_id]
        if source is None:
            return []
        if scope in ('sprite', 'pixels'):
            return [e for e in self.entries if self._owners[e['id']] == owner]
        mode = mode or self.modes[0]
        target = self._target(entry_id, mode, self._single)
        group = self.entries[entry_id]['group']
        return [e for e in self.entries if self._target(e['id'], mode, self._single) == target
                and (scope != 'group' or e['group'] == group)]

    def color_usage(self, entry_id, mode, color_index, scope='palette'):
        return [e for e in self.affected(entry_id, mode, scope)
                if color_index in self._pixels.get(self._owners[e['id']], self._shapes[e['id']][2])]

    def palette_family(self, entry_id, mode=None):
        source = self._groups[entry_id]
        if source is None:
            return dict(number=0, total=0, entries=[], source_ordinal=0, source_key=[], numbering='真彩色')
        families = sorted(set(self._groups) - {None})
        key = self._keys[source] if self._version == 1 else self._v2_palettes[source].key
        return dict(number=families.index(source)+1, total=len(families),
                    entries=self.affected(entry_id, mode), source_ordinal=source, source_key=key,
                    numbering='按实际色表来源编号')

    def source_summary(self, entry_id, mode=None, scope='palette'):
        mode = mode or self.modes[0]
        if self._groups[entry_id] is None:
            return '真彩色精灵，没有索引色表。'
        count = len(self.affected(entry_id, mode, scope))
        target = self._target(entry_id, mode, self._single)
        if scope == 'pixels':
            return f'更改索引像素会影响同一图片的 {count} 条链接。'
        if scope != 'palette':
            return f'为 {count} 条关联精灵建立固定色表。'
        if target[0] == 'act':
            return f'{mode} · 当前 ACT 色表 · {count} 条关联精灵'
        if target[0] == 'v2':
            group, number = self._v2_palettes[target[1]].key
            return f'内置色表 {group},{number} · {count} 条关联精灵'
        return f'内置色表 · {count} 条关联精灵，按实际共享关系保存。'

    def set_color(self, entry_id, mode, index, rgb, scope='palette'):
        entry = self._entry(entry_id)
        if not entry['indexed'] or mode not in self.modes or scope not in ('palette', 'sprite', 'group'):
            raise SpriteEditorError('请选择有效的索引色表及修改范围。')
        if not 1 <= index <= 255 or len(rgb) != 3 or any(not 0 <= n <= 255 for n in rgb):
            raise SpriteEditorError('色号须为 1～255，RGB 须为 0～255。')
        target = self._target(entry_id, mode, self._single)
        if target[0] == 'v2' and index >= self._v2_palettes[target[1]].colors:
            raise SpriteEditorError('该颜色编号超出实际色表长度。')
        if scope == 'palette':
            old = self._color_table(target)
            new = old[:3*index] + bytes(rgb) + old[3*index+3:]
            if old != new:
                self._record()
                (self._single if target[0] == 'single' else self._palettes)[
                    target[1] if target[0] == 'single' else target] = new
            return
        if not entry['can_isolate']:
            raise SpriteEditorError('SFF v2 在此页按已有色表编辑。')
        owners = sorted({self._owners[e['id']] for e in self.affected(entry_id, mode, scope)})
        if 0 in owners:
            raise SpriteEditorError('第一张精灵承载人物主色表，请改共用色表。')
        changes = {}
        for owner in owners:
            old = self._color_table(self._target(owner, mode, self._single))
            new = old[:3*index] + bytes(rgb) + old[3*index+3:]
            if old != new:
                changes[owner] = new
        if changes:
            self._record()
            self._single.update(changes)

    def paint(self, entry_id, x, y, index):
        entry = self._entry(entry_id)
        if not entry['can_paint'] or not (0 <= x < entry['width'] and 0 <= y < entry['height'] and 0 <= index <= 255):
            raise SpriteEditorError('当前精灵不支持此像素修改。')
        owner = self._owners[entry_id]
        old = self._pixels.get(owner, self._shapes[entry_id][2])
        position = y*entry['width']+x
        if old[position] != index:
            self._record()
            self._pixels[owner] = old[:position] + bytes((index,)) + old[position+1:]

    def _backup_files(self):
        return set(self._original_files) - {self.character.definition}

    def _backup_root(self):
        namespace = sha(self.character.definition.casefold().encode('utf8'))[:16]
        return _safe_path(self.root, f'.recon/palette_backups/{namespace}')

    def _reopen(self):
        self.__init__(read_character(self.root, self.character.definition))

    def plan_save(self):
        if not self.dirty:
            raise SpriteEditorError('没有待保存的修改。')
        self._check_external()
        colors = {key: value for key, value in self._palettes.items() if value != self._initial[0].get(key)}
        output = {name: _act_bytes(value, self._original_files[name])
                  for (kind, name), value in colors.items() if kind == 'act'}
        name = self.character.sprite
        original = self._original_files[name]
        if self._version == 1 and (self._pixels or self._single or any(key[0] == 'v1' for key in colors)):
            desired = [self._single.get(owner, self._palettes['v1', source])
                       for owner, source in zip(self._owners, self._groups)]
            candidate = _sff_bytes(self._prefix, self._sprites, desired, self._pixels)
            _, sprites = parse_sff(candidate)
            for i, (before, after) in enumerate(zip(self._sprites, sprites)):
                if (before.key, before.x, before.y, before.link, before.reserved, before.image.header) != (
                        after.key, after.x, after.y, after.link, after.reserved, after.image.header):
                    raise SpriteEditorError('图片身份或头部保护核对失败。')
                if after.palette != desired[i] or after.image.pixels != self._pixels.get(self._owners[i], before.image.pixels):
                    raise SpriteEditorError('像素或色表保护核对失败。')
            output[name] = candidate
        elif self._version == 2 and colors:
            output[name] = replace_palettes(original, self._v2_palettes, {owner: rgb for (_, owner), rgb in colors.items()})
        changed_ids = [i for i in range(len(self.entries)) if self._owners[i] in self._pixels
                       or any(self._target(i, mode, self._single) in colors for mode in self.modes)
                       or self._owners[i] in self._single]
        warnings = ['局部色表将作为固定色表使用。'] if self._single else []
        public = dict(summary=f'{len(output)} 个文件将更新并自动备份。', files=list(output),
                      changed_sprites=len(changed_ids), changed_palettes=len(colors)+len(self._single),
                      warnings=warnings, changed_ids=changed_ids, token=uuid.uuid4().hex)
        self._cached_plan = (public, output, self._revision)
        return public
