"""Prototype 7 server: access control, CSRF/DNS-rebinding guards, settings, control API."""
import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_p7_media import free_udp_ports  # noqa: E402

from mirrordesk.server import MirrorDeskServer
from mirrordesk.settings import SettingsStore, validate_update, parse_ports

ROOT = Path(__file__).resolve().parents[1]
FAKE = [sys.executable, str(ROOT / 'tests' / 'fake_uxplay.py')]
W = {'X-MirrorDesk': '1'}


@pytest.fixture
async def make_client(tmp_path):
    clients = []

    async def make(start_receiver=False, command=None, env=None):
        video_port, audio_port = free_udp_ports()
        server = MirrorDeskServer(tmp_path / f'data{len(clients)}', port=8765, video_port=video_port,
                                  audio_port=audio_port, start_receiver=start_receiver, receiver_command=command)
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        clients.append(client)
        return server, client
    yield make
    for client in clients:
        await client.close()


REMOTE = {'Host': '192.168.1.50:8765'}          # loopback peer, LAN Host -> treated as remote
PROXIED = {'X-Forwarded-For': '100.64.0.9'}     # e.g. tailscale serve / reverse proxy


async def test_local_browser_is_trusted_and_gets_pin(make_client):
    server, client = await make_client()
    response = await client.get('/api/status')
    assert response.status == 200
    data = await response.json()
    assert data['client']['local'] is True
    assert data['receiver']['pin'] == server.store.settings.pin
    assert 'access_code' not in json.dumps(data['settings'])
    assert response.headers['Cache-Control'] == 'no-store'
    assert "default-src 'self'" in response.headers['Content-Security-Policy']


@pytest.mark.parametrize('headers', [REMOTE, PROXIED, {'Host': 'mirrordesk.tailnet-abc.ts.net', 'X-Forwarded-For': '1.2.3.4'}])
async def test_remote_or_proxied_clients_must_sign_in(make_client, headers):
    server, client = await make_client()
    assert (await client.get('/api/status', headers=headers)).status == 401
    page = await client.get('/', headers=headers, allow_redirects=False)
    assert page.status == 302 and page.headers['Location'] == '/login'
    assert (await client.get('/login', headers=headers)).status == 200
    assert (await client.get('/app/app.css', headers=headers)).status == 200  # login page styling is public
    assert (await client.get('/app/theme.js', headers=headers)).status == 200  # and its theme


async def test_usb_status_and_reconnect_without_usb(make_client):
    """Tests run with MIRRORDESK_USB=0: the status says so and nothing touches USB."""
    server, client = await make_client()
    assert server.usb is None
    data = await (await client.get('/api/status')).json()
    assert data['usb']['state'] == 'unavailable' and data['usb']['enabled'] is False
    assert data['source']['input'] == 'airplay'
    assert (await client.post('/api/usb/reconnect', headers=W)).status == 404


async def test_usb_setting_starts_and_stops_the_source(tmp_path):
    from mirrordesk.usbsource import UsbSource

    class Source(UsbSource):
        started = stopped = 0

        async def start(self):
            Source.started += 1
            self.state = 'waiting'

        async def stop(self):
            Source.stopped += 1
            self.state = 'off'

    video_port, audio_port = free_udp_ports()
    server = MirrorDeskServer(tmp_path / 'data', port=8765, video_port=video_port, audio_port=audio_port,
                              start_receiver=False, usb=False)
    server.usb = Source(server.store, server.hub)
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    try:
        assert Source.started == 1  # usb_enabled defaults to on
        response = await client.put('/api/settings', json={'usb_enabled': False}, headers=W)
        assert response.status == 200 and Source.stopped == 1
        assert (await client.post('/api/usb/reconnect', headers=W)).status == 409
        assert (await (await client.get('/api/status')).json())['usb']['enabled'] is False
        await client.put('/api/settings', json={'usb_enabled': True}, headers=W)
        assert Source.started == 2
    finally:
        await client.close()
    assert Source.stopped == 2  # shutdown hands the phone back


async def test_app_script_requires_auth_for_remote(make_client):
    server, client = await make_client()
    assert (await client.get('/app/app.js', headers=REMOTE, allow_redirects=False)).status == 302


