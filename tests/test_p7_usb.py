"""USB cable input: the capture thread and watcher against a simulated phone.

The simulated phone behaves like the real iPhone 11 this was developed on
(2026-10-14): it changes USB mode only when its port is reset, a capture mode
left over from an earlier session can stay silent until the phone goes back to
normal mode, and newer iOS versions may wait for the computer's PING. Its
capture traffic replays packets captured from real iPhones, and its USB
configurations are the real iPhone's descriptors (tests/fixtures/iphone-usb/).
Pairing is stubbed here; tests/test_p7_lockdown.py covers it.
"""
import asyncio
from pathlib import Path
import struct
import threading
import time

import pytest

from mirrordesk import lockdown
from mirrordesk import quicktime as qt
from mirrordesk import usbsource
from mirrordesk.media import MediaHub
from mirrordesk.settings import SettingsStore

FIXTURES = Path(__file__).parent / 'fixtures'


def packet(name):
    data = (FIXTURES / 'quicktime' / name).read_bytes()
    if struct.unpack_from('<I', data)[0] in (qt.SYNC, qt.ASYN):
        return struct.pack('<I', len(data) + 4) + data  # one capture was saved without its length
    return data


def real_configurations(capture_mode):
    configs = {}
    for path in sorted((FIXTURES / 'iphone-usb').glob('config-*.bin')):
        value, interfaces = usbsource.parse_configuration(path.read_bytes())
        if value != 5 or capture_mode:
            configs[value] = interfaces
    return configs


NORMAL, CAPTURE = real_configurations(False), real_configurations(True)
CAPTURE_IN, CAPTURE_OUT = 0x86, 0x05


class FakeTimeout(Exception):
    pass


class FakeUnplugged(Exception):
    pass


class FakePhone:
    """Both the device pyusb finds and the opened handle."""
    idProduct = 0x12A8
    iSerialNumber = 3
    iProduct = 2

    def __init__(self, serial='00008030-TEST', feeds=6, ping_first=True, leftover=False, switches=True):
        self.serial = serial
        self.feeds = self.initial_feeds = feeds
        self.ping_first = ping_first
        self.capture_mode = leftover
        self.stuck = leftover        # a leftover capture mode stays silent until the phone goes back to normal
        self.switches = switches
        self.pending = None
        self.active = None
        self.queue = []
        self.lock = threading.Lock()
        self.controls, self.configs_set, self.sent, self.claimed, self.read_sizes = [], [], [], [], []
        self.resets = 0
        self.unplugged = False
        self.answered = False
        self.released = False

    def _new_session(self):
        """A port reset (or replugging) starts the phone's capture conversation afresh."""
        self.answered = self.released = False
        self.feeds = self.initial_feeds

    # -- handle API
    def configurations(self):
        if self.unplugged:
            raise FakeUnplugged()
        return CAPTURE if self.capture_mode else NORMAL

    def control(self, request_type, request, value, index, data=b'', timeout=2000):
        if self.unplugged:
            raise FakeUnplugged()
        self.controls.append((request_type, request, value, index))
        if (request_type, request) == (0x40, 0x52):
            self.pending = index
        return 0

    def reset(self):
        self.resets += 1
        if self.pending is not None and self.switches:
            self.capture_mode = self.pending == usbsource.MODE_CAPTURE
            if self.pending == usbsource.MODE_NORMAL:
                self.stuck = False
        self.pending = None
        self.active = None
        self._new_session()
        with self.lock:
            self.queue.clear()
        raise FakeUnplugged('libusb-win32 reports the reset as an error')

    def set_configuration(self, value):
        self.configs_set.append(value)
        self.active = value

    def claim(self, number):
        self.claimed.append(number)
        if number == 2 and self.active == 5 and self.ping_first and not self.stuck:
            self._queue(qt.ping_packet())

    def release(self, number):
        pass

    def close(self):
        pass

    def _queue(self, data):
        with self.lock:
            # Deliver large packets in pieces, like USB bulk reads do.
            self.queue.extend(data[i:i + 16384] for i in range(0, len(data), 16384))

    def read(self, endpoint, size, timeout):
        if self.unplugged:
            raise FakeUnplugged('device gone')
        self.read_sizes.append(size)
        with self.lock:
            chunk = self.queue.pop(0) if self.queue else None
            if chunk is not None and len(chunk) > size:  # a read never returns more than it asked for
                chunk, rest = chunk[:size], chunk[size:]
                self.queue.insert(0, rest)
        if chunk is None:
            time.sleep(0.005)
            raise FakeTimeout()
        return chunk

    def write(self, endpoint, data, timeout=2000):
        if self.unplugged:
            raise FakeUnplugged('device gone')
        data = bytes(data)
        kind = struct.unpack_from('<I', data, 4)[0]
        if self.stuck:
            raise FakeTimeout()  # the capture function refuses data
        subtype = struct.unpack_from('<I', data, 16)[0] if kind == qt.ASYN else None
        self.sent.append(subtype if kind == qt.ASYN else kind)
        if kind == qt.PING and not self.answered:
            self.answered = True
            for name in ('cwpa-request1', 'afmt-request', 'cvrp-request', 'clok-request', 'time-request1',
                         'asyn-feed', 'asyn-eat'):
                self._queue(packet(name))
        elif kind == qt.ASYN and subtype == qt.NEED and self.feeds > 0:
            self.feeds -= 1
            self._queue(packet('asyn-feed-nofdsc'))
            self._queue(packet('asyn-eat'))
        elif kind == qt.ASYN and subtype == qt.HPD0 and not self.released:
            self.released = True
            self.sent.append(qt.RELS)
            self._queue(packet('asyn-rels') * 2)
        return len(data)


