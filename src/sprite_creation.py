"""Draft pixel-art color adaptation and additive SFF writes."""
from dataclasses import dataclass
import struct

from PySide6.QtGui import QImage, qRgba

from mugen_character import read_character
from mugen_palette import PaletteDocument
from normalize_character_palettes import palette_sources
from port_reference_assets import Image, encode_pcx, parse_sff
from sff_v2 import read_sff_v2
from sprite_editor import SpriteEditorError


class CreationError(SpriteEditorError):
    pass


MAX_SIZE = 512


@dataclass(frozen=True)
class IndexedDraft:
    width: int
    height: int
    pixels: bytes
    adjusted: int
    clipped_alpha: int
    ambiguous: int


def adapt_colors(image, palette, alpha, count=256):
    """Choose stable nearest colors. Index zero stays transparent."""
    width, height = image.width(), image.height()
    if not 0 < width <= MAX_SIZE or not 0 < height <= MAX_SIZE:
        raise CreationError(f'画布宽高必须在 1～{MAX_SIZE} 像素内。')
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    raw = rgba.constBits()
    choices = [(i, *palette[3*i:3*i+3]) for i in range(1, count) if alpha[i] > 0]
    if not choices:
        raise CreationError('这份色表没有可用的不透明色号。')
    cache, result = {}, bytearray()
    adjusted = clipped_alpha = ambiguous = 0
    for pos in range(0, width*height*4, 4):
        r,g,b,a = raw[pos:pos+4]
        if a < 128:
            result.append(0)
            clipped_alpha += a != 0
            continue
        key = r,g,b
        if key not in cache:
            index, cr, cg, cb = min(choices, key=lambda c:
                3*(c[1]-r)**2 + 6*(c[2]-g)**2 + 2*(c[3]-b)**2)
            exact = (cr,cg,cb) == key
            cache[key] = index, not exact, exact and sum(c[1:] == key for c in choices) > 1
        index, different, duplicate = cache[key]
        result.append(index)
        adjusted += different
        ambiguous += duplicate
        clipped_alpha += a != alpha[index]
    if not any(result):
        raise CreationError('适配后没有可见像素。')
    return IndexedDraft(width,height,bytes(result),adjusted,clipped_alpha,ambiguous)


def indexed_preview(draft, palette, alpha):
    image = QImage(draft.pixels, draft.width, draft.height, draft.width,
                   QImage.Format.Format_Indexed8).copy()
    image.setColorTable([qRgba(*palette[i*3:i*3+3], 0 if i == 0 else alpha[i]) for i in range(256)])
    return image


def _v1_insert(raw, sprites, source, key, axis, draft):
    offsets = []
    offset = struct.unpack_from('<I', raw, 24)[0]
    for _ in sprites:
        offsets.append(offset)
        offset = struct.unpack_from('<I', raw, offset)[0]
    # Insert directly after the actual palette owner. Subsequent existing
    # shared sprites still see that owner, and linked entries follow their image.
    ordinal = source + 1
    location = offsets[ordinal] if ordinal < len(offsets) else len(raw)
    palette = sprites[source].palette
    pcx = bytearray(128)
    pcx[:4] = bytes((10, 5, 1, 8))
    pcx[65] = 1
    struct.pack_into('<HHHH', pcx, 4, 0, 0, draft.width-1, draft.height-1)
    struct.pack_into('<H', pcx, 68, 1)
    payload = encode_pcx(Image(bytes(pcx),draft.width,draft.height,draft.pixels,palette),palette)[:-768]
    shift = 32 + len(payload)
    new_node = struct.pack('<IIhhHHHB13x', location+shift if ordinal < len(sprites) else 0,
                           len(payload), *axis, *key, 0, 1) + payload
    output = bytearray(raw[:location]+new_node+raw[location:])
    moved = [old + (shift if i >= ordinal else 0) for i,old in enumerate(offsets)]
    for i, (sprite, position) in enumerate(zip(sprites,moved)):
        if i+1 == ordinal:
            next_position = location
        elif i+1 < len(sprites):
            next_position = moved[i+1]
        else:
            continue
        struct.pack_into('<I',output,position,next_position)
    for sprite,position in zip(sprites,moved):
        if not sprite.raw_payload and sprite.link >= ordinal:
            struct.pack_into('<H',output,position+16,sprite.link+1)
    groups = len({sprite.group for sprite in sprites} | {key[0]})
    struct.pack_into('<II',output,16,groups,len(sprites)+1)
    candidate = bytes(output)
    _, checked = parse_sff(candidate)
    added = checked.pop(ordinal)
    if (added.key != key or (added.x, added.y) != axis or added.image.pixels != draft.pixels
            or added.palette != palette):
        raise CreationError('新精灵的 SFF v1 复核失败。')
    for previous,current in zip(sprites,checked):
        expected_link = previous.link + (not previous.raw_payload and previous.link >= ordinal)
        if (current.key,current.x,current.y,current.link,current.shared,current.reserved,current.raw_payload,current.palette) != (
                previous.key,previous.x,previous.y,expected_link,previous.shared,previous.reserved,previous.raw_payload,previous.palette):
            raise CreationError('原有精灵或共享色表保护核对失败。')
    return candidate


