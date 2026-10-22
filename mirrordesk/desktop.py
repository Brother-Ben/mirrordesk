"""Desktop integration: background mode (no console window), start at sign-in,
single-instance detection, and on Windows a notification-area icon.

Windows: icon next to the clock, the per-user Run key. macOS: MirrorDesk.app (made
by unixsetup.py; Dock icon while running) and a per-user LaunchAgent. Linux: an
app-menu entry and an XDG autostart entry. Everything here is optional; the server
never depends on it (Docker and Linux servers run headless and get no Desktop
object, so the browser hides Quit/auto-start).
"""
import ctypes
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from aiohttp import web

from . import INSTALL_ID

log = logging.getLogger('mirrordesk.desktop')
ROOT = Path(__file__).resolve().parents[1]
ENTRY_SCRIPT = ROOT / 'MirrorDesk.pyw'
# Per-user "run at sign-in" list (no admin rights; visible in Task Manager > Startup apps).
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
APPROVED_KEY = r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
RUN_VALUE = 'MirrorDesk'
LAUNCH_AGENT = 'com.mirrordesk.app'          # macOS LaunchAgent label and file name
AUTOSTART_DESKTOP = 'mirrordesk.desktop'     # Linux XDG autostart entry
QUIT_DELAY = 1.2   # seconds between Quit and the shutdown: pages, which poll once a second, see it coming


def desktop_os():
    if os.name == 'nt':
        return 'windows'
    return 'macos' if sys.platform == 'darwin' else 'linux'


def mac_app():
    """MirrorDesk.app made by unixsetup.py (the launcher in ~/Applications)."""
    return Path.home() / 'Applications' / 'MirrorDesk.app'


def launch_agent_path():
    return Path.home() / 'Library' / 'LaunchAgents' / f'{LAUNCH_AGENT}.plist'


def xdg_autostart_path():
    config = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(config) / 'autostart' / AUTOSTART_DESKTOP


# -- single instance ----------------------------------------------------------------
def probe_instance(port, timeout=1.5):
    """What answers HTTP on this computer's port: None (nothing), 'current' (this
    copy of MirrorDesk; a second start just opens the browser), 'elsewhere' (MirrorDesk
    from another folder, e.g. the Prototype 6 folder), 'older' (a MirrorDesk from
    before the single-instance check, or the Prototype 6 backend) or 'other'."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a system proxy
    base = f'http://127.0.0.1:{port}'
    try:
        with opener.open(base + '/api/health', timeout=timeout) as response:
            health = json.load(response)
            if health.get('app') == 'mirrordesk':
                return 'current' if health.get('install') == INSTALL_ID else 'elsewhere'
    except urllib.error.HTTPError:
        pass  # answered, but no (current) health endpoint
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with opener.open(base + '/', timeout=timeout) as response:
            page = response.read(65536)
    except urllib.error.HTTPError as exc:
        page = exc.read(65536) if exc.fp else b''
    except OSError:
        return None
    return 'older' if b'MirrorDesk' in page else 'other'


def running_instance(port, timeout=1.5):
    """True when the current MirrorDesk already answers on this computer's port."""
    return probe_instance(port, timeout) == 'current'


def quit_running(port, data_dir, timeout=20.0, notify=None, udp_ports=()):
    """`--quit` (the app menu's "Quit MirrorDesk", start-mirrordesk.sh stop): ask the MirrorDesk on
    this computer to quit, with the token it keeps in its data folder, and wait until it is gone."""
    notify = notify or (lambda text: print(text, flush=True))
    found = probe_instance(port)
    if found is None:
        notify('MirrorDesk is not running.')
        return 0
    if found not in ('current', 'elsewhere'):
        notify(f'Port {port} is used by another program, not by MirrorDesk.')
        return 1
    headers = {'X-MirrorDesk': '1'}
    try:
        headers['X-MirrorDesk-Quit'] = (Path(data_dir) / 'quit-token').read_text(encoding='ascii').strip()
    except OSError:
        pass   # a MirrorDesk started without its desktop parts: only this computer's own sign-in counts
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/desktop/quit', data=b'', method='POST',
                                     headers=headers)
    try:
        opener.open(request, timeout=5).close()
    except urllib.error.HTTPError as exc:
        notify(f'MirrorDesk did not quit (it answered {exc.code}). Quit it from its page instead.')
        return 1
    except OSError:
        pass   # it may close the connection while it quits
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_instance(port, timeout=0.5) is None:
            # Its page closes first; its other ports stay taken while it finishes (open connections get
            # up to 5 s, then UxPlay stops). "Quit, then start again at once" must find them free.
            wait_ports_free(None, port, udp_ports, max(0.0, deadline - time.monotonic()))
            notify('MirrorDesk has quit.')
            return 0
        time.sleep(0.3)
    notify('MirrorDesk is still shutting down.')
    return 1


