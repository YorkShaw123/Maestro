#!/usr/bin/env python3
"""Prepare SFF v1 character palette sharing; --apply writes only Shun.sff.

No game, tests, packaging or installation is run. PCX headers, compressed pixel
streams, sprite keys, axes and duplicate occurrences are retained. A bundle with
the original, candidate and per-occurrence mapping is saved before replacement.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import struct

from port_reference_assets import parse_sff, require, sha


HUMAN_GROUPS = frozenset({
    0, 5, 6, 11, 15, 20, 21, 41, 42, 100, 120, 121, 122, 181, 190,
    195, 200, 210, 220, 230, 240, 250, 400, 410, 420, 430, 440, 600,
    610, 630, 704, 1000, 5000, 5001, 5002, 5010, 5011, 5012, 5020,
    5030, 5031, 5032, 5040, 5041, 5042, 5060, 5061, 5062, 5070,
    5071, 5072, 5300, 5400, 5510, 5520, 6340, 9005, 9020, 9105,
    11231, 12250, 15150, 15402, 15800, 15801, 15802, 15920,
    15952, 17230, 18300, 19230, 26184, 29025, 32050,
})
EXCEPTIONS = frozenset({(5040, 21), (5041, 11)})
# Ordinary/blue-flame projectile flight and disappearance frames must each
# explicitly own their palette, rather than rely on legacy shared-palette flags.
FIXED_PROJECTILE_GROUPS = frozenset({1010, 1015, 1020, 1025})
PROTECTED_FILES = (
    "Shun.air", "Shun.cns", "Shun.snd", "Shun.cmd", "AI/ai_main.cmd",
    "Shun-Azure.act", "Shun-Evil.act", "打包角色.bat",
)


def is_human(sprite):
    return sprite.group in HUMAN_GROUPS and sprite.key not in EXCEPTIONS


def read_act(data):
    require(len(data) in (768, 772), "Expected a complete 256-color ACT")
    return b"".join(data[i:i + 3] for i in range(765, -1, -3))


def pixel_payload(sprite):
    """Return header + exact encoded scanlines, excluding palette markers.

    FF3 shared PCXs can end with an orphan 0x0c marker without RGB data.
    Keeping that marker and appending another marker shifts MUGEN's palette
    read by one byte. Locate the scanline end instead of guessing from EOF.
    """
    payload = sprite.raw_payload
    require(payload, "A linked sprite has no PCX payload")
    stride = struct.unpack_from("<H", payload, 66)[0]
    expected = stride * sprite.image.height
    end = 128
    if payload[2] == 0:
        end += expected
        require(end <= len(payload), "Truncated raw PCX pixels")
    else:
        decoded = 0
        while decoded < expected:
            require(end < len(payload), "Truncated PCX RLE")
            value = payload[end]
            end += 1
            run = 1
            if value & 0xc0 == 0xc0:
                run = value & 0x3f
                require(run > 0 and end < len(payload), "Invalid PCX RLE")
                end += 1
            decoded += run
            require(decoded <= expected, "PCX RLE exceeds scanlines")
    trailer = payload[end:]
    if sprite.image.local_palette is not None:
        require(payload[-769:-768] == b"\x0c", "Missing PCX palette marker")
        trailer = trailer[:-769]
    require(trailer in (b"", b"\x0c"), "Unexpected data after PCX scanlines")
    return payload[:end]


def palette_sources(sprites):
    """Track palette identity, not color equality (ACT replaces source zero)."""
    sources = []
    current = None
    for sprite in sprites:
        if sprite.raw_payload:
            if not sprite.shared or (current is None and sprite.image.local_palette is not None):
                current = sprite.ordinal
            require(current is not None, "Shared PCX has no palette source")
            sources.append(current)
        else:
            require(sprite.link < sprite.ordinal, "Forward sprite link")
            sources.append(sources[sprite.link])
    return sources


def prepare(original, azure, evil):
    prefix, before = parse_sff(original)
    require(HUMAN_GROUPS <= {s.group for s in before}, "A requested human group is missing")
    require(EXCEPTIONS <= {s.key for s in before}, "An excluded sprite is missing")
    humans = [s for s in before if is_human(s)]
    fixed = [s for s in before if not is_human(s)]
    require(humans[0].key == (0, 0) and humans[0].raw_payload,
            "The character palette must start at an unlinked 0,0 sprite")
    # Stable partition retains occurrence ordering for duplicate group/index
    # keys, while all body PCXs inherit the very first palette. Later effects
    # start private palette chains, even when their RGB bytes equal Azure.
    ordered = humans + fixed
    positions = {s.ordinal: index for index, s in enumerate(ordered)}
    records = []
    current_palette = None
    private_chain = False
    for new_index, sprite in enumerate(ordered):
        human = is_human(sprite)
        link = sprite.link
        if not sprite.raw_payload:
            require(is_human(before[link]) == human,
                    "Cross-boundary sprite alias needs explicit review")
            link = positions[link]
            require(link < new_index, "Partition would create a forward link")
            payload, shared = b"", sprite.shared
        elif human:
            shared = int(new_index != 0)
            payload = pixel_payload(sprite)
            if not shared:
                payload += b"\x0c" + azure
            current_palette = azure
        else:
            # Keep existing private palettes and sharing where possible. A
            # formerly shared image at a moved boundary gets its exact palette
            # embedded, preserving its entire compressed pixel stream.
            shared = int(sprite.group not in FIXED_PROJECTILE_GROUPS
                         and bool(sprite.shared) and private_chain
                         and current_palette == sprite.palette)
            payload = sprite.raw_payload if shared else pixel_payload(sprite) + b"\x0c" + sprite.palette
            current_palette = sprite.palette
            private_chain = True
        records.append((sprite, payload, link, shared))

    output = bytearray(prefix)
    # Keep the source header and its palette mode. Only physical node order,
    # offsets, sprite links and palette trailers/flags need to change.
    for index, (sprite, payload, link, shared) in enumerate(records):
        end = len(output) + 32 + len(payload)
        next_offset = end if index + 1 < len(records) else 0
        output.extend(struct.pack("<IIhhHHHB", next_offset, len(payload),
                                  sprite.x, sprite.y, sprite.group, sprite.number,
                                  link, shared))
        output.extend(sprite.reserved)
        output.extend(payload)
    candidate = bytes(output)
    print("Candidate prepared; reading every sprite to check material integrity.", flush=True)
    after_prefix, after = parse_sff(candidate)
    require(after_prefix == prefix and len(after) == len(before), "SFF header/count changed")
    require(Counter(s.key for s in before) == Counter(s.key for s in after), "Sprite keys changed")
    sources = palette_sources(after)
    mappings = []
    for old in before:
        new = after[positions[old.ordinal]]
        human = is_human(old)
        require((old.key, old.x, old.y, old.reserved, old.image.header,
                 old.image.width, old.image.height, old.image.pixels)
                == (new.key, new.x, new.y, new.reserved, new.image.header,
                    new.image.width, new.image.height, new.image.pixels),
                f"Pixel/axis/metadata change at source occurrence {old.ordinal}")
        if old.raw_payload:
            require(pixel_payload(old) == pixel_payload(new), "Compressed pixel stream changed")
        else:
            require(not new.raw_payload and new.link == positions[old.link], "Alias changed")
        require(new.palette == (azure if human else old.palette), "Unexpected RGB palette change")
        require((sources[new.ordinal] == 0) == human, "ACT sharing leaks across the human boundary")
        if old.group in FIXED_PROJECTILE_GROUPS:
            require(new.raw_payload and not new.shared
                    and new.image.local_palette == old.palette,
                    "Projectile frame must explicitly own its original palette")
        # With either external ACT, only source zero is replaced. All other
        # sources retain their original 256-color palette and pixel indices.
        for selected in (azure, evil):
            effective = selected if sources[new.ordinal] == 0 else new.palette
            require(effective == (selected if human else old.palette), "ACT color isolation failed")
        mappings.append({"old_ordinal": old.ordinal, "new_ordinal": new.ordinal,
                         "group": old.group, "index": old.number, "human": human,
                         "old_palette_sha256": sha(old.palette),
                         "new_palette_sha256": sha(new.palette),
                         "new_palette_source": sources[new.ordinal],
                         "pixel_sha256": sha(old.image.pixels)})

    variations = []
    for palette in dict.fromkeys(s.palette for s in humans):
        members = [s for s in humans if s.palette == palette]
        used = set().union(*(set(s.image.pixels) for s in members)) - {0}
        changed = [i for i in range(256) if palette[i * 3:i * 3 + 3] != azure[i * 3:i * 3 + 3]]
        variations.append({"sha256": sha(palette), "sprite_count": len(members),
                           "different_azure_slots": changed,
                           "used_different_azure_slots": sorted(used.intersection(changed))})
    report = {
        "format": "character-palette-normalization-v1",
        "source_sff_sha256": sha(original), "candidate_sff_sha256": sha(candidate),
        "sff_version_preserved": list(original[12:16]),
        "human_groups": sorted(HUMAN_GROUPS), "human_group_count": len(HUMAN_GROUPS),
        "exceptions": [list(key) for key in sorted(EXCEPTIONS)],
        "sprite_count": len(before), "human_sprite_count": len(humans),
        "fixed_color_sprite_count": len(fixed),
        "explicit_private_projectile_groups": sorted(FIXED_PROJECTILE_GROUPS),
        "explicit_private_projectile_sprite_count": sum(
            s.group in FIXED_PROJECTILE_GROUPS for s in after),
        "human_group_sprite_counts": dict(sorted(Counter(s.group for s in humans).items())),
        "original_human_palette_variants": variations,
        "all_pixel_indices_and_compressed_streams_preserved": True,
        "all_axes_sizes_keys_aliases_and_duplicates_preserved": True,
        "all_nonhuman_palettes_preserved": True,
        "all_humans_use_palette_source_zero": True,
        "all_nonhumans_use_private_palette_sources": True,
        "human_azure_colors_normalized_to_act": True,
        "game_and_tests_run": False,
        "occurrences": mappings,
    }
    return candidate, report


def write_json(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Replace Shun.sff after saving and checking the bundle")
    parser.add_argument("--checkpoint", required=True, help="Pre-change Git commit recorded in the report")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "Shun.sff"
    original = source.read_bytes()
    protected = {name: sha((root / name).read_bytes()) for name in PROTECTED_FILES}
    azure = read_act((root / "Shun-Azure.act").read_bytes())
    evil = read_act((root / "Shun-Evil.act").read_bytes())
    print("Reading SFF v1 and preserving all original PCX pixel streams.", flush=True)
    candidate, report = prepare(original, azure, evil)
    bundle = root / ".recon" / ("evil_palette_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    bundle.mkdir(parents=True)
    (bundle / "original.sff").write_bytes(original)
    (bundle / "candidate.sff").write_bytes(candidate)
    report.update({"checkpoint": args.checkpoint, "bundle": str(bundle),
                   "protected_file_sha256": protected, "applied": False})
    write_json(bundle / "report.json", report)
    require(source.read_bytes() == original, "SFF changed during preparation; refusing replacement")
    require(all(sha((root / name).read_bytes()) == digest for name, digest in protected.items()),
            "A protected source changed during preparation; refusing replacement")
    if args.apply:
        temporary = root / "Shun.sff.palette-tmp"
        require(not temporary.exists(), "Temporary replacement already exists")
        created_temporary = False
        try:
            with temporary.open("xb") as handle:
                created_temporary = True
                handle.write(candidate)
            os.replace(temporary, source)
        finally:
            if created_temporary:
                temporary.unlink(missing_ok=True)
        require(sha(source.read_bytes()) == sha(candidate), "SFF replacement hash mismatch")
        report["applied"] = True
        write_json(bundle / "report.json", report)
        summary = {key: value for key, value in report.items() if key not in ("occurrences", "bundle")}
        write_json(root / "tools" / "character_palette_manifest.json", summary)
    print(json.dumps({"bundle": str(bundle), "applied": args.apply,
                      "humans": report["human_sprite_count"],
                      "fixed_color": report["fixed_color_sprite_count"],
                      "candidate_sha256": report["candidate_sff_sha256"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
