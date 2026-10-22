"""macOS and Linux desktop integration (desktop.py, unixsetup.py) and the POSIX parts of
receiver.py / usbsource.py. Runs on any OS: the platform is simulated, nothing is installed."""
import errno
import plistlib
import signal
import sys
from pathlib import Path

import pytest

from mirrordesk import desktop, receiver, unixsetup, usbsource


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / '.local' / 'share'))
    return tmp_path


# -- start at login ---------------------------------------------------------------------
def test_linux_autostart_entry_roundtrip(fake_home, monkeypatch):
    monkeypatch.setattr(desktop, 'desktop_os', lambda: 'linux')
    assert desktop.autostart_enabled() is False
    assert desktop.set_autostart(True) is True
    entry = (fake_home / '.config' / 'autostart' / 'mirrordesk.desktop').read_text(encoding='utf-8')
    assert entry.startswith('[Desktop Entry]\n') and 'NoDisplay=true' in entry
    assert f'Exec={desktop.desktop_exec([sys.executable, str(desktop.ENTRY_SCRIPT)])}\n' in entry
    # Switched off in the desktop's own startup settings -> off.
    path = desktop.xdg_autostart_path()
    path.write_text(entry.replace('X-GNOME-Autostart-enabled=true', 'X-GNOME-Autostart-enabled=false'), encoding='utf-8')
    assert desktop.autostart_enabled() is False
    # An entry made by another copy of MirrorDesk is not "this copy starts at login".
    path.write_text(desktop.autostart_entry(['/elsewhere/.venv/bin/python', '/elsewhere/MirrorDesk.pyw']),
                    encoding='utf-8')
    assert desktop.autostart_enabled() is False
    assert desktop.set_autostart(False) is False and not path.exists()
    assert desktop.set_autostart(False) is False   # idempotent


def test_macos_launch_agent_opens_the_app_without_a_browser(fake_home, monkeypatch):
    monkeypatch.setattr(desktop, 'desktop_os', lambda: 'macos')
    assert desktop.set_autostart(True) is True
    with open(fake_home / 'Library' / 'LaunchAgents' / 'com.mirrordesk.app.plist', 'rb') as stream:
        agent = plistlib.load(stream)
    assert agent['Label'] == 'com.mirrordesk.app' and agent['RunAtLoad'] is True
    assert agent['ProgramArguments'] == [sys.executable, str(desktop.ENTRY_SCRIPT)], 'no app yet: MirrorDesk itself'
    # Once MirrorDesk.app exists, login opens the app (Dock icon) with MIRRORDESK_AT_LOGIN=1.
    (fake_home / 'Applications' / 'MirrorDesk.app').mkdir(parents=True)
    assert desktop.autostart_enabled() is False, 'the agent no longer matches what this copy would run'
    assert desktop.set_autostart(True) is True
    with open(desktop.launch_agent_path(), 'rb') as stream:
        args = plistlib.load(stream)['ProgramArguments']
    assert args[:3] == ['/usr/bin/open', '-g', '-a'] and args[-2:] == ['--env', 'MIRRORDESK_AT_LOGIN=1']
    assert desktop.set_autostart(False) is False and not desktop.launch_agent_path().exists()


def test_desktop_exec_quotes_per_the_desktop_entry_spec():
    assert desktop.desktop_exec(['/usr/bin/python3', '/opt/md/MirrorDesk.pyw']) == '/usr/bin/python3 /opt/md/MirrorDesk.pyw'
    assert desktop.desktop_exec(['/home/a b/py', '100%', 'say "$HOME"']) == \
        '"/home/a b/py" "100%%" "say \\"\\$HOME\\""'


def test_desktop_status_names_the_os():
    status = desktop.Desktop('http://127.0.0.1:1/').status()
    assert status['os'] == desktop.desktop_os() and status['quit'] is True


