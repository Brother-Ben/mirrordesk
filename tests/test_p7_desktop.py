"""Desktop integration: persistent sign-in sessions, quit / start-at-sign-in API,
Windows registry entry, single-instance detection and real background mode."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_p7_media import free_udp_ports  # noqa: E402

from mirrordesk import desktop
from mirrordesk.security import COOKIE
from mirrordesk.server import MirrorDeskServer

ROOT = Path(__file__).resolve().parents[1]
W = {'X-MirrorDesk': '1'}
REMOTE = {'Host': '192.168.1.50:8765'}
windows_only = pytest.mark.skipif(os.name != 'nt', reason='Windows desktop integration')


class FakeDesktop:
    def __init__(self):
        self.quits = 0
        self.autostart = False

    def status(self):
        return {'quit': True, 'autostart': self.autostart, 'background': False}

    def set_autostart(self, enabled):
        self.autostart = enabled
        return enabled

    def quit(self, delay=0.0):
        self.quits += 1
        self.delay = delay


@pytest.fixture
async def make_client(tmp_path):
    clients = []

    async def make(data='data', desktop_obj=None):
        video_port, audio_port = free_udp_ports()
        server = MirrorDeskServer(tmp_path / data, port=8765, video_port=video_port, audio_port=audio_port,
                                  start_receiver=False, desktop=desktop_obj)
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        clients.append(client)
        return server, client
    yield make
    for client in clients:
        await client.close()


async def sign_in(server, client):
    response = await client.post('/api/login', json={'code': server.store.settings.access_code},
                                 headers={**REMOTE, **W})
    assert response.status == 200
    return response.cookies[COOKIE].value


# -- sessions survive restarts --------------------------------------------------------
async def test_remote_sign_in_survives_server_restart(make_client, tmp_path):
    server, client = await make_client()
    token = await sign_in(server, client)
    stored = (tmp_path / 'data' / 'sessions.json').read_text(encoding='utf-8')
    assert token not in stored, 'only hashes may be stored'
    await client.close()

    server2, client2 = await make_client()  # same data dir = MirrorDesk started again
    client2.session.cookie_jar.update_cookies({COOKIE: token})
    assert (await client2.get('/api/status', headers=REMOTE)).status == 200
    assert (await client2.post('/api/logout', headers={**REMOTE, **W})).status == 200
    assert server2.access.sessions == {}
    client2.session.cookie_jar.update_cookies({COOKIE: token})
    assert (await client2.get('/api/status', headers=REMOTE)).status == 401


async def test_new_access_code_signs_everyone_out_even_after_restart(make_client, tmp_path):
    server, client = await make_client()
    token = await sign_in(server, client)
    await client.close()
    settings = json.loads((tmp_path / 'data' / 'settings.json').read_text(encoding='utf-8'))
    settings['access_code'] = 'NEWCODE9'  # e.g. MIRRORDESK_ACCESS_CODE changed between runs
    (tmp_path / 'data' / 'settings.json').write_text(json.dumps(settings), encoding='utf-8')
    server2, client2 = await make_client()
    client2.session.cookie_jar.update_cookies({COOKIE: token})
    assert (await client2.get('/api/status', headers=REMOTE)).status == 401


async def test_regenerated_code_revokes_persisted_sessions(make_client):
    server, client = await make_client()
    token = await sign_in(server, client)
    assert (await client.post('/api/regenerate/access-code', headers=W)).status == 200  # local, trusted
    assert json.loads((server.data_dir / 'sessions.json').read_text(encoding='utf-8')) == {}
    client.session.cookie_jar.update_cookies({COOKIE: token})
    assert (await client.get('/api/status', headers=REMOTE)).status == 401


@pytest.mark.parametrize('content', ['{not json', '[1, 2]', json.dumps({'short': 9e18}),
                                     json.dumps({'a' * 64: 'soon'}), json.dumps({'b' * 64: 1.0})])
async def test_bad_or_expired_sessions_file_is_ignored(make_client, tmp_path, content):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'sessions.json').write_text(content, encoding='utf-8')
    server, client = await make_client()
    assert server.access.sessions == {}
    assert (await client.get('/api/status')).status == 200


# -- desktop API ----------------------------------------------------------------------
async def test_desktop_api_absent_without_desktop(make_client):
    server, client = await make_client()
    assert (await client.get('/api/desktop')).status == 404
    assert (await client.post('/api/desktop/quit', headers=W)).status == 404
    assert (await client.post('/api/desktop/autostart', json={'enabled': True}, headers=W)).status == 404


async def test_desktop_quit_and_autostart_api(make_client):
    fake = FakeDesktop()
    server, client = await make_client(desktop_obj=fake)
    assert (await (await client.get('/api/desktop')).json()) == fake.status()
    for bad in ({'enabled': 'yes'}, {'enabled': True, 'x': 1}, {}):
        assert (await client.post('/api/desktop/autostart', json=bad, headers=W)).status == 400
    response = await client.post('/api/desktop/autostart', json={'enabled': True}, headers=W)
    assert response.status == 200 and (await response.json())['autostart'] is True
    assert (await client.post('/api/desktop/quit')).status == 403  # CSRF header required
    assert (await client.post('/api/desktop/quit', headers={**REMOTE, **W})).status == 401  # remote must sign in
    response = await client.post('/api/desktop/quit', headers=W)
    assert response.status == 200 and (await response.json()) == {'quitting': True}
    # The shutdown follows later (Desktop.quit's delay): the answer goes out, and open pages
    # (polling once a second) see "quitting" first.
    assert fake.quits == 1 and fake.delay >= 1.0


def test_desktop_quit_raises_graceful_exit_on_the_server_loop():
    loop = asyncio.new_event_loop()
    try:
        controls = desktop.Desktop('http://127.0.0.1:1/', tray=False)
        hooks = []
        controls.quit_hooks.append(lambda: hooks.append(loop.time()))
        controls.attach(loop)
        controls.quit(0.2)
        loop.call_later(5, loop.stop)  # safety net: the test fails instead of hanging
        start = loop.time()
        with pytest.raises(web.GracefulExit):
            loop.run_forever()
        assert hooks and loop.time() - start >= 0.19, 'pages are told first, the shutdown follows'
    finally:
        loop.close()


async def test_status_tells_open_pages_that_mirrordesk_is_quitting(make_client):
    """Quit from the icon next to the clock, or from another window: every open page shows that
    MirrorDesk is going off (and closes itself where the browser allows it)."""
    controls = desktop.Desktop('http://127.0.0.1:1/', tray=False)
    server, client = await make_client(desktop_obj=controls)
    assert (await (await client.get('/api/status')).json())['quitting'] is False
    controls.loop = asyncio.get_running_loop()
    controls._begin_quit(3600)            # the hooks only: the shutdown itself is an hour away
    assert (await (await client.get('/api/status')).json())['quitting'] is True


def test_app_window_uses_its_own_profile_and_closes_with_mirrordesk(tmp_path, monkeypatch):
    """Windows: MirrorDesk's window is Edge/Chrome in app mode with a profile of its own, inside a
    job object; Quit closes it. Never the user's own browser or profile."""
    started = []

    class FakeProcess:
        _handle = 0

    def fake_popen(args, **kwargs):
        started.append((args, kwargs))
        return FakeProcess()
    monkeypatch.setattr(desktop.os, 'name', 'nt')
    monkeypatch.setattr(desktop, 'app_browser', lambda: Path(r'C:\Edge\msedge.exe'))
    monkeypatch.setattr(desktop.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(desktop, '_resume', lambda process: None)
    adopted = []

    class FakeJob:
        def adopt(self, process):
            adopted.append(process)
            return True
    monkeypatch.setattr(desktop, '_WindowJob', FakeJob)
    windows = desktop.AppWindows(tmp_path / 'data')
    assert windows.open('http://127.0.0.1:8765/') is True
    args, kwargs = started[0]
    assert args[0] == r'C:\Edge\msedge.exe' and '--app=http://127.0.0.1:8765/' in args
    assert f'--user-data-dir={tmp_path / "data" / "window"}' in args
    assert kwargs['creationflags'] & desktop.CREATE_SUSPENDED, 'joins the job before it can start anything'
    assert len(adopted) == 1
    opened = []
    monkeypatch.setattr(desktop, 'app_browser', lambda: None)
    monkeypatch.setattr(desktop.webbrowser, 'open', opened.append)
    assert windows.open('http://127.0.0.1:8765/') is False and opened == ['http://127.0.0.1:8765/']


async def test_health_identifies_mirrordesk_for_single_instance(make_client):
    server, client = await make_client()
    assert await asyncio.to_thread(desktop.running_instance, client.server.port)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        unused = s.getsockname()[1]
    assert not await asyncio.to_thread(desktop.running_instance, unused, 0.5)


def test_redirect_output_keeps_previous_run(tmp_path):
    saved = sys.stdout, sys.stderr
    try:
        first = desktop.redirect_output(tmp_path)
        print('run one')
        sys.stdout.close()
        desktop.redirect_output(tmp_path)
        print('run two', file=sys.stderr)
        sys.stdout.close()
    finally:
        sys.stdout, sys.stderr = saved
    assert first == tmp_path / 'logs' / 'mirrordesk.log'
    assert first.read_text(encoding='utf-8') == 'run two\n'
    assert (tmp_path / 'logs' / 'mirrordesk.previous.log').read_text(encoding='utf-8') == 'run one\n'


@windows_only
def test_tray_icon_image_matches_app_icon():
    image = desktop.icon_image(32)
    assert image.size == (32, 32) and image.mode == 'RGBA'
    assert image.getpixel((16, 2))[:3] == (0x4f, 0x8c, 0xff)


# -- Windows start at sign-in (isolated registry key) ------------------------------------
@pytest.fixture
def test_run_keys(monkeypatch):
    import winreg
    base = r'Software\MirrorDeskTest-' + str(os.getpid())
    monkeypatch.setattr(desktop, 'RUN_KEY', base + r'\Run')
    monkeypatch.setattr(desktop, 'APPROVED_KEY', base + r'\StartupApproved\Run')
    yield winreg, base
    for sub in (r'\StartupApproved\Run', r'\StartupApproved', r'\Run', ''):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + sub)
        except OSError:
            pass


