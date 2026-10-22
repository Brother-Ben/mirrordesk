"""macOS and Linux desktop install/uninstall steps (`python -m mirrordesk.unixsetup`).

Run by packaging/macos/install.sh and packaging/linux/install.sh (desktop mode) after
they have built UxPlay and created .venv; firewall rules and system packages are the
shell installers' job (they need sudo).

install:   Android tools (Google adb, checksum-pinned) when this CPU has them, the
           launcher - macOS: ~/Applications/MirrorDesk.app (a small stay-open
           AppleScript app: Dock icon while MirrorDesk runs, click = open the page,
           Quit = stop MirrorDesk); Linux: "MirrorDesk" and "Quit MirrorDesk" in the
           app menu (the first also offers Quit when right-clicked) and the command
           `mirrordesk` (start | open | stop | restart | status | log) - then starts
           MirrorDesk.
uninstall: removes the launchers and the start-at-login entry, stops the adb server.
           The MirrorDesk folder itself is removed by the shell uninstaller.
"""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from . import desktop

ROOT = Path(__file__).resolve().parents[1]
DESKTOP_FILE = 'mirrordesk.desktop'
QUIT_DESKTOP_FILE = 'mirrordesk-quit.desktop'
COMMAND = 'mirrordesk'                       # ~/.local/bin/mirrordesk -> start-mirrordesk.sh
BUNDLE_ID = 'com.mirrordesk.app'
HEALTH = 'http://127.0.0.1:8765/api/health'


def say(text):
    print(text, flush=True)


def applications_dir():
    data = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
    return Path(data) / 'applications'


def _applescript_string(text):
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'


def applet_source(python, entry, log):
    """MirrorDesk.app's script. Stay-open: the Dock icon shows MirrorDesk is running."""
    return f'''-- MirrorDesk launcher, made by the MirrorDesk installer.
property mdPython : {_applescript_string(python)}
property mdEntry : {_applescript_string(entry)}
property mdLog : {_applescript_string(log)}
property mdHealth : "{HEALTH}"
property seenRunning : false

on run
	-- Started at login (LaunchAgent): no browser window.
	set atLogin to (system attribute "MIRRORDESK_AT_LOGIN") is "1"
	startMirrorDesk(not atLogin)
end run

on reopen
	startMirrorDesk(true)
end reopen

on startMirrorDesk(openBrowser)
	set extra to ""
	if openBrowser then set extra to " --open-browser"
	-- Already running: this only opens the browser.
	do shell script quoted form of mdPython & " " & quoted form of mdEntry & extra & " >/dev/null 2>&1 &"
	repeat 120 times
		if isRunning() then
			set seenRunning to true
			return
		end if
		delay 0.5
	end repeat
	display dialog "MirrorDesk did not start. Details are in its log:" & return & mdLog with title "MirrorDesk" buttons {{"OK"}} default button "OK" with icon stop
	quit
end startMirrorDesk

on isRunning()
	try
		do shell script "/usr/bin/curl -fsS -m 2 " & quoted form of mdHealth & " >/dev/null"
		return true
	on error
		return false
	end try
end isRunning

on idle
	-- Quit in the browser: the Dock icon goes too.
	if seenRunning and not isRunning() then
		set seenRunning to false
		quit
	end if
	return 5
end idle

on quit
	-- Quit from the Dock: MirrorDesk stops too. --quit proves itself with the token in MirrorDesk's
	-- data folder, so this also works when the page asks for the owner code on this computer.
	if isRunning() then
		try
			do shell script quoted form of mdPython & " " & quoted form of mdEntry & " --quit >/dev/null 2>&1"
		end try
	end if
	continue quit
end quit
'''