async def test_login_flow_wrong_then_right_code(make_client):
    server, client = await make_client()
    bad = await client.post('/api/login', json={'code': 'WRONG1'}, headers={**REMOTE, **W, 'Origin': 'http://192.168.1.50:8765'})
    assert bad.status == 403
    good = await client.post('/api/login', json={'code': server.store.settings.access_code.lower()},
                             headers={**REMOTE, **W, 'Origin': 'http://192.168.1.50:8765'})
    assert good.status == 200
    cookie = good.cookies['mirrordesk_session']
    assert cookie['httponly'] and cookie['samesite'] == 'Strict'
    status = await client.get('/api/status', headers=REMOTE)
    assert status.status == 200 and (await status.json())['client']['local'] is False
    # Regenerating the code revokes every remote session.
    assert (await client.post('/api/regenerate/access-code', headers=W)).status == 200
    assert (await client.get('/api/status', headers=REMOTE)).status == 401


REMOTE_W = {**REMOTE, **W, 'Origin': 'http://192.168.1.50:8765'}
# Everything but watching: what a friend signed in with the viewer code must not reach.
OWNER_ONLY = [('GET', '/api/settings'), ('PUT', '/api/settings'), ('GET', '/api/access'), ('GET', '/api/network'),
              ('POST', '/api/network/check'), ('POST', '/api/receiver/stop'), ('POST', '/api/receiver/start'),
              ('POST', '/api/receiver/restart'), ('GET', '/api/receiver/log'), ('POST', '/api/regenerate/pin'),
              ('POST', '/api/regenerate/access-code'), ('POST', '/api/regenerate/owner-code'),
              ('POST', '/api/usb/reconnect'), ('POST', '/api/android/reconnect'), ('POST', '/api/android/key'),
              ('GET', '/api/android/log'), ('GET', '/api/android/control'), ('GET', '/api/desktop'),
              ('POST', '/api/desktop/quit'), ('POST', '/api/diagnostics/mark'), ('POST', '/api/diagnostics/export'),
              ('POST', '/api/phone/disconnect'), ('POST', '/api/receiver/forget'), ('POST', '/api/qr')]


async def sign_in_as(client, code):
    """Remote sign-in; returns the session token and leaves the cookie jar empty."""
    response = await client.post('/api/login', json={'code': code}, headers=REMOTE_W)
    assert response.status == 200
    client.session.cookie_jar.clear()
    return (await response.json())['role'], response.cookies['mirrordesk_session'].value


async def call(client, method, path, token):
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({'mirrordesk_session': token})
    response = await client.request(method, path, headers=REMOTE_W, allow_redirects=False,
                                    json=None if method == 'GET' else {})
    client.session.cookie_jar.clear()
    return response


async def test_viewer_code_can_only_watch(make_client):
    """Regression: a friend signed in over Tailscale could press Stop and Start and change
    settings. The access code now only lets a device watch."""
    server, client = await make_client()
    role, token = await sign_in_as(client, server.store.settings.access_code)
    assert role == 'viewer'
    data = await (await call(client, 'GET', '/api/status', token)).json()
    assert data['client'] == {'local': False, 'role': 'viewer'}
    assert set(data['receiver']) == {'state', 'phone'}, 'no pairing code or phone name'
    assert 'settings' not in data and 'list' not in data['viewers'] and 'platform' not in data
    text = json.dumps(data)
    for secret in (server.store.settings.access_code, server.store.settings.owner_code):
        assert secret not in text
    for method, path in OWNER_ONLY:
        assert (await call(client, method, path, token)).status == 403, (method, path)
    assert server.store.settings.resolution == '1080p'
    # Watching still works: the page, its script and a viewer offer (refused as malformed, not as forbidden).
    assert (await call(client, 'GET', '/', token)).status == 200
    assert (await call(client, 'GET', '/app/app.js', token)).status == 200
    assert (await call(client, 'POST', '/api/viewers', token)).status == 400
    assert (await call(client, 'GET', '/login', token)).status == 200, 'may sign in again as the owner'


async def test_owner_code_gives_full_control_and_replaces_a_viewer_session(make_client):
    server, client = await make_client()
    # Signing in again from the same browser (cookie sent along) swaps the session.
    _, viewer_token = await sign_in_as(client, server.store.settings.access_code)
    client.session.cookie_jar.update_cookies({'mirrordesk_session': viewer_token})
    response = await client.post('/api/login', json={'code': server.store.settings.owner_code.lower()}, headers=REMOTE_W)
    assert response.status == 200 and (await response.json())['role'] == 'owner'
    owner_token = response.cookies['mirrordesk_session'].value
    assert (await call(client, 'GET', '/api/status', viewer_token)).status == 401, 'the viewer session ended'
    data = await (await call(client, 'GET', '/api/status', owner_token)).json()
    assert data['client']['role'] == 'owner' and data['receiver']['pin'] == server.store.settings.pin
    assert (await call(client, 'GET', '/api/settings', owner_token)).status == 200
    assert (await call(client, 'GET', '/login', owner_token)).status == 302