class FakeBus:
    def __init__(self, *phones):
        self.phones = list(phones)

    def find(self, backend):
        return [p for p in self.phones if not p.unplugged]


@pytest.fixture
def bus(monkeypatch):
    fake = FakeBus()
    sessions = []

    def trust(handle, interface, store, serial, on_wait):
        assert interface.subclass == usbsource.MUX_SUBCLASS
        sessions.append(handle.active)
        return 'Test iPhone'
    fake.sessions = sessions
    monkeypatch.setattr(usbsource, 'find_phones', lambda backend: fake.find(backend))
    monkeypatch.setattr(usbsource, 'open_handle', lambda device: device)
    monkeypatch.setattr(usbsource, 'establish_trust', trust)
    monkeypatch.setattr(usbsource, '_string', lambda device, index: {3: device.serial, 2: 'iPhone'}.get(index))
    monkeypatch.setattr(usbsource, '_is_timeout', lambda exc: isinstance(exc, FakeTimeout))
    for name, value in (('POLL_SECONDS', 0.05), ('REATTACH_POLL', 0.02), ('MODE_DELAY', 0.0),
                        ('SETTLE_SECONDS', 0.0), ('READ_TIMEOUT_MS', 10), ('SILENCE_SECONDS', 0.6),
                        ('REATTACH_SECONDS', 1.0)):
        monkeypatch.setattr(usbsource, name, value)
    return fake


class AudioCollector:
    def __init__(self):
        self.chunks = []

    def push(self, chunk):
        self.chunks.append(chunk)


async def wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def make_source(tmp_path, hub, **kwargs):
    store = SettingsStore(tmp_path / 'settings.json')
    kwargs.setdefault('backend_loader', lambda: (object(), None))
    kwargs.setdefault('driver_check', lambda: [])
    return usbsource.UsbSource(store, hub, data_dir=tmp_path, **kwargs)


def test_real_iphone_descriptors_parse():
    assert usbsource.capture_configuration(NORMAL) is None
    assert usbsource.mux_configuration(NORMAL) == 4
    assert usbsource.capture_configuration(CAPTURE) == 5
    assert usbsource.mux_configuration(CAPTURE) == 4
    capture = usbsource.find_interface(CAPTURE[5], usbsource.QT_SUBCLASS)
    assert capture.number == 2 and capture.bulk() == (CAPTURE_IN, CAPTURE_OUT, 512)
    assert usbsource.find_interface(CAPTURE[5], usbsource.MUX_SUBCLASS).bulk()[:2] == (0x85, 0x04)


