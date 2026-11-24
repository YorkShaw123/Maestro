"""Check the packaged runtime without opening or automating a window."""
import ctypes
from contextlib import ExitStack
import os
from pathlib import Path
import sys

release = Path(sys.argv[1]).resolve()
runtime = release / '_internal'
assert (release / 'Maestro.exe').is_file(), 'Missing executable'
assert (runtime / 'assets' / 'maestro.ico').is_file(), 'Missing icon'
assert not (runtime / 'icuuc.dll').exists(), 'Third-party ICU shadows Windows ICU'
with ExitStack() as directories:
    for folder in (runtime, runtime / 'PySide6', runtime / 'shiboken6'):
        if folder.is_dir():
            directories.enter_context(os.add_dll_directory(str(folder)))
    for name in ('Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Widgets.dll'):
        library = runtime / 'PySide6' / name
        if not library.is_file():
            library = runtime / name
        ctypes.WinDLL(str(library))
        print('Loaded', name)
for name in ('user-guide.md', 'git-guide.md', 'installation.md'):
    assert (runtime / 'docs' / name).is_file(), f'Missing help document {name}'
print('Release runtime and resources verified.')
