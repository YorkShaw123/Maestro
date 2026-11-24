"""Create the portable Windows archive."""
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
release = root / 'dist' / 'Maestro'
shutil.copy2(root / 'docs' / 'quick-start.md', release / 'README.md')
archive = root / 'dist' / 'Maestro-windows-x64.zip'
with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as package:
    for path in sorted(release.rglob('*')):
        if path.is_file():
            package.write(path, path.relative_to(release.parent))
with ZipFile(archive) as package:
    damaged = package.testzip()
    if damaged:
        raise RuntimeError(f'Archive integrity check failed for {damaged}')
print('Portable archive verified:', archive)