async def test_phone_on_cable_streams_into_the_hub(tmp_path, bus):
    phone = FakePhone()
    bus.phones.append(phone)
    hub = MediaHub(audio_rate=44100)
    audio = AudioCollector()
    hub.audio_subscribers.add(audio)
    source = make_source(tmp_path, hub)
    await source.start()
    try:
        assert await wait_for(lambda: hub.keyframes >= 1 and hub.video_units >= 4 and audio.chunks)
        assert source.state == 'streaming'
        assert hub.input == 'usb' and hub.status()['input'] == 'usb'
        assert (hub.sps_info.width, hub.sps_info.height) == (1126, 2436)
        assert source.status()['device'] == {'name': 'Test iPhone', 'width': 1126, 'height': 2436}
        assert phone.serial not in str(source.status())
        chunk = audio.chunks[-1]  # 48 kHz phone audio arrives at the hub's 44.1 kHz as interleaved s16
        assert chunk.samples.dtype.name == 'int16' and chunk.samples.shape[0] == 1
        assert audio.chunks[0].discontinuity
        # Trusted session in normal mode, capture request + port reset, trusted session again, capture.
        assert phone.controls[0] == (0x40, 0x52, 0, usbsource.MODE_CAPTURE) and phone.resets == 1
        assert bus.sessions == [4, 5] and phone.configs_set == [4, 5]
        assert not any(c[:2] == (0x02, 0x01) for c in phone.controls), 'no CLEAR_FEATURE: it can wedge the phone'
        assert all(size % 512 == 0 and size <= usbsource.READ_SIZE for size in phone.read_sizes)
    finally:
        await source.stop()
    assert hub.input == 'airplay' and source.state == 'off'
    # Handed back cleanly: told to stop streaming, then back to normal mode via a port reset.
    assert qt.HPA0 in phone.sent and qt.HPD0 in phone.sent
    assert phone.controls[-1] == (0x40, 0x52, 0, usbsource.MODE_NORMAL) and phone.resets == 2
    assert not phone.capture_mode


async def test_phone_that_waits_for_the_computer_to_ping(tmp_path, bus):
    phone = FakePhone(ping_first=False)
    bus.phones.append(phone)
    hub = MediaHub()
    source = make_source(tmp_path, hub)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming' and hub.video_units >= 2)
        assert (0x40, 0x40, 0x6400, 0x6400) in phone.controls
    finally:
        await source.stop()


async def test_leftover_capture_mode_is_restarted_cleanly(tmp_path, bus):
    phone = FakePhone(leftover=True)
    bus.phones.append(phone)
    hub = MediaHub()
    source = make_source(tmp_path, hub)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming' and hub.video_units >= 2)
        modes = [c[3] for c in phone.controls if c[:2] == (0x40, 0x52)]
        assert modes == [usbsource.MODE_NORMAL, usbsource.MODE_CAPTURE]
        assert source.errors == 0
    finally:
        await source.stop()


async def test_airplay_packets_are_ignored_while_usb_is_active(tmp_path, bus):
    bus.phones.append(FakePhone())
    hub = MediaHub()
    source = make_source(tmp_path, hub)
    await source.start()
    try:
        assert await wait_for(lambda: hub.input == 'usb')
        hub._on_video(b'\x80' + bytes(20), ('127.0.0.1', 1))
        assert hub.ignored_packets == 1
    finally:
        await source.stop()


async def test_unplugging_returns_to_waiting(tmp_path, bus):
    phone = FakePhone()
    bus.phones.append(phone)
    hub = MediaHub()
    source = make_source(tmp_path, hub)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming')
        phone.unplugged = True
        assert await wait_for(lambda: source.state == 'waiting')
        assert hub.input == 'airplay' and source.errors == 0
        assert source.status()['device'] is None
    finally:
        await source.stop()