def _v2_insert(raw, slot, key, axis, draft):
    spr,count,pal,pals,local,size,translated,translated_size = struct.unpack_from('<8I',raw,36)
    record = struct.pack('<4H2hH2B2I2H', *key, draft.width,draft.height,*axis,
                         0,0,8,size,len(draft.pixels),slot,0)
    insertions = sorted(((spr+count*28, record), (local+size, draft.pixels)), key=lambda pair: pair[0])
    pieces, cursor = [], 0
    for offset,data in insertions:
        pieces.extend((raw[cursor:offset],data))
        cursor = offset
    pieces.append(raw[cursor:])
    output = bytearray(b''.join(pieces))

    def moved(offset):
        return offset + sum(len(data) for point,data in insertions if point <= offset)

    struct.pack_into('<8I',output,36,moved(spr),count+1,moved(pal),pals,
                     moved(local),size+len(draft.pixels),moved(translated),translated_size)
    candidate = bytes(output)
    checked, _ = read_sff_v2(candidate)
    new = checked[-1]
    if (new.key != key or new.palette != slot or new.pixels != draft.pixels
            or struct.unpack_from('<hh', candidate, moved(spr)+count*28+8) != axis):
        raise CreationError('新精灵的 SFF v2 复核失败。')
    if candidate[moved(spr):moved(spr)+count*28] != raw[spr:spr+count*28]:
        raise CreationError('原 SFF v2 精灵索引保护核对失败。')
    if candidate[moved(local):moved(local)+size] != raw[local:local+size]:
        raise CreationError('原 SFF v2 图像及色表载荷保护核对失败。')
    for offset, length in ((pal, pals*16), (translated, translated_size)):
        if candidate[moved(offset):moved(offset)+length] != raw[offset:offset+length]:
            raise CreationError('原 SFF v2 色表目录或翻译数据段保护核对失败。')
    return candidate


class SpriteSources:
    def __init__(self, root, definition):
        character = read_character(root, definition)
        if not character.sprite:
            raise CreationError('当前 DEF 缺少可读取的 sprite 文件。')
        self.document = PaletteDocument(character)
        self.name = self.document.character.sprite
        self.raw = self.document._original_files[self.name]
        self.entries = self.document.entries
        self.modes = self.document.modes
        self.version = self.document._version
        self.keys = {self.key(entry['id']) for entry in self.entries}
        self.default = next((e['id'] for e in self.entries if self.key(e['id']) == (0,0) and e['indexed']),
                            next((e['id'] for e in self.entries if e['indexed']), None))

    def key(self, entry):
        data = self.entries[entry]
        return data['group'],data['index']

    def image(self, entry, mode):
        row = self.document.sprite(entry,mode)
        if row.get('rgba') is not None:
            return QImage(row['rgba'],row['width'],row['height'],row['width']*4,
                          QImage.Format.Format_RGBA8888).copy()
        alpha = row.get('alpha') or bytes((0,)) + b'\xff'*255
        draft = IndexedDraft(row['width'],row['height'],row['pixels'],0,0,0)
        return indexed_preview(draft,row['palette'],alpha).convertToFormat(QImage.Format.Format_RGBA8888)

    def axis(self, entry):
        if self.version == 1:
            sprite = self.document._sprites[entry]
            return sprite.x,sprite.y
        offset = struct.unpack_from('<I',self.raw,36)[0]+28*entry
        return struct.unpack_from('<hh',self.raw,offset+8)

    def colors(self, entry, mode):
        if entry is None or not self.entries[entry]['indexed']:
            raise CreationError('请先选择一张有索引色表的参考精灵。')
        row = self.document.sprite(entry,mode)
        target = self.document._target(entry,mode,{})
        count = 256
        if self.version == 2:
            palettes = self.document._v2_palettes
            count = min(palettes[target[1]].colors, palettes[self._v2_slot(entry)].colors)
        return row['palette'], row.get('alpha') or bytes((0,))+b'\xff'*255, count

    def _v2_slot(self, entry):
        start = struct.unpack_from('<I', self.raw, 36)[0]
        return struct.unpack_from('<H', self.raw, start+self.document._owners[entry]*28+24)[0]

    def insert(self, key, axis, draft, reference):
        self.document._check_external()
        if key in self.keys:
            raise CreationError(f'编号 {key[0]},{key[1]} 已存在。')
        if not all(0 <= part <= 65535 for part in key) or not all(-32768 <= n <= 32767 for n in axis):
            raise CreationError('精灵编号或轴点超出 SFF 范围。')
        if reference is None or not self.entries[reference]['indexed']:
            raise CreationError('请选择已有的索引色表。')
        if (not 0 < draft.width <= MAX_SIZE or not 0 < draft.height <= MAX_SIZE
                or len(draft.pixels) != draft.width*draft.height or not any(draft.pixels)):
            raise CreationError('待写入的精灵尺寸或像素无效。')
        if self.version==1:
            sprites = self.document._sprites
            candidate = _v1_insert(self.raw,sprites,palette_sources(sprites)[reference],key,axis,draft)
        else:
            slot = self._v2_slot(reference)
            if max(draft.pixels) >= self.document._v2_palettes[slot].colors:
                raise CreationError('当前颜色索引超出了实际绑定色表。')
            candidate = _v2_insert(self.raw,slot,key,axis,draft)
        return self.document._commit({self.name:candidate},f'插入精灵 {key[0]},{key[1]}')
