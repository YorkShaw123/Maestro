"""Read-only reconciliation of ordinary script edits around owned color output.

Binary assets, generated AIR blocks, and managed playback parameters must still
match. The returned record keeps new ordinary script edits for the next build.
"""
from copy import deepcopy
from pathlib import Path
from collections import defaultdict,Counter
import base64
import difflib
import hashlib
import re

TEXT_SUFFIXES=('.air','.cns','.cmd')
MARKER=b'SPRITE_EDITOR_EVIL_V1'


class ReconcileError(ValueError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _pack(data):
    return base64.b64encode(data).decode('ascii')


def _unpack(value):
    return base64.b64decode(value,validate=True)


def _normal(data):
    return data.replace(b'\r\n',b'\n')


def _parameter(line,key,old,new):
    from sprite_editor_effects import PARAM
    match=PARAM.match(line.decode('cp936'))
    if not match or match[2].lower()!=key.lower() or match[3].strip()!=old:
        raise ReconcileError('受管理的播放参数已变化，不能自动合并。')
    return (match[1]+new+match[4]+(match[5] or '')+(match[6] or '')).encode('cp936')


def _from_original(name,state):
    """Recover legacy exact saved outputs from their original/call contracts."""
    value=state.get('script_originals',{}).get(name)
    if value is None:
        return None
    original=_unpack(value)
    bank=state.get('effect_bank',{})
    if name.lower().endswith(('.cns','.cmd')):
        lines=original.splitlines(keepends=True)
        for call in bank.get('patched_calls',[]):
            if call['file']!=name:
                continue
            index=call['line']-1
            if not 0<=index<len(lines):
                return None
            lines[index]=_parameter(lines[index],call['parameter'],call['before'],call['after'])
        return b''.join(lines)
    if name=='Shun.air':
        from sprite_editor_effects import _actions,FRAME
        actions=_actions(original.decode('utf-8-sig'))
        mapping={int(k):int(v) for k,v in bank.get('generated_action_map',{}).items()}
        sprites={tuple(row['source_key']):tuple(row['generated_key'])
                 for row in bank.get('generated_sprite_map',[])}
        additions=[]
        for number in sorted(mapping):
            for action in actions.get(number,[]):
                copied=[]
                for index,line in enumerate(action.lines):
                    if index==0:
                        line=re.sub(r'(?i)(\[Begin\s+Action\s+)-?\d+',
                                    lambda m:m[1]+str(mapping[number]),line,count=1)
                    else:
                        ending='\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
                        frame=FRAME.match(line.rstrip('\r\n'))
                        key=(int(frame[2]),int(frame[4])) if frame else None
                        if key in sprites:
                            group,item=sprites[key]
                            line=f'{frame[1]}{group}{frame[3]}{item}{frame[5]}{ending}'
                    copied.append(line)
                body=''.join(copied).replace('\r\n','\n').replace('\n','\r\n')
                additions.append(f'; {MARKER.decode()} source=Shun.air:{action.line} action={number}\r\n'+body)
        if not additions:
            return original
        return original+(b'' if original.endswith(b'\n') else b'\r\n')+b'\r\n'+('\r\n'.join(additions)).encode('utf8')
    return None


def _expected(root,name,state):
    wanted=state['files'][name]
    saved=state.get('script_outputs',{}).get(name)
    if saved is not None:
        raw=_unpack(saved)
        if _sha(raw)==wanted:
            return raw
    try:
        reconstructed=_from_original(name,state)
        if reconstructed is not None and _sha(reconstructed)==wanted:
            return reconstructed
    except (ValueError,KeyError,IndexError,UnicodeError):
        pass
    # Legacy records did not embed outputs. Trust candidate bytes only when
    # their digest matches the recorded file, never merely the newest folder.
    folder=root/'.recon/sprite_editor_backups'
    if folder.is_dir():
        for backup in sorted(folder.iterdir(),reverse=True):
            path=backup/'candidate'/name
            if not path.resolve().is_relative_to(root):
                continue
            if any(p.exists() and (p.is_symlink() or getattr(p.lstat(),'st_file_attributes',0)&0x400)
                   for p in [folder,backup,backup/'candidate',path]):
                continue
            if path.is_file():
                raw=path.read_bytes()
                if _sha(raw)==wanted:
                    return raw
    return None


def _strip_suffix(current,suffix):
    if current.endswith(suffix):
        return current[:-len(suffix)]
    normalized=_normal(current)
    tail=_normal(suffix)
    if not tail or not normalized.endswith(tail):
        raise ReconcileError('配色专用动作区已被修改或移走，不能自动合并。')
    count=len(normalized)-len(tail)
    pos=0
    for _ in range(count):
        pos+=2 if current[pos:pos+2]==b'\r\n' else 1
    return current[:pos]


def _unpatch(name,current,expected,original,calls):
    from sprite_editor_effects import _source_blocks
    def contexts(raw):
        rows,block_counts,param_counts={},Counter(),{}
        for block in _source_blocks(raw)[1]:
            kind='statedef' if block['is_statedef'] else block['params'].get('type','').strip().casefold()
            key=(block['state'],' '.join(block['header'].split()).casefold(),kind)
            occurrence=block_counts[key];block_counts[key]+=1
            seen=Counter()
            for index,parameter,_,_ in block['entries']:
                rows[index]=(key,occurrence,parameter,seen[parameter])
                seen[parameter]+=1
            for parameter,count in seen.items():
                param_counts[key,occurrence,parameter]=count
        return rows,block_counts,param_counts
    old=expected.splitlines(keepends=True)
    new=current.splitlines(keepends=True)
    old_context,old_counts,old_parameters=contexts(expected)
    new_context,new_counts,new_parameters=contexts(current)
    restored=list(old)
    for call in calls:
        index=call['line']-1
        if not 0<=index<len(old):
            raise ReconcileError('配色播放记录的行号不完整。')
        restored[index]=_parameter(old[index],call['parameter'],call['after'],call['before'])
    if original is not None and _normal(b''.join(restored))!=_normal(original):
        raise ReconcileError('基础脚本与配色播放记录不能相互还原。')
    links={}
    matcher=difflib.SequenceMatcher(None,[_normal(x) for x in old],[_normal(x) for x in new],autojunk=False)
    for match in matcher.get_matching_blocks():
        for offset in range(match.size):
            links[match.a+offset]=match.b+offset
    remapped=[]
    for call in calls:
        index=call['line']-1
        if index not in links:
            raise ReconcileError(f"{name}:{call['line']} 的配色播放分支发生变化，未覆盖。")
        target=links[index]
        context=old_context.get(index)
        if context is None or context!=new_context.get(target):
            raise ReconcileError(f"{name}:{call['line']} 的配色播放参数所属状态或控制器发生变化，未覆盖。")
        key,occurrence,parameter,_=context
        if (old_counts[key]!=new_counts[key] or
                old_parameters[key,occurrence,parameter]!=new_parameters.get((key,occurrence,parameter))):
            raise ReconcileError(f"{name}:{call['line']} 的配色控制器出现增删或歧义，未覆盖。")
        new[target]=_parameter(new[target],call['parameter'],call['after'],call['before'])
        updated=dict(call);updated['line']=target+1;remapped.append(updated)
    return b''.join(new),remapped,b''.join(restored)


def _validate_base(name,base,state):
    ids={int(value) for value in state.get('effect_bank',{}).get('generated_action_map',{}).values()}
    if name.endswith('.air'):
        from sprite_editor_effects import _actions,FRAME
        text=base.decode('utf-8-sig')
        if ids & set(_actions(text)):
            raise ReconcileError('新增原始动作与配色专用动作编号冲突。')
        generated={tuple(row['generated_key']) for row in state.get('effect_bank',{}).get('generated_sprite_map',[])}
        for line in text.splitlines():
            match=FRAME.match(line)
            if match and (int(match[2]),int(match[4])) in generated:
                raise ReconcileError('原始动作新增了对配色生成图片的直接引用，不能自动合并。')
    else:
        base.decode('cp936')  # Fail before any metadata could accept bad encoding.
        code=b'\n'.join(line.split(b';',1)[0] for line in base.splitlines())
        if ids & {int(value) for value in re.findall(rb'\b\d+\b',code)}:
            raise ReconcileError('普通脚本新增了对配色专用动作编号的引用，不能自动合并。')
    if MARKER in base:
        raise ReconcileError('基础脚本仍含未还原的配色生成区。')


def _derived_actions(raw,keys):
    from sprite_editor_effects import _actions
    return {number:tuple(tuple(re.sub(r'\s+','',line.split(';',1)[0]).casefold()
                               for line in action.lines if line.split(';',1)[0].strip())
                         for action in actions)
            for number,actions in _actions(raw.decode('utf-8-sig')).items()
            if any(action.keys & keys for action in actions)}


def _render_sites(raw,actions):
    from sprite_editor_effects import _source_blocks,_finite
    found=Counter()
    for block in _source_blocks(raw)[1]:
        kind='statedef' if block['is_statedef'] else block['params'].get('type','').lower()
        for _,key,value,_ in block['entries']:
            relevant=(key=='anim' and kind in {'statedef','explod','superpause'} or
                key=='value' and kind in {'changeanim','changeanim2'} or
                kind=='projectile' and key in {'projanim','projhitanim','projremanim','projcancelanim'} or
                kind in {'hitdef','projectile'} and key in {'sparkno','guard.sparkno'})
            if not relevant:
                continue
            if key in {'sparkno','guard.sparkno'} or kind=='superpause':
                if not value[:1].lower()=='s':
                    continue
                value=value[1:].strip()
            elif value[:1].lower()=='f':
                continue
            domain=_finite(value)
            if domain is None or domain & actions:
                found[block['state'],block['header'],kind,key,value]+=1
    return found


def reconcile_state(root,state,files):
    """Return (new in-memory state, details); never write or weaken binary guards."""
    root=Path(root).resolve()
    mismatches=[name for name,value in state.get('files',{}).items()
                if name not in files or _sha(files[name])!=value]
    if not mismatches:
        return state,{'files':[],'kinds':{}}
    unsafe=[name for name in mismatches if name not in files or not name.lower().endswith(TEXT_SUFFIXES)]
    if unsafe:
        raise ReconcileError('图片、配色或入口文件与记录不一致：'+'、'.join(unsafe)+'。现有文件已保留，需要单独核对。')
    updated=deepcopy(state)
    grouped=defaultdict(list)
    for call in state.get('effect_bank',{}).get('patched_calls',[]):
        grouped[call['file']].append(call)
    kinds={}
    old_bases,new_bases={},{}
    for name in mismatches:
        current=files[name]
        original_value=state.get('script_originals',{}).get(name)
        original=_unpack(original_value) if original_value is not None else None
        expected=_expected(root,name,state)
        calls=grouped[name]
        old_base=original if original is not None else expected
        if name.endswith('.air') and original is not None:
            if expected is None or not expected.startswith(original):
                raise ReconcileError(name+'：缺少可验证的旧动作输出，未覆盖。')
            suffix=expected[len(original):]
            base=_strip_suffix(current,suffix) if suffix else current
        elif calls:
            if expected is None:
                raise ReconcileError(name+'：缺少可验证的旧播放脚本，未覆盖。')
            base,remapped,recovered=_unpatch(name,current,expected,original,calls)
            if original is None:
                baseline=state.get('effect_bank',{}).get('baseline_sha256',{}).get(name)
                if baseline is None or _sha(recovered)!=baseline:
                    raise ReconcileError(name+'：旧CMD基础脚本无法核对。')
            grouped[name]=remapped
            old_base=original if original is not None else recovered
        elif original is not None:
            raise ReconcileError(name+'：缺少受管理参数记录，未覆盖。')
        else:
            base=current
        _validate_base(name,base,state)
        old_bases[name]=old_base;new_bases[name]=base
        if original is not None or calls:
            updated.setdefault('script_originals',{})[name]=_pack(base)
            updated.setdefault('script_outputs',{})[name]=_pack(current)
        updated['files'][name]=_sha(current)
        if name in updated.get('effect_bank',{}).get('baseline_sha256',{}):
            updated['effect_bank']['baseline_sha256'][name]=_sha(base)
        kinds[name]='line_endings' if expected is not None and _normal(expected)==_normal(current) else 'ordinary_script_edit'
    if 'effect_bank' in updated:
        updated['effect_bank']['patched_calls']=[
            call for name,items in grouped.items() for call in items]
    keys={tuple(row['source_key']) for row in state.get('effect_bank',{}).get('generated_sprite_map',[])}
    needs_rebuild=bool(state.get('pending_rebuild'))
    if keys:
        old_air=old_bases.get('Shun.air') if 'Shun.air' in old_bases else (
            _unpack(state['script_originals']['Shun.air']) if 'Shun.air' in state.get('script_originals',{}) else files['Shun.air'])
        new_air=new_bases.get('Shun.air',old_air or files['Shun.air'])
        old_actions=_derived_actions(old_air,keys) if old_air is not None else {}
        new_actions=_derived_actions(new_air,keys)
        needs_rebuild|=old_actions!=new_actions
        action_ids=set(old_actions)|set(new_actions)
        for name,new_base in new_bases.items():
            if name.endswith(('.cns','.cmd')):
                old_base=old_bases[name]
                needs_rebuild|=_render_sites(old_base,action_ids)!=_render_sites(new_base,action_ids) if old_base is not None else bool(_render_sites(new_base,action_ids))
    if needs_rebuild:
        updated['pending_rebuild']=True
    return updated,{'files':mismatches,'kinds':kinds}