async def test_rotating_one_code_signs_out_only_the_devices_that_used_it(make_client):
    server, client = await make_client()
    _, viewer = await sign_in_as(client, server.store.settings.access_code)
    _, owner = await sign_in_as(client, server.store.settings.owner_code)
    assert (await call(client, 'POST', '/api/regenerate/access-code', owner)).status == 200
    assert (await call(client, 'GET', '/api/status', viewer)).status == 401
    assert (await call(client, 'GET', '/api/status', owner)).status == 200
    rotated = await call(client, 'POST', '/api/regenerate/owner-code', owner)
    assert rotated.status == 200
    code = (await rotated.json())['owner_code']
    assert code == server.store.settings.owner_code != server.store.settings.access_code
    assert (await call(client, 'GET', '/api/status', owner)).status == 401
    fresh = rotated.cookies['mirrordesk_session'].value
    assert (await call(client, 'GET', '/api/status', fresh)).status == 200, 'the device that rotated it stays in'


def test_sessions_from_before_owner_codes_are_viewers(tmp_path):
    """Everyone who signed in before owner codes used the one access code, friends included."""
    import hashlib
    import time
    from aiohttp.test_utils import make_mocked_request
    from mirrordesk.security import VIEWER, AccessControl
    store = SettingsStore(tmp_path / 'settings.json')
    key = hashlib.sha256(f'{store.settings.access_code}:oldtoken'.encode()).hexdigest()
    (tmp_path / 'sessions.json').write_text(json.dumps({key: time.time() + 3600}), encoding='utf-8')
    access = AccessControl(store, trust_localhost=False, sessions_path=tmp_path / 'sessions.json')
    request = make_mocked_request('GET', '/', headers={'Cookie': 'mirrordesk_session=oldtoken'})
    assert access.role(request) == VIEWER


def test_owner_code_is_created_once_and_never_equals_the_viewer_code(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'access_code': 'OLDCODE2'}), encoding='utf-8')  # an install from before owner codes
    store = SettingsStore(path)
    assert store.settings.access_code == 'OLDCODE2', 'the code everyone has becomes the viewer code'
    owner = store.settings.owner_code
    assert len(owner) == 6 and owner != 'OLDCODE2'
    assert SettingsStore(path).settings.owner_code == owner, 'kept across restarts'
    assert 'owner_code' not in json.dumps(store.settings.public())
    monkeypatch.setenv('MIRRORDESK_OWNER_CODE', 'boss2026')
    pinned = SettingsStore(path)
    assert pinned.settings.owner_code == 'BOSS2026' and pinned.owner_code_pinned
    with pytest.raises(ValueError, match='MIRRORDESK_OWNER_CODE'):
        pinned.regenerate_owner_code()
    monkeypatch.setenv('MIRRORDESK_OWNER_CODE', 'OLDCODE2')  # same as the viewer code: ignored
    assert SettingsStore(path).settings.owner_code != 'OLDCODE2'


async def test_login_is_rate_limited(make_client):
    server, client = await make_client()
    codes = []
    for _ in range(11):
        r = await client.post('/api/login', json={'code': 'NOPE00'}, headers={**REMOTE, **W})
        codes.append(r.status)
    assert codes[:10] == [403] * 10 and codes[10] == 429


async def test_dns_rebinding_host_rejected(make_client):
    server, client = await make_client()
    response = await client.get('/api/status', headers={'Host': 'evil.example.com:8765'})
    assert response.status == 421


async def test_state_change_requires_header_and_same_origin(make_client):
    server, client = await make_client()
    assert (await client.post('/api/diagnostics/mark', json={'issue': 'freeze'})).status == 403
    cross = await client.post('/api/diagnostics/mark', json={'issue': 'freeze'},
                              headers={**W, 'Origin': 'http://127.0.0.1:9999'})
    assert cross.status == 403
    ok = await client.post('/api/diagnostics/mark', json={'issue': 'no_mirrordesk_option'},
                           headers={**W, 'Origin': f'http://{client.host}:{client.port}'})
    assert ok.status == 200