def show_running(port, data_dir, timeout=5.0):
    """A second start (the shortcut, Start-MirrorDesk.cmd): ask the MirrorDesk already running on
    this computer to open its window, with the token from its data folder. False: it could not."""
    headers = {'X-MirrorDesk': '1'}
    try:
        headers['X-MirrorDesk-Quit'] = (Path(data_dir) / 'quit-token').read_text(encoding='ascii').strip()
    except OSError:
        pass
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/desktop/window', data=b'', method='POST',
                                     headers=headers)
    try:
        opener.open(request, timeout=timeout).close()
        return True
    except (OSError, urllib.error.HTTPError):
        return False


def ports_free(host, tcp_port, udp_ports):
    """Can this server bind its HTTP port and loopback RTP ports? (bind test, released at once)"""
    family = socket.AF_INET6 if ':' in (host or '') else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            if os.name != 'nt':  # as asyncio's server does: TIME_WAIT leftovers must not count as busy
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host or '0.0.0.0', tcp_port))
        for port in udp_ports:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(('127.0.0.1', port))
    except OSError:
        return False
    return True


def wait_ports_free(host, tcp_port, udp_ports, timeout):
    """ports_free(), checked again until `timeout` seconds have passed."""
    deadline = time.monotonic() + timeout
    while not ports_free(host, tcp_port, udp_ports):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


def parse_netstat(text, tcp_port, udp_ports):
    """PIDs listening on tcp_port or bound to udp_ports in `netstat -ano` output.
    Locale-proof: a listening TCP row is recognised by its ':0' foreign port."""
    udp = {str(p) for p in udp_ports}
    owners = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[-1].isdigit():
            continue
        local_port = parts[1].rsplit(':', 1)[-1]
        if parts[0] == 'TCP' and len(parts) == 5 and local_port == str(tcp_port) and parts[2].endswith(':0'):
            owners.add(int(parts[-1]))
        elif parts[0] == 'UDP' and local_port in udp:
            owners.add(int(parts[-1]))
    owners.discard(0)
    return owners


