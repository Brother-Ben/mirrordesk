@echo off
setlocal
cd /d "%~dp0"
title MirrorDesk
echo MirrorDesk (Prototype 7): everything is controlled from the browser window that opens.
echo Other devices on your network / Tailscale can watch too: give them the viewer code shown below (the owner code gives full control).
echo First run: Windows may ask to allow Python on Private networks - Allow lets other devices connect.
echo For this computer only, run:  Start-MirrorDesk-Console.cmd --host 127.0.0.1
echo (Start-MirrorDesk.cmd runs the same server in the background without this window.)
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Project-local Python environment missing. Run Setup-MirrorDesk.cmd once first.
  goto fail
)
".venv\Scripts\python.exe" -m mirrordesk --open-browser %*
if errorlevel 1 goto fail
exit /b 0
:fail
echo.
echo MirrorDesk stopped with an error. Copy the message above.
pause
exit /b 1