@windows_only
def test_autostart_registry_entry_roundtrip(test_run_keys):
    winreg, base = test_run_keys
    assert desktop.autostart_enabled() is False
    assert desktop.set_autostart(True) is True
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, desktop.RUN_KEY) as key:
        command = winreg.QueryValueEx(key, 'MirrorDesk')[0]
    assert command == desktop.autostart_command()
    assert 'pythonw.exe' in command and command.endswith('MirrorDesk.pyw"')
    # Switched off in Task Manager > Startup apps -> reported off; enabling again clears that.
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, desktop.APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'MirrorDesk', 0, winreg.REG_BINARY, b'\x03' + b'\x00' * 11)
    assert desktop.autostart_enabled() is False
    assert desktop.set_autostart(True) is True
    # An entry from another copy of the project is not "this copy starts at sign-in".
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, desktop.RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'MirrorDesk', 0, winreg.REG_SZ, '"C:\\old\\pythonw.exe" "C:\\old\\MirrorDesk.pyw"')
    assert desktop.autostart_enabled() is False
    assert desktop.set_autostart(False) is False
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, desktop.RUN_KEY) as key:
        with pytest.raises(FileNotFoundError):
            winreg.QueryValueEx(key, 'MirrorDesk')
    assert desktop.set_autostart(False) is False  # idempotent


