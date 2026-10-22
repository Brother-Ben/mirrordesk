"""Android input: scrcpy 5.0.1 wire format, adb handling, sessions, Wi-Fi pairing, control.

The phone is tests/fake_adb.py (adb 37.0.1 output formats + the phone side of the
scrcpy protocol with synthetic H.264 and a tone). Real-phone evidence is pending.
"""
import asyncio
import json
import socket
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mirrordesk import androidsource as A
from mirrordesk import androidtools
from mirrordesk.media import LiveAudioTrack, MediaHub
from mirrordesk.settings import Settings, validate_update

FAKE_ADB = [sys.executable, str(Path(__file__).resolve().parent / 'fake_adb.py')]


def free_udp_ports():
    ports = []
    for _ in range(2):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(('127.0.0.1', 0))
            ports.append(s.getsockname()[1])
    return ports


# ------------------------------------------------------------------ wire format
def test_headers_follow_the_scrcpy_demuxer():
    assert A.parse_header(struct.pack('>III', 0x80000000, 1080, 2400)) == ('session', 1080, 2400)
    assert A.parse_header(struct.pack('>III', 0x80000001, 800, 600)) == ('session', 800, 600)  # client-resize bit
    assert A.parse_header(struct.pack('>QI', 1 << 62, 30)) == ('media', 0, 30, True, False)
    assert A.parse_header(struct.pack('>QI', (1 << 61) | 123456, 9000)) == ('media', 123456, 9000, False, True)


def test_control_messages_match_the_phone_side_reader():
    # ControlMessageReader.parseInjectTouchEvent: u8 type, u8 action, i64 pointer, i32 x, i32 y,
    # u16 w, u16 h, u16 pressure, i32 action button, i32 buttons.
    touch = A.msg_touch(A.ACTION_DOWN, -2, 119, 263, 240, 528, 1.0)
    assert len(touch) == 32
    assert struct.unpack('>BBqiiHHHii', touch) == (2, 0, -2, 119, 263, 240, 528, 0xFFFF, 0, 0)
    assert struct.unpack('>BBqiiHHHii', A.msg_touch(A.ACTION_UP, 3, 0, 0, 10, 10, 0.5))[7] == 0x8000
    assert A.msg_reset_video() == b'\x11'
    assert struct.unpack('>BBiii', A.msg_keycode(1, 3)) == (0, 1, 3, 0, 0)
    assert A.msg_back_or_screen_on(0) == b'\x04\x00'
    scroll = A.msg_scroll(5, 6, 100, 200, 0, -1)
    assert len(scroll) == 21 and struct.unpack('>BiiHHhhi', scroll)[5:7] == (0, -2048)  # -1/16 in i16 fixed point
    text = A.msg_text('hé' * 200)
    assert text[0] == 1 and struct.unpack('>I', text[1:5])[0] == len(text) - 5 <= A.INJECT_TEXT_MAX
    text[5:].decode('utf-8')   # never cut inside a character


def test_server_arguments():
    settings = Settings(pin='1234', access_code='ABCDEF')
    args = A.server_args(0x1234ABCD, settings)
    assert args[0] == androidtools.SCRCPY_VERSION == '5.0.1'
    for expected in ('scid=1234abcd', 'video_codec=h264', 'audio_codec=raw', 'audio=true', 'tunnel_forward=true',
                     'control=true', 'clipboard_autosync=false', 'max_size=1920', 'max_fps=60',
                     'video_bit_rate=8000000', 'video_codec_options=i-frame-interval=2'):
        assert expected in args
    settings.android_max_size, settings.audio_enabled = 0, False
    args = A.server_args(1, settings)
    assert not any(a.startswith('max_size') for a in args) and 'audio=false' in args
    settings.audio_enabled, settings.android_audio_on_phone = True, True
    assert {'audio_source=playback', 'audio_dup=true'} <= set(A.server_args(1, settings))


