"""Build a portable, isolated native MUGEN matchup from the current AI draft."""
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from uuid import uuid4

from ai_workbench import AIError, source_text
from mugen_character import _entries, _reference, local_path, reparse


@dataclass(frozen=True)
class TestRun:
    root: Path
    args: tuple[str, ...]


def copy_runtime(source, dest):
    if reparse(source):
        raise AIError(f'运行库含链接，无法安全复制：{source}')
    if source.is_dir():
        dest.mkdir()
        for child in source.iterdir():
            copy_runtime(child, dest / child.name)
    else:
        shutil.copy2(source, dest)


def settings(path, updates):
    raw = path.read_bytes()
    for key, value in updates.items():
        pattern = rb'(?im)^([ \t]*' + key.encode() + rb'[ \t]*=[ \t]*)[^;\r\n]*'
        raw = re.sub(pattern, lambda match: match[1] + str(value).encode() + b' ', raw)
    path.write_bytes(raw)


def controller(name, kind, *values):
    return f'\n[State -1, {name}]\ntype = {kind}\n' + '\n'.join(values) + '\n'


def instrument(doc, root, power, forced):
    base = local_path(root, doc.character.definition)
    command = next((value.strip('"') for section, key, value in _entries(base)
                    if section == 'files' and key == 'cmd'), None)
    if command is None:
        raise AIError('测试需要 DEF 指定 CMD 文件。')
    path = _reference(root, base.parent, command)
    text, encoding = source_text(path.read_bytes())
    match = re.search(r'(?im)^\s*\[statedef\s+-1\s*\][^\r\n]*', text)
    if not match:
        raise AIError('测试需要 CMD 的 [Statedef -1] 入口。')
    hint = controller('workbench power', 'PowerSet', 'triggerall = !IsHelper',
                      'trigger1 = RoundState = 1', f'value = {power}')
    hint += controller('workbench trace', 'DisplayToClipboard', 'trigger1 = !IsHelper',
                       'text = "AI state:%d time:%d hit:%d guard:%d power:%d"',
                       'params = StateNo, Time, MoveHit, MoveGuarded, Power', 'ignorehitpause = 1')
    extra = ''
    if forced is not None:
        if forced not in doc.states or forced in doc.duplicates or forced < 0:
            raise AIError('强制试招需要唯一的现有招式状态。')
        marker = next(n for n in range(790100, 795100)
                      if n not in doc.states and all(re.search(rf'\b{n}\b', s) is None for s in doc.text.values()))
        conditions = ('triggerall = !IsHelper && RoundState = 2', 'triggerall = Ctrl',
                      f'trigger1 = NumHelper({marker}) = 0')
        # Enter an intermediate state that creates the one-shot marker before switching.
        start = marker + 1
        while start in doc.states or any(re.search(rf'\b{start}\b', s) for s in doc.text.values()):
            start += 1
        hint += controller('workbench forced starter', 'ChangeState', *conditions, f'value = {start}')
        extra = f"""
[Statedef {start}]
type = S
movetype = I
physics = N
ctrl = 0
[State {start}, marker]
type = Helper
trigger1 = Time = 0
id = {marker}
stateno = {marker}
keyctrl = 0
ownpal = 1
[State {start}, selected move]
type = ChangeState
trigger1 = Time = 0
value = {forced}
[Statedef {marker}]
type = S
movetype = I
physics = N
ctrl = 0
[State {marker}, hide]
type = AssertSpecial
trigger1 = 1
flag = invisible
flag2 = noshadow
[State {marker}, collision]
type = PlayerPush
trigger1 = 1
value = 0
[State {marker}, invulnerable]
type = NotHitBy
trigger1 = 1
value = SCA
"""
    path.write_bytes((text[:match.end()] + '\n' + hint + text[match.end():] + '\n' + extra).encode(encoding))


