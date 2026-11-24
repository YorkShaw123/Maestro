# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'src' / 'character_toolbox.py')],
    pathex=[str(root / 'src')],
    binaries=[],
    datas=[(str(root / 'assets'), 'assets'), (str(root / 'docs'), 'docs')],
    hiddenimports=['git_helper', 'sprite_editor_ui', 'sprite_creation_ui',
                   'ai_workbench_ui', 'dockyard'],
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False,
)
# This Qt build uses the Windows ICU API. Version-suffixed third-party ICU
# exports are incompatible and must not shadow the Windows implementation.
a.binaries = [entry for entry in a.binaries
              if not Path(entry[0]).name.lower().startswith(('icuuc', 'icuin', 'icudt'))]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Maestro',
          icon=str(root / 'assets' / 'maestro.ico'), console=False,
          debug=False, strip=False, upx=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Maestro')