async def test_settings_roundtrip_validation_and_persistence(make_client, tmp_path):
    server, client = await make_client()
    response = await client.put('/api/settings', json={'resolution': '720p', 'fps': 30, 'receiver_name': 'Living Room'},
                                headers=W)
    assert response.status == 200
    body = await response.json()
    assert set(body['changed']) == {'resolution', 'fps', 'receiver_name'}
    assert body['receiver_restarting'] is False  # receiver was not running
    for bad in ({'fps': 45}, {'resolution': '4k'}, {'receiver_name': '-n evil'}, {'pin': '0000'},
                {'max_viewers': 0}, {'airplay_ports': '80'}, {'audio_enabled': 'yes'}, {'unknown': 1}):
        assert (await client.put('/api/settings', json=bad, headers=W)).status == 400, bad
    reloaded = SettingsStore(server.data_dir / 'settings.json').settings
    assert (reloaded.resolution, reloaded.fps, reloaded.receiver_name) == ('720p', 30, 'Living Room')
    assert reloaded.pin == server.store.settings.pin and len(reloaded.access_code) == 6


def test_settings_store_recovers_from_corrupt_file(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{not json', encoding='utf-8')
    settings = SettingsStore(path).settings
    assert settings.resolution == '1080p' and settings.fps == 60 and len(settings.pin) == 4
    path.write_text(json.dumps({'fps': 'sixty', 'resolution': '1440p', 'pin': 'abcd'}), encoding='utf-8')
    settings = SettingsStore(path).settings
    assert settings.fps == 60 and settings.resolution == '1440p' and settings.pin.isdigit()


def test_settings_reject_unhashable_values_without_crashing(tmp_path):
    """Review finding: a list/dict value raised TypeError (startup crash / HTTP 500)."""
    for bad in ({'resolution': ['1080p']}, {'video_mode': {'x': 1}}, {'fps': [60]}, {'airplay_ports': None}):
        with pytest.raises(ValueError):
            validate_update(bad)
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'resolution': ['1080p'], 'av_sync': {'a': 1}}), encoding='utf-8')
    settings = SettingsStore(path).settings
    assert settings.resolution == '1080p' and settings.av_sync == 'low_latency'


async def test_env_pinned_access_code_cannot_be_rotated(make_client, monkeypatch):
    """Review finding: a UI rotation silently reverted to MIRRORDESK_ACCESS_CODE on restart."""
    monkeypatch.setenv('MIRRORDESK_ACCESS_CODE', 'PINNED42')
    server, client = await make_client()
    access = await (await client.get('/api/access')).json()
    assert access['access_code'] == 'PINNED42' and access['pinned'] is True
    response = await client.post('/api/regenerate/access-code', headers=W)
    assert response.status == 409 and 'MIRRORDESK_ACCESS_CODE' in await response.text()


async def test_container_does_not_trust_localhost(make_client, monkeypatch):
    """Review finding: loopback forwarders (userspace Tailscale, nginx, socat) relay
    remote users as 127.0.0.1 without proxy headers."""
    monkeypatch.setenv('MIRRORDESK_TRUST_LOCALHOST', '0')
    server, client = await make_client()
    assert (await client.get('/api/status')).status == 401
    assert (await client.get('/api/access')).status == 401
    code = server.store.settings.access_code
    assert (await client.post('/api/login', json={'code': code}, headers=W)).status == 200
    assert (await client.get('/api/status')).status == 200


async def test_stop_during_pending_restart_wins(make_client, monkeypatch):
    """Review finding: Stop clicked while a restart was pending got undone."""
    monkeypatch.setenv('FAKE_UXPLAY_NO_MEDIA', '1')
    server, client = await make_client(start_receiver=True, command=FAKE)
    for _ in range(100):
        if server.receiver.state == 'running':
            break
        await asyncio.sleep(0.05)
    restart = asyncio.create_task(server.receiver.restart())
    await asyncio.sleep(0)        # restart's stop() now holds the lock
    await server.receiver.stop()  # the user's Stop queues behind it
    await restart
    assert server.receiver.state == 'stopped' and not server.receiver.want_running
    assert server.receiver.process.poll() is not None


async def test_static_screen_reports_paused_not_idle_and_stop_clears_it(make_client, monkeypatch):
    """Review finding: a still phone screen (no RTP for >3 s) flipped the UI to the
    pairing overlay; but after Stop no 'paused' phone may linger either."""
    from mirrordesk import media
    server, client = await make_client()
    hub, rx = server.hub, server.receiver
    hub.last_video = media.time.monotonic() - 10
    rx.state = 'running'
    assert hub.source_state() == 'paused' and rx.phone_state() == 'paused'
    rx.state = 'stopped'
    assert rx.phone_state() == 'idle'
    hub.last_video = media.time.monotonic() - media.SOURCE_PAUSED_SECONDS - 1
    assert hub.source_state() == 'idle'