def test_adb_output_parsing():
    devices = A.parse_devices(
        'List of devices attached\n'
        'R58M123ABC             device usb:1-1 product:beyond1ltexx model:SM_G973F device:beyond1 transport_id:1\n'
        '192.168.1.5:41235      device product:oriole model:Pixel_6 device:oriole transport_id:3\n'
        'adb-29011FDH-xyz._adb-tls-connect._tcp device product:oriole model:Pixel_6 transport_id:4\n'
        'emulator-5554 unauthorized usb:2-1 transport_id:5\n'
        'ABCD   no permissions (missing udev rules? user is in the plugdev group); see [http://x] usb:1-2 transport_id:6\n')
    assert [(d.serial, d.state, d.transport) for d in devices] == [
        ('R58M123ABC', 'device', 'usb'), ('192.168.1.5:41235', 'device', 'wifi'),
        ('adb-29011FDH-xyz._adb-tls-connect._tcp', 'device', 'wifi'), ('emulator-5554', 'unauthorized', 'usb'),
        ('ABCD', 'no permissions', 'usb')]
    assert devices[0].label == 'SM G973F'
    assert A.parse_mdns('List of discovered mdns services\nmirrordesk-ab12\t_adb-tls-pairing._tcp\t192.168.1.5:37011\n') == [
        ('mirrordesk-ab12', '_adb-tls-pairing._tcp', '192.168.1.5:37011')]


def test_settings_validation():
    assert validate_update({'android_max_size': 1280, 'android_bit_rate': 16, 'android_control': 'all',
                            'android_fps': 30, 'android_enabled': False, 'android_audio_on_phone': True})
    for bad in ({'android_max_size': 1000}, {'android_bit_rate': 9}, {'android_control': 'everyone'},
                {'android_fps': 45}, {'android_enabled': 'yes'}):
        with pytest.raises(ValueError):
            validate_update(bad)


def test_bundled_scrcpy_server_is_verified(tmp_path):
    assert androidtools.scrcpy_server().name == 'scrcpy-server-v5.0.1'
    altered = tmp_path / 'scrcpy-server'
    altered.write_bytes(androidtools.SCRCPY_SERVER.read_bytes() + b'x')
    with pytest.raises(androidtools.ToolsError, match='damaged'):
        androidtools.scrcpy_server(altered)


def test_tools_install_verifies_the_download(tmp_path, monkeypatch):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('platform-tools/' + androidtools.adb_name(), b'fake adb')
        archive.writestr('platform-tools/../evil.txt', b'no')
        archive.writestr('other/readme.txt', b'no')
    data = buffer.getvalue()
    with pytest.raises(androidtools.ToolsError, match='checksum'):
        androidtools.install(tmp_path, fetch=lambda url, size, progress: data)
    assert not (tmp_path / 'platform-tools').exists(), 'nothing unpacked from an unverified download'
    import hashlib
    name, size, _ = androidtools.PLATFORM_TOOLS[androidtools.host_os()]
    monkeypatch.setitem(androidtools.PLATFORM_TOOLS, androidtools.host_os(), (name, size, hashlib.sha256(data).hexdigest()))
    path = androidtools.install(tmp_path, fetch=lambda url, size, progress: data)
    assert Path(path).read_bytes() == b'fake adb'
    assert not (tmp_path / 'evil.txt').exists() and not (tmp_path / 'other').exists()


# ------------------------------------------------------------------ sessions with the fake phone
def store(**changes):
    settings = Settings(pin='1234', access_code='ABCDEF')
    for key, value in changes.items():
        setattr(settings, key, value)
    return SimpleNamespace(settings=settings)


