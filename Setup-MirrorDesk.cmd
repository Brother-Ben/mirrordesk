@echo off
setlocal
cd /d "%~dp0"
echo MirrorDesk (Prototype 7) setup: downloads the locked Python dependencies into this folder's .venv.
echo It does not install MSYS2 or UxPlay, or change PATH or the firewall. Re-run it after updating MirrorDesk.
where uv >nul 2>nul
if errorlevel 1 (
  echo ERROR: uv missing. Get it from https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv sync --locked --python 3.13
if errorlevel 1 (
  echo ERROR: Dependency setup failed.
  pause
  exit /b 1
)
echo Setup complete. Double-click Start-MirrorDesk.cmd to start MirrorDesk.
pause
exit /b 0
