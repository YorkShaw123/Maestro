"""Build SFF-v1 Evil effect variants without writing character files.

The caller supplies an ungenerated Azure baseline on every save.  Only local
effect animations with fully understood call sites can receive a variant;
body/helper animation changes and ambiguous references fail closed.  Palettes
are 768 RGB bytes in PCX pixel-index order, not reversed ACT order.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import struct

from port_reference_assets import MAX_NODES, Sprite, encode_pcx, parse_sff


MARKER = "SPRITE_EDITOR_EVIL_V1"
ACTION = re.compile(r"^\s*\[Begin\s+Action\s+(-?\d+)\s*\]", re.I)
FRAME = re.compile(r"^(\s*)(-?\d+)(\s*,\s*)(-?\d+)(\s*,.*)$")
HEADER = re.compile(r"^\s*\[([^]\r\n]+)\]", re.I)
PARAM = re.compile(r"^(\s*([\w.]+)\s*=\s*)([^;\r\n]*?)(\s*)(;[^\r\n]*)?(\r?\n)?$")


class EffectVariantError(ValueError):
    """The supplied baseline or palette data is invalid."""


@dataclass(frozen=True)
class _Action:
    number: int
    line: int
    lines: tuple[str, ...]
    keys: frozenset[tuple[int, int]]


def _actions(text):
    lines = text.splitlines(keepends=True)
    starts = [(i, ACTION.match(line)) for i, line in enumerate(lines) if ACTION.match(line)]
    result = {}
    for index, (start, match) in enumerate(starts):
        stop = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        body = tuple(lines[start:stop])
        keys = frozenset((int(m[2]), int(m[4])) for line in body
                         if (m := FRAME.match(line.rstrip("\r\n"))))
        number = int(match[1])
        result.setdefault(number, []).append(_Action(number, start + 1, body, keys))
    return result


def _split_ifelse(expression):
    """Return condition/true/false only for one complete IfElse expression."""
    match = re.match(r"(?is)^ifelse\s*\((.*)\)$", expression.strip())
    if not match:
        return None
    body = match[1]
    depth, quote, pieces, start = 0, False, [], 0
    for index, char in enumerate(body):
        if char == '"':
            quote = not quote
        elif not quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return None
            elif char == "," and depth == 0:
                pieces.append(body[start:index].strip())
                start = index + 1
    pieces.append(body[start:].strip())
    return pieces if depth == 0 and not quote and len(pieces) == 3 else None


def _finite(expression):
    """Infer only literal animation numbers and finite IfElse branches."""
    expression = expression.strip()
    if re.fullmatch(r"-?\d+", expression):
        return {int(expression)}
    parts = _split_ifelse(expression)
    if parts:
        left, right = _finite(parts[1]), _finite(parts[2])
        if left is not None and right is not None:
            return left | right
    return None


def _branched(expression, mapping):
    """Branch animation leaves without evaluating original conditions twice.

    MUGEN IfElse eagerly evaluates its arguments. Wrapping two copies of an
    existing tree would double Random/variable assignments in its conditions.
    Keep the tree once and introduce palette checks only at literal leaves.
    """
    expression = expression.strip()
    if re.fullmatch(r"-?\d+", expression):
        value = int(expression)
        return (f"IfElse(Cond(IsHelper, (Root, PalNo), PalNo) = 2, {mapping[value]}, {expression})"
                if value in mapping else expression)
    parts = _split_ifelse(expression)
    if not parts:
        raise EffectVariantError("Cannot rewrite an unbounded animation expression")
    return f"IfElse({parts[0]}, {_branched(parts[1], mapping)}, {_branched(parts[2], mapping)})"


def _registered_inputs(repo, overrides=None):
    """Read only DEF-registered sources, plus the supported manual CMD entry."""
    root = repo.resolve()
    definition = (root / "1Shun_OB.def").read_bytes().decode("utf-8-sig")
    names, inside = [], False
    for raw in definition.splitlines():
        line = raw.split(";", 1)[0].strip()
        if line.startswith("["):
            inside = line.lower() == "[files]"
        elif inside and "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            if re.fullmatch(r"cns|st\d*|cmd", key, re.I):
                names.extend(part.strip().strip('"').replace("\\", "/")
                             for part in value.split(",") if part.strip())
    if (root / "Shun.cmd").is_file():
        names.append("Shun.cmd")
    result = {}
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise EffectVariantError("DEF source escapes the selected character folder")
        relative = path.relative_to(root).as_posix()
        if relative.lower() not in {item.lower() for item in result}:
            result[relative] = (overrides[relative] if overrides is not None and relative in overrides
                                else path.read_bytes())
    return result


def _source_blocks(raw):
    lines = raw.decode("cp936").splitlines(keepends=True)
    blocks, current, state = [], None, None
    for index, line in enumerate(lines):
        header = HEADER.match(line)
        if header:
            title = header[1].strip()
            statedef = re.fullmatch(r"statedef\s+(-?\d+)", title, re.I)
            if statedef:
                state = int(statedef[1])
            current = {"state": state, "header": title, "line": index + 1,
                       "is_statedef": bool(statedef), "params": {}, "entries": []}
            blocks.append(current)
        elif current and (match := PARAM.match(line)):
            key = match[2].lower()
            value = match[3].strip()
            current["params"][key] = value
            current["entries"].append((index, key, value, match))
    return lines, blocks


def _source_palette_ids(sprites):
    result, current = [], None
    for index, sprite in enumerate(sprites):
        if sprite.raw_payload:
            if not sprite.shared:
                current = index
            if current is None:
                raise EffectVariantError("The first sprite has no palette source")
            result.append(current)
        else:
            if not 0 <= sprite.link < index:
                raise EffectVariantError("Invalid backwards sprite alias")
            result.append(result[sprite.link])
    return result


def _append_sprites(raw, sprites, palettes):
    """Preserve old PCX payloads/links/order; append only standalone copies."""
    count, offset = struct.unpack_from("<II", raw, 20)
    if count != len(sprites) or count + len(palettes) > MAX_NODES:
        raise EffectVariantError("SFF sprite count mismatch or capacity exceeded")
    previous = None
    for _ in range(count):
        previous, offset = offset, struct.unpack_from("<I", raw, offset)[0]
    groups = {sprite.group for sprite in sprites}
    # AIR lookup in both native engines treats SFF-v1 groups as signed 16-bit.
    # High unsigned groups can parse in our reader yet draw nothing in MUGEN.
    free = (value for value in range(20000, 32768) if value not in groups)
    group_map, records, mapping = {}, [], {}
    for ordinal in sorted(palettes):
        sprite = sprites[ordinal]
        if sprite.group not in group_map:
            try:
                group_map[sprite.group] = next(free)
            except StopIteration as exc:
                raise EffectVariantError("No unused SFF group for an effect variant") from exc
        key = (group_map[sprite.group], sprite.number)
        payload = encode_pcx(sprite.image, palettes[ordinal])
        records.append((sprite, key, payload))
        mapping[sprite.key] = key
    output = bytearray(raw)
    if records:
        struct.pack_into("<I", output, previous, len(output))
        struct.pack_into("<II", output, 16, len(groups) + len(group_map), count + len(records))
    metadata = []
    for index, (sprite, key, payload) in enumerate(records):
        start = len(output)
        following = start + 32 + len(payload) if index + 1 < len(records) else 0
        output.extend(struct.pack("<IIhhHHHB", following, len(payload), sprite.x, sprite.y,
                                  key[0], key[1], 0, 0))
        output.extend(sprite.reserved)
        output.extend(payload)
        metadata.append({"source_ordinal": sprite.ordinal, "source_key": list(sprite.key),
                         "generated_ordinal": count + index, "generated_key": list(key),
                         "palette_sha256": hashlib.sha256(palettes[sprite.ordinal]).hexdigest()})
    return bytes(output), mapping, metadata


def build_effect_variant(repo: Path, sff_bytes: bytes, evil_palettes: dict[int, bytes],
                         base_sprites: list[Sprite], *,
                         source_files: dict[str, bytes] | None = None) -> dict:
    """Return a complete candidate or unchanged bytes plus explicit blockers.

    ``cns`` contains only changed source files.  No file is written.  Call this
    with the original, ungenerated AIR/CNS/SFF baseline for repeatable saves;
    an existing generation marker is rejected instead of duplicating output.
    """
    repo = Path(repo)
    overrides = ({name.replace("\\", "/"): raw for name, raw in source_files.items()}
                 if source_files is not None else {})
    air_raw = (overrides["Shun.air"] if "Shun.air" in overrides
               else (repo / "Shun.air").read_bytes())
    sources = _registered_inputs(repo, overrides)
    baseline = {"sff": sff_bytes, "air": air_raw, "cns": {}, "warnings": [],
                "blocked": [], "metadata": {"schema": 1, "marker": MARKER,
                "supported_ordinals": [], "generated_sprite_map": [],
                "generated_action_map": {}, "patched_calls": [], "unused_ordinals": [],
                "unused_actions": [],
                "baseline_sha256": {"Shun.air": hashlib.sha256(air_raw).hexdigest(),
                                    **{name: hashlib.sha256(raw).hexdigest()
                                       for name, raw in sources.items()}}}}
    if not evil_palettes:
        return baseline
    if MARKER.encode() in air_raw or any(MARKER.encode() in raw for raw in sources.values()):
        baseline["blocked"].append({"reason": "generated_baseline",
                                    "message": "Rebuild from the saved pre-generation baseline"})
        return baseline
    _, parsed = parse_sff(sff_bytes)
    if len(parsed) != len(base_sprites):
        raise EffectVariantError("base_sprites must describe the supplied SFF bytes")
    for index, (actual, supplied) in enumerate(zip(parsed, base_sprites)):
        if (actual.ordinal != supplied.ordinal or actual.key != supplied.key
                or actual.x != supplied.x or actual.y != supplied.y
                or actual.image.pixels != supplied.image.pixels
                or actual.palette != supplied.palette):
            raise EffectVariantError(f"base_sprites differs from candidate SFF at {index}")
    palettes = {}
    identities = _source_palette_ids(parsed)
    key_counts = {}
    for sprite in parsed:
        key_counts[sprite.key] = key_counts.get(sprite.key, 0) + 1
    for ordinal, palette in evil_palettes.items():
        if type(ordinal) is not int or not 0 <= ordinal < len(parsed):
            raise EffectVariantError("Effect sprite ordinal is outside the SFF")
        if not isinstance(palette, bytes) or len(palette) != 768:
            raise EffectVariantError("Evil palettes must be 768 RGB bytes in PCX index order")
        sprite = parsed[ordinal]
        incomplete_duplicate = key_counts[sprite.key] != 1 and any(
            other.key == sprite.key and other.ordinal not in evil_palettes for other in parsed)
        reason = ("body_palette_uses_act" if identities[ordinal] == 0 else
                  "portrait_is_fixed" if sprite.group == 9000 else
                  "duplicate_sprite_key_select_all_occurrences" if incomplete_duplicate else None)
        if reason:
            baseline["blocked"].append({"ordinal": ordinal, "sprite_key": list(sprite.key),
                                        "reason": reason})
        palettes[ordinal] = palette
    if baseline["blocked"]:
        return baseline
    selected_keys = {parsed[index].key for index in palettes}
    air_text = air_raw.decode("utf-8-sig")
    actions = _actions(air_text)
    target_actions = {number for number, variants in actions.items()
                      if any(action.keys & selected_keys for action in variants)}
    referenced = set().union(*(action.keys for number in target_actions for action in actions[number])) if target_actions else set()
    for ordinal in sorted(palettes):
        if parsed[ordinal].key not in referenced:
            baseline["metadata"]["unused_ordinals"].append(ordinal)
            baseline["warnings"].append(
                f"图片 {parsed[ordinal].group},{parsed[ordinal].number}：当前无播放入口；保存独立配色，不声称游戏中已显示")
    parsed_sources = {name: _source_blocks(raw) for name, raw in sources.items()}
    sites, covered = [], set()
    helper_states=set()
    for _,blocks in parsed_sources.values():
        for block in blocks:
            if block["params"].get("type","").lower()=="helper":
                helper_states.update(_finite(block["params"].get("stateno","")) or ())
    changed=True
    while changed:
        changed=False
        for _,blocks in parsed_sources.values():
            for block in blocks:
                if block["state"] in helper_states and block["params"].get("type","").lower()=="changestate":
                    before=len(helper_states)
                    helper_states.update(_finite(block["params"].get("value","")) or ())
                    changed|=len(helper_states)!=before
    root_entries=set()
    for _,blocks in parsed_sources.values():
        for block in blocks:
            if block["state"] not in helper_states and block["params"].get("type","").lower() in {"changestate","selfstate","targetstate","p1stateno"}:
                root_entries.update(_finite(block["params"].get("value","")) or ())
    helper_states-=root_entries
    # A graphics-only helper may display a private effect AIR directly.  Permit
    # only a state made exclusively of a neutral, stationary definition and
    # Null/DestroySelf controllers. Attack helpers and human animations never
    # qualify. The guard is structural, so later edits invalidate this proof.
    pure_effect_states = set()
    for name, (_, blocks) in parsed_sources.items():
        grouped = {}
        for block in blocks:
            grouped.setdefault(block["state"], []).append(block)
        for state, items in grouped.items():
            definitions = [block for block in items if block["is_statedef"]]
            if len(definitions) != 1:
                continue
            params = definitions[0]["params"]
            domain = _finite(params.get("anim", ""))
            controllers = [block["params"].get("type", "").lower()
                           for block in items if not block["is_statedef"]]
            keys = set().union(*(action.keys for number in domain
                                 if number in actions for action in actions[number])) if domain else set()
            keys={key for key in keys if key[0]>=0}
            key_lookup = {sprite.key: index for index, sprite in enumerate(parsed)}
            passive=controllers and all(kind in {"null","destroyself"} for kind in controllers)
            proven_helper=(state in helper_states and params.get("movetype","").upper() in {"I","A"}
                and all(kind in {"null","destroyself","bindtoparent","playsnd","nothitby",
                    "hitby","playerpush","changestate","varset","varadd","trans","angledraw",
                    "superpause","envcolor","explod","hitdef","poweradd","envshake",
                    "afterimage"} for kind in controllers)
                and not any(re.search(r"\bAnim\b", value, re.I)
                    for block in items for key,value in block["params"].items()
                    if key!="anim"))
            if (domain and domain & target_actions and (passive or proven_helper)
                    and params.get("physics", "").upper() == "N"
                    and params.get("velset", "0,0").replace(" ", "") == "0,0"
                    and keys and all(key in key_lookup and identities[key_lookup[key]] != 0
                                     and key[0] != 9000 for key in keys)):
                pure_effect_states.add((name, state))
    # Infer current-animation domains to handle the two literal-body Explods
    # anim=Anim. Unknown domains remain blockers, never guessed to be body-only.
    domains = {}
    for name, (_, blocks) in parsed_sources.items():
        for block in blocks:
            kind = block["params"].get("type", "").lower()
            for _, key, value, _ in block["entries"]:
                is_body = block["is_statedef"] and key == "anim"
                is_change = kind in {"changeanim", "changeanim2"} and key == "value"
                if is_body or is_change:
                    domain = _finite(value)
                    item = domains.setdefault((name, block["state"]), {"known": set(), "unknown": False})
                    if domain is None:
                        item["unknown"] = True
                    else:
                        item["known"].update(domain)
    for name, (_, blocks) in parsed_sources.items():
        for block in blocks:
            kind = block["params"].get("type", "").lower()
            for index, key, value, match in block["entries"]:
                prefix, expression = "", value
                safe = ((kind == "explod" and key == "anim") or
                        (kind == "projectile" and key in
                         {"projanim", "projhitanim", "projremanim", "projcancelanim"}))
                spark = key in {"sparkno", "guard.sparkno"} and kind in {"hitdef", "projectile"}
                pause = kind == "superpause" and key == "anim"
                body = ((block["is_statedef"] and key == "anim") or
                        (kind in {"changeanim", "changeanim2"} and key == "value"))
                if body and block["is_statedef"] and (name, block["state"]) in pure_effect_states:
                    safe, body = True, False
                if not (safe or spark or pause or body):
                    continue
                if spark or pause:
                    if not expression[:1].lower() == "s":
                        continue  # fightfx/system source, not character sprites
                    prefix, expression = expression[:1], expression[1:].strip()
                    safe = True
                elif expression[:1].lower() == "f":
                    baseline["warnings"].append(f"{name}:{index + 1}: external fightfx animation {value}")
                    continue
                domain = _finite(expression)
                if expression.lower() == "anim" and safe:
                    item = domains.get((name, block["state"]))
                    if item and not item["unknown"] and not (item["known"] & target_actions):
                        continue
                if domain is None:
                    if not safe and not body:
                        continue
                    # Arithmetic body gethit routing cannot target these dedicated
                    # effect actions unless the selected ID appears explicitly.
                    # Effect invocation expressions must be fully bounded.
                    if target_actions and (safe or (set(map(int, re.findall(r"\b\d+\b", expression))) & target_actions)):
                        baseline["blocked"].append({"file": name, "line": index + 1,
                            "state": block["state"], "expression": value,
                            "reason": "unbounded_animation_expression"})
                    continue
                overlap = domain & target_actions
                if not overlap:
                    continue
                if not safe or body:
                    baseline["blocked"].append({"file": name, "line": index + 1,
                        "state": block["state"], "actions": sorted(overlap),
                        "reason": "body_or_helper_animation_reference"})
                    continue
                covered.update(overlap)
                sites.append((name, index, key, value, prefix, expression, match,
                              block["state"], kind))
    helper_actions = set()
    permitted_lines = {(name, index) for name, index, *_ in sites}
    for name, (_, blocks) in parsed_sources.items():
        for block in blocks:
            if block["is_statedef"] and (name, block["state"]) in pure_effect_states:
                helper_actions.update(_finite(block["params"]["anim"]) or ())
    if helper_actions:
        for name, (lines, _) in parsed_sources.items():
            for index, line in enumerate(lines):
                code = line.split(";", 1)[0]
                for match in re.finditer(r"\bAnim\s*(?:=|!=|<>|>=|<=|>|<)\s*(\d+)\b", code, re.I):
                    if int(match[1]) in helper_actions and (name, index) not in permitted_lines:
                        baseline["blocked"].append({"file": name, "line": index + 1,
                            "action": int(match[1]), "reason": "helper_anim_comparison_dependency"})
    for number in sorted(target_actions - covered):
        # An AIR entry without any registered call has no runtime branch to
        # redirect. Keep its provenance explicit instead of inventing a caller.
        baseline["metadata"]["unused_actions"].append(number)
        baseline["warnings"].append(f"动作 {number}：当前无播放入口；独立配色副本暂未被调用")
    if baseline["blocked"]:
        return baseline
    used_numbers = set(actions)
    for raw in sources.values():
        used_numbers.update(map(int, re.findall(rb"\b\d+\b", raw)))
    next_action, action_map = 1_000_000, {}
    for number in sorted(target_actions):
        while next_action in used_numbers:
            next_action += 1
        action_map[number] = next_action
        used_numbers.add(next_action)
        next_action += 1
    sff, sprite_map, sprite_metadata = _append_sprites(sff_bytes, base_sprites, palettes)
    additions = []
    for number,action in [(number,action) for number in sorted(target_actions) for action in actions[number]]:
        copied = []
        for index, line in enumerate(action.lines):
            if index == 0:
                copied.append(re.sub(r"(?i)(\[Begin\s+Action\s+)-?\d+",
                                     lambda m: m[1] + str(action_map[number]), line, count=1))
            else:
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                frame = FRAME.match(line.rstrip("\r\n"))
                key = (int(frame[2]), int(frame[4])) if frame else None
                if key in sprite_map:
                    group, item = sprite_map[key]
                    line = f"{frame[1]}{group}{frame[3]}{item}{frame[5]}{ending}"
                copied.append(line)
        body = "".join(copied).replace("\r\n", "\n").replace("\n", "\r\n")
        additions.append(f"; {MARKER} source=Shun.air:{action.line} action={number}\r\n" + body)
    air = air_raw
    if additions:
        air += (b"" if air_raw.endswith(b"\n") else b"\r\n") + b"\r\n"
        air += ("\r\n".join(additions)).encode("utf-8")
    changed, edits = {}, []
    for name, index, key, value, prefix, expression, match, state, kind in sites:
        lines = changed.setdefault(name, list(parsed_sources[name][0]))
        # Root redirection is invalid on a root player. The generated Cond
        # consults Root only for Helpers and uses ordinary PalNo on the root.
        rewritten = prefix + _branched(expression, action_map)
        lines[index] = match[1] + rewritten + match[4] + (match[5] or "") + (match[6] or "")
        edits.append({"file": name, "line": index + 1, "state": state,
                      "controller": "statedef" if key == "anim" and kind not in {"explod", "superpause"} else kind,
                      "parameter": key, "before": value, "after": rewritten})
    baseline.update(sff=sff, air=air, cns={name: "".join(lines).encode("cp936")
                                        for name, lines in changed.items()})
    baseline["metadata"].update(supported_ordinals=sorted(palettes),
        generated_sprite_map=sprite_metadata,
        generated_action_map={str(key): value for key, value in action_map.items()},
        patched_calls=edits)
    # Parsing the result verifies bounded chains, aliases, palettes and all PCX
    # payloads before the caller can offer a transaction for saving.
    _, verified = parse_sff(sff)
    if len(verified) != len(base_sprites) + len(palettes):
        raise EffectVariantError("Generated SFF failed structural validation")
    return baseline