async def test_disconnecting_from_the_page_holds_the_phone_until_it_is_replugged(tmp_path, bus):
    phone = FakePhone()
    bus.phones.append(phone)
    hub = MediaHub()
    source = make_source(tmp_path, hub)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming')
        assert await source.disconnect_phone() is True
        assert source.state == 'held' and source.status()['held'] and hub.input == 'airplay'
        assert source.status()['device'] is None
        await asyncio.sleep(0.4)                      # many polls, phone still plugged in
        assert source.capture is None and source.state == 'held', 'not mirrored again by itself'
        source.held_at -= usbsource.HELD_RESET_SECONDS   # past the port reset of handing it back
        phone.unplugged = True
        assert await wait_for(lambda: source.held is None)
        phone.unplugged = False                       # plugged in again
        phone._new_session()
        assert await wait_for(lambda: source.state == 'streaming')
        assert await source.disconnect_phone() is True
        await source.restart()                        # Reconnect clears the hold
        assert await wait_for(lambda: source.state == 'streaming')
    finally:
        await source.stop()


class SharedPhone(FakePhone):
    """Linux: usbmuxd holds the phone. It claimed the usbmux interface in the configuration it
    chose; with USBMUXD_DEFAULT_DEVICE_MODE=2 the phone sits in capture mode (configuration 5).
    usbmuxd's own default (mode 1) switches a phone in capture mode straight back."""

    def __init__(self, usbmuxd_mode=2, **kwargs):
        super().__init__(**kwargs)
        self.usbmuxd_mode = usbmuxd_mode
        self._usbmuxd_attach()

    def _usbmuxd_attach(self):
        self.capture_mode = self.usbmuxd_mode == 2
        self.active = 5 if self.capture_mode else 4

    def active_configuration(self):
        return self.active

    def set_configuration(self, value):
        if value != self.active:   # the kernel refuses while usbmuxd holds an interface
            raise usbsource.CaptureError(usbsource.LINUX_BUSY)

    def claim(self, number):
        mux = usbsource.find_interface((CAPTURE if self.capture_mode else NORMAL)[self.active], usbsource.MUX_SUBCLASS)
        if number == mux.number:
            raise usbsource.CaptureError(usbsource.LINUX_BUSY)
        super().claim(number)

    def reset(self):
        self.resets += 1
        self.pending = None
        self._new_session()
        with self.lock:
            self.queue.clear()
        self._usbmuxd_attach()     # re-attached, and usbmuxd picked the mode again
        raise FakeUnplugged('the phone detaches')


@pytest.fixture
def usbmuxd(monkeypatch):
    calls = []
    monkeypatch.setattr(usbsource, '_shares_with_usbmuxd', lambda: True)
    monkeypatch.setattr(usbsource.usbmuxd, 'DeviceLink', lambda serial: ('usbmuxd', serial))

    def trust(link, store, serial, on_wait=None):
        calls.append(link)
        return 'Test iPhone'
    monkeypatch.setattr(usbsource.lockdown, 'trust', trust)
    return calls


async def test_linux_shares_the_phone_with_usbmuxd(tmp_path, bus, usbmuxd):
    """Regression (a tester's Fedora 44): usbmuxd holds every plugged-in iPhone, so claiming its
    usbmux interface failed ("resource busy") and the page asked to stop usbmuxd with sudo.
    MirrorDesk now runs the trusted session through usbmuxd and claims only the capture interface."""
    phone = SharedPhone()
    bus.phones.append(phone)
    hub = MediaHub()
    source = make_source(tmp_path, hub)
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'streaming' and hub.video_units >= 2)
        assert usbmuxd == [('usbmuxd', phone.serial)], 'trusted session through usbmuxd'
        assert bus.sessions == [], 'no usbmux of our own on an interface usbmuxd holds'
        assert phone.claimed == [2], 'only the capture interface'
        assert source.capture.shared
    finally:
        await source.stop()
    assert phone.resets == 0 and not any(c[:2] == (0x40, 0x52) for c in phone.controls), \
        'usbmuxd keeps deciding the phone\'s mode: no mode switch or port reset from MirrorDesk'


