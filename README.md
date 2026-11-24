# Maestro

Maestro is a Windows desktop workbench for MUGEN 1.0 and 1.1 characters.
It includes palette and sprite tools, an AI workbench, character packaging,
and maintenance document templates.

## Run

Open `dist/Maestro/Maestro.exe` and choose **打开角色 DEF**.
Keep `Maestro.exe` and `_internal` together when copying the portable release.
The release includes Python and Qt. Local Git is required for Git operations.
Native playtesting requires a locally installed MUGEN engine and a stage.

## Project layout

- `src/` contains Python application modules.
- `assets/` contains the application icon and maintenance templates.
- `docs/` contains application help and installation instructions.
- `scripts/` contains build and release verification commands.
- `build/` contains disposable PyInstaller intermediates.
- `dist/Maestro/` contains the portable release.
- `dist/Maestro-windows-x64.zip` contains the portable distribution archive.
- `.recon/` contains local development scratch data.

## Develop and build

Use 64-bit Python 3.10 or newer on Windows. The release currently relies on the
Windows ICU API. This build was verified on the development machine only.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe src\character_toolbox.py
.\scripts\build.ps1
```

The build script imports Qt before packaging and checks the packaged DLLs and
resources afterward. The spec excludes third-party ICU DLLs whose versioned
exports conflict with this Qt build's Windows ICU imports.

For release testing, launch the EXE and open the required pages. Verification on
other Windows machines remains necessary before broader distribution.
