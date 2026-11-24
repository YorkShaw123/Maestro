"""DEF-driven package preview and transactional local export."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from mugen_character import CharacterError, local_path, read_character, reparse


class PackageError(RuntimeError):
    """An export cannot safely start, or its result is not usable."""


@dataclass(frozen=True)
class PackageInfo:
    root: Path
    destination: Path
    files: tuple[str, ...]
    total_bytes: int
    export_exists: bool
    missing_palettes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageResult:
    info: PackageInfo
    output: str


def inspect_package(root, *, definition=None, extras=()) -> PackageInfo:
    """Recommend the selected DEF and its local [Files] dependencies."""
    candidate = Path(os.path.abspath(Path(root).expanduser()))
    try:
        if any(reparse(node) for node in (candidate, *candidate.parents)):
            raise PackageError('角色目录不能使用链接或重解析点。')
        character = read_character(candidate, definition)
        source = character.root
        destination = source / Path(character.definition).stem
        # A wrapper directory can already contain the actual source folder
        # under the DEF stem. Keep that folder outside the export transaction.
        if any((source / name).is_relative_to(destination) for name in character.files):
            destination = source / (Path(character.definition).stem + '-export')
        palette_paths = {name for _, name in character.palettes}
        missing = tuple(name for name in character.missing if name not in palette_paths)
        if missing:
            raise PackageError('DEF 引用的文件缺失：' + '、'.join(missing))
        missing_palettes = tuple(name for name in character.missing if name in palette_paths)
        names = {}
        for name in (*character.files, *extras):
            item = local_path(source, name)
            if item.is_relative_to(destination) or item.relative_to(source).parts[0].casefold() in ('.git', '.recon'):
                raise PackageError(f'不能将导出目录或工具内部文件加入包：{name}')
            if not item.is_file():
                raise PackageError(f'找不到打包文件：{name}')
            relative = item.relative_to(source).as_posix()
            names.setdefault(relative.casefold(), relative)
        files = tuple(names.values())
        export_exists = os.path.lexists(destination)
        if export_exists and (reparse(destination) or not destination.is_dir()):
            raise PackageError('现有导出路径不是普通目录：' + str(destination))
        return PackageInfo(source, destination, files,
                           sum((source / name).stat().st_size for name in files), export_exists, missing_palettes)
    except (CharacterError, OSError) as error:
        raise PackageError(str(error)) from error


def _digest(path: Path) -> bytes:
    with path.open('rb') as handle:
        result = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(chunk)
    return result.digest()


def package_character(root, *, definition=None, extras=(), preview=None) -> PackageResult:
    """Stage a verified export before replacing the previous directory."""
    info = inspect_package(root, definition=definition, extras=extras)
    if preview is not None and info != preview:
        raise PackageError('源文件状态或导出位置已改变，请重新检查并确认。')
    try:
        archive_root = local_path(info.root, '.recon/package_exports')
        archive_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='staging-', dir=archive_root) as temporary:
            stage = Path(temporary) / info.destination.name
            stage.mkdir()
            for name in info.files:
                source = local_path(info.root, name)
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            for name in info.files:
                if _digest(local_path(info.root, name)) != _digest(stage / name):
                    raise PackageError('源文件在打包期间改变，请重新打包：' + name)
            current = inspect_package(info.root, definition=definition, extras=extras)
            if current != info:
                raise PackageError('打包清单或导出路径已改变，请重新确认。')
            archived = None
            if info.export_exists:
                archived = archive_root / ('previous-' + uuid.uuid4().hex)
                info.destination.rename(archived)
            try:
                stage.rename(info.destination)
            except OSError:
                if archived is not None and not os.path.lexists(info.destination):
                    archived.rename(info.destination)
                raise
    except (CharacterError, OSError) as error:
        raise PackageError('无法发布导出：' + str(error)) from error
    output = f'旧导出已备份到 {archived}' if archived is not None else '已创建角色导出。'
    return PackageResult(info, output)