async def test_usbmuxd_that_switches_the_phone_back_is_explained(tmp_path, bus, usbmuxd):
    bus.phones.append(SharedPhone(usbmuxd_mode=1))
    source = make_source(tmp_path, MediaHub())
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'error')
        assert source.message == usbsource.USBMUXD_MODE and 'installer' in source.message
    finally:
        await source.stop()


async def test_phone_that_never_enters_capture_mode_reports_why(tmp_path, bus):
    bus.phones.append(FakePhone(switches=False))
    source = make_source(tmp_path, MediaHub())
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'error')
        assert 'did not switch' in source.message and source.errors == 1
    finally:
        await source.stop()


async def test_waiting_for_trust_and_refused_trust(tmp_path, bus, monkeypatch):
    release = threading.Event()

    def trust(handle, interface, store, serial, on_wait):
        on_wait('Tap Trust on the iPhone, then enter its passcode.')
        release.wait(5)
        raise lockdown.LockdownError('"Don\'t Trust" was chosen on the phone.')
    monkeypatch.setattr(usbsource, 'establish_trust', trust)
    bus.phones.append(FakePhone())
    source = make_source(tmp_path, MediaHub())
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'pairing')
        assert 'Tap Trust' in source.message
        release.set()
        assert await wait_for(lambda: source.state == 'error')
        assert "Don't Trust" in source.message
    finally:
        release.set()
        await source.stop()


async def test_phone_with_apples_driver_is_explained(tmp_path, bus):
    source = make_source(tmp_path, MediaHub(), driver_check=lambda: [('Apple iPhone', False)])
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'driver')
        assert "Apple's driver" in source.message
    finally:
        await source.stop()


async def test_missing_libusb_is_reported(tmp_path, bus, monkeypatch):
    monkeypatch.setattr(usbsource, '_on_windows', lambda: False)
    source = make_source(tmp_path, MediaHub(), backend_loader=lambda: (None, 'The libusb driver is not installed.'))
    await source.start()
    assert await wait_for(lambda: source.state == 'unavailable')
    assert source.message == 'The libusb driver is not installed.'
    await source.stop()


async def test_new_windows_pc_offers_the_driver_then_picks_it_up(tmp_path, bus, monkeypatch):
    """No libusb yet: a plugged-in iPhone on Apple's driver gets the install offer, and the
    source starts working as soon as the driver (libusb0.dll) is there - no restart needed."""
    monkeypatch.setattr(usbsource, '_on_windows', lambda: True)
    monkeypatch.setattr(usbsource, 'DRIVER_CHECK_SECONDS', 0.3)
    state = {'backend': None, 'phones': []}
    source = make_source(tmp_path, MediaHub(), driver_check=lambda: state['phones'],
                         backend_loader=lambda: (state['backend'], 'The libusb driver is not installed.'))
    await source.start()
    try:
        assert await wait_for(lambda: source.state == 'unavailable')
        assert 'Plug in the iPhone' in source.message
        state['phones'] = [('Apple Mobile Device USB Composite Device', False)]
        assert await wait_for(lambda: source.state == 'driver')
        assert 'Install cable driver' in source.message
        state['backend'] = object()
        assert await wait_for(lambda: source.state in ('waiting', 'driver') and source.message != '' and
                              'Plug in' not in source.message and source.task is not None)
    finally:
        await source.stop()


