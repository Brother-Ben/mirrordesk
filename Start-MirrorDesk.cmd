@echo off
setlocal
cd /d "%~dp0"
rem MirrorDesk (Prototype 7) in the background: no console window. Look for the MirrorDesk
rem icon next to the clock (it may be under the ^ arrow): Open / Start with Windows / Quit.
rem Already running? This just opens the browser. Output: .runtime\logs\mirrordesk.log
rem Visible console instead: Start-MirrorDesk-Console.cmd   This computer only: add --host 127.0.0.1
if not exist ".venv\Scripts\pythonw.exe" (
  echo ERROR: Project-local Python environment missing. Run Setup-MirrorDesk.cmd once first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0MirrorDesk.pyw" --open-browser %*
exit /b 0
