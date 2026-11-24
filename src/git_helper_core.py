"""Local Git operations for the character's small desktop helper."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


class GitError(RuntimeError):
    pass


@dataclass
class Change:
    status: str
    path: str
    old_path: str = ''


@dataclass
class Commit:
    oid: str
    short: str
    time: str
    author: str
    subject: str


@dataclass
class RestoreResult:
    oid: str
    target: str
    backup_oid: str | None = None
    changed: bool = True


# Preserve the complete toolbox runtime when restoring an older character.
KEEP_PATHS = (
    'tools/git_helper.py',
    'tools/git_helper_core.py',
    'tools/check_git_helper.py',
    'docs/维护/Git使用与小工具.md',
    '角色工具箱.bat',
    'tools/launch_toolbox.bat',
    'tools/character_toolbox.py',
    'tools/toolbox_ui.py',
    'tools/character_package.py',
    'tools/pack_character.ps1',
    'docs/维护/角色工具箱.md',
    'docs/维护/安装到MUGEN.md',
    'tools/check_character_package.py',
    'tools/check_character_toolbox.py',
    'tools/bootstrap_toolbox.py',
    'tools/requirements-toolbox.txt',
    '角色工具箱.vbs',
    'tools/launch_toolbox.vbs',
    'tools/check_toolbox_bootstrap.py',
    'tools/sprite_editor.py',
    'tools/mugen_character.py',
    'tools/mugen_palette.py',
    'tools/sff_v2.py',
    'tools/palette_assistant.py',
    'tools/sprite_editor_ui.py',
    'tools/sprite_creation.py',
    'tools/sprite_creation_ui.py',
    'tools/check_sprite_creation.py',
    'tools/ai_workbench.py',
    'tools/dockyard.py',
    'tools/dockyard_templates/README.md',
    'tools/dockyard_templates/状态与资源.md',
    'tools/dockyard_templates/AI与连段.md',
    'tools/dockyard_templates/调色板与精灵.md',
    'tools/dockyard_templates/测试与性能.md',
    'tools/ai_workbench_expression.py',
    'tools/ai_workbench_ui.py',
    'tools/ai_workbench_graph.py',
    'tools/ai_workbench_runner.py',
    'tools/check_ai_workbench.py',
    'tools/inspect_ai_moves.py',
    'tools/sprite_editor_effects.py',
    'tools/port_reference_assets.py',
    'tools/normalize_character_palettes.py',
    'tools/check_sprite_editor.py',
    'tools/check_sprite_editor_ui.py',
    'tools/check_sprite_editor_effects.py',
    'tools/sprite_editor_results_20261102.json',
    'tools/sprite_editor_reconcile.py',
    'tools/sprite_color_presets.py',
    'tools/check_sprite_editor_reconcile.py',
    'tools/sprite_editor_followup_20261103.json',
)


RECENT_LIMIT = 30
FAVORITE_LIMIT = 10
FAVORITE_PREFIX = 'refs/mugen-toolbox/favorites/'


@contextmanager
def workspace_write_lock(lock_path):
    if GitRepo._reparse(lock_path):
        raise GitError('工具箱锁路径是链接，已停止写入。')
    with lock_path.open('a+b') as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise GitError('另一个工具箱进程正在写入，请稍后重试。') from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GitRepo:
    @classmethod
    def initialize(cls, path):
        directory = Path(path).resolve(strict=True)
        if not directory.is_dir() or os.path.lexists(directory / '.git'):
            raise GitError('该目录无效，或已经存在 Git 仓库。')
        # Use the same isolated environment and hidden subprocess as ordinary Git commands.
        runner = cls.__new__(cls)
        runner.root = directory
        runner._git('init', '--initial-branch=main')
        exclude = directory / '.git' / 'info' / 'exclude'
        with exclude.open('a', encoding='utf8') as handle:
            handle.write('\n/.recon/\n')
        return cls(directory)

    def __init__(self, path):
        directory = Path(path).expanduser().resolve()
        self.root = directory
        self.root = Path(self._git('rev-parse', '--show-toplevel').strip()).resolve()
        if self._git('rev-parse', '--is-bare-repository').strip() != 'false':
            raise GitError('请选择有工作目录的 Git 仓库。')
        self._git_dir = Path(self._git('rev-parse', '--absolute-git-dir').strip())

    def _run(self, *args, allowed=(0,)):
        command = ['git', '-c', 'core.quotepath=false', '-c',
                   'i18n.logOutputEncoding=utf-8', '-c',
                   'i18n.commitEncoding=UTF-8', '-C', str(self.root), *args]
        overrides = {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                     'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                     'GIT_NAMESPACE', 'GIT_PREFIX', 'GIT_CONFIG', 'GIT_CONFIG_COUNT',
                     'GIT_CONFIG_PARAMETERS'}
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() not in overrides and
                       not re.fullmatch(r'GIT_CONFIG_(KEY|VALUE)_\d+', key.upper())}
        environment['GIT_TERMINAL_PROMPT'] = '0'
        options = {}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=environment, **options)
        except OSError as error:
            raise GitError('无法启动 Git：' + str(error)) from error
        if result.returncode not in allowed:
            message = (result.stderr or result.stdout).decode('utf-8', 'replace').strip()
            raise GitError(message or f'Git 操作失败（退出码 {result.returncode}）。')
        return result

    def _git(self, *args, allowed=(0,)):
        return self._run(*args, allowed=allowed).stdout.decode('utf-8', 'replace')

    def _head(self):
        result = self._run('rev-parse', '--verify', 'HEAD', allowed=(0, 128))
        return result.stdout.decode('ascii').strip() if result.returncode == 0 else ''

    def _resolve_commit(self, oid):
        value = str(oid).strip()
        if not re.fullmatch(r'[0-9a-fA-F]{7,64}', value):
            raise GitError('请输入 7～64 位十六进制提交号。')
        return self._git('rev-parse', '--verify', value + '^{commit}').strip()

    def _write_lock(self):
        return workspace_write_lock(self._git_dir / 'git-helper.write.lock')

    def operation_lock(self):
        """Share the repository write lock with other toolbox operations.

        Commit and restore already take this lock and must not be nested in it.
        """
        return self._write_lock()

    def _ensure_ready(self):
        for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                     'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG'):
            if os.path.lexists(self._git_dir / name):
                raise GitError('仓库有合并、变基、拣选或其他未完成操作，请先处理：' + name)
        if self._git('ls-files', '--unmerged', '-z'):
            raise GitError('仓库还有未解决的合并冲突。')
        branch = self._run('symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1))
        if branch.returncode != 0:
            raise GitError('当前是分离 HEAD，请先切回分支，再提交或回档。')

    def _require_identity(self):
        name, email = self.identity()
        if not name or not email:
            raise GitError('请先设置此仓库的提交姓名和邮箱。')
        self._git('var', 'GIT_AUTHOR_IDENT')
        self._git('var', 'GIT_COMMITTER_IDENT')

    @staticmethod
    def _reparse(path):
        try:
            info = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return False
        return (stat.S_ISLNK(info.st_mode) or
                bool(getattr(info, 'st_file_attributes', 0) &
                     getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)))

    @staticmethod
    def _key(path):
        value = path.replace('\\', '/').rstrip('/')
        return value.casefold() if os.name == 'nt' else value

    def _kept(self, path):
        return self._key(path) in {self._key(item) for item in KEEP_PATHS}

    def _restore_specs(self):
        return ['.', *(':(top,literal,exclude)' + item for item in KEEP_PATHS)]

    def _tree(self, oid):
        entries = {}
        for record in self._git('ls-tree', '-r', '-z', oid).split('\0'):
            if record:
                metadata, path = record.split('\t', 1)
                entries[path] = metadata.split()[0]
        return entries

    def _index(self):
        entries = {}
        for record in self._git('ls-files', '--stage', '-z').split('\0'):
            if record:
                metadata, path = record.split('\t', 1)
                entries[path] = metadata.split()[0]
        return entries

    def _others(self, ignored=False):
        flags = ['--others', '--exclude-standard', '--directory', '-z']
        if ignored:
            flags.insert(1, '--ignored')
        return [item for item in self._git('ls-files', *flags).split('\0') if item]

    def _different_paths(self, oid):
        return [item for item in self._git(
            'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
            '--name-only', '-z', oid, '--', *self._restore_specs()).split('\0') if item]

    def _check_paths(self, target):
        tree, index = self._tree(target), self._index()
        if '160000' in tree.values() or '160000' in index.values():
            raise GitError('含子模块的仓库不能使用此回档工具。')
        target_paths = [p for p in tree if not self._kept(p)]
        target_keys = [self._key(p) for p in target_paths]
        untracked = self._others()
        ignored = self._others(ignored=True)
        for other in untracked + ignored:
            if self._kept(other):
                continue
            key = self._key(other)
            if any(key == wanted or key.startswith(wanted + '/') or
                   wanted.startswith(key + '/') for wanted in target_keys):
                raise GitError('目标会覆盖未跟踪或被忽略的文件／目录，请先移开：' + other)

        # Also inspect untracked files that the visible backup would add and
        # the subsequent restore would remove. Ignored caches are never added.
        affected = set(self._different_paths(target))
        affected.update(p for p in untracked if not self._kept(p))
        for name in affected:
            parts = PurePosixPath(name.rstrip('/')).parts
            if not parts or any(p in ('.', '..') for p in parts) or parts[0] == '/':
                raise GitError('仓库包含无法安全处理的路径：' + name)
            node = self.root
            for part in parts:
                node = node / part
                if self._reparse(node):
                    raise GitError('回档涉及符号链接或 Windows 重解析路径：' + name)
            if tree.get(name) == '120000' or index.get(name) == '120000':
                raise GitError('回档涉及符号链接：' + name)

        # Git does not list empty untracked directories. Do not let a file
        # checkout silently replace one, or a file obstruct a required parent.
        tracked_keys = [self._key(p) for p in index]
        for name in target_paths:
            destination = self.root.joinpath(*PurePosixPath(name).parts)
            key = self._key(name)
            if destination.is_dir() and not any(p.startswith(key + '/') for p in tracked_keys):
                raise GitError('目标文件与现有未跟踪目录冲突：' + name)
            parent = destination.parent
            while parent != self.root:
                if parent.exists() and not parent.is_dir():
                    relative = parent.relative_to(self.root).as_posix()
                    if self._key(relative) not in tracked_keys:
                        raise GitError('目标目录与现有未跟踪文件冲突：' + relative)
                parent = parent.parent

    def status(self) -> list[Change]:
        records = self._git('status', '--porcelain=v1', '-z', '--untracked-files=all').split('\0')
        result = []
        i = 0
        while i < len(records):
            record = records[i]
            i += 1
            if not record:
                continue
            status, path = record[:2], record[3:]
            previous = ''
            if 'R' in status or 'C' in status:
                previous = records[i]
                i += 1
            result.append(Change(status, path, previous))
        return result

    def branch(self) -> str:
        result = self._run('symbolic-ref', '--quiet', '--short', 'HEAD', allowed=(0, 1))
        return result.stdout.decode('utf-8', 'replace').strip() if result.returncode == 0 else '分离 HEAD'

    def log(self, limit=RECENT_LIMIT) -> list[Commit]:
        if not self._head():
            return []
        try:
            count = max(1, min(int(limit), RECENT_LIMIT))
        except (ValueError, TypeError) as error:
            raise GitError('历史条数必须是整数。') from error
        records = self._git('log', f'-{count}', '--format=%H%x00%h%x00%cI%x00%an%x00%s%x00').split('\0')
        return [Commit(records[i].lstrip('\r\n'), *records[i + 1:i + 5])
                for i in range(0, len(records) - 1, 5)]

    def favorites(self):
        return tuple(self._git('for-each-ref', '--sort=-committerdate',
                               '--format=%(objectname)', FAVORITE_PREFIX).splitlines())

    def history(self):
        recent = self.log()
        favorite_oids = self.favorites()
        recent_oids = {commit.oid for commit in recent}
        extra = []
        for oid in favorite_oids:
            if oid not in recent_oids:
                fields = self._git('show', '-s', '--format=%H%x00%h%x00%cI%x00%an%x00%s%x00', oid)
                extra.append(Commit(*fields.split('\0', 5)[:5]))
        return recent + extra, frozenset(favorite_oids)

    def toggle_favorite(self, oid):
        with self._write_lock():
            target = self._resolve_commit(oid)
            favorite_oids = self.favorites()
            reference = FAVORITE_PREFIX + target
            if target in favorite_oids:
                self._git('update-ref', '-d', reference, target)
                return False
            if target not in {commit.oid for commit in self.log()}:
                raise GitError('只可收藏当前最近 30 条历史中的记录。')
            if len(favorite_oids) >= FAVORITE_LIMIT:
                raise GitError('已收藏 10 条，请先取消一条收藏。')
            self._git('update-ref', reference, target, '0' * len(target))
            return True

    def _restore_target(self, oid):
        target = self._resolve_commit(oid)
        if target not in self.favorites() and target not in {commit.oid for commit in self.log()}:
            raise GitError('此记录已超出工具箱的回档范围，请用 Git 或独立备份恢复。')
        return target

    def details(self, oid) -> str:
        target = self._resolve_commit(oid)
        output = self._git('show', '--no-ext-diff', '--no-textconv', '--no-color',
                           '--format=fuller', '--stat', '--patch', target, '--')
        return self._text_diff(output)

    @staticmethod
    def _text_diff(output):
        return re.sub(r'(?ms)^GIT binary patch\n.*?(?=^diff --git |\Z)',
                      '（二进制补丁内容已省略）\n', output)

    def diff(self) -> str:
        options = ('--no-ext-diff', '--no-textconv', '--no-color')
        staged = self._text_diff(self._git('diff', '--cached', *options, '--'))
        unstaged = self._text_diff(self._git('diff', *options, '--'))
        untracked = [item.path for item in self.status() if item.status == '??']
        listing = '\n'.join(untracked) if untracked else '（无）'
        return f'已暂存\n{staged or "（无）"}\n\n未暂存\n{unstaged or "（无）"}\n\n未跟踪文件\n{listing}'

    def restore_preview(self, target_oid) -> str:
        target = self._restore_target(target_oid)
        self._check_paths(target)
        patch = self._text_diff(self._git(
            'diff', '-R', target, '--stat', '--patch', '--no-ext-diff',
            '--no-textconv', '--no-color', '--', *self._restore_specs()))
        untracked = []
        for change in self.status():
            if change.status == '??':
                suffix = '（工具文件保留）' if self._kept(change.path) else '（先备份，再从工作区移出）'
                untracked.append(change.path + suffix)
        listing = '\n'.join(untracked) or '（无）'
        kept = '\n'.join(KEEP_PATHS)
        return (f'还原方向：当前工作区 → 目标提交 {target}\n\n'
                f'以下工具文件保留当前内容：\n{kept}\n\n'
                f'实际还原差异：\n{patch or "（无已跟踪文件差异）"}\n\n'
                f'当前未跟踪文件：\n{listing}')

    def identity(self) -> tuple[str, str]:
        return tuple(self._git('config', '--get', key, allowed=(0, 1)).strip()
                     for key in ('user.name', 'user.email'))

    def set_identity(self, name, email):
        values = [str(name).strip(), str(email).strip()]
        if any(not value or any(c in value for c in '\r\n\0') for value in values):
            raise GitError('姓名和邮箱不能为空，也不能包含换行。')
        with self._write_lock():
            self._git('config', '--local', 'user.name', values[0])
            self._git('config', '--local', 'user.email', values[1])

    def _write_commit(self, message):
        self._git('commit', '-m', message)
        return self._head()

    def _save_all(self, message):
        self._git('add', '--all', '--', '.')
        return self._write_commit(message)

    def commit(self, message) -> str:
        message = str(message).strip()
        if not message or '\0' in message:
            raise GitError('请填写提交说明。')
        with self._write_lock():
            self._ensure_ready()
            self._require_identity()
            if not self.status():
                raise GitError('当前没有可以提交的更改。')
            return self._save_all(message)

    def restore(self, target_oid) -> RestoreResult:
        with self._write_lock():
            self._ensure_ready()
            target = self._restore_target(target_oid)
            self._require_identity()
            original = self._head()
            if not original:
                raise GitError('请先建立一次提交，再使用回档。')
            self._check_paths(target)
            backup = None
            snapshot = original
            if self.status():
                stamp = datetime.now().astimezone().isoformat(timespec='seconds')
                try:
                    backup = self._save_all(f'自动备份：回档到 {target[:12]} 前的工作区\n\n{stamp}')
                except GitError as error:
                    raise GitError('自动备份未成功，尚未执行回档；当前文件保留。\n' + str(error)) from error
                snapshot = backup
                if self.status():
                    raise GitError(f'已保存备份 {backup}，但提交后出现新更改；尚未执行回档。')
            if not self._different_paths(target):
                return RestoreResult(snapshot, target, backup, changed=False)
            try:
                self._git('restore', '--source=' + target, '--staged', '--worktree',
                          '--', *self._restore_specs())
                oid = self._write_commit(f'回档：恢复到 {target[:12]}\n\n目标提交：{target}')
                return RestoreResult(oid, target, backup)
            except GitError as error:
                try:
                    self._check_paths(snapshot)
                    self._git('restore', '--source=' + snapshot, '--staged', '--worktree', '--', '.')
                    if self._head() != snapshot:
                        self._write_commit(f'恢复：回档失败后恢复保存快照 {snapshot[:12]}')
                except GitError as recovery_error:
                    raise GitError(f'回档失败：{error}\n保存快照：{snapshot}\n自动恢复未完成：{recovery_error}') from error
                raise GitError(f'回档失败，已恢复保存快照 {snapshot}。\n原因：{error}') from error