@pytest.fixture
def phone(tmp_path, monkeypatch):
    state = tmp_path / 'adb'
    monkeypatch.setenv('FAKE_ADB_STATE', str(state))
    monkeypatch.setenv('FAKE_SCRCPY_SIZE', '240x528')
    monkeypatch.setattr(A, 'POLL_SECONDS', 0.3)
    monkeypatch.setattr(A, 'RETRY_SECONDS', 0.5)

    def set_devices(devices):
        state.mkdir(parents=True, exist_ok=True)
        (state / 'devices.json').write_text(json.dumps(devices), encoding='utf-8')
    return SimpleNamespace(state=state, set_devices=set_devices)


@pytest.fixture
async def hub():
    video_port, audio_port = free_udp_ports()
    instance = MediaHub(video_port, audio_port)
    await instance.start()
    yield instance
    await instance.stop()


async def wait_for(predicate, timeout=20.0, step=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


async def test_phone_on_usb_streams_video_and_sound_into_the_hub(phone, hub):
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    track = LiveAudioTrack(hub)
    track.started = True
    await source.start()
    try:
        assert await wait_for(lambda: hub.video_units > 30 and hub.audio_packets > 30)
        status = source.status()
        assert status['state'] == 'streaming' and hub.input == 'android'
        assert status['device']['name'] == 'Pixel 8' and status['device']['transport'] == 'usb'
        assert status['device']['android'] == '14' and (status['device']['width'], status['device']['height']) == (240, 528)
        assert status['audio']['state'] == 'on'
        assert (hub.sps_info.width, hub.sps_info.height) == (240, 528)
        assert hub.gop_valid and hub.gop[0].keyframe and hub.gop[0].nals[0][0] & 0x1F == 7, 'config merged into the IDR'
        frame = await asyncio.wait_for(track.recv(), 5)
        assert frame.sample_rate == 48000 and frame.samples == 960
        # Viewers that need a picture ask the phone for a keyframe (scrcpy RESET_VIDEO).
        hub.last_source_keyframe_request = -1e9
        source.session.last_keyframe_request = -1e9
        assert hub.request_source_keyframe() is True
        await asyncio.sleep(0.5)
        assert '11' in (phone.state / 'control.log').read_text().split()
    finally:
        track.stop()
        await source.stop()
    assert hub.input == 'airplay' and source.status()['state'] == 'off'
    forwards = json.loads((phone.state / 'forwards.json').read_text())
    assert not any(forwards.values()), 'the adb port forward is removed on stop'


async def test_touch_keys_and_text_reach_the_phone_only_when_allowed(phone, hub):
    source = A.AndroidSource(store(android_control='local'), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming' and source.session and source.session.width)
        assert source.control_allowed(local=True) and not source.control_allowed(local=False)
        assert source.control({'t': 'touch', 'a': 'down', 'x': 0.5, 'y': 0.5, 'id': -2})
        assert source.control({'t': 'touch', 'a': 'up', 'x': 0.5, 'y': 0.5, 'id': -2})
        assert source.control({'t': 'key', 'k': 'home'}) and source.control({'t': 'key', 'k': 'back'})
        assert source.control({'t': 'text', 's': 'hello'})
        assert not source.control({'t': 'key', 'k': 'format_phone'}) and not source.control({'t': 'nope'})
        await asyncio.sleep(0.5)
        log = ''.join((phone.state / 'control.log').read_text().split())
        assert A.msg_touch(A.ACTION_DOWN, -2, 119, 263, 240, 528, 1.0).hex() in log
        assert (A.msg_keycode(0, 3) + A.msg_keycode(1, 3)).hex() in log
        assert A.msg_text('hello').hex() in log
        source.store.settings.android_control = 'off'
        assert not source.control_allowed(local=True)
    finally:
        await source.stop()


async def test_blocked_control_is_explained_while_mirroring_continues(phone, hub):
    phone.state.mkdir(parents=True, exist_ok=True)
    (phone.state / 'deny-inject').write_text('1')
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming' and source.session and source.session.width)
        assert source.status()['control_note'] is None
        source.control({'t': 'touch', 'a': 'down', 'x': 0.5, 'y': 0.5})
        assert await wait_for(lambda: source.status()['control_note'] is not None)
        assert 'USB debugging (Security settings)' in source.status()['control_note']
        assert source.state == 'streaming', 'mirroring is not interrupted'
    finally:
        await source.stop()


async def test_diagnostics_say_why_a_session_failed_without_private_data(phone, hub, tmp_path):
    """A tester's exported diagnostics must explain Android problems (categories and numbers),
    never the phone's serial, name or addresses."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import diagnostics
    session_log = diagnostics.DiagnosticSession.create(tmp_path / 'diag')
    phone.state.mkdir(parents=True, exist_ok=True)
    (phone.state / 'fail-encoder').write_text('1')
    source = A.AndroidSource(store(), hub, diagnostics=session_log, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'error')
        (phone.state / 'fail-encoder').unlink()
        assert await wait_for(lambda: source.state == 'streaming' and hub.video_units > 10)
        await asyncio.sleep(1.0)
        phone.set_devices([])
        assert await wait_for(lambda: source.state == 'waiting')
    finally:
        await source.stop()
    lines = [json.loads(line) for line in (session_log.directory / 'backend.jsonl').read_text().splitlines()]
    events = [e['event'] for e in lines]
    start = next(e for e in lines if e['event'] == 'android_session_start')
    assert (start['android_sdk'], start['android_transport'], start['android_brand']) == (34, 'usb', 'google')
    assert any(e['event'] == 'android_state' and e.get('android_reason') == 'encoder' for e in lines)
    ends = [e for e in lines if e['event'] == 'android_session_end']
    assert ends[0]['android_result'] == 'encoder' and ends[-1]['android_result'] == 'unplugged'
    assert ends[-1]['android_frames'] > 10 and ends[-1]['android_audio_packets'] > 0
    assert (ends[-1]['android_width'], ends[-1]['android_height']) == (240, 528)
    assert 'android_audio' in events
    text = (session_log.directory / 'backend.jsonl').read_text()
    assert 'FAKEPIXEL01' not in text and 'Pixel' not in text, 'no serial or phone name in diagnostics'
    # The categories in the code and the diagnostics allowlist must stay in sync.
    assert A.REASON_NAMES == diagnostics.ENUMS['android_reason']
    assert set(A.BRANDS) | {'other', 'unknown'} == diagnostics.ENUMS['android_brand']
    assert set(A.PAIRING_STATES) == diagnostics.ENUMS['android_pairing_state']


async def test_unplugging_returns_to_waiting_and_frees_the_screen(phone, hub):
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming')
        phone.set_devices([])
        assert await wait_for(lambda: source.state == 'waiting' and source.session is None)
        assert hub.input == 'airplay' and source.status()['device'] is None
    finally:
        await source.stop()


async def test_disconnecting_from_the_page_holds_the_phone_until_it_is_replugged(phone, hub):
    """The owner's Disconnect ends mirroring, and it does not start again by itself while the phone
    stays plugged in; unplugging it (or Reconnect) lets it mirror again."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fake_adb import DEFAULT_DEVICES
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming')
        assert await source.disconnect_phone() is True
        assert source.state == 'held' and source.status()['held'] and hub.input == 'airplay'
        await asyncio.sleep(1.2)                       # several adb polls
        assert source.session is None and source.state == 'held', 'not mirrored again by itself'
        phone.set_devices([])                          # unplugged
        assert await wait_for(lambda: source.state == 'waiting' and not source.status()['held'])
        phone.set_devices(DEFAULT_DEVICES)             # plugged in again
        assert await wait_for(lambda: source.state == 'streaming')
        assert await source.disconnect_phone() is True
        await source.restart()                         # Reconnect on the Phones page
        assert await wait_for(lambda: source.state == 'streaming')
        assert await source.disconnect_phone() is True
        assert await source.disconnect_phone() is False, 'nothing left to disconnect'
    finally:
        await source.stop()


def test_pairing_qr_code_is_an_image_browsers_can_show():
    """Regression (Brave, any browser): segno's svg_inline() has no SVG namespace, so the pairing
    QR code shown as an <img> was a broken image and could never be scanned."""
    from xml.dom import minidom
    svg = A.qr_svg('WIFI:T:ADB;S:mirrordesk-123456;P:abcdefABCDEF;;')
    root = minidom.parseString(svg).documentElement
    assert root.tagName == 'svg' and root.namespaceURI == 'http://www.w3.org/2000/svg'
    assert int(root.getAttribute('width')) >= 150


async def test_unauthorized_phone_asks_to_allow_usb_debugging(phone, hub):
    phone.set_devices([{'serial': 'NEWPHONE', 'state': 'unauthorized'}])
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'unauthorized')
        assert 'Allow' in source.message
    finally:
        await source.stop()


async def test_phone_without_sound_capture_keeps_video(phone, hub, monkeypatch):
    monkeypatch.setenv('FAKE_SCRCPY_AUDIO', 'disabled')
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: hub.video_units > 10 and source.status()['audio']['state'] == 'unsupported')
        assert 'Android 11' in source.status()['audio']['message'] and source.state == 'streaming'
    finally:
        await source.stop()