def make_mac_app(app=None, python=None, entry=None, run=subprocess.run):
    """Build MirrorDesk.app with osacompile, then give it MirrorDesk's name, icon and
    local-network explanation, and sign it ad hoc (its contents changed)."""
    app = Path(app or desktop.mac_app())
    python = python or sys.executable
    entry = entry or desktop.ENTRY_SCRIPT
    log = Path(os.environ.get('MIRRORDESK_DATA') or ROOT / '.runtime') / 'logs' / 'mirrordesk.log'
    source = app.with_name('.MirrorDesk.applescript')
    app.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(applet_source(python, entry, log), encoding='utf-8')
    if app.exists():
        shutil.rmtree(app)
    try:
        run(['osacompile', '-s', '-o', str(app), str(source)], check=True, capture_output=True, timeout=120)
    finally:
        source.unlink(missing_ok=True)
    contents = app / 'Contents'
    info_path = contents / 'Info.plist'
    with open(info_path, 'rb') as stream:
        info = plistlib.load(stream)
    info.update({'CFBundleName': 'MirrorDesk', 'CFBundleDisplayName': 'MirrorDesk', 'CFBundleIdentifier': BUNDLE_ID,
                 'CFBundleIconFile': 'applet',
                 'NSLocalNetworkUsageDescription': 'MirrorDesk receives your phone\'s screen over the local network '
                                                   'and sends it to browsers on that network.'})
    info.pop('CFBundleIconName', None)   # the asset catalog's icon would win over applet.icns
    with open(info_path, 'wb') as stream:
        plistlib.dump(info, stream)
    icon = ROOT / 'mirrordesk.icns'
    resources = contents / 'Resources'
    if icon.is_file():
        (resources / 'Assets.car').unlink(missing_ok=True)
        shutil.copyfile(icon, resources / 'applet.icns')
    run(['codesign', '--force', '--deep', '--sign', '-', str(app)], check=False, capture_output=True, timeout=120)
    return app


def _icon():
    icon = ROOT / 'mirrordesk.png'
    return icon if icon.is_file() else ROOT / 'mirrordesk' / 'web' / 'icon.svg'


def desktop_entry(python=None, entry=None):
    """The app-menu entry: starts MirrorDesk (or opens its page when it runs); right-click
    (desktop actions) offers Open and Quit."""
    argv = [python or sys.executable, str(entry or desktop.ENTRY_SCRIPT)]
    open_line = desktop.desktop_exec(argv + ['--open-browser'])
    quit_line = desktop.desktop_exec(argv + ['--quit'])
    return ('[Desktop Entry]\nType=Application\nName=MirrorDesk\nGenericName=Phone mirroring\n'
            'Comment=Mirror an iPhone, iPad or Android phone to any browser\n'
            f'Exec={open_line}\nIcon={_icon()}\nTerminal=false\nCategories=AudioVideo;Network;\n'
            'Keywords=AirPlay;iPhone;Android;mirror;screen;\nStartupNotify=false\nActions=open;quit;\n\n'
            f'[Desktop Action open]\nName=Open MirrorDesk\nExec={open_line}\n\n'
            f'[Desktop Action quit]\nName=Quit MirrorDesk\nExec={quit_line}\n')


def quit_entry(python=None, entry=None):
    """A separate "Quit MirrorDesk" in the app menu: Linux has no icon next to the clock to quit
    from, and not every desktop shows the right-click actions of an app-menu entry."""
    quit_line = desktop.desktop_exec([python or sys.executable, str(entry or desktop.ENTRY_SCRIPT), '--quit'])
    return ('[Desktop Entry]\nType=Application\nName=Quit MirrorDesk\nComment=Stop MirrorDesk on this computer\n'
            f'Exec={quit_line}\nIcon={_icon()}\nTerminal=false\nCategories=AudioVideo;Network;\n'
            'Keywords=MirrorDesk;stop;quit;close;\nStartupNotify=false\n')


def command_path():
    return Path.home() / '.local' / 'bin' / COMMAND


def make_command(link=None, script=None):
    """`mirrordesk start|open|stop|restart|status|log` (start-mirrordesk.sh), as a link in
    ~/.local/bin, which most desktops put on PATH. Returns the link, or None."""
    script = Path(script or ROOT / 'start-mirrordesk.sh')
    link = Path(link or command_path())
    if not script.is_file():
        return None
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if Path(os.readlink(link)) == script:
            return link
        link.unlink()          # left by an install in another folder
    elif link.exists():
        return None            # someone else's program of that name: leave it
    link.symlink_to(script)
    return link