def test_hardware_check_command_reports_and_saves(tmp_path, bus, monkeypatch, capsys):
    import av
    monkeypatch.setattr(usbsource, 'load_backend', lambda: (object(), None))
    phone = FakePhone(feeds=3)
    bus.phones.append(phone)
    save = tmp_path / 'usb-test.h264'
    assert usbsource.check(seconds=0.3, save=str(save), data_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert 'OK - cable mirroring works' in out and '1126×2436' in out and '48000 Hz' in out
    assert phone.serial not in out
    frames = list(av.open(str(save), format='h264').decode(video=0))
    assert frames and (frames[0].width, frames[0].height) == (1126, 2436)
    assert phone.controls[-1] == (0x40, 0x52, 0, usbsource.MODE_NORMAL), 'the phone is handed back afterwards'


def test_hardware_check_without_a_phone(bus, monkeypatch, capsys):
    monkeypatch.setattr(usbsource, 'load_backend', lambda: (object(), None))
    monkeypatch.setattr(usbsource, 'windows_apple_drivers', lambda: [('Apple iPhone', False)])
    assert usbsource.check(seconds=0.1) == 1
    assert "uses Apple's driver" in capsys.readouterr().out


def test_reads_ask_only_for_the_current_frame():
    reader = qt.FrameReader()
    assert usbsource.read_size(reader, 512) == 512          # length field still unknown
    reader.feed(struct.pack('<I', 91607) + b'x' * 100)       # a 91,607-byte packet has begun
    assert usbsource.read_size(reader, 512) == 91648         # 91,503 bytes left, in whole packets
    assert usbsource.read_size(reader, 1024) == 91136 + 1024
    reader = qt.FrameReader()
    reader.feed(struct.pack('<I', 50_000_000))               # absurd sizes are still capped
    assert usbsource.read_size(reader, 512) == usbsource.READ_SIZE


def test_libusb_win32_timeouts_are_recognised():
    import usb.core
    assert usbsource._is_timeout(usb.core.USBError(b'libusb0-dll:err [_usb_reap_async] timeout error\n', -116))
    assert usbsource._is_timeout(usb.core.USBTimeoutError('timeout', -7, 110))
    assert not usbsource._is_timeout(usb.core.USBError('No such device', -19))


def test_pnputil_parsing(monkeypatch):
    output = '\r\n'.join([
        'Microsoft PnP Utility', '',
        'Instance ID:                USB\\VID_05AC&PID_12A8\\00008030000F1E2D3C4B5A69',
        'Device Description:         Apple Mobile Device USB Composite Device',
        'Class Name:                 USBDevice',
        'Manufacturer Name:          Apple, Inc.',
        'Driver Name:                oem126.inf', '',
        'Instance ID:                USB\\VID_05AC&PID_12A8&MI_01\\7&1F5613C&8&0001',
        'Device Description:         Apple Mobile Device USB Device',
        'Class Name:                 USBDevice', '',
        'Instance ID:                USB\\VID_05AC&PID_12AB\\0000ABCD',
        'Device Description:         iPad (Composite Parent)',
        'Class Name:                 libusb-win32 devices',
        'Driver Name:                oem7.inf', '',
        'Instance ID:                USB\\VID_046D&PID_C336\\116D396E3635',
        'Device Description:         G213', ''])

    class Result:
        stdout = output
    monkeypatch.setattr(usbsource, '_on_windows', lambda: True)
    monkeypatch.setattr(usbsource.subprocess, 'run', lambda *a, **k: Result())
    assert usbsource.windows_apple_drivers() == [('Apple Mobile Device USB Composite Device', False),
                                                  ('iPad (Composite Parent)', True)]


def test_audio_converter_passthrough_and_resample():
    import numpy as np
    pcm = (np.arange(4800 * 2, dtype='<i2') % 1000).tobytes()  # 0.1 s of 48 kHz stereo
    same = usbsource._AudioConverter(48000, 2).convert(pcm, qt.AudioFormat())
    assert same.shape == (1, 9600) and same.dtype == np.int16
    resampled = usbsource._AudioConverter(44100, 2).convert(pcm, qt.AudioFormat())
    assert resampled.dtype == np.int16 and resampled.shape[0] == 1
    assert 0 < resampled.shape[1] <= 4410 * 2
    assert usbsource._AudioConverter(44100, 2).convert(pcm, qt.AudioFormat(bits_per_channel=32)) is None
