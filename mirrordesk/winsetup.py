"""Windows install/uninstall steps after setup.ps1 has created .venv (`python -m mirrordesk.winsetup`).

install:   Android tools (Google adb, checksum-pinned), shortcut icon, Start menu and
           desktop shortcuts, Windows Firewall rules for MirrorDesk's programs (one
           Windows permission prompt), then starts MirrorDesk.
uninstall: removes the firewall rules (one permission prompt), the shortcuts and the
           start-at-sign-in entry. The folder itself is then deleted by the user.

Firewall rules allow incoming connections on Private (and domain) networks only,
for exactly these files: the Python that runs MirrorDesk (python.exe/pythonw.exe of
the interpreter inside this folder), the bundled UxPlay and adb. Phones and other
devices need them: AirPlay discovery and video, WebRTC viewers, Wireless debugging.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RULE_PREFIX = 'MirrorDesk'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def say(text):
    print(text, flush=True)


def interpreter_files():
    """The interpreter that actually opens the sockets: a venv's python.exe is only a
    launcher for the base interpreter, so firewall rules must name the base files."""
    base = Path(getattr(sys, '_base_executable', sys.executable) or sys.executable).resolve()
    files = [base]
    windowless = base.with_name('pythonw.exe')
    if windowless.is_file():
        files.append(windowless)
    return files


def program_rules(root=ROOT):
    """[(rule name, program path)] for every program that receives connections."""
    rules = [(f'{RULE_PREFIX} - {path.name}', path) for path in interpreter_files()]
    uxplay = root / 'uxplay' / 'bin' / 'uxplay.exe'
    if uxplay.is_file():
        rules.append((f'{RULE_PREFIX} - AirPlay receiver (uxplay.exe)', uxplay))
    from . import androidtools
    adb = androidtools.bundled_adb()
    if adb.is_file():
        rules.append((f'{RULE_PREFIX} - Android (adb.exe)', adb))
    return rules


def firewall_commands(rules, add=True):
    """netsh commands: remove our old rules by name, then (install) add one rule per program."""
    commands = []
    for name, path in rules:
        commands.append(f'netsh advfirewall firewall delete rule name="{name}" >nul 2>nul')
        if add:
            commands.append(f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow '
                            f'program="{path}" enable=yes profile=private,domain')
    return commands


def apply_firewall(rules, data_dir, add=True, run_elevated=None):
    from . import usbdriver
    run_elevated = run_elevated or usbdriver._run_elevated
    log = Path(data_dir) / 'logs' / ('firewall-setup.log' if add else 'firewall-remove.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    # One elevated cmd for all rules = one Windows permission prompt.
    run_elevated(' & '.join(firewall_commands(rules, add)), log)
    # Reading rules needs no rights: confirm the result instead of trusting exit codes.
    return all(rule_exists(name) == add for name, _ in rules)


def rule_exists(name):
    result = subprocess.run(['netsh', 'advfirewall', 'firewall', 'show', 'rule', f'name={name}'],
                            capture_output=True, text=True, creationflags=NO_WINDOW, timeout=30)
    return result.returncode == 0 and name in result.stdout


def make_icon(path):
    from .desktop import icon_image
    images = [icon_image(size) for size in (16, 24, 32, 48, 64, 256)]
    images[-1].save(path, format='ICO', sizes=[(i.width, i.height) for i in images])
    return path


def _special_folder(name):
    script = f"[Environment]::GetFolderPath('{name}')"
    result = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, text=True,
                            creationflags=NO_WINDOW, timeout=30)
    return Path(result.stdout.strip()) if result.stdout.strip() else None


def shortcut_paths():
    paths = []
    programs = _special_folder('Programs')
    desktop = _special_folder('Desktop')
    if programs:
        paths.append(programs / 'MirrorDesk.lnk')
    if desktop:
        paths.append(desktop / 'MirrorDesk.lnk')
    return paths


def create_shortcut(link, target, arguments, workdir, icon):
    def quote(text):
        return "'" + str(text).replace("'", "''") + "'"
    script = ('$s = (New-Object -ComObject WScript.Shell).CreateShortcut(' + quote(link) + '); '
              f'$s.TargetPath = {quote(target)}; $s.Arguments = {quote(arguments)}; '
              f'$s.WorkingDirectory = {quote(workdir)}; $s.IconLocation = {quote(icon)}; '
              "$s.Description = 'MirrorDesk: mirror a phone to any browser'; $s.Save()")
    subprocess.run(['powershell', '-NoProfile', '-Command', script], check=True, creationflags=NO_WINDOW, timeout=60)


def install(launch=True, firewall=True, shortcuts=True):
    data = ROOT / '.runtime'
    data.mkdir(parents=True, exist_ok=True)
    from . import androidtools
    say('[1/4] Android tools (Google adb)...')
    if androidtools.bundled_adb().is_file():
        say('      already installed')
    else:
        try:
            androidtools.install()
            say('      installed')
        except (androidtools.ToolsError, OSError) as exc:
            say(f'      could not download them now ({exc}). Android can still be set up later from the '
                'Devices page ("Install Android tools").')
    say('[2/4] Shortcuts...')
    icon = make_icon(ROOT / 'mirrordesk.ico')
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    for link in (shortcut_paths() if shortcuts else []):
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            create_shortcut(link, pythonw, f'"{ROOT / "MirrorDesk.pyw"}" --open-browser', ROOT, icon)
            say(f'      {link}')
        except (OSError, subprocess.SubprocessError) as exc:
            say(f'      could not create {link.name} ({exc})')
    say('[3/4] Windows Firewall (Windows asks for permission once)...')
    if firewall:
        rules = program_rules()
        for name, path in rules:
            say(f'      allow on private networks: {path}')
        try:
            ok = apply_firewall(rules, data)
            say('      done' if ok else '      Windows reported a problem; see .runtime\\logs\\firewall-setup.log')
        except Exception as exc:  # noqa: BLE001 - declined prompt etc.: MirrorDesk still works on this computer
            say(f'      skipped ({exc}). Phones and other devices may not reach MirrorDesk until this is done: '
                'run Setup-MirrorDesk.cmd again and allow the prompt.')
    else:
        say('      skipped (--no-firewall)')
    say('[4/4] Starting MirrorDesk...')
    if launch:
        subprocess.Popen([str(pythonw), str(ROOT / 'MirrorDesk.pyw'), '--open-browser'], cwd=ROOT,
                         creationflags=NO_WINDOW | getattr(subprocess, 'DETACHED_PROCESS', 0))
        say('      MirrorDesk runs in the background; its icon is next to the clock. Your browser opens it.')
    else:
        say('      skipped (--no-launch)')
    return 0


def uninstall():
    data = ROOT / '.runtime'
    from . import desktop
    say('Removing the start-at-sign-in entry...')
    try:
        desktop.set_autostart(False)
    except OSError:
        pass
    say('Removing shortcuts...')
    for link in shortcut_paths():
        try:
            link.unlink()
        except OSError:
            pass
    from . import androidtools
    adb = androidtools.bundled_adb()
    if adb.is_file():   # an adb daemon from this folder would keep the folder from being deleted
        subprocess.run([str(adb), 'kill-server'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=20)
    say('Removing the Windows Firewall rules (Windows asks for permission once)...')
    try:
        apply_firewall(program_rules(), data, add=False)
    except Exception as exc:  # noqa: BLE001
        say(f'  skipped ({exc})')
    say('Done. Quit MirrorDesk (icon next to the clock), then delete this folder.')
    say("If you installed the iPhone cable driver: before deleting, open MirrorDesk > Connect > USB cable and "
        "choose \"restore Apple's driver\" (or Device Manager > the iPhone > Uninstall device).")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m mirrordesk.winsetup')
    parser.add_argument('command', choices=['install', 'uninstall'])
    parser.add_argument('--no-launch', action='store_true')
    parser.add_argument('--no-firewall', action='store_true')
    parser.add_argument('--no-shortcuts', action='store_true')
    options = parser.parse_args(argv)
    if options.command == 'install':
        return install(launch=not options.no_launch, firewall=not options.no_firewall,
                       shortcuts=not options.no_shortcuts)
    return uninstall()


if __name__ == '__main__':
    sys.exit(main())
