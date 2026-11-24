param([string]$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & $Python -B -c "from PySide6 import QtCore, QtGui, QtWidgets; print('Qt', QtCore.__version__)"
    if ($LASTEXITCODE -ne 0) { throw 'Qt import check failed.' }
    & $Python -m PyInstaller --noconfirm --clean --workpath build Maestro.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & $Python -B scripts\verify_release.py dist\Maestro
    if ($LASTEXITCODE -ne 0) { throw 'Release verification failed.' }
    & $Python -B scripts\package_release.py
    if ($LASTEXITCODE -ne 0) { throw 'Portable archive creation failed.' }
} finally {
    Pop-Location
}