async def test_encoder_failure_is_explained_and_retried(phone, hub):
    phone.state.mkdir(parents=True, exist_ok=True)
    (phone.state / 'fail-encoder').write_text('1')
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'error')
        assert 'lower size' in source.message and source.errors >= 1
        (phone.state / 'fail-encoder').unlink()
        assert await wait_for(lambda: source.state == 'streaming'), 'retried after the error delay'
    finally:
        await source.stop()


async def test_rotation_changes_the_stream_size(phone, hub, monkeypatch):
    monkeypatch.setenv('FAKE_SCRCPY_ROTATE_AFTER', '1.5')
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: hub.sps_info is not None and hub.sps_info.width == 528, timeout=25)
        assert (source.status()['device']['width'], source.status()['device']['height']) == (528, 240)
    finally:
        await source.stop()


async def test_iphone_on_usb_keeps_the_screen_until_it_stops(phone, hub):
    assert hub.claim('usb')
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming')
        await asyncio.sleep(0.5)
        assert hub.input == 'usb', 'a running cable input is not interrupted'
        hub.release('usb')
        assert await wait_for(lambda: hub.input == 'android' and hub.gop_valid)
    finally:
        await source.stop()


async def test_missing_adb_offers_the_download(hub, monkeypatch):
    monkeypatch.setattr(androidtools, 'installable', lambda: True)
    source = A.AndroidSource(store(), hub, adb_finder=lambda: None)
    await source.start()
    status = source.status()
    assert status['state'] == 'unavailable' and 'Install Android tools' in status['message']
    assert status['tools']['adb'] is False and status['tools']['installable'] is True


