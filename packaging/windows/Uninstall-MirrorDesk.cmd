@echo off
setlocal
cd /d "%~dp0"
title Uninstall MirrorDesk
if not exist ".venv\Scripts\python.exe" (
  echo MirrorDesk was not set up in this folder. You can simply delete the folder.
  pause
  exit /b 0
)
echo Removes MirrorDesk's firewall rules, shortcuts and start-at-sign-in entry.
echo Windows asks for permission once. Quit MirrorDesk first (icon next to the clock).
echo.
".venv\Scripts\python.exe" -m mirrordesk.winsetup uninstall
pause
exit /b 0
