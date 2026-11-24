"""Reusable RGB swatches: local toolbox preferences, never character palettes."""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import uuid

SLOT_COUNT=16


class ColorPresetError(ValueError):
    pass


def _path(repo):
    root=Path(repo).resolve()
    path=root/'.recon/sprite_editor_preferences/colors.json'
    for item in [path,path.with_suffix('.json.previous'),path.parent,path.parent.parent]:
        if item.exists() and (item.is_symlink() or
                getattr(item.lstat(),'st_file_attributes',0)&0x400):
            raise ColorPresetError('常用颜色目录或文件不能使用链接：'+str(item))
    if not path.resolve().is_relative_to(root):
        raise ColorPresetError('常用颜色路径越出当前工程。')
    return path


def _rgb(value):
    if value is None:
        return None
    if not isinstance(value,(list,tuple)) or len(value)!=3 or any(
            type(v) is not int or not 0<=v<=255 for v in value):
        raise ColorPresetError('常用颜色必须是三个0～255的RGB数字。')
    return tuple(value)


def _decode(raw):
    if raw is None:
        return [None]*SLOT_COUNT
    if len(raw)>16384:
        raise ColorPresetError('常用颜色文件异常过大，未覆盖原文件。')
    try:
        obj=json.loads(raw.decode('utf-8-sig'))
        if not isinstance(obj,dict) or obj.get('schema')!=1 or len(obj['colors'])!=SLOT_COUNT:
            raise ValueError('invalid schema')
        return [_rgb(v) for v in obj['colors']]
    except (ValueError,KeyError,TypeError,UnicodeError) as error:
        raise ColorPresetError('常用颜色文件格式损坏，已保留原文件；角色文件未受影响。') from error


def load_colors(repo):
    path=_path(repo)
    return _decode(path.read_bytes() if path.is_file() else None)


@contextmanager
def _lock(folder):
    path=folder/'colors.lock'
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError as error:
        raise ColorPresetError('另一窗口正在保存常用颜色，请稍后再试。') from error
    try:
        os.write(fd,str(os.getpid()).encode('ascii'))
        yield
    finally:
        os.close(fd)
        path.unlink()


def _replace(path,data):
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def save_color(repo,index,rgb):
    if type(index) is not int or not 0<=index<SLOT_COUNT:
        raise ColorPresetError('请选择1～16号常用颜色格。')
    rgb=_rgb(rgb)
    path=_path(repo)
    path.parent.mkdir(parents=True,exist_ok=True)
    with _lock(path.parent):
        _path(repo)
        original=path.read_bytes() if path.is_file() else None
        colors=_decode(original)
        colors[index]=rgb
        data=(json.dumps({'schema':1,'colors':colors},ensure_ascii=False,indent=2)+'\n').encode('utf8')
        if (path.read_bytes() if path.is_file() else None)!=original:
            raise ColorPresetError('常用颜色已由外部修改，未覆盖。')
        if original is not None:
            _replace(path.with_suffix('.json.previous'),original)
        _replace(path,data)
        if path.read_bytes()!=data:
            raise ColorPresetError('常用颜色保存后核对失败，旧内容保留在colors.json.previous。')
        return colors