async def test_wifi_pairing_with_code_then_connect(phone, hub):
    phone.set_devices([])
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'waiting')
        with pytest.raises(A.AdbError, match='6 digits'):
            await source.pair_with_code('192.168.1.5', 37011, '12ab56')
        with pytest.raises(A.AdbError, match='IP address'):
            await source.pair_with_code('192.168.1.5; rm -rf /', 37011, '123456')
        with pytest.raises(A.AdbError, match='port'):
            await source.pair_with_code('192.168.1.5', 70000, '123456')
        result = await source.pair_with_code('192.168.1.5', 37011, '000000')
        assert result['state'] == 'failed'
        result = await source.pair_with_code('192.168.1.5', 37011, '123456')
        assert result['state'] == 'paired'
        await source.connect_wifi('192.168.1.5', 41235)
        assert await wait_for(lambda: source.state == 'streaming')
        assert source.status()['device']['transport'] == 'wifi'
        await source.disconnect_wifi('192.168.1.5:41235')
        assert await wait_for(lambda: source.state == 'waiting')
    finally:
        await source.stop()


async def test_qr_pairing_finds_the_phone_by_mdns(phone, hub, monkeypatch):
    phone.set_devices([])
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        result = await source.qr_start()
        assert result['qr_svg'].startswith('<svg') and source.pairing['state'] == 'waiting'
        name, password = source._qr
        assert password not in json.dumps(source.status()), 'the pairing password is never exposed'
        # The phone scanned the code and now announces the pairing service under our name;
        # after pairing it announces its connect service, which MirrorDesk dials.
        (phone.state / 'mdns.json').write_text(json.dumps([[name, '_adb-tls-pairing._tcp', '192.168.1.7:37099'],
                                                           ['adb-X', '_adb-tls-connect._tcp', '192.168.1.7:41000']]))
        (phone.state / 'pair-code.json').write_text(json.dumps(password))
        assert await wait_for(lambda: source.pairing['state'] in ('failed', 'connected'), timeout=30)
        assert source.pairing['state'] == 'connected', source.pairing
        assert await wait_for(lambda: source.state == 'streaming' and source.status()['device']['transport'] == 'wifi')
    finally:
        await source.stop()