def test_airplay_code_is_new_every_time_mirrordesk_starts(tmp_path, monkeypatch):
    """A code someone saw once is useless after the next start (tester request)."""
    from mirrordesk import settings as settings_module
    codes = iter(['1111', '2222', '3333', '4444'])
    monkeypatch.setattr(settings_module, 'new_pin', lambda: next(codes))
    pins = []
    for _ in range(2):
        video_port, audio_port = free_udp_ports()
        server = MirrorDeskServer(tmp_path / 'data', port=8765, video_port=video_port, audio_port=audio_port,
                                  start_receiver=False, usb=False, android=False)
        pins.append(server.store.settings.pin)
    assert pins == ['2222', '3333']
    assert SettingsStore(tmp_path / 'data' / 'settings.json').settings.pin == '3333'


async def test_qr_code_for_the_invitation_is_a_standalone_svg(make_client):
    server, client = await make_client()
    response = await client.post('/api/qr', json={'text': 'http://100.64.0.1:8765/login?code=ABCDEF'}, headers=W)
    assert response.status == 200
    assert (await response.json())['svg'].startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    for bad in ({'text': 'javascript:alert(1)'}, {'text': 'http://' + 'x' * 600}, {'text': 5}, {}):
        assert (await client.post('/api/qr', json=bad, headers=W)).status == 400, bad


async def test_owner_can_disconnect_the_phone(make_client):
    from mirrordesk import media
    server, client = await make_client()
    assert (await client.post('/api/phone/disconnect', headers=W)).status == 409, 'no phone to disconnect'
    kicked = []

    async def disconnect_phone():
        kicked.append(True)
    server.receiver.disconnect_phone = disconnect_phone
    server.hub.last_video = media.time.monotonic()
    response = await client.post('/api/phone/disconnect', headers=W)
    assert response.status == 200 and await response.json() == {'disconnected': 'airplay'} and kicked


async def test_phone_tapping_stop_mirroring_ends_the_session_at_once(make_client):
    """The page showed the frozen last frame for up to a minute after Stop Mirroring."""
    from mirrordesk import media
    server, client = await make_client()
    hub, rx = server.hub, server.receiver
    rx.state = 'running'
    hub.last_video = media.time.monotonic()
    rx.on_line('connection request from Test iPhone (iPhone15,2) with deviceID = 00:11:22:33:44:55')
    assert rx.phone_state() == 'mirroring'
    rx.on_line('raop_rtp_mirror->running is no longer true')
    assert hub.source_state() == 'idle' and rx.phone_state() == 'idle' and hub.sessions_ended == 1
    status = await (await client.get('/api/status')).json()
    assert status['source']['state'] == 'idle' and status['receiver']['phone'] == 'idle'
    hub.last_video = media.time.monotonic()
    rx.on_line('*** ERROR lost connection with client (network problem?)')
    assert hub.source_state() == 'idle' and hub.sessions_ended == 2


@pytest.mark.parametrize('path', ['/api/regenerate/pin', '/api/receiver/forget'])
async def test_a_new_code_ends_the_session_and_forgets_every_phone(make_client, monkeypatch, path):
    """The user (2026-10-20): a phone has to enter a new code every session; when a session ends, a new
    code is made and every phone is forgotten. A new code ends the session at once: a new receiver key
    and an empty register, so also a phone trusted a moment ago has to enter it."""
    from mirrordesk import settings as settings_module
    server, client = await make_client()
    monkeypatch.setattr(settings_module, 'new_pin', lambda: '9876')
    files = [server.data_dir / 'uxplay-key.pem', server.data_dir / 'uxplay-register.txt']
    for item in files:
        item.write_text('from the last session', encoding='ascii')
    response = await client.post(path, headers=W)
    assert response.status == 200 and not any(item.exists() for item in files)
    assert server.store.settings.pin == '9876' and (await response.json())['pin'] == '9876'