def make_linux_entry(path=None):
    path = Path(path or applications_dir() / DESKTOP_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    for target, text in ((path, desktop_entry()), (path.with_name(QUIT_DESKTOP_FILE), quit_entry())):
        target.write_text(text, encoding='utf-8')
        target.chmod(0o755)
    if shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', str(path.parent)], capture_output=True, timeout=60)
    return path


def launch(kind):
    if kind == 'macos':
        subprocess.Popen(['/usr/bin/open', '-a', str(desktop.mac_app())])
    else:
        subprocess.Popen([sys.executable, str(desktop.ENTRY_SCRIPT), '--open-browser'], cwd=ROOT,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def install(launch_app=True, launcher=True):
    kind = desktop.desktop_os()
    if kind == 'windows':
        raise SystemExit('Windows: use Setup-MirrorDesk.cmd.')
    (ROOT / '.runtime').mkdir(parents=True, exist_ok=True)
    from . import androidtools
    say('[1/3] Android tools (Google adb)...')
    found = androidtools.find_adb()
    if found:
        say(f'      using {found}')
    elif androidtools.installable():
        try:
            androidtools.install()
            say('      installed')
        except (androidtools.ToolsError, OSError) as exc:
            say(f'      could not download them now ({exc}). Android can still be set up later from the '
                'Devices page ("Install Android tools").')
    else:
        say('      not available for this CPU from Google; install your system\'s adb package for Android phones.')
    say('[2/3] Launcher...')
    if not launcher:
        say('      skipped (--no-launcher)')
    elif kind == 'macos':
        try:
            say(f'      {make_mac_app()}')
        except (OSError, subprocess.SubprocessError) as exc:
            say(f'      could not create MirrorDesk.app ({exc}); start MirrorDesk with start-mirrordesk.sh instead.')
    else:
        say(f'      {make_linux_entry()}  ("MirrorDesk" and "Quit MirrorDesk" in the app menu)')
        try:
            link = make_command()
        except OSError as exc:
            link = None
            say(f'      the command `mirrordesk` could not be added ({exc})')
        if link is not None:
            say(f'      {link}  (the command `mirrordesk`: start, open, stop, restart, status, log)')
    if desktop.autostart_enabled():
        desktop.set_autostart(True)   # refresh an entry from an earlier install of this copy
    say('[3/3] Starting MirrorDesk...')
    if launch_app:
        launch(kind if launcher else 'linux')
        where = ('its icon is in the Dock while it runs' if kind == 'macos'
                 else '"MirrorDesk" and "Quit MirrorDesk" are in the app menu')
        say(f'      MirrorDesk runs in the background ({where}). Your browser opens it.')
    else:
        say('      skipped (--no-launch)')
    return 0


def uninstall():
    kind = desktop.desktop_os()
    say('Removing the start-at-login entry...')
    try:
        desktop.set_autostart(False)
    except OSError:
        pass
    say('Removing the launcher...')
    if kind == 'macos':
        shutil.rmtree(desktop.mac_app(), ignore_errors=True)
    else:
        (applications_dir() / DESKTOP_FILE).unlink(missing_ok=True)
        (applications_dir() / QUIT_DESKTOP_FILE).unlink(missing_ok=True)
        link = command_path()
        if link.is_symlink() and Path(os.readlink(link)) == ROOT / 'start-mirrordesk.sh':
            link.unlink()
    from . import androidtools
    adb = androidtools.bundled_adb()
    if adb.is_file():   # an adb server started from this folder keeps running otherwise
        subprocess.run([str(adb), 'kill-server'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL, timeout=20)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m mirrordesk.unixsetup')
    parser.add_argument('command', choices=['install', 'uninstall'])
    parser.add_argument('--no-launch', action='store_true')
    parser.add_argument('--no-launcher', action='store_true')
    options = parser.parse_args(argv)
    if options.command == 'install':
        return install(launch_app=not options.no_launch, launcher=not options.no_launcher)
    return uninstall()


if __name__ == '__main__':
    sys.exit(main())
