"""Read SFF v2 indexed and true-color images; edit palette bytes in place."""
from dataclasses import dataclass
from bisect import bisect_right
import struct


@dataclass(frozen=True)
class Palette:
    key: tuple[int, int]
    owner: int
    offset: int
    colors: int
    rgb: bytes
    alpha: bytes


@dataclass(frozen=True)
class V2Sprite:
    key: tuple[int, int]
    width: int
    height: int
    owner: int
    palette: int | None
    pixels: bytes
    rgba: bytes | None = None


def _require(condition, message):
    if not condition:
        raise ValueError('SFF v2：' + message)


def _pixels(payload, code, depth, width, height):
    size = width * height
    _require(0 < size <= 16_777_216, '精灵尺寸超出支持范围')
    if code == 0:
        _require(depth in (8, 24, 32) and len(payload) == size * (depth // 8), '原始像素长度或色深无效')
        if depth == 8:
            return payload, None
        if depth == 32:
            return b'', payload
        return b'', b''.join(payload[i:i+3] + b'\xff' for i in range(0, len(payload), 3))
    _require(len(payload) >= 4, '精灵数据不完整')
    data = payload[4:]
    if code in (10, 11, 12):
        from PySide6.QtGui import QImage
        _require(len(data) >= 29 and data.startswith(b'\x89PNG\r\n\x1a\n') and
                 struct.unpack_from('>II', data, 16) == (width, height), 'PNG 尺寸不匹配')
        image = QImage.fromData(data, 'PNG')
        _require(not image.isNull(), 'PNG 数据无效')
        if code == 10:
            if image.format() in (QImage.Format.Format_Mono, QImage.Format.Format_MonoLSB):
                return bytes(image.pixelIndex(x, y) for y in range(height) for x in range(width)), None
            _require(image.format() == QImage.Format.Format_Indexed8, 'PNG8 无法保留索引')
            stride = image.bytesPerLine()
            raw = image.constBits()
            return b''.join(bytes(raw[y*stride:y*stride+width]) for y in range(height)), None
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        return b'', bytes(image.constBits())
    _require(code in (2, 3, 4), f'未知像素编码 {code}')
    output = bytearray()
    cursor = 0

    def take():
        nonlocal cursor
        _require(cursor < len(data), '压缩数据提前结束')
        value = data[cursor]
        cursor += 1
        return value

    def run(color, count):
        _require(count > 0 and count <= size - len(output), '游程超出图像范围')
        output.extend(bytes((color,)) * count)

    if code == 2:
        while len(output) < size:
            token = take()
            if token & 0xc0 == 0x40:
                run(take(), token & 0x3f)
            else:
                run(token, 1)
    elif code == 3:
        while len(output) < size:
            length, packet = take(), take()
            run(take() if packet & 0x80 else 0, length + 1)
            for _ in range(packet & 0x7f):
                token = take()
                run(token & 31, (token >> 5) + 1)
    else:
        distance_bits, short_packets = 0, 0
        while len(output) < size:
            flags = take()
            for bit in range(8):
                if len(output) == size:
                    break
                token = take()
                if not flags & (1 << bit):
                    run(token & 31, (token >> 5) or take() + 8)
                    continue
                length = token & 63
                if length == 0:
                    distance = (token << 2 | take()) + 1
                    length = take() + 3
                else:
                    distance_bits |= (token & 192) >> (2 * short_packets)
                    short_packets += 1
                    if short_packets == 4:
                        distance = distance_bits + 1
                        distance_bits, short_packets = 0, 0
                    else:
                        distance = take() + 1
                    length += 1
                _require(0 < distance <= len(output) and length <= size - len(output), 'LZ5 引用范围无效')
                for _ in range(length):
                    output.append(output[-distance])
    return bytes(output), None


def read_sff_v2(raw):
    _require(len(raw) >= 68 and raw[:12] == b'ElecbyteSpr\0' and raw[15] == 2, '文件头无效')
    sprite_start, sprite_count, pal_start, pal_count, local, local_size, translated, translated_size = struct.unpack_from('<8I', raw, 36)
    _require(0 < sprite_count <= 100_000 and sprite_start >= 68 and
             sprite_start + 28 * sprite_count <= len(raw), '精灵表超出文件范围')
    _require(pal_count <= 65_536 and (not pal_count or pal_start >= 68) and
             pal_start + 16 * pal_count <= len(raw), '色表目录超出文件范围')
    _require(local + local_size <= len(raw) and translated + translated_size <= len(raw), '数据段超出文件范围')

    sections = [(sprite_start, sprite_start + 28 * sprite_count)]
    sections.extend((start, start + length) for start, length in (
        (pal_start, 16 * pal_count), (local, local_size), (translated, translated_size)) if length)
    sections.sort()
    _require(all(end <= following for (_, end), (following, _) in zip(sections, sections[1:])) and
             sections[0][0] >= 68, '文件区段重叠')
    palettes, stored = [], {}
    for i in range(pal_count):
        group, number, count, link, offset, length = struct.unpack_from('<4H2I', raw, pal_start + 16 * i)
        key = (group, number)
        if length == 0:
            _require(link < i, '色表链接无效')
            old = palettes[link]
            palettes.append(Palette(key, old.owner, old.offset, old.colors, old.rgb, old.alpha))
            continue
        _require(0 < length <= 1024 and length % 4 == 0 and offset + length <= local_size,
                 '色表载荷范围无效')
        start = local + offset
        existing = stored.get((start, length))
        if existing is not None:
            palettes.append(Palette(key, existing.owner, start, existing.colors, existing.rgb, existing.alpha))
            continue
        data = raw[start:start+length]
        rgb = b''.join(data[j:j+3] for j in range(0, length, 4)).ljust(768, b'\0')
        alpha = (bytes((0,)) + bytes((255,)) * 255) if raw[13] == 0 else data[3::4].ljust(256, b'\0')
        palettes.append(Palette(key, i, start, length // 4, rgb, alpha))
        stored[start, length] = palettes[-1]

    color_spans = sorted((start, start + length) for start, length in stored)
    _require(all(end <= following for (_, end), (following, _) in zip(color_spans, color_spans[1:])),
             '色表载荷重叠')
    color_ends = [end for _, end in color_spans]

    sprites = []
    for i in range(sprite_count):
        group, number, width, height, _, _, link, code, depth, offset, length, pal, flags = struct.unpack_from(
            '<4H2hH2B2I2H', raw, sprite_start + i * 28)
        key = (group, number)
        if length == 0:
            _require(link < i, '精灵链接无效')
            old = sprites[link]
            sprites.append(V2Sprite(key, old.width, old.height, old.owner, old.palette, old.pixels, old.rgba))
            continue
        base, limit = (translated, translated_size) if flags & 1 else (local, local_size)
        _require(offset + length <= limit, '精灵像素载荷超出数据段')
        first = bisect_right(color_ends, base + offset)
        _require(first == len(color_spans) or color_spans[first][0] >= base + offset + length,
                 '精灵像素与色表载荷重叠')
        pixels, rgba = _pixels(raw[base+offset:base+offset+length], code, depth, width, height)
        if rgba is None:
            _require(pal < len(palettes), '精灵引用了不存在的色表')
        sprites.append(V2Sprite(key, width, height, i, pal if rgba is None else None, pixels, rgba))
    return sprites, palettes


def replace_palettes(raw, palettes, changes):
    output = bytearray(raw)
    for owner, rgb in changes.items():
        palette = palettes[owner]
        for index in range(palette.colors):
            offset = palette.offset + index * 4
            output[offset:offset+3] = rgb[index*3:index*3+3]
    return bytes(output)