async def test_disconnecting_the_phone_keeps_the_session(make_client, monkeypatch):
    """The user (2026-10-21): "make sure code changes every session, not every time someone disconnects
    during the same session". Disconnect stops that phone's mirroring; code and paired phones stay."""
    from mirrordesk import settings as settings_module
    monkeypatch.setenv('FAKE_UXPLAY_NO_MEDIA', '1')
    server, client = await make_client(start_receiver=True, command=FAKE)
    rx = server.receiver
    for _ in range(100):
        if rx.state == 'running':
            break
        await asyncio.sleep(0.05)
    pin, first = server.store.settings.pin, rx.process
    monkeypatch.setattr(settings_module, 'new_pin', lambda: '5555')
    register = server.data_dir / 'uxplay-register.txt'
    register.write_text('this phone', encoding='ascii')
    await rx.disconnect_phone()
    assert rx.process is not first, 'UxPlay cannot drop one phone: it restarts'
    assert server.store.settings.pin == pin and register.exists(), 'the same session: no new code'


async def test_renaming_restarts_the_receiver_at_once_in_the_same_session(make_client, monkeypatch):
    """The name shown on devices changes when it is saved; the code and paired phones stay."""
    monkeypatch.setenv('FAKE_UXPLAY_NO_MEDIA', '1')
    server, client = await make_client(start_receiver=True, command=FAKE)
    rx = server.receiver
    for _ in range(100):
        if rx.state == 'running':
            break
        await asyncio.sleep(0.05)
    pin, first = server.store.settings.pin, rx.process
    register = server.data_dir / 'uxplay-register.txt'
    register.write_text('this phone', encoding='ascii')
    response = await client.put('/api/settings', json={'receiver_name': 'Living Room'}, headers=W)
    assert (await response.json())['receiver_restarting'] is True
    for _ in range(100):
        if rx.process is not first and rx.state == 'running':
            break
        await asyncio.sleep(0.05)
    assert rx.process is not first and rx.process.args[rx.process.args.index('-n') + 1] == 'Living Room'
    assert server.store.settings.pin == pin and register.exists()


async def test_session_ends_after_hours_without_a_phone_with_a_new_code(make_client, monkeypatch):
    """Stop Mirroring on the phone: the session goes on (it may come back without the code) until no
    phone has been connected for SESSION_IDLE_SECONDS; then the receiver starts again with a new
    code, a new key and an empty register."""
    from mirrordesk import receiver
    monkeypatch.setattr(receiver, 'SESSION_IDLE_SECONDS', 0.8)
    monkeypatch.setattr(receiver, 'SESSION_CHECK_SECONDS', 0.1)
    monkeypatch.setenv('FAKE_UXPLAY_NO_MEDIA', '1')
    monkeypatch.setenv('FAKE_UXPLAY_CONNECT_DELAY', '0.3')
    monkeypatch.setenv('FAKE_UXPLAY_STOP_AFTER', '0.6')
    server, client = await make_client(start_receiver=True, command=FAKE)
    rx = server.receiver
    for _ in range(100):
        if rx.state == 'running' and rx.session_used:
            break
        await asyncio.sleep(0.05)
    first_pin, first_process = server.store.settings.pin, rx.process
    args = first_process.args
    register = server.data_dir / 'uxplay-register.txt'
    assert args[args.index('-reg') + 1] == str(register), 'only phones paired in this session skip the code'
    register.write_text('this phone', encoding='ascii')          # UxPlay registers the phone that paired
    await asyncio.sleep(0.6)                                      # the phone stopped; the session goes on...
    assert server.store.settings.pin == first_pin and register.exists(), 'a dropped phone may come back'
    for _ in range(60):
        if server.store.settings.pin != first_pin:
            break
        await asyncio.sleep(0.05)
    assert server.store.settings.pin != first_pin and not register.exists(), '...until it was idle long enough'
    for _ in range(100):
        if rx.process is not first_process and rx.state == 'running':
            break
        await asyncio.sleep(0.05)
    assert rx.process is not first_process and rx.state == 'running'
    assert rx.process.args[rx.process.args.index('-pin') + 1] == server.store.settings.pin