def test_only_python_programs_may_be_closed():
    assert desktop._is_python(r'C:\uv\python\pythonw.exe')
    if sys.platform != 'win32':
        assert desktop._is_python('/Users/x/Library/Application Support/MirrorDesk/runtime/python/bin/python3.13')
    assert not desktop._is_python('/usr/sbin/nginx')
    assert not desktop._is_python('pythonista-helper')


# -- MirrorDesk.app and the Linux app-menu entry -----------------------------------------------
def test_applet_script_quotes_paths_and_quits_with_mirrordesk():
    source = unixsetup.applet_source('/Users/a "b"/py', '/Users/x/MirrorDesk.pyw', '/Users/x/log')
    assert 'property mdPython : "/Users/a \\"b\\"/py"' in source
    assert 'quoted form of mdPython' in source and '--open-browser' in source
    assert 'MIRRORDESK_AT_LOGIN' in source and 'on reopen' in source and 'on idle' in source
    assert 'quoted form of mdEntry & " --quit' in source and 'api/desktop/quit' not in source, \
        'the Dock Quit uses the quit token (works with "ask for the owner code on this computer too")'
    assert '{{' not in source and 'buttons {"OK"}' in source


def test_make_mac_app_names_signs_and_explains_local_network(tmp_path, monkeypatch):
    calls = []
    icon = tmp_path / 'root' / 'mirrordesk.icns'
    icon.parent.mkdir()
    icon.write_bytes(b'icns-test')
    monkeypatch.setattr(unixsetup, 'ROOT', tmp_path / 'root')

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'osacompile':   # what osacompile -s makes
            app = Path(command[command.index('-o') + 1])
            assert Path(command[-1]).read_text(encoding='utf-8').startswith('-- MirrorDesk launcher')
            (app / 'Contents' / 'Resources').mkdir(parents=True)
            (app / 'Contents' / 'Resources' / 'Assets.car').write_bytes(b'')
            with open(app / 'Contents' / 'Info.plist', 'wb') as stream:
                plistlib.dump({'CFBundleName': 'MirrorDesk', 'CFBundleIconName': 'applet', 'OSAAppletStayOpen': True},
                              stream)
    app = unixsetup.make_mac_app(tmp_path / 'Applications' / 'MirrorDesk.app', '/py', '/entry.pyw', run=run)
    assert calls[0][:2] == ['osacompile', '-s'] and calls[-1][:4] == ['codesign', '--force', '--deep', '--sign']
    with open(app / 'Contents' / 'Info.plist', 'rb') as stream:
        info = plistlib.load(stream)
    assert info['CFBundleIdentifier'] == 'com.mirrordesk.app' and info['OSAAppletStayOpen'] is True
    assert 'local network' in info['NSLocalNetworkUsageDescription'] and 'CFBundleIconName' not in info
    assert (app / 'Contents' / 'Resources' / 'applet.icns').read_bytes() == b'icns-test'
    assert not (app / 'Contents' / 'Resources' / 'Assets.car').exists()
    assert not (tmp_path / 'Applications' / '.MirrorDesk.applescript').exists()


def test_linux_menu_entry_starts_this_copy_and_opens_the_browser(tmp_path):
    entry = unixsetup.desktop_entry('/opt/md/.venv/bin/python', '/opt/md/MirrorDesk.pyw')
    assert 'Exec=/opt/md/.venv/bin/python /opt/md/MirrorDesk.pyw --open-browser\n' in entry
    assert 'Type=Application' in entry and 'Terminal=false' in entry and 'Icon=' in entry
    path = unixsetup.make_linux_entry(tmp_path / 'applications' / 'mirrordesk.desktop')
    assert path.read_text(encoding='utf-8').startswith('[Desktop Entry]')


