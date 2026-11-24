"""Discover a MUGEN character from its DEF and resolve local dependencies."""
from dataclasses import dataclass
import csv
import os
from pathlib import Path, PureWindowsPath
import re
import stat


class CharacterError(ValueError):
    pass


def reparse(path):
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) &
                                     getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def local_path(root, name):
    windows = PureWindowsPath(name)
    if windows.anchor or not windows.parts or any(part == '..' or ':' in part for part in windows.parts):
        raise CharacterError(f'路径必须在角色目录内：{name}')
    path = root
    for part in windows.parts:
        path /= part
        if reparse(path):
            raise CharacterError(f'角色文件不能使用链接或重解析点：{path}')
    if not path.resolve().is_relative_to(root):
        raise CharacterError(f'路径超出角色目录：{name}')
    return path


def _entries(path):
    raw = path.read_bytes()
    encodings = ('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else (
        'utf-8-sig', 'gb18030', 'cp932', 'latin-1')
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise CharacterError(f'DEF 文本编码无效：{path.name}')
    section = ''
    for line in text.splitlines():
        line = re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', line, maxsplit=1)[0].strip()
        if line.startswith('['):
            section = line.partition(']')[0][1:].strip().lower()
        elif '=' in line:
            key, value = line.split('=', 1)
            yield section, key.strip().lower(), value.strip()


def role_files(root):
    """Walk the workspace without following links or tool data directories."""
    for folder, directories, filenames in os.walk(root, followlinks=False):
        base = Path(folder)
        directories[:] = sorted((name for name in directories
                                 if name.casefold() not in {'.git', '.recon', '__macosx', '__pycache__'}
                                 and not reparse(base / name)), key=str.casefold)
        for name in sorted(filenames, key=str.casefold):
            path = base / name
            if not name.startswith('._') and not reparse(path):
                yield path.relative_to(root).as_posix()


def choices(root):
    """Collect character DEFs by relative path, including nested folders."""
    definitions = []
    for name in role_files(root):
        if Path(name).suffix.casefold() != '.def':
            continue
        try:
            if any(section == 'files' and key == 'sprite' and value
                   for section, key, value in _entries(root / name)):
                definitions.append(name)
        except (OSError, CharacterError):
            continue
    return tuple(sorted(definitions, key=lambda name: (name.count('/'), name.casefold())))


def preferred_definition(root, available):
    direct = [name for name in available if '/' not in name]
    exact = next((name for name in direct if Path(name).stem.casefold() == root.name.casefold()), None)
    if exact:
        return exact
    if len(direct) == 1:
        return direct[0]
    if len(available) == 1:
        return available[0]
    return None


def _reference(root, base, name):
    """Resolve DEF-relative references without allowing a workspace escape."""
    windows = PureWindowsPath(name)
    if windows.anchor or any(':' in part for part in windows.parts):
        raise CharacterError(f'DEF 引用必须位于角色目录内：{name}')
    parts = list(base.relative_to(root).parts)
    for part in windows.parts:
        if part == '..':
            if not parts:
                raise CharacterError(f'DEF 引用超出角色目录：{name}')
            parts.pop()
        else:
            parts.append(part)
    return local_path(root, '/'.join(parts))


@dataclass(frozen=True)
class Character:
    root: Path
    definition: str
    files: tuple[str, ...]
    sprite: str | None
    palettes: tuple[tuple[int, str], ...]
    missing: tuple[str, ...]


def read_character(root, definition=None):
    root = Path(root).resolve()
    if definition is None:
        available = choices(root)
        definition = preferred_definition(root, available)
        if definition is None:
            raise CharacterError('请选择角色目录中的 DEF 文件。' if available else '这个目录中没有角色 DEF 文件。')
    entry = local_path(root, definition)
    if entry.suffix.casefold() != '.def' or not entry.is_file():
        raise CharacterError(f'找不到角色 DEF：{definition}')
    definition = entry.relative_to(root).as_posix()
    names, missing, palettes = [definition], [], {}
    sprite = None
    for section, key, value in _entries(entry):
        if section != 'files' or not value:
            continue
        pal = re.fullmatch(r'pal(\d+)', key)
        if not pal and not re.fullmatch(r'cmd|cns|st\d*|stcommon\d*|sprite|anim|sound', key):
            continue
        values = next(csv.reader([value], skipinitialspace=True)) if key.startswith('st') else (value,)
        for filename in values:
            filename = filename.strip().strip('"')
            if not filename:
                continue
            common = key.startswith('stcommon') and PureWindowsPath(filename).name.casefold() == 'common1.cns'
            try:
                path = _reference(root, entry.parent, filename)
            except CharacterError:
                if common and '..' in PureWindowsPath(filename).parts:
                    continue
                raise
            name = path.relative_to(root).as_posix()
            if pal:
                palettes[int(pal[1])] = name
            elif key == 'sprite':
                sprite = name
            if not path.is_file():
                if not common:
                    missing.append(name)
                continue
            names.append(name)
    unique = {name.casefold(): name for name in reversed(names)}
    if sprite is None:
        raise CharacterError(f'DEF 未声明 sprite：{definition}')
    return Character(root, definition,
                     tuple(dict.fromkeys(unique[name.casefold()] for name in names)),
                     sprite, tuple(sorted(palettes.items())), tuple(dict.fromkeys(missing)))