async def test_quit_command_works_when_this_computer_must_sign_in(tmp_path):
    """The app menu's "Quit MirrorDesk" (and start-mirrordesk.sh stop) prove themselves with a token
    from the private data folder, so they also work with "Ask for the owner code on this computer"."""
    class FakeDesktop:
        quits = 0

        def quit(self, delay=0.0):
            FakeDesktop.quits += 1

        def status(self):
            return {'quit': True, 'autostart': False, 'background': True, 'os': 'linux'}

    video_port, audio_port = free_udp_ports()
    server = MirrorDeskServer(tmp_path / 'data', port=8765, video_port=video_port, audio_port=audio_port,
                              start_receiver=False, desktop=FakeDesktop(), usb=False, android=False)
    server.store.update({'require_code_on_localhost': True})
    token = (tmp_path / 'data' / 'quit-token').read_text(encoding='ascii')
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    try:
        assert (await client.post('/api/desktop/quit', headers=W)).status == 401
        assert (await client.post('/api/desktop/quit', headers={**W, 'X-MirrorDesk-Quit': 'wrong'})).status == 401
        assert (await client.post('/api/desktop/quit', headers={**REMOTE_W, 'X-MirrorDesk-Quit': token})).status == 401, \
            'only on this computer'
        assert (await client.post('/api/desktop/status', headers={**W, 'X-MirrorDesk-Quit': token})).status in (401, 404, 405)
        assert (await client.post('/api/desktop/quit', headers={**W, 'X-MirrorDesk-Quit': token})).status == 200
        await asyncio.sleep(0.5)
        assert FakeDesktop.quits == 1
    finally:
        await client.close()
    assert not (tmp_path / 'data' / 'quit-token').exists(), 'removed when MirrorDesk stops'


def test_new_diagnostics_fields_are_allowlisted():
    sys.path.insert(0, str(ROOT))
    import diagnostics
    fields = diagnostics.safe_fields({'source_warm': True, 'source_replayable': False, 'source_audio_missed_ms': 40,
                                      'source_sessions_ended': 2, 'signal': 'mirror_stop', 'usb_state': 'held',
                                      'android_state': 'held', 'setting': 'paired_phones'})
    assert len(fields) == 8, fields
    audio = {'source_audio_jitter_ms': 99, 'source_audio_backlog_ms': 140, 'source_audio_trimmed_ms': 20,
             'source_audio_held_ms': 40, 'source_audio_flushes': 3, 'source_audio_flushed_ms': 120}
    assert diagnostics.safe_fields(audio) == audio
    assert diagnostics.safe_fields({'signal': 'new_session'}) == {'signal': 'new_session'}
    assert 'phone_disconnect' in diagnostics.EVENTS


def test_port_parsing():
    assert parse_ports('') == [] and parse_ports('7100') == [7100, 7101, 7102]
    assert parse_ports('7100,7000,7001') == [7100, 7000, 7001]
    assert parse_ports('80') is None and parse_ports('1,2') is None and parse_ports('7000,7000,7001') is None
    with pytest.raises(ValueError):
        validate_update({'airplay_ports': 'abc'})


async def test_diagnostics_export_has_p7_samples_and_no_secrets(make_client):
    server, client = await make_client()
    await client.post('/api/diagnostics/mark', json={'issue': 'delay'}, headers=W)
    server._record('hub_sample', source_state='idle', source_width=498, source_profile='high', viewer_count=0)
    response = await client.post('/api/diagnostics/export', headers=W)
    assert response.status == 200
    archive = zipfile.ZipFile(io.BytesIO(await response.read()))
    text = b''.join(archive.read(name) for name in archive.namelist()).decode('utf-8', 'replace')
    assert '"hub_sample"' in text and '"delay"' in text
    for secret in (server.store.settings.pin, server.store.settings.access_code):
        assert f'"{secret}"' not in text


async def test_viewer_offer_rejects_garbage(make_client):
    server, client = await make_client()
    assert (await client.post('/api/viewers', json={'type': 'offer', 'sdp': 'nope'}, headers=W)).status == 400
    assert (await client.post('/api/viewers', data='x' * 10, headers={**W, 'Content-Type': 'application/json'})).status == 400


async def test_no_viewer_while_nothing_is_mirrored(make_client):
    """A page left open from an older version joined at once and held a viewer place ("1 of 4")
    with no phone; pages join only while a phone mirrors, and the server holds them to it."""
    server, client = await make_client()
    offer = {'type': 'offer', 'sdp': 'v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n'}
    response = await client.post('/api/viewers', json=offer, headers=W)
    assert response.status == 409 and 'Nothing is being mirrored' in await response.text()
    assert len(server.viewers.viewers) == 0