async def test_qr_pairing_times_out_with_advice(phone, hub, monkeypatch):
    monkeypatch.setattr(A, 'PAIRING_SECONDS', 1.5)
    phone.set_devices([])
    source = A.AndroidSource(store(), hub, adb_finder=lambda: FAKE_ADB)
    await source.start()
    try:
        await source.qr_start()
        assert await wait_for(lambda: source.pairing['state'] == 'failed', timeout=10)
        assert 'same Wi-Fi' in source.pairing['message']
    finally:
        await source.stop()


# ------------------------------------------------------------------ server API
async def test_server_status_settings_and_control_socket(phone):
    from aiohttp.test_utils import TestClient, TestServer
    from mirrordesk.server import MirrorDeskServer
    data = phone.state.parent / 'data'
    video_port, audio_port = free_udp_ports()
    server = MirrorDeskServer(data, video_port=video_port, audio_port=audio_port, start_receiver=False, usb=False,
                              android=False)
    server.android = A.AndroidSource(server.store, server.hub, adb_finder=lambda: FAKE_ADB)
    android = server.android
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    try:
        assert await wait_for(lambda: android.state == 'streaming')
        status = await (await client.get('/api/status')).json()
        assert status['android']['state'] == 'streaming' and status['android']['control'] is True
        headers = {'X-MirrorDesk': '1'}
        settings = await (await client.get('/api/settings')).json()
        assert settings['choices']['android_control'] == ['off', 'local', 'all']
        response = await client.post('/api/android/key', json={'key': 'home'}, headers=headers)
        assert response.status == 200
        # WebSocket control: a page from another site is refused, the app's own page works.
        bad = await client.get('/api/android/control', headers={'Origin': 'http://evil.example', 'Upgrade': 'websocket',
                                                                'Connection': 'Upgrade', 'Sec-WebSocket-Version': '13',
                                                                'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='})
        assert bad.status == 403
        host = f'{client.host}:{client.port}'
        ws = await client.ws_connect('/api/android/control', headers={'Origin': f'http://{host}'})
        await ws.send_str(json.dumps({'t': 'key', 'k': 'recents'}))
        await ws.send_str('not json')
        await asyncio.sleep(0.5)
        await ws.close()
        log = ''.join((phone.state / 'control.log').read_text().split())
        assert (A.msg_keycode(0, 187) + A.msg_keycode(1, 187)).hex() in log
        put = await client.put('/api/settings', json={'android_control': 'off'}, headers=headers)
        assert put.status == 200
        assert (await client.post('/api/android/key', json={'key': 'home'}, headers=headers)).status == 403
        missing = await client.post('/api/android/select', json={'serial': 'nope'}, headers=headers)
        assert missing.status == 400
    finally:
        await client.close()