def dummy(root, doc, posture):
    folder = root / 'chars' / 'workbench_dummy'
    folder.mkdir()
    sprite = local_path(root / 'chars' / 'workbench_actor', doc.character.sprite)
    shutil.copy2(sprite, folder / 'dummy.sff')
    (folder / 'dummy.def').write_text(
        '[Info]\nname = "Workbench Dummy"\nmugenversion = 1.0\n[Files]\ncmd = dummy.cmd\ncns = dummy.cns\n'
        'st = dummy.cns\nstcommon = common1.cns\nsprite = dummy.sff\nanim = dummy.air\n', encoding='ascii')
    commands = ''.join(f'[Command]\nname = "{name}"\ncommand = /{key}\ntime = 1\n'
                       for name, key in (('holdfwd','F'),('holdback','B'),('holdup','U'),('holddown','D')))
    commands += '[Command]\nname = "recovery"\ncommand = x+y\ntime = 1\n'
    (folder / 'dummy.cmd').write_text(commands + '[Statedef -1]\n[State -1, idle]\ntype = Null\ntrigger1 = 1\n', encoding='ascii')
    actions = (0, 5, 6, 10, 11, 12, 20, 40, 41, 42, 43, 44, 45, 47, 50, 51, 52,
               120, 121, 122, 130, 131, 132, 140, 141, 142, 150, 151, 152, 153, 154, 155,
               170, 180, 190, 191, 5000, 5001, 5002, 5010, 5011, 5012, 5020, 5030,
               5040, 5050, 5060, 5070, 5080, 5100, 5110, 5120, 5150)
    crouch = posture in ('crouch', 'low_guard')
    top = -36 if crouch else -62
    (folder / 'dummy.air').write_text(''.join(
        f'[Begin Action {action}]\nClsn2Default: 1\n Clsn2[0] = -12,{top},12,0\n0,0,0,0,8\n'
        for action in actions), encoding='ascii')
    posture_type = 'C' if crouch else 'S'
    idle = f"""
[Data]
life = 1000
power = 3000
attack = 100
defence = 100
fall.defence_up = 50
liedown.time = 45
airjuggle = 15
sparkno = 2
guard.sparkno = 40
KO.echo = 0
volume = 0
IntPersistIndex = 60
FloatPersistIndex = 40
[Size]
xscale = 1
yscale = 1
ground.back = 15
ground.front = 15
height = 60
air.back = 12
air.front = 12
attack.dist = 160
proj.attack.dist = 90
proj.doscale = 0
head.pos = -5,-65
mid.pos = -5,-40
shadowoffset = 0
draw.offset = 0,0
[Velocity]
walk.fwd = 3
walk.back = -3
run.fwd = 6,0
run.back = -4,-4
jump.neu = 0,-7
jump.back = -3
jump.fwd = 3
runjump.back = -4,-7
runjump.fwd = 4,-7
airjump.neu = 0,-7
airjump.back = -3
airjump.fwd = 3
[Movement]
airjump.num = 0
airjump.height = 35
yaccel = 0.44
stand.friction = 0.85
crouch.friction = 0.82
[Statedef 190]
type = S
anim = 0
[State 190, wait]
type = ChangeState
trigger1 = Time = 0
value = 0
[Statedef 0]
type = {posture_type}
movetype = I
physics = N
ctrl = 1
anim = 0
[State 0, idle]
type = VelSet
trigger1 = 1
x = 0
y = 0
"""
    if posture in ('guard', 'low_guard'):
        guard = 131 if crouch else 130
        idle += f"""
[State 0, guard]
type = ChangeState
trigger1 = RoundState = 2 && InGuardDist
value = {guard}
[Statedef {guard}]
type = {posture_type}
movetype = I
physics = N
anim = {guard}
ctrl = 0
[State {guard}, release]
type = ChangeState
trigger1 = !InGuardDist
value = 0
"""
    elif posture == 'jump':
        idle += """
[State 0, jump]
type = ChangeState
trigger1 = RoundState = 2 && Time >= 24
value = 7001
[Statedef 7001]
type = A
movetype = I
physics = N
anim = 41
ctrl = 0
velset = 0,-7
[State 7001, gravity]
type = VelAdd
trigger1 = 1
y = .44
[State 7001, floor]
type = PosSet
trigger1 = Vel Y > 0 && Pos Y >= 0
y = 0
[State 7001, stand]
type = ChangeState
trigger1 = Vel Y > 0 && Pos Y >= 0
value = 0
"""
    (folder / 'dummy.cns').write_text(idle, encoding='ascii')
    return 'workbench_dummy/dummy.def'


def prepare_run(doc, engine, stage, style, power, distance, forced):
    doc.assert_current()
    engine = Path(engine).resolve()
    stage = Path(stage).resolve()
    if not engine.is_file() or engine.suffix.casefold() != '.exe' or not stage.is_file():
        raise AIError('请选择有效的 MUGEN EXE 和舞台 DEF。')
    source = engine.parent
    if not (source / 'data' / 'mugen.cfg').is_file():
        raise AIError('所选引擎缺少 data/mugen.cfg。')
    missing = set(doc.character.missing) - {name for _, name in doc.character.palettes}
    if missing:
        raise AIError('当前角色缺少测试依赖：' + '、'.join(sorted(missing)))
    root = local_path(doc.root, f'.recon/ai_workbench_runs/{uuid4().hex}')
    root.mkdir(parents=True)
    shutil.copy2(engine, root / 'mugen.exe')
    for child in source.iterdir():
        if child.name in ('data', 'font', 'Elecbyte.MUGEN.libs', 'Microsoft.VC90.CRT') or child.suffix.casefold() in ('.dll', '.manifest'):
            copy_runtime(child, root / child.name)
    settings(root / 'data' / 'mugen.cfg', {'FullScreen': 0, 'PauseOnDefocus': 0,
                                           'Debug': 1, 'AllowDebugMode': 1})
    (root / 'stages').mkdir()
    scene = root / 'stages' / 'workbench.def'
    shutil.copy2(stage, scene)
    stage_asset = next((value.strip('"') for section, key, value in _entries(stage)
                        if section == 'bgdef' and key == 'spr'), None)
    if stage_asset:
        original = local_path(stage.parent, stage_asset)
        target = local_path(scene.parent, stage_asset)
        target.parent.mkdir(exist_ok=True, parents=True)
        shutil.copy2(original, target)
    settings(scene, {'p1startx': -(distance//2), 'p2startx': (distance+1)//2, 'bgmusic': ''})
    actor = root / 'chars' / 'workbench_actor'
    actor.mkdir(parents=True)
    candidates = doc.candidates()
    for path in doc.character.files:
        data = candidates.get(path, doc.raw.get(path))
        target = local_path(actor, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if data is None:
            shutil.copy2(local_path(doc.root, path), target)
        else:
            target.write_bytes(data)
    if style == 'mirror':
        shutil.copytree(actor, root / 'chars/workbench_mirror')
        opponent = f'workbench_mirror/{doc.character.definition}'
    else:
        opponent = dummy(root, doc, style)
    instrument(doc, actor, power, forced)
    doc.assert_current()
    return TestRun(root, (f'workbench_actor/{doc.character.definition}', opponent, '-s', 'workbench',
                          '-p1.ai', '8', '-p2.ai', '8' if style == 'mirror' else '0', '-rounds', '1'))