async def test_page_names_its_build_and_assets_are_revalidated(make_client):
    """A browser kept 0.10.0's app.js and app.css after the update (they had no cache rule) and
    ran them with 0.11.0's page: only the Mirror tab, a player bar, nothing worked."""
    server, client = await make_client()
    html = await (await client.get('/')).text()
    tag = server.build
    assert tag.startswith(__import__('mirrordesk').__version__ + '-')
    assert f'<meta name="mirrordesk-build" content="{tag}">' in html
    for asset in ('/app/app.js', '/app/app.css', '/app/theme.js'):
        assert f'"{asset}?v={tag}"' in html
    login = await (await client.get('/login', headers=REMOTE)).text()
    assert f'"/app/login.js?v={tag}"' in login
    script = await client.get(f'/app/app.js?v={tag}')
    assert script.status == 200 and script.headers['Cache-Control'] == 'no-cache'
    assert (await (await client.get('/api/status')).json())['build'] == tag


def test_build_tag_follows_the_page_files(tmp_path):
    from mirrordesk.server import build_tag
    (tmp_path / 'app.js').write_text('one', encoding='utf-8')
    first = build_tag(tmp_path)
    (tmp_path / 'app.js').write_text('two', encoding='utf-8')
    assert build_tag(tmp_path) != first


async def test_loop_monitor_reports_a_stalled_loop():
    from mirrordesk.server import LoopMonitor
    monitor = LoopMonitor()
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.1)
    __import__('time').sleep(0.12)          # the event loop is blocked, as by a busy computer
    await asyncio.sleep(0.05)
    task.cancel()
    sample = monitor.take()
    assert sample['lag_max_ms'] >= 80 and sample['stalls'] >= 1
    assert monitor.take() == {'lag_max_ms': 0, 'stalls': 0}


async def test_network_endpoint_lists_urls(make_client):
    server, client = await make_client()
    data = await (await client.get('/api/network')).json()
    assert data['port'] == 8765 and isinstance(data['interfaces'], list)
    assert any(u['kind'] == 'mdns' for u in data['urls'])


async def test_receiver_lifecycle_with_fake_uxplay(make_client, monkeypatch):
    monkeypatch.setenv('FAKE_UXPLAY_NO_MEDIA', '1')
    monkeypatch.setenv('FAKE_UXPLAY_CONNECT_DELAY', '0.3')
    server, client = await make_client(start_receiver=True, command=FAKE)
    for _ in range(100):
        status = (await (await client.get('/api/status')).json())['receiver']
        if status['state'] == 'running' and status['phone'] == 'connecting':
            break
        await asyncio.sleep(0.1)
    assert status['state'] == 'running' and status['version'] == '1.74'
    assert status['phone'] == 'connecting' and status['phone_name'] == 'Test iPhone'
    lines = (await (await client.get('/api/receiver/log')).json())['lines']
    assert any('connection request' in item['line'] for item in lines)
    args = server.receiver.process.args
    assert args[args.index('-s') + 1] == '1920x1080@60' and args[args.index('-fps') + 1] == '60'
    assert args[args.index('-pin') + 1] == server.store.settings.pin and '-nohold' in args
    assert f'port={server.hub.video_port}' in args[args.index('-vrtp') + 1]
    stop = await client.post('/api/receiver/stop', headers=W)
    assert (await stop.json())['state'] == 'stopped'
    assert server.receiver.process.poll() is not None


async def test_receiver_crash_is_restarted_then_gives_up(make_client, monkeypatch):
    from mirrordesk import receiver
    monkeypatch.setattr(receiver, 'RESTART_BACKOFF', (0.1,))
    monkeypatch.setattr(receiver, 'FAILURE_LIMIT', 3)
    monkeypatch.setenv('FAKE_UXPLAY_EXIT_AFTER', '0.2')
    monkeypatch.setenv('FAKE_UXPLAY_NO_MEDIA', '1')
    server, client = await make_client(start_receiver=True, command=FAKE)
    server.receiver.failures = receiver.deque(maxlen=3)
    states = set()
    for _ in range(150):
        status = server.receiver.status()
        states.add(status['state'])
        if status['state'] == 'failed':
            break
        await asyncio.sleep(0.1)
    assert 'restarting' in states and status['state'] == 'failed'
    assert server.receiver.restarts >= 2


async def test_missing_uxplay_reports_unavailable(make_client, tmp_path, monkeypatch):
    monkeypatch.setenv('MIRRORDESK_UXPLAY', str(tmp_path / 'does-not-exist.exe'))
    monkeypatch.delenv('MIRRORDESK_UXPLAY_CMD', raising=False)
    server, client = await make_client(start_receiver=True, command=None)
    server.receiver.command_override = None
    await server.receiver.start()
    assert server.receiver.state == 'unavailable'
