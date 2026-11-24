#!/usr/bin/env python3
"""Prepare a reversible, scoped SFF v1 / AIR / CNS / SND resource port.

Default operation writes a review bundle under .recon and never changes the four
character files. --apply builds and validates a fresh bundle, then replaces only
those four files, with backups and exception rollback. No game is launched.

The palette file must contain all 256 RGB triples. --palette-act-order indexed
means RGB triples already in PCX index 0..255 order (the palette88 extraction
used for this port). Ordinary MUGEN ACT files use the reverse triple order;
select --palette-act-order mugen for those. Pixel indices never change.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import uuid

GROUPS = frozenset({15800, 9020, 19230, 9005, 15801, 15802, 17230, 11231,
                    15150, 15402, 18300, 29025, 12250, 15920, 26184, 15952})
FILES = ("Shun.sff", "Shun.air", "Shun.cns", "Shun.snd")
MARKER = "PORT_REFERENCE_ASSETS_V1"
SOUND_KEYS = frozenset({(5010, i) for i in (8, 10, 11, 13, 14, 23)}
                      | {(15952, i) for i in range(5)})
MAX_NODES = 65535
MAX_PLANE = 128 * 1024 * 1024
FRAME_RE = re.compile(r"^(\s*)(-?\d+)(\s*,\s*)(-?\d+)(\s*,.*)$")
ACTION_RE = re.compile(r"(?im)^[ \t]*\[Begin[ \t]+Action[ \t]+(-?\d+)\][ \t]*(?:;[^\n]*)?$")
STATEDEF_RE = re.compile(r"(?im)^[ \t]*\[Statedef[ \t]+(-?\d+)\][ \t]*(?:;[^\n]*)?$")
CONTROLLER_RE = re.compile(r"(?im)^[ \t]*\[State[ \t]+[^\]\n]+\][^\n]*$")


class PortError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise PortError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def nl(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def crlf(text, encoding):
    return nl(text).replace("\n", "\r\n").encode(encoding)


def read_reference_text(data):
    try:
        return nl(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        # These reference files contain legacy non-ASCII comments. Executable
        # AIR/CNS tokens are ASCII; replacement is limited to source comments.
        return nl(data.decode("cp936", errors="replace"))


@dataclass(frozen=True)
class Image:
    header: bytes
    width: int
    height: int
    pixels: bytes
    local_palette: bytes | None


@dataclass(frozen=True)
class Sprite:
    ordinal: int
    group: int
    number: int
    x: int
    y: int
    image: Image
    palette: bytes
    reserved: bytes = b"\0" * 13
    raw_payload: bytes | None = None
    shared: int = 0
    link: int = 0
    linked_alias: bool = False

    @property
    def key(self):
        return (self.group, self.number)


@dataclass(frozen=True)
class Sound:
    group: int
    number: int
    payload: bytes

    @property
    def key(self):
        return (self.group, self.number)


@dataclass(frozen=True)
class Action:
    number: int
    body: str


def decode_pcx(payload):
    require(len(payload) >= 128, "PCX header is truncated")
    head = payload[:128]
    require(head[0] == 10 and head[2] in (0, 1) and head[3] == 8 and head[65] == 1,
            "Only 8-bit, one-plane PCX images are supported")
    xmin, ymin, xmax, ymax = struct.unpack_from("<HHHH", head, 4)
    width, height = xmax - xmin + 1, ymax - ymin + 1
    stride = struct.unpack_from("<H", head, 66)[0]
    require(width > 0 and height > 0 and stride >= width,
            "PCX dimensions/stride are invalid")
    require(stride * height <= MAX_PLANE, "PCX allocation limit exceeded")
    # Decode to the declared stride. Some reference PCX data overruns Pillow's
    # tightly packed decoder, although the actual declared scanlines are valid.
    palette = None
    limit = len(payload)
    expected = stride * height
    if head[2] == 0:
        require(128 + expected <= limit, "PCX raw scanlines are truncated")
        plane = payload[128:128 + expected]
        pos = 128 + expected
    else:
        plane = bytearray()
        pos = 128
        while len(plane) < expected:
            require(pos < limit, "PCX RLE scanlines are truncated")
            value = payload[pos]
            pos += 1
            count = 1
            if value & 0xC0 == 0xC0:
                count = value & 0x3F
                require(count > 0 and pos < limit, "PCX RLE run is truncated")
                value = payload[pos]
                pos += 1
            require(len(plane) + count <= expected, "PCX RLE exceeds declared stride")
            plane.extend(bytes((value,)) * count)
    # Palette metadata can only follow the complete RLE scanlines.
    if len(payload) - 769 >= pos and payload[-769] == 12:
        palette = payload[-768:]
    pixels = b"".join(plane[row * stride:row * stride + width] for row in range(height))
    return Image(head, width, height, pixels, palette)


def rle_encode(row):
    output = bytearray()
    index = 0
    while index < len(row):
        end = index + 1
        while end < len(row) and row[end] == row[index] and end - index < 63:
            end += 1
        count = end - index
        value = row[index]
        if count > 1 or value >= 0xC0:
            output.extend((0xC0 | count, value))
        else:
            output.append(value)
        index = end
    return bytes(output)


def encode_pcx(image, palette):
    require(len(palette) == 768, "An RGB palette must contain 768 bytes")
    require(len(image.pixels) == image.width * image.height, "PCX pixels are incomplete")
    header = bytearray(image.header)
    header[0], header[2], header[3], header[65] = 10, 1, 8, 1
    stride = (image.width + 1) & ~1
    struct.pack_into("<H", header, 66, stride)
    rows = []
    for row in range(image.height):
        data = image.pixels[row * image.width:(row + 1) * image.width]
        rows.append(rle_encode(data + b"\0" * (stride - image.width)))
    return bytes(header) + b"".join(rows) + b"\x0c" + palette


def parse_sff(data):
    require(len(data) >= 512 and data[:11] == b"ElecbyteSpr", "Not an SFF file")
    require(data[15] == 1, "Only SFF v1 is supported")
    count, first, subsize = struct.unpack_from("<III", data, 20)
    require(0 < count <= MAX_NODES and subsize == 32 and first >= 512,
            "SFF v1 count/first/subheader size is invalid")
    nodes, visited = [], set()
    current_palette = None
    off = first
    for ordinal in range(count):
        require(off not in visited and off >= first and off + 32 <= len(data),
                f"SFF node {ordinal} is outside the bounded linked list")
        visited.add(off)
        nxt, length, x, y, group, number, link = struct.unpack_from("<IIhhHHH", data, off)
        shared = data[off + 18]
        require(shared in (0, 1), "SFF shared-palette flag is invalid")
        if length:
            require(off + 32 + length <= len(data), "SFF PCX payload is truncated")
            image = decode_pcx(data[off + 32:off + 32 + length])
            if not shared:
                require(image.local_palette is not None, "Individual SFF image lacks a palette")
                current_palette = image.local_palette
            elif current_palette is None:
                require(image.local_palette is not None, "First shared SFF image lacks a palette")
                current_palette = image.local_palette
            palette = current_palette
        else:
            require(link < ordinal, "SFF linked sprite does not refer backwards")
            image = nodes[link].image
            # Linked sprites reuse the already decoded image and its effective
            # palette; they do not introduce a new palette into the PCX stream.
            palette = nodes[link].palette
        require(palette is not None, "SFF palette resolution failed")
        nodes.append(Sprite(ordinal, group, number, x, y, image, palette,
                            data[off + 19:off + 32],
                            data[off + 32:off + 32 + length], shared, link))
        if ordinal + 1 < count:
            require(nxt > off and nxt >= off + 32 + length,
                    "SFF node chain overlaps or ends before its declared count")
        off = nxt
    return data[:first], nodes


def build_sff(prefix, nodes, original_count=0):
    require(0 < len(nodes) <= MAX_NODES, "SFF node limit exceeded")
    head = bytearray(prefix)
    first = len(head)
    struct.pack_into("<III", head, 16, len({s.group for s in nodes}), len(nodes), first)
    output = bytearray(head)
    for ordinal, sprite in enumerate(nodes):
        preserve = ordinal < original_count and sprite.group not in GROUPS
        if sprite.linked_alias:
            payload, link, shared = b"", sprite.link, 0
        elif preserve:
            require(sprite.raw_payload is not None, "Missing original PCX payload")
            payload, link, shared = sprite.raw_payload, sprite.link, sprite.shared
        else:
            payload, link, shared = encode_pcx(sprite.image, sprite.palette), 0, 0
        off = len(output)
        nxt = off + 32 + len(payload) if ordinal + 1 < len(nodes) else 0
        output.extend(struct.pack("<IIhhHHHB", nxt, len(payload), sprite.x, sprite.y,
                                  sprite.group, sprite.number, link, shared))
        output.extend(sprite.reserved)
        output.extend(payload)
    return bytes(output)


def parse_snd(data):
    require(len(data) >= 24 and data[:11] == b"ElecbyteSnd", "Not an SND file")
    count, first = struct.unpack_from("<II", data, 16)
    require(count <= MAX_NODES and first >= 24, "SND count/first offset is invalid")
    sounds, visited = [], set()
    off = first
    for ordinal in range(count):
        require(off not in visited and off >= first and off + 16 <= len(data),
                f"SND node {ordinal} is outside its bounded list")
        visited.add(off)
        nxt, size, group, number = struct.unpack_from("<IIII", data, off)
        require(off + 16 + size <= len(data), "SND payload is truncated")
        sounds.append(Sound(group, number, data[off + 16:off + 16 + size]))
        if ordinal + 1 < count:
            require(nxt >= off + 16 + size, "SND chain ends or overlaps")
        off = nxt
    return data[:first], sounds


def build_snd(prefix, sounds):
    header = bytearray(prefix)
    struct.pack_into("<II", header, 16, len(sounds), len(prefix))
    output = bytearray(header)
    for ordinal, sound in enumerate(sounds):
        nxt = len(output) + 16 + len(sound.payload) if ordinal + 1 < len(sounds) else 0
        output.extend(struct.pack("<IIII", nxt, len(sound.payload), sound.group, sound.number))
        output.extend(sound.payload)
    return bytes(output)


def sprite_fingerprint(sprite):
    image = sprite.image
    return {
        "ordinal": sprite.ordinal, "key": list(sprite.key), "axis": [sprite.x, sprite.y],
        "size": [image.width, image.height], "indices_sha256": sha(image.pixels),
        "palette_sha256": sha(sprite.palette),
    }


def visual_key(sprite):
    image = sprite.image
    digest = hashlib.sha256(struct.pack("<IIhh", image.width, image.height, sprite.x, sprite.y))
    lut = [b"\0\0\0\0"] + [sprite.palette[i * 3:i * 3 + 3] + b"\xff" for i in range(1, 256)]
    for row in range(image.height):
        digest.update(b"".join(lut[p] for p in image.pixels[row * image.width:(row + 1) * image.width]))
    return digest.hexdigest()


def parse_actions(text):
    matches = list(ACTION_RE.finditer(text))
    return [Action(int(match[1]), text[match.start():matches[i + 1].start()
                                      if i + 1 < len(matches) else len(text)])
            for i, match in enumerate(matches)]


def frame_keys(body):
    result = []
    for line in body.splitlines():
        match = FRAME_RE.match(line)
        if match:
            result.append((int(match[2]), int(match[4])))
    return result


def relevant_actions(text):
    return [action for action in parse_actions(text)
            if any(group in GROUPS for group, _ in frame_keys(action.body))]


def canonical_action(body):
    lines = []
    for line in body.splitlines()[1:]:
        code = re.sub(r"\s+", "", line.split(";", 1)[0]).lower().rstrip(",")
        if code:
            lines.append(code)
    return "\n".join(lines)


def remap_action(action, mapping, available):
    output, missing = [], set()
    for line in action.body.splitlines():
        match = FRAME_RE.match(line)
        if match:
            key = (int(match[2]), int(match[4]))
            if key[0] >= 0:
                dest = mapping.get(key, key)
                if dest not in available:
                    missing.add(key)
                line = match[1] + str(dest[0]) + match[3] + str(dest[1]) + match[5]
        output.append(line.rstrip())
    return "\n".join(output), sorted(missing)


def port_sprites(original, references, palette, manifest):
    candidates = []
    for old in original:
        new = replace(old, palette=palette) if old.group in GROUPS else old
        candidates.append(new)
        if old.group in GROUPS:
            manifest["existing_target_recolored"].append({
                "before": sprite_fingerprint(old), "after": sprite_fingerprint(new)})
    # Reserve source indices in advance so collisions never steal a later
    # non-conflicting source index.
    occupied = {sprite.key for sprite in candidates}
    reserved = occupied | {sprite.key for sprites in references.values() for sprite in sprites
                           if sprite.group in GROUPS}
    next_number = {group: max((n for g, n in reserved if g == group), default=-1) + 1
                   for group in GROUPS}
    seen = {}
    for sprite in candidates:
        seen.setdefault(visual_key(sprite), sprite)
    mappings = {}
    for name, sprites in references.items():
        mapping = {}
        for old in sprites:
            if old.group not in GROUPS:
                continue
            recolored = replace(old, palette=palette)
            fingerprint = visual_key(recolored)
            found = seen.get(fingerprint)
            duplicate = found is not None
            if duplicate and old.key not in occupied:
                found = replace(recolored, ordinal=len(candidates), linked_alias=True,
                                link=found.ordinal, raw_payload=b"", shared=0)
                candidates.append(found)
                occupied.add(found.key)
            if found is None:
                group, number = old.key
                if (group, number) in occupied:
                    number = next_number[group]
                    while (group, number) in reserved or (group, number) in occupied:
                        number += 1
                    next_number[group] = number + 1
                require(0 <= number <= 65535, "SFF sprite index limit exceeded")
                found = replace(recolored, ordinal=len(candidates), number=number)
                candidates.append(found)
                occupied.add(found.key)
                seen[fingerprint] = found
            # First occurrence is the reference AIR lookup. Every physical
            # duplicate source record is nevertheless retained in the manifest.
            mapping.setdefault(old.key, found.key)
            manifest["sprite_mapping"].append({
                "source": name, "source_ordinal": old.ordinal,
                "source_key": list(old.key), "destination_key": list(found.key),
                "destination_ordinal": found.ordinal, "skipped_duplicate": duplicate,
                "before": sprite_fingerprint(old), "after": sprite_fingerprint(found),
                "recolored_visual_sha256": fingerprint,
            })
        mappings[name] = mapping
    return candidates, mappings


def port_air(original_text, source_texts, mappings, available, manifest):
    require(MARKER not in original_text, "This port has already been applied (AIR marker)")
    occupied = {action.number for action in parse_actions(original_text)}
    standard, new_actions, aliases = {}, [], {}
    for name, text in source_texts.items():
        for action in relevant_actions(text):
            body, missing = remap_action(action, mappings[name], available)
            if missing:
                manifest["blocked"].append({
                    "kind": "animation", "source": name, "action": action.number,
                    "missing_sprite_keys": [list(key) for key in missing],
                    "reason": "Reference animation uses sprites absent from both sources and target",
                    "source_body": action.body,
                })
                continue
            number = action.number
            if number not in standard:
                require(number not in occupied, f"Existing target action {number} conflicts with port")
                standard[number] = body
                aliases[number] = []
                occupied.add(number)
            elif canonical_action(standard[number]) == canonical_action(body):
                manifest["animation_duplicates"].append({"source": name, "action": number})
                continue
            else:
                number = 500000 + number
                require(number not in occupied, f"Alternate animation {number} is occupied")
                occupied.add(number)
                body = ACTION_RE.sub("[Begin Action " + str(number) + "]", body, count=1)
                aliases[action.number].append(number)
            new_actions.append(body.rstrip() + "\n")
            manifest["animations_added"].append({
                "source": name, "source_action": action.number, "action": number,
                "standard": number == action.number,
                "automatic_opponent_compatibility": number == action.number,
                "note": ("Standard opponent animation number" if number == action.number
                         else "Alternate artwork; opponents do not automatically call this alias"),
                "frame_count": len(frame_keys(body)),
            })
    require(standard, "No complete reference animations were found")
    new_text = original_text + "\n; " + MARKER + "_BEGIN\n"
    new_text += "\n".join(new_actions) + "; " + MARKER + "_END\n"
    return new_text, aliases


def port_cns(original_text, reference_text, aliases, manifest):
    require(MARKER not in original_text, "This port has already been applied (CNS marker)")
    matches = list(STATEDEF_RE.finditer(reference_text))
    sections = {int(match[1]): reference_text[match.start():matches[i + 1].start()
                if i + 1 < len(matches) else len(reference_text)]
                for i, match in enumerate(matches)}
    require(-2 in sections, "Reference CNS lacks -2")
    controllers = list(CONTROLLER_RE.finditer(sections[-2]))
    imported = []
    for i, match in enumerate(controllers):
        block = sections[-2][match.start():controllers[i + 1].start()
                             if i + 1 < len(controllers) else len(sections[-2])]
        anim_match = re.search(r"(?im)^[ \t]*triggerall[ \t]*=[ \t]*anim[ \t]*=[ \t]*(36699|15952)[ \t]*$", block)
        if not anim_match or not re.search(r"(?im)^[ \t]*type[ \t]*=[ \t]*PlaySnd[ \t]*$", block):
            continue
        anim = int(anim_match[1])
        expr = " || ".join("Anim = " + str(n) for n in [anim] + aliases.get(anim, []))
        block = block[:anim_match.start()] + "triggerall = " + expr + block[anim_match.end():]
        block = re.sub(r"(?im)^([ \t]*type[ \t]*=[ \t]*PlaySnd[ \t]*)$",
                       r"\1\ntriggerall = !IsHelper", block, count=1)
        block = CONTROLLER_RE.sub("[State -2, port reference " + str(anim) + " "
                                  + str(len(imported) + 1) + "]", block, count=1)
        # Keep only the controller code, not comments introducing a later state.
        lines = block.splitlines()
        while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith(";")):
            lines.pop()
        imported.append("\n".join(line.rstrip() for line in lines) + "\n")
        manifest["controllers_added"].append({
            "animation": anim, "aliases": aliases.get(anim, []),
            "type": "PlaySnd", "helper_guard": True,
        })
    require(len(imported) == 14, f"Expected 14 scoped PlaySnd controllers, found {len(imported)}")
    target_sections = list(STATEDEF_RE.finditer(original_text))
    locations = [(i, match) for i, match in enumerate(target_sections) if int(match[1]) == -2]
    require(len(locations) == 1, "Target CNS must have exactly one -2 section")
    index, match = locations[0]
    end = target_sections[index + 1].start() if index + 1 < len(target_sections) else len(original_text)
    insertion = ("\n; " + MARKER + "_BEGIN\n" + "\n".join(imported)
                 + "; " + MARKER + "_END\n\n")
    return original_text[:end] + insertion + original_text[end:]


def port_sounds(original, source, manifest):
    candidates = list(original)
    occupied = {sound.key for sound in original}
    first_source = {}
    for sound in source:
        first_source.setdefault(sound.key, sound)
    for key in sorted(SOUND_KEYS):
        require(key in first_source, f"Source sound {key} is missing")
        if key in occupied:
            manifest["sounds_reused"].append(list(key))
            continue
        sound = first_source[key]
        require(sound.payload[:4] == b"RIFF" and sound.payload[8:12] == b"WAVE",
                f"Source sound {key} is not a RIFF WAVE")
        candidates.append(sound)
        manifest["sounds_added"].append({
            "source": "Shun", "key": list(key), "payload_sha256": sha(sound.payload),
            "bytes": len(sound.payload)})
    return candidates


def validate_sff(before, after, palette):
    require(len(after) >= len(before), "SFF lost original records")
    for old, new in zip(before, after):
        require(old.key == new.key and (old.x, old.y) == (new.x, new.y),
                "Original SFF key/order/axis changed")
        require((old.image.width, old.image.height, old.image.pixels)
                == (new.image.width, new.image.height, new.image.pixels),
                "Original indexed pixels changed")
        if old.group not in GROUPS and old.raw_payload is not None:
            require((old.raw_payload, old.shared, old.link)
                    == (new.raw_payload, new.shared, new.link),
                    "Original non-target PCX payload/share/link changed")
        expected = palette if old.group in GROUPS else old.palette
        require(new.palette == expected, f"Unexpected palette change at original {old.ordinal}")
    for new in after[len(before):]:
        require(new.group in GROUPS and new.palette == palette,
                "An imported sprite exceeds the group/palette scope")
    require(Counter(s.key for s in after[:len(before)]) == Counter(s.key for s in before),
            "Original duplicate sprite records were lost")


def protected_hashes(root):
    paths = list(root.glob("*"))
    for folder in ("AI",):
        if (root / folder).is_dir():
            paths.extend((root / folder).rglob("*"))
    result = {}
    for path in paths:
        if path.is_file() and path.name not in FILES:
            result[str(path.relative_to(root))] = sha(path.read_bytes())
    return dict(sorted(result.items()))


def snapshot_inputs(root, references, palette_path):
    paths = [root / name for name in FILES]
    for folder in references.values():
        paths.extend(folder / ("ShunKof" + ext) for ext in (".sff", ".air", ".cns", ".snd"))
    paths.append(palette_path)
    return {str(path.resolve()): sha(path.read_bytes()) for path in paths}


def check_snapshot(snapshot):
    for path, digest in snapshot.items():
        require(sha(Path(path).read_bytes()) == digest, "Input changed during preparation: " + path)


def write_json(path, data):
    path.write_bytes(crlf(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8"))


def atomic_write(path, data):
    temporary = path.with_name("." + path.name + ".port-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def prepare(root, references, palette_path, palette_order):
    palette_bytes = palette_path.read_bytes()
    require(len(palette_bytes) in (768, 772), "Palette ACT must have 768 RGB bytes (optionally 4 metadata bytes)")
    palette = palette_bytes[:768]
    if palette_order == "mugen":
        palette = b"".join(palette[i:i + 3] for i in range(765, -1, -3))
    originals = {name: (root / name).read_bytes() for name in FILES}
    current_air = nl(originals["Shun.air"].decode("utf-8-sig"))
    current_cns = nl(originals["Shun.cns"].decode("cp936"))
    require(MARKER not in current_air and MARKER not in current_cns,
            "This port has already been applied; restore its recorded backup before rerunning")
    inputs = snapshot_inputs(root, references, palette_path)
    protected = protected_hashes(root)
    manifest = {
        "schema": 1, "marker": MARKER, "status": "prepared",
        "target": str(root), "groups": sorted(GROUPS),
        "source_priority": list(references),
        "palette": {"path": str(palette_path), "input_sha256": sha(palette_bytes),
                    "input_order": palette_order, "pcx_rgb_sha256": sha(palette),
                    "transparent_index": 0, "pixel_index_remapping": False},
        "input_hashes": inputs, "protected_hashes": protected,
        "sprite_mapping": [], "existing_target_recolored": [],
        "animations_added": [], "animation_duplicates": [], "controllers_added": [],
        "sounds_added": [], "sounds_reused": [], "blocked": [],
        "limitations": [
            "Static structure and preservation checks do not establish native MUGEN interoperability.",
            "Alternate animation numbers are stored artwork variants and are not automatically selected by opponents.",
            "Groups 17230 and 12250 have no associated animation in either source.",
            "Reference action 29055 is disabled because source sprites 15908,0..2 are missing.",
            "Existing target sprites, including repeated group/index records, retain their original record order.",
        ],
    }
    sff_prefix, before_sprites = parse_sff(originals["Shun.sff"])
    reference_sprites = {name: parse_sff((folder / "ShunKof.sff").read_bytes())[1]
                         for name, folder in references.items()}
    after_sprites, mappings = port_sprites(before_sprites, reference_sprites, palette, manifest)
    candidate_sff = build_sff(sff_prefix, after_sprites, len(before_sprites))
    _, decoded_after = parse_sff(candidate_sff)
    validate_sff(before_sprites, decoded_after, palette)
    manifest["sff_fingerprints"] = {
        "original": [sprite_fingerprint(s) for s in before_sprites],
        "candidate": [sprite_fingerprint(s) for s in decoded_after],
    }
    reference_air = {name: read_reference_text((folder / "ShunKof.air").read_bytes())
                     for name, folder in references.items()}
    candidate_air, aliases = port_air(current_air, reference_air, mappings,
                                     {sprite.key for sprite in decoded_after}, manifest)
    candidate_cns = port_cns(current_cns, read_reference_text(
        (references["Shun"] / "ShunKof.cns").read_bytes()), aliases, manifest)
    snd_prefix, before_sounds = parse_snd(originals["Shun.snd"])
    _, reference_sounds = parse_snd((references["Shun"] / "ShunKof.snd").read_bytes())
    after_sounds = port_sounds(before_sounds, reference_sounds, manifest)
    candidate_snd = build_snd(snd_prefix, after_sounds)
    _, decoded_sounds = parse_snd(candidate_snd)
    require(decoded_sounds[:len(before_sounds)] == before_sounds, "Existing SND records/payloads changed")
    candidates = {
        "Shun.sff": candidate_sff, "Shun.air": crlf(candidate_air, "utf-8"),
        "Shun.cns": crlf(candidate_cns, "cp936"), "Shun.snd": candidate_snd,
    }
    # Existing definitions remain verbatim apart from required newline encoding.
    require(candidate_air.startswith(current_air), "Existing AIR content changed")
    begin = candidate_cns.index("\n; " + MARKER + "_BEGIN\n")
    end = candidate_cns.index("; " + MARKER + "_END\n\n", begin) + len("; " + MARKER + "_END\n\n")
    require(candidate_cns[:begin] + candidate_cns[end:] == current_cns,
            "Existing CNS content changed outside the inserted controller block")
    manifest["files"] = {name: {"original_sha256": sha(originals[name]),
                              "candidate_sha256": sha(candidates[name]),
                              "original_bytes": len(originals[name]),
                              "candidate_bytes": len(candidates[name])} for name in FILES}
    manifest["validation"] = {
        "original_sprites": len(before_sprites), "candidate_sprites": len(decoded_after),
        "non_target_sprites_preserved": sum(s.group not in GROUPS for s in before_sprites),
        "existing_target_sprites_recolored": len(manifest["existing_target_recolored"]),
        "reference_sprite_records": len(manifest["sprite_mapping"]),
        "sprites_added": len(decoded_after) - len(before_sprites),
        "reference_duplicates_skipped": sum(m["skipped_duplicate"] for m in manifest["sprite_mapping"]),
        "animations_added": len(manifest["animations_added"]),
        "standard_animations_added": sum(m["standard"] for m in manifest["animations_added"]),
        "controllers_added": len(manifest["controllers_added"]),
        "sounds_added": len(manifest["sounds_added"]),
        "native_engine_tested": False,
    }
    check_snapshot(inputs)
    require(protected_hashes(root) == protected, "Protected files changed while preparing")
    return originals, candidates, manifest


def review_bundle(root, output, originals, candidates, manifest):
    require(output.is_relative_to(root / ".recon"), "Review output must be inside target .recon")
    output.mkdir(parents=True, exist_ok=False)
    (output / "original").mkdir()
    (output / "candidate").mkdir()
    for name in FILES:
        (output / "original" / name).write_bytes(originals[name])
        (output / "candidate" / name).write_bytes(candidates[name])
        require(sha((output / "original" / name).read_bytes()) == sha(originals[name]), "Backup readback failed")
        require(sha((output / "candidate" / name).read_bytes()) == sha(candidates[name]), "Candidate readback failed")
    manifest["bundle"] = str(output)
    write_json(output / "manifest.json", manifest)
    write_json(output / "protected_hashes.json", manifest["protected_hashes"])


def apply_bundle(root, output, originals, candidates, manifest):
    check_snapshot(manifest["input_hashes"])
    require(protected_hashes(root) == manifest["protected_hashes"], "Protected files changed before apply")
    written, rollback_errors = [], []
    try:
        for name in FILES:
            require((root / name).read_bytes() == originals[name], "Target changed before replace: " + name)
        for name in FILES:
            atomic_write(root / name, candidates[name])
            written.append(name)
        for name in FILES:
            require((root / name).read_bytes() == candidates[name], "Applied file readback failed: " + name)
        require(protected_hashes(root) == manifest["protected_hashes"], "Protected files changed during apply")
        manifest["status"] = "applied"
        write_json(output / "manifest.json", manifest)
        portable = {key: manifest[key] for key in (
            "schema", "marker", "status", "groups", "source_priority", "sprite_mapping",
            "existing_target_recolored", "animations_added", "animation_duplicates",
            "controllers_added", "sounds_added", "sounds_reused", "blocked",
            "limitations", "sff_fingerprints", "files", "validation", "protected_hashes")}
        portable["palette"] = {key: manifest["palette"][key] for key in (
            "input_order", "input_sha256", "pcx_rgb_sha256", "transparent_index",
            "pixel_index_remapping")}
        atomic_write(root / "tools" / "reference_asset_port_manifest.json",
                     crlf(json.dumps(portable, ensure_ascii=False, indent=2) + "\n", "utf-8"))
    except BaseException as failure:
        for name in reversed(written):
            try:
                require((root / name).read_bytes() == candidates[name],
                        "Concurrent target change prevents safe rollback: " + name)
                atomic_write(root / name, originals[name])
            except BaseException as rollback_failure:
                rollback_errors.append(str(rollback_failure))
        manifest["status"] = "rollback_incomplete" if rollback_errors else "rolled_back"
        manifest["apply_failure"] = str(failure)
        manifest["rollback_errors"] = rollback_errors
        write_json(output / "manifest.json", manifest)
        if rollback_errors:
            raise PortError("Apply failed and rollback needs inspection: " + "; ".join(rollback_errors)) from failure
        raise


def self_test():
    palette = bytes(range(256)) * 3
    header = bytearray(128)
    header[0], header[1], header[2], header[3], header[65] = 10, 5, 1, 8, 1
    struct.pack_into("<HHHH", header, 4, 0, 0, 2, 1)
    struct.pack_into("<H", header, 66, 4)
    image = Image(bytes(header), 3, 2, bytes((0, 192, 192, 255, 1, 2)), palette)
    decoded = decode_pcx(encode_pcx(image, palette))
    require(decoded.pixels == image.pixels and decoded.local_palette == palette,
            "PCX padding/RLE/transparent-index roundtrip failed")
    try:
        decode_pcx(bytes(header) + b"\xc0\x00")
    except PortError:
        pass
    else:
        raise PortError("Malformed PCX run was accepted")
    prefix = bytearray(512)
    prefix[:12] = b"ElecbyteSpr\0"
    prefix[15] = 1
    struct.pack_into("<III", prefix, 20, 1, 512, 32)
    a = Sprite(0, 19230, 0, 2, -3, image, palette)
    b = Sprite(1, 0, 0, 2, -3, image, palette)
    alternate = bytes(reversed(palette))
    after = [replace(a, palette=alternate), b, replace(b, ordinal=2, group=9020, palette=alternate)]
    sff = build_sff(bytes(prefix), after)
    _, decoded_sprites = parse_sff(sff)
    validate_sff([a, b], decoded_sprites, alternate)
    require(decoded_sprites[1].palette == palette and len(decoded_sprites) == 3,
            "Target recolor leaked into the following non-target sprite")
    invalid = bytearray(sff)
    struct.pack_into("<I", invalid, 20, 4)
    try:
        parse_sff(bytes(invalid))
    except PortError:
        pass
    else:
        raise PortError("SFF count/chain mismatch was accepted")
    snd_prefix = bytearray(24)
    snd_prefix[:12] = b"ElecbyteSnd\0"
    struct.pack_into("<II", snd_prefix, 16, 0, 24)
    sounds = [Sound(1, 2, b"same payload"), Sound(1, 2, b"duplicate key preserved")]
    require(parse_snd(build_snd(bytes(snd_prefix), sounds))[1] == sounds,
            "SND duplicate-record/payload roundtrip failed")
    print("PASS: PCX stride/RLE, malformed input, SFF bounded count, palette isolation, duplicate preservation, SND payloads")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--reference-root", type=Path,
                        default=Path("D:/全N格斗2/mugen1.0(格斗2020版)/chars"))
    parser.add_argument("--palette-act", type=Path)
    parser.add_argument("--palette-act-order", choices=("indexed", "mugen"), default="indexed")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply four validated candidate files after backup")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    parser.error("--palette-act must identify the approved 88/89 palette") if args.palette_act is None else None
    root = args.root.resolve()
    references = {name: (args.reference_root / name).resolve() for name in ("Shun", "ShunKof")}
    require(all(not folder.is_relative_to(root) for folder in references.values()),
            "Reference directories must be separate from the target")
    output = args.output.resolve() if args.output else (root / ".recon" /
        ("reference_port_" + datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_%f")))
    originals, candidates, manifest = prepare(root, references, args.palette_act.resolve(), args.palette_act_order)
    review_bundle(root, output, originals, candidates, manifest)
    if args.apply:
        apply_bundle(root, output, originals, candidates, manifest)
    else:
        manifest["status"] = "dry_run"
        write_json(output / "manifest.json", manifest)
    print(json.dumps({"status": manifest["status"], "bundle": str(output),
                      "validation": manifest["validation"], "blocked": [
                          {"action": b.get("action"), "missing_sprite_keys": b.get("missing_sprite_keys")}
                          for b in manifest["blocked"]]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PortError, OSError, UnicodeError, struct.error) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(2)