def test_linux_app_menu_can_quit_mirrordesk(tmp_path):
    """Tester request: Linux had no way to quit but the page (no icon next to the clock)."""
    entry = unixsetup.desktop_entry('/opt/md/.venv/bin/python', '/opt/md/MirrorDesk.pyw')
    assert 'Actions=open;quit;' in entry
    assert '[Desktop Action quit]\nName=Quit MirrorDesk\nExec=/opt/md/.venv/bin/python /opt/md/MirrorDesk.pyw --quit\n' in entry
    quit_entry = unixsetup.quit_entry('/opt/md/.venv/bin/python', '/opt/md/MirrorDesk.pyw')
    assert 'Name=Quit MirrorDesk\n' in quit_entry and 'Exec=/opt/md/.venv/bin/python /opt/md/MirrorDesk.pyw --quit\n' in quit_entry
    path = unixsetup.make_linux_entry(tmp_path / 'applications' / 'mirrordesk.desktop')
    assert (path.parent / unixsetup.QUIT_DESKTOP_FILE).read_text(encoding='utf-8').startswith('[Desktop Entry]')


def test_mirrordesk_command_link(tmp_path):
    script = tmp_path / 'app' / 'start-mirrordesk.sh'
    script.parent.mkdir()
    script.write_text('#!/bin/sh\n', encoding='ascii')
    link = tmp_path / 'bin' / 'mirrordesk'
    try:
        assert unixsetup.make_command(link, script) == link
    except OSError as exc:   # Windows without the right to make symbolic links
        pytest.skip(f'symbolic links unavailable here: {exc}')
    assert link.is_symlink() and Path(unixsetup.os.readlink(link)) == script
    assert unixsetup.make_command(link, script) == link, 'running the installer again keeps it'
    link.unlink()
    link.write_text('someone else\'s program', encoding='ascii')
    assert unixsetup.make_command(link, script) is None and not link.is_symlink(), 'never replaced'
    assert unixsetup.make_command(link, tmp_path / 'missing.sh') is None


def test_quit_command_asks_the_running_copy_with_its_token(tmp_path, monkeypatch):
    (tmp_path / 'quit-token').write_text('tok123', encoding='ascii')
    answers = iter(['current', None])
    monkeypatch.setattr(desktop, 'probe_instance', lambda port, timeout=1.5: next(answers))
    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append((request.full_url, request.get_method(), dict(request.header_items())))
            return type('Response', (), {'close': lambda self: None})()
    monkeypatch.setattr(desktop.urllib.request, 'build_opener', lambda *args: Opener())
    monkeypatch.setattr(desktop, 'ports_free', lambda host, tcp, udp: True)
    messages = []
    assert desktop.quit_running(8765, tmp_path, notify=messages.append) == 0
    url, method, headers = sent[0]
    assert url == 'http://127.0.0.1:8765/api/desktop/quit' and method == 'POST'
    assert headers['X-mirrordesk-quit'] == 'tok123' and headers['X-mirrordesk'] == '1'
    assert messages == ['MirrorDesk has quit.']
    monkeypatch.setattr(desktop, 'probe_instance', lambda port, timeout=1.5: None)
    assert desktop.quit_running(8765, tmp_path, notify=messages.append) == 0
    assert messages[-1] == 'MirrorDesk is not running.'


# -- receiver on macOS / Linux ------------------------------------------------------------------
def test_line_buffering_uses_homebrew_gstdbuf_on_macos(tmp_path, monkeypatch):
    gstdbuf = tmp_path / 'gstdbuf'
    gstdbuf.write_bytes(b'')
    monkeypatch.setattr(receiver, 'HOMEBREW_GSTDBUF', (str(tmp_path / 'missing'), str(gstdbuf)))
    assert receiver.line_buffered(['uxplay'], 'posix', lambda name: None) == [str(gstdbuf), '-oL', '-eL', 'uxplay']
    assert receiver.line_buffered(['uxplay'], 'nt', lambda name: 'x') == ['uxplay']


def test_leftover_uxplay_from_a_crashed_run_is_stopped(tmp_path):
    pidfile = tmp_path / 'uxplay.pid'
    pidfile.write_text('4242\n')
    alive = {4242}
    kills = []

    def kill(pid, sig):
        kills.append((pid, sig))
        alive.discard(pid)
    names = lambda pid: '/Users/x/MirrorDesk/uxplay/bin/uxplay' if pid in alive else None
    assert receiver.reap_orphan(pidfile, command_name=names, kill=kill) is True
    assert kills == [(4242, signal.SIGTERM)] and not pidfile.exists()