def port_owners(tcp_port, udp_ports):
    if os.name != 'nt':
        return _lsof_owners(tcp_port, udp_ports) - {os.getpid()}
    try:
        text = subprocess.run(['netstat', '-ano'], capture_output=True, text=True, timeout=15,
                              errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return parse_netstat(text, tcp_port, udp_ports) - {os.getpid()}


def _lsof_owners(tcp_port, udp_ports):
    """macOS/Linux: PIDs of this user's processes holding the ports (lsof sees only our own
    processes without root, which is all take_over may close anyway)."""
    lsof = shutil.which('lsof') or ('/usr/sbin/lsof' if Path('/usr/sbin/lsof').is_file() else None)
    if not lsof:
        return set()
    owners = set()
    specs = [[f'-iTCP:{tcp_port}', '-sTCP:LISTEN']] + [[f'-iUDP@127.0.0.1:{port}'] for port in udp_ports]
    for spec in specs:
        try:
            text = subprocess.run([lsof, '-nP', '-t', *spec], capture_output=True, text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        owners.update(int(line) for line in text.split() if line.isdigit())
    return owners


def process_image(pid):
    """Full path (Windows) or command name (macOS/Linux) of a process, or None. No admin needed."""
    if os.name != 'nt':
        try:
            text = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        return text or None
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _is_python(image):
    """python.exe / pythonw.exe (Windows), python3.13 / Python (macOS, Linux)."""
    name = Path(image).name.lower()
    return name in ('python.exe', 'pythonw.exe') or (os.name != 'nt' and re.fullmatch(r'python[0-9.]*', name))


def take_over(host, tcp_port, udp_ports, found, confirm):
    """Something holds MirrorDesk's ports. If it is Python (an older MirrorDesk, a
    console copy, the Prototype 6 launcher) offer to close it; never touch other
    programs. Returns (True, None) when the ports are free again, else (False, why)."""
    ports = ', '.join(str(p) for p in (tcp_port, *udp_ports))
    owners = port_owners(tcp_port, udp_ports)
    if not owners:
        return False, (f'Another program is using MirrorDesk\'s ports ({ports}) and it could not be identified. '
                       'Close it (or restart the computer) and start MirrorDesk again.')
    images = {pid: process_image(pid) for pid in owners}
    names = sorted({Path(image).name if image else f'process {pid}' for pid, image in images.items()})
    if not all(image and _is_python(image) for image in images.values()):
        return False, (f'{", ".join(names)} is using MirrorDesk\'s ports ({ports}). Close that program, or start '
                       'MirrorDesk with a different --port.')
    who, example = {
        'older': ('An older MirrorDesk', 'a black MirrorDesk console window, or a copy started before an update'),
        'elsewhere': ('MirrorDesk from another folder', 'the copy in the old Prototype 6 folder'),
    }.get(found, ('Another MirrorDesk (or the Prototype 6 launcher)',
                  'a black MirrorDesk console window, or a copy started before an update'))
    question = (f'{who} is still running on this computer (process {", ".join(map(str, sorted(owners)))}) - for example '
                f'{example}.\n\n'
                'Close it and start MirrorDesk now?\n\n'
                '(If its black window stays open afterwards, you can just close it.)')
    if not confirm(question):
        return False, 'MirrorDesk was not started because the other copy is still running.'
    for pid in owners:
        try:
            os.kill(pid, signal.SIGTERM)  # Windows: TerminateProcess; its Job Object stops its UxPlay
        except OSError:
            pass
    if wait_ports_free(host, tcp_port, udp_ports, 15):
        return True, None
    return False, f'The other copy was asked to close but MirrorDesk\'s ports ({ports}) are still busy. Try again.'


# -- start at sign-in -------------------------------------------------------------------
def autostart_command():
    """pythonw (no console) running MirrorDesk.pyw from this copy of the project."""
    exe = Path(sys.executable)
    pythonw = exe.with_name('pythonw.exe')
    return f'"{pythonw if pythonw.is_file() else exe}" "{ENTRY_SCRIPT}"'


def posix_autostart_argv(os_kind=None):
    """What macOS/Linux run at sign-in for this copy. macOS opens MirrorDesk.app when it
    exists (the Dock icon, and the app macOS names when it asks about the local
    network); the app then starts MirrorDesk without opening the browser."""
    app = mac_app()
    if (os_kind or desktop_os()) == 'macos' and app.is_dir():
        return ['/usr/bin/open', '-g', '-a', str(app), '--env', 'MIRRORDESK_AT_LOGIN=1']
    return [sys.executable, str(ENTRY_SCRIPT)]


def desktop_exec(argv):
    """argv as a .desktop Exec= value (arguments quoted per the Desktop Entry spec)."""
    def quote(arg):
        if re.fullmatch(r'[A-Za-z0-9_@+=:,./-]+', arg):
            return arg
        escaped = re.sub(r'(["`$\\])', r'\\\1', arg)
        return '"' + escaped.replace('%', '%%') + '"'
    return ' '.join(quote(arg) for arg in argv)


def launch_agent(argv):
    return {'Label': LAUNCH_AGENT, 'ProgramArguments': list(argv), 'RunAtLoad': True,
            'ProcessType': 'Interactive', 'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}


def autostart_entry(argv):
    return ('[Desktop Entry]\nType=Application\nName=MirrorDesk\n'
            'Comment=Mirror a phone to any browser (starts in the background)\n'
            f'Exec={desktop_exec(argv)}\nIcon={ROOT / "mirrordesk" / "web" / "icon.svg"}\n'
            'Terminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n')


def autostart_enabled():
    """Whether THIS copy starts at sign-in (an entry from another copy does not count)."""
    kind = desktop_os()
    if kind == 'macos':
        try:
            with open(launch_agent_path(), 'rb') as stream:
                return plistlib.load(stream).get('ProgramArguments') == posix_autostart_argv()
        except (OSError, ValueError, plistlib.InvalidFileException, AttributeError):
            return False
    if kind == 'linux':
        try:
            text = xdg_autostart_path().read_text(encoding='utf-8')
        except OSError:
            return False
        lines = {line.strip() for line in text.splitlines()}
        return (f'Exec={desktop_exec(posix_autostart_argv())}' in lines and 'Hidden=true' not in lines
                and 'X-GNOME-Autostart-enabled=false' not in lines)
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return False
    if value != autostart_command():
        return False  # e.g. an entry left by a copy of the project in another folder
    try:  # Task Manager / Settings > Startup apps can switch the entry off
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            flags, _ = winreg.QueryValueEx(key, RUN_VALUE)
        if isinstance(flags, bytes) and flags and flags[0] & 1:
            return False
    except OSError:
        pass
    return True


def set_autostart(enabled):
    kind = desktop_os()
    if kind in ('macos', 'linux'):
        path = launch_agent_path() if kind == 'macos' else xdg_autostart_path()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            argv = posix_autostart_argv()
            temporary = path.with_name(path.name + '.tmp')
            if kind == 'macos':
                temporary.write_bytes(plistlib.dumps(launch_agent(argv)))
            else:
                temporary.write_text(autostart_entry(argv), encoding='utf-8')
            os.replace(temporary, path)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return autostart_enabled()
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
    try:  # clear a "disabled in Startup apps" flag so an explicit enable takes effect
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except OSError:
        pass
    return autostart_enabled()


# -- background mode ----------------------------------------------------------------------
def redirect_output(data_dir):
    """pythonw has no console (sys.stdout/stderr are None): keep the banner,
    warnings and tracebacks in <data>/logs/mirrordesk.log; the previous run is
    kept as mirrordesk.previous.log. Like settings.json, the log holds the
    access code, so it stays in the private data folder."""
    logs = Path(data_dir) / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / 'mirrordesk.log'
    try:
        if path.exists():
            path.replace(logs / 'mirrordesk.previous.log')
    except OSError:
        pass
    stream = open(path, 'a', encoding='utf-8', errors='replace', buffering=1)
    sys.stdout = sys.stderr = stream
    return path


def _applescript_text(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _dialog(text, question):
    """macOS (osascript) or Linux desktop (zenity / kdialog) dialog: True = Yes/OK.
    None when no dialog program or no display is available."""
    kind = desktop_os()
    try:
        if kind == 'macos':
            buttons = '{"No", "Yes"} default button "Yes"' if question else '{"OK"} default button "OK"'
            icon = 'caution' if question else 'stop'
            script = (f'display dialog {_applescript_text(text)} with title "MirrorDesk" buttons {buttons} '
                      f'with icon {icon}')
            result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=600)
            if not question:
                return True
            return result.returncode == 0 and 'Yes' in result.stdout
        if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            return None
        if shutil.which('zenity'):
            command = ['zenity', '--question' if question else '--error', '--title=MirrorDesk', f'--text={text}',
                       '--no-markup']
        elif shutil.which('kdialog'):
            command = ['kdialog', '--title', 'MirrorDesk', '--yesno' if question else '--error', text]
        else:
            return None
        return subprocess.run(command, timeout=600).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def notify(message):
    """A short desktop notification (macOS, Linux with notify-send) for launcher actions such as
    the app menu's Quit entry, which have no window to show text in."""
    print(message, flush=True)
    try:
        if desktop_os() == 'macos':
            subprocess.run(['osascript', '-e', f'display notification {_applescript_text(message)} with title '
                                               '"MirrorDesk"'], capture_output=True, timeout=10)
        elif desktop_os() == 'linux' and shutil.which('notify-send'):
            subprocess.run(['notify-send', '--app-name=MirrorDesk', 'MirrorDesk', message], capture_output=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def show_error(message):
    """A visible error for background mode, where nobody sees the console."""
    print(message, file=sys.stderr, flush=True)
    if os.name == 'nt':
        try:
            ctypes.windll.user32.MessageBoxW(None, message, 'MirrorDesk', 0x10 | 0x40000)  # error icon, topmost
        except (AttributeError, OSError):
            pass
    else:
        _dialog(message, question=False)


def ask_yes_no(question):
    """Background mode: a Yes/No box (Yes = 6 on Windows). No dialog available: no."""
    print(question, flush=True)
    if os.name != 'nt':
        return bool(_dialog(question, question=True))
    try:
        return ctypes.windll.user32.MessageBoxW(None, question, 'MirrorDesk', 0x4 | 0x20 | 0x40000) == 6
    except (AttributeError, OSError):
        return False


def ask_console(question):
    """Console mode: y/N on the terminal; never blocks when there is no terminal."""
    if not (sys.stdin and sys.stdin.isatty()):
        print(question, flush=True)
        return False
    try:
        return input(question + ' [y/N] ').strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def _graceful_exit():
    raise web.GracefulExit()  # a SystemExit: leaves run_app, which then runs the normal cleanup


# -- MirrorDesk's own window (Windows) ------------------------------------------------------------
CREATE_SUSPENDED = 0x4
WM_CLOSE = 0x0010
APP_WINDOW_ARGS = ('--no-first-run', '--no-default-browser-check', '--disable-sync', '--window-size=1280,860')


def app_browser():
    """Edge (part of Windows 10/11) or Chrome, for MirrorDesk's window; None: neither is there."""
    roots = [os.environ.get(name) for name in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA')]
    for name in (r'Microsoft\Edge\Application\msedge.exe', r'Google\Chrome\Application\chrome.exe'):
        for root in roots:
            if root and (Path(root) / name).is_file():
                return Path(root) / name
    return None


class _WindowJob:
    """A Windows job object holding MirrorDesk's browser windows: they close with MirrorDesk, even
    when it crashes (kill on job close)."""

    def __init__(self):
        from ctypes import wintypes
        from .receiver import ProcessContainment
        self.kernel32, self.handle = ProcessContainment._create_job()
        self.kernel32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                            wintypes.DWORD, ctypes.c_void_p)
        self.kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)

    def adopt(self, process):
        return bool(self.kernel32.AssignProcessToJobObject(self.handle, int(process._handle)))

    def pids(self):
        class ProcessIds(ctypes.Structure):
            _fields_ = [('assigned', ctypes.c_uint32), ('listed', ctypes.c_uint32), ('ids', ctypes.c_size_t * 256)]
        info = ProcessIds()
        if not self.kernel32.QueryInformationJobObject(self.handle, 3, ctypes.byref(info), ctypes.sizeof(info), None):
            return set()
        return {int(info.ids[i]) for i in range(min(info.listed, 256))}

    def terminate(self):
        self.kernel32.TerminateJobObject(self.handle, 0)


def _resume(process):
    """Start a process created suspended (Popen keeps no thread handle: resume the whole process)."""
    ntdll = ctypes.WinDLL('ntdll')
    ntdll.NtResumeProcess.argtypes = (ctypes.c_void_p,)
    ntdll.NtResumeProcess(int(process._handle))


def _close_windows(pids):
    """Ask the windows of these processes to close, as clicking their X does."""
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    user32.EnumWindows(visit, 0)
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    for hwnd in found:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return len(found)


class AppWindows:
    """MirrorDesk's own window on Windows: Edge (or Chrome) in app mode, with a browser profile of
    its own (<data>/window), in a job object. Quit closes every such window and nothing else: the
    user's own browser, tabs and profile are never touched. Before, MirrorDesk opened a tab in the
    default browser, every start opened another, and they all stayed open after Quit. Elsewhere
    (macOS, Linux, no Edge or Chrome) the default browser opens as before."""

    def __init__(self, data_dir):
        self.profile = Path(data_dir) / 'window'
        self.job = None

    def open(self, url):
        browser = app_browser() if os.name == 'nt' else None
        if browser is None:
            webbrowser.open(url)
            return False
        self._exited_cleanly()
        extra = shlex.split(os.environ.get('MIRRORDESK_WINDOW_ARGS', ''), posix=False)   # tests: e.g. off-screen
        try:
            process = subprocess.Popen([str(browser), f'--app={url}', f'--user-data-dir={self.profile}', *APP_WINDOW_ARGS,
                                        *extra],
                                       creationflags=CREATE_SUSPENDED, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.warning('Could not open MirrorDesk\'s window (%s); opening the default browser.', exc)
            webbrowser.open(url)
            return False
        try:
            if self.job is None:
                self.job = _WindowJob()
            self.job.adopt(process)     # still suspended: everything it starts is in the job too
        except OSError as exc:
            log.warning('MirrorDesk\'s window is not tied to MirrorDesk (%s).', exc)
        finally:
            _resume(process)
        return True

    def close(self, timeout=3.0):
        """Close every MirrorDesk window: as the X button does first (the browser saves its state
        and exits), then for sure."""
        if self.job is None:
            return
        try:
            pids = self.job.pids()
            if pids:
                _close_windows(pids)
                deadline = time.monotonic() + timeout
                while self.job.pids() and time.monotonic() < deadline:
                    time.sleep(0.1)
            self.job.terminate()
        except OSError:
            pass

    def _exited_cleanly(self):
        """A window MirrorDesk had to close by force must not greet the next one with "Restore
        pages?": mark the profile's last exit as normal."""
        path = self.profile / 'Default' / 'Preferences'
        try:
            prefs = json.loads(path.read_text(encoding='utf-8'))
            profile = prefs.setdefault('profile', {})
            if profile.get('exit_type') != 'Normal' or profile.get('exited_cleanly') is not True:
                profile['exit_type'], profile['exited_cleanly'] = 'Normal', True
                path.write_text(json.dumps(prefs), encoding='utf-8')
        except (OSError, ValueError, AttributeError):
            pass


class Desktop:
    """What the browser and the tray icon may do to the desktop app."""

    def __init__(self, url, tray=False, data_dir=None):
        self.url = url
        self.want_tray = tray
        self.tray = None
        self.loop = None
        self.windows = AppWindows(data_dir) if data_dir is not None else None
        self.quit_hooks = []     # called on the event loop when a quit begins (server.py: pages see it)

    def attach(self, loop):
        """Called once the server is listening (on the event loop)."""
        self.loop = loop
        if self.want_tray and self.tray is None and os.name == 'nt':   # macOS: MirrorDesk.app's Dock icon
            try:
                self.tray = TrayIcon(self)
                self.tray.start()
            except Exception as exc:  # no icon is an inconvenience, never a reason to stop
                log.warning('Notification-area icon unavailable: %s', exc)
                self.tray = None

    def status(self):
        return {'quit': True, 'autostart': autostart_enabled(), 'background': self.want_tray, 'os': desktop_os()}

    def set_autostart(self, enabled):
        result = set_autostart(enabled)
        if self.tray is not None:
            self.tray.refresh()
        return result

    def open(self):
        """Show MirrorDesk: its own window (Windows), or the default browser."""
        if self.windows is not None:
            self.windows.open(self.url)
        else:
            webbrowser.open(self.url)

    def quit(self, delay=0.0):
        """Thread-safe: stop the tray icon, close MirrorDesk's windows and shut the server down
        gracefully, `delay` seconds from now (pages poll once a second: they see it coming)."""
        if self.tray is not None:
            self.tray.stop()
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._begin_quit, delay)

    def _begin_quit(self, delay):
        for hook in list(self.quit_hooks):
            try:
                hook()
            except Exception:   # noqa: BLE001 - quitting must never fail
                log.debug('quit hook failed', exc_info=True)
        if self.windows is not None:
            threading.Thread(target=self.windows.close, name='mirrordesk-close-windows', daemon=True).start()
        self.loop.call_later(delay, _graceful_exit)


def icon_image(size=64):
    """web/icon.svg drawn with Pillow (64-unit grid)."""
    from PIL import Image, ImageDraw
    s = size / 64
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=14 * s, fill='#4f8cff')
    draw.rounded_rectangle((8 * s, 12 * s, 42 * s, 38 * s), radius=4 * s, outline='white', width=max(1, round(4 * s)))
    draw.rounded_rectangle((38 * s, 24 * s, 54 * s, 52 * s), radius=3 * s, fill='white')
    draw.line((18 * s, 44 * s, 34 * s, 44 * s), fill='white', width=max(1, round(4 * s)))
    return image


class TrayIcon:
    """Notification-area icon (pystray; its own Win32 message-loop thread).
    Left click opens the browser; right click: Open / Start with Windows / Quit."""

    def __init__(self, desktop):
        import pystray
        self.desktop = desktop
        item = pystray.MenuItem
        menu = pystray.Menu(
            item('Open MirrorDesk', lambda icon, _: desktop.open(), default=True),
            item('Start with Windows', self._toggle_autostart, checked=lambda _: bool(autostart_enabled())),
            pystray.Menu.SEPARATOR,
            item('Quit MirrorDesk', lambda icon, _: desktop.quit(QUIT_DELAY)))
        self.icon = pystray.Icon('MirrorDesk', icon_image(), 'MirrorDesk', menu)

    def start(self):
        threading.Thread(target=self.icon.run, name='mirrordesk-tray', daemon=True).start()

    def refresh(self):
        try:
            self.icon.update_menu()  # re-reads the "Start with Windows" check mark
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass

    def _toggle_autostart(self, icon, item):
        try:
            set_autostart(not autostart_enabled())
        except OSError as exc:
            log.warning('Could not change start at sign-in: %s', exc)
        self.refresh()
