@echo off
setlocal
cd /d "%~dp0"
title MirrorDesk setup
echo MirrorDesk setup
echo ================
echo Installs everything MirrorDesk needs into this folder (nothing is installed system-wide
echo except Windows Firewall rules and shortcuts). Needs the internet once, about 200 MB:
echo Python and its libraries, and Google's Android tools.
echo Windows asks for permission once, for the firewall rules.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\setup.ps1" %*
if errorlevel 1 (
  echo.
  echo Setup did not finish. Copy the messages above and send them to whoever gave you MirrorDesk.
  pause
  exit /b 1
)
echo.
echo Setup finished. Start MirrorDesk from the Start menu or the desktop shortcut next time.
echo The tester guide is TESTER-GUIDE.html in this folder.
pause
exit /b 0