def test_a_reused_pid_is_never_killed(tmp_path):
    pidfile = tmp_path / 'uxplay.pid'
    pidfile.write_text('4242\n')
    assert receiver.reap_orphan(pidfile, command_name=lambda pid: '/usr/bin/vim',
                                kill=lambda pid, sig: pytest.fail('must not kill another program')) is False
    assert not pidfile.exists()
    assert receiver.reap_orphan(tmp_path / 'none.pid', kill=lambda pid, sig: pytest.fail('no file')) is False


def test_installed_uxplay_is_used_on_posix(tmp_path, monkeypatch):
    bundle = tmp_path / 'uxplay'
    name = 'uxplay.exe' if sys.platform == 'win32' else 'uxplay'
    (bundle / 'bin').mkdir(parents=True)
    (bundle / 'bin' / name).write_bytes(b'')
    monkeypatch.setattr(receiver, 'BUNDLE_DIR', bundle)
    monkeypatch.delenv('MIRRORDESK_UXPLAY', raising=False)

    class Store:
        class settings:
            uxplay_path = ''
    supervisor = receiver.ReceiverSupervisor.__new__(receiver.ReceiverSupervisor)
    supervisor.store, supervisor.command_override = Store, None
    assert supervisor.executable() == [str(bundle / 'bin' / name)]
    assert supervisor.platform() == ('windows-bundle' if sys.platform == 'win32' else 'posix-bundle')


# -- USB cable on macOS / Linux -----------------------------------------------------------------
def test_mac_says_cable_mirroring_is_not_available(monkeypatch):
    monkeypatch.setattr(usbsource.sys, 'platform', 'darwin')
    backend, problem = usbsource.load_backend()
    assert backend is None and problem == usbsource.MAC_UNSUPPORTED and 'AirPlay' in problem


def test_linux_busy_phone_names_usbmuxd(monkeypatch):
    import usb.core
    monkeypatch.setattr(usbsource.sys, 'platform', 'linux')
    with pytest.raises(usbsource.CaptureError, match='usbmuxd'):
        with usbsource._linux_busy():
            raise usb.core.USBError('Resource busy', -6, errno.EBUSY)
    with pytest.raises(usb.core.USBError):   # other errors pass through unchanged
        with usbsource._linux_busy():
            raise usb.core.USBError('Pipe error', -9, errno.EPIPE)


def test_linux_detaches_kernel_drivers_before_configuring(monkeypatch):
    monkeypatch.setattr(usbsource.sys, 'platform', 'linux')
    log = []

    class Backend:
        def is_kernel_driver_active(self, handle, number):
            if number > 2:
                raise OSError('no such interface')
            return number == 2

        def detach_kernel_driver(self, handle, number):
            log.append(('detach', number))

        def set_configuration(self, handle, value):
            log.append(('config', value))

        def claim_interface(self, handle, number):
            log.append(('claim', number))
    handle = usbsource.Handle.__new__(usbsource.Handle)
    handle.backend, handle.handle = Backend(), object()
    handle.set_configuration(4)
    handle.claim(2)
    assert log == [('detach', 2), ('config', 4), ('detach', 2), ('claim', 2)]


def test_linux_does_not_set_an_active_configuration_again(monkeypatch):
    """With usbmuxd holding an interface, setting even the active configuration fails ("busy")."""
    monkeypatch.setattr(usbsource.sys, 'platform', 'linux')
    log = []

    class Backend:
        def get_configuration(self, handle):
            return 5

        def set_configuration(self, handle, value):
            log.append(('config', value))

        def is_kernel_driver_active(self, handle, number):
            return False
    handle = usbsource.Handle.__new__(usbsource.Handle)
    handle.backend, handle.handle = Backend(), object()
    handle.set_configuration(5)
    assert log == []
    handle.set_configuration(4)
    assert log == [('config', 4)]