# -- an older copy still holding the ports (user report 2026-10-14) ---------------------------
NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:8765           0.0.0.0:0              LISTENING       9744
  TCP    127.0.0.1:8765         127.0.0.1:51000        ESTABLISHED     9744
  TCP    127.0.0.1:51000        127.0.0.1:8765         ESTABLISHED     4242
  TCP    [::]:8765              [::]:0                 ABHÖREN         9745
  TCP    0.0.0.0:18765          0.0.0.0:0              LISTENING       1111
  UDP    127.0.0.1:5004         *:*                                    9744
  UDP    127.0.0.1:5006         *:*                                    9746
  UDP    0.0.0.0:5353           *:*                                    2222
  UDP    [::]:5004              *:*                                    9747
"""


def test_netstat_parsing_finds_listeners_and_rtp_owners_in_any_language():
    assert desktop.parse_netstat(NETSTAT, 8765, (5004, 5006)) == {9744, 9745, 9746, 9747}
    assert desktop.parse_netstat(NETSTAT, 9999, ()) == set()


def _free_tcp_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _start_old_copy():
    port = _free_tcp_port()
    video_port, audio_port = free_udp_ports()
    process = subprocess.Popen([sys.executable, str(ROOT / 'tests' / 'fake_old_mirrordesk.py'),
                                str(port), str(video_port), str(audio_port)])
    deadline = time.monotonic() + 20
    while desktop.probe_instance(port, 0.5) is None:
        assert process.poll() is None and time.monotonic() < deadline, 'fake old MirrorDesk did not start'
        time.sleep(0.2)
    return process, port, (video_port, audio_port)


@pytest.fixture
def old_copy():
    process, port, udp = _start_old_copy()
    yield process, port, udp
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)


async def test_probe_tells_current_older_other_and_nothing(make_client):
    server, client = await make_client()
    assert await asyncio.to_thread(desktop.probe_instance, client.server.port) == 'current'
    async def hello(request):
        return web.Response(text='hello')
    other = web.Application()
    other.router.add_get('/', hello)
    other_client = TestClient(TestServer(other))
    await other_client.start_server()
    try:
        assert await asyncio.to_thread(desktop.probe_instance, other_client.server.port) == 'other'
    finally:
        await other_client.close()
    assert await asyncio.to_thread(desktop.probe_instance, _free_tcp_port(), 0.5) is None


async def test_probe_tells_a_copy_in_another_folder_from_itself():
    """Prototype 7 in its own folder next to the Prototype 6 folder's copy: a running copy
    from the other folder (with another install id, or none at all) is not 'current'."""
    for body in ({'ok': True, 'app': 'mirrordesk'}, {'ok': True, 'app': 'mirrordesk', 'install': '0123456789ab'}):
        async def health(request, body=body):
            return web.json_response(body)
        other = web.Application()
        other.router.add_get('/api/health', health)
        other_client = TestClient(TestServer(other))
        await other_client.start_server()
        try:
            assert await asyncio.to_thread(desktop.probe_instance, other_client.server.port) == 'elsewhere'
        finally:
            await other_client.close()


def test_take_over_names_a_copy_in_another_folder(monkeypatch):
    monkeypatch.setattr(desktop, 'port_owners', lambda tcp, udp: {4321})
    monkeypatch.setattr(desktop, 'process_image', lambda pid: r'C:\uv\python\pythonw.exe')
    questions = []
    ok, why = desktop.take_over('0.0.0.0', 8765, (5004, 5006), 'elsewhere', lambda q: questions.append(q) or False)
    assert not ok and 'still running' in why
    assert questions[0].startswith('MirrorDesk from another folder') and 'Prototype 6 folder' in questions[0]


@windows_only
def test_take_over_asks_then_closes_an_older_copy(old_copy):
    process, port, udp = old_copy
    assert desktop.probe_instance(port) == 'older'
    assert not desktop.ports_free('0.0.0.0', port, udp)
    questions = []
    ok, why = desktop.take_over('0.0.0.0', port, udp, 'older', lambda q: questions.append(q) or False)
    assert not ok and 'still running' in why and process.poll() is None, 'No = leave it alone'
    # The port owner is the real interpreter (the venv launcher's child), named by its PID.
    assert 'An older MirrorDesk' in questions[0] and 'process ' in questions[0]
    ok, why = desktop.take_over('0.0.0.0', port, udp, 'older', lambda q: True)
    assert ok and why is None
    assert process.wait(timeout=10) is not None
    assert desktop.ports_free('0.0.0.0', port, udp)


def test_take_over_never_closes_other_programs(monkeypatch):
    monkeypatch.setattr(desktop, 'port_owners', lambda tcp, udp: {4321})
    monkeypatch.setattr(desktop, 'process_image', lambda pid: r'C:\Program Files\nginx\nginx.exe')
    ok, why = desktop.take_over('0.0.0.0', 8765, (5004, 5006), 'other',
                                lambda q: pytest.fail('must not offer to close a non-Python program'))
    assert not ok and 'nginx.exe' in why and '--port' in why
    monkeypatch.setattr(desktop, 'port_owners', lambda tcp, udp: set())
    ok, why = desktop.take_over('0.0.0.0', 8765, (5004, 5006), None, lambda q: pytest.fail('nothing to close'))
    assert not ok and 'could not be identified' in why


def test_start_right_after_quit_waits_for_the_closing_copy(tmp_path):
    """Linux tester build, 0.11.0 checks: Quit, then MirrorDesk again at once. The quitting copy's page
    was gone, but it held its media ports a few seconds longer, so the new start took it for another
    MirrorDesk (a yes/no question, or no start at all without a dialog). Now the start waits for it."""
    port = _free_tcp_port()
    video_port, audio_port = free_udp_ports()
    closing = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # the copy that is still closing
    closing.bind(('127.0.0.1', video_port))
    python = ROOT / '.venv' / 'Scripts' / 'pythonw.exe' if os.name == 'nt' else Path(sys.executable)
    args = [str(python), str(ROOT / 'MirrorDesk.pyw'), '--host', '127.0.0.1', '--port', str(port),
            '--data', str(tmp_path), '--no-receiver', '--video-port', str(video_port), '--audio-port', str(audio_port)]
    process = subprocess.Popen(args, cwd=tmp_path, env={**os.environ, 'MIRRORDESK_ICE_ADDRESSES': 'loopback'})
    log = tmp_path / 'logs' / 'mirrordesk.log'
    try:
        deadline = time.monotonic() + 30
        while not log.exists():   # background mode opens its log just before it checks its ports
            assert process.poll() is None and time.monotonic() < deadline, 'MirrorDesk did not reach its port check'
            time.sleep(0.05)
        time.sleep(1.5)
        assert process.poll() is None, log.read_text(errors='replace')
        closing.close()
        while desktop.probe_instance(port, 0.5) != 'current':
            assert process.poll() is None, log.read_text(errors='replace') if log.exists() else 'no log'
            assert time.monotonic() < deadline, 'MirrorDesk did not start'
            time.sleep(0.2)
        messages = []
        assert desktop.quit_running(port, tmp_path, notify=messages.append, udp_ports=(video_port, audio_port)) == 0
        assert messages == ['MirrorDesk has quit.']
        assert desktop.ports_free('127.0.0.1', port, (video_port, audio_port)), 'a start right now would work'
        assert process.wait(timeout=20) == 0
    finally:
        closing.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_quit_waits_until_the_ports_are_free(tmp_path, monkeypatch):
    answers = iter(['current', None])
    monkeypatch.setattr(desktop, 'probe_instance', lambda port, timeout=1.5: next(answers))
    monkeypatch.setattr(desktop.urllib.request, 'build_opener', lambda *args: type('Opener', (), {
        'open': lambda self, request, timeout: type('Response', (), {'close': lambda self: None})()})())
    checks = iter([False, False, True])   # the page is gone, its media ports follow later
    asked = []
    monkeypatch.setattr(desktop, 'ports_free', lambda host, tcp, udp: asked.append((tcp, tuple(udp))) or next(checks))
    monkeypatch.setattr(desktop.time, 'sleep', lambda seconds: None)
    messages = []
    assert desktop.quit_running(8765, tmp_path, notify=messages.append, udp_ports=(5004, 5006)) == 0
    assert asked == [(8765, (5004, 5006))] * 3 and messages == ['MirrorDesk has quit.']


@windows_only
def test_new_launch_replaces_older_copy_holding_the_ports(tmp_path, old_copy):
    """The 2026-10-14 report: an old console MirrorDesk held 8765/5004/5006, so every new
    start failed with 'UDP port 5004 is in use'. Now: console without a terminal refuses
    with a clear message; --replace (= answering Yes) closes it and starts."""
    process, port, (video_port, audio_port) = old_copy
    args = ['--host', '127.0.0.1', '--port', str(port), '--data', str(tmp_path), '--no-receiver',
            '--video-port', str(video_port), '--audio-port', str(audio_port)]
    env = {**os.environ, 'MIRRORDESK_ICE_ADDRESSES': 'loopback'}
    refused = subprocess.run([sys.executable, '-m', 'mirrordesk', *args], cwd=ROOT, env=env, timeout=60,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert refused.returncode == 1 and 'still running' in refused.stdout + refused.stderr
    assert process.poll() is None
    pythonw = ROOT / '.venv' / 'Scripts' / 'pythonw.exe'
    new = subprocess.Popen([str(pythonw), str(ROOT / 'MirrorDesk.pyw'), '--replace', *args], cwd=tmp_path, env=env)
    try:
        assert process.wait(timeout=20) is not None, 'old copy closed'
        deadline = time.monotonic() + 30
        while not desktop.running_instance(port, 0.5):
            assert new.poll() is None and time.monotonic() < deadline, \
                (tmp_path / 'logs' / 'mirrordesk.log').read_text(errors='replace')
            time.sleep(0.2)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f'http://127.0.0.1:{port}/api/desktop/quit', data=b'', method='POST',
                                         headers={'X-MirrorDesk': '1'})
        opener.open(request, timeout=5).close()
        assert new.wait(timeout=20) == 0
    finally:
        if new.poll() is None:
            new.kill()
            new.wait(timeout=10)


# -- the real background process ------------------------------------------------------------
def _get(url, timeout=2):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read())


@windows_only
def test_background_mode_runs_windowless_single_instance_and_quits(tmp_path):
    pythonw = ROOT / '.venv' / 'Scripts' / 'pythonw.exe'
    if not pythonw.is_file():
        pytest.skip('project .venv with pythonw.exe required')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    video_port, audio_port = free_udp_ports()
    args = [str(pythonw), str(ROOT / 'MirrorDesk.pyw'), '--host', '127.0.0.1', '--port', str(port),
            '--data', str(tmp_path), '--no-receiver', '--video-port', str(video_port),
            '--audio-port', str(audio_port)]
    env = {**os.environ, 'MIRRORDESK_ICE_ADDRESSES': 'loopback'}
    process = subprocess.Popen(args, cwd=tmp_path, env=env)  # cwd like a sign-in start: not the project
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not desktop.running_instance(port, 0.5):
            assert process.poll() is None, (tmp_path / 'logs' / 'mirrordesk.log').read_text(errors='replace')
            time.sleep(0.2)
        assert _get(f'http://127.0.0.1:{port}/api/desktop') == {
            'quit': True, 'autostart': desktop.autostart_enabled(), 'background': True, 'os': 'windows'}
        log_text = (tmp_path / 'logs' / 'mirrordesk.log').read_text(encoding='utf-8')
        assert 'Running in the background' in log_text and 'Ctrl+C' not in log_text
        # A second start (double-click while already running) exits at once instead of failing.
        second = subprocess.run(args, cwd=tmp_path, env=env, timeout=20)
        assert second.returncode == 0
        assert process.poll() is None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f'http://127.0.0.1:{port}/api/desktop/quit', data=b'', method='POST',
                                         headers={'X-MirrorDesk': '1'})
        with opener.open(request, timeout=5) as response:
            assert json.loads(response.read()) == {'quitting': True}
        assert process.wait(timeout=20) == 0
        assert not desktop.running_instance(port, 0.5)
        assert 'session_end' in ''.join(p.read_text(encoding='utf-8', errors='replace')
                                        for p in (tmp_path / 'diagnostics').rglob('*.jsonl'))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
