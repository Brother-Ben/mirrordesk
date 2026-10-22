"""Android phones as a source: screen and sound over a USB cable or Wi-Fi.

Google's adb starts scrcpy-server (Genymobile/scrcpy, Apache-2.0, bundled in
third_party/scrcpy) on the phone. It encodes the screen with the phone's own
H.264 encoder and captures the sound (Android 11+) as raw 48 kHz PCM; both
reach MirrorDesk through an adb port forward, and the H.264 goes to browsers
untouched, exactly like an iPhone's. Nothing is installed on the phone: USB
debugging (or Wireless debugging, Android 11+) is all it needs.

Wire format, scrcpy 5.0.1 (server Streamer.java / DesktopConnection.java,
client app/src/demuxer.c), all integers big-endian:
  video socket   [dummy byte][device name: 64 bytes, NUL padded][u32 codec id]
                 then 12-byte headers, either
                   session (top bit set): u32 flags, u32 width, u32 height
                   media:                 u64 pts|flags, u32 size, then <size> bytes
                 media flags: bit 62 = config (SPS/PPS), bit 61 = key frame
  audio socket   [u32 codec id: 'raw', or 0 = disabled by the phone, 1 = error]
                 then media packets of s16le stereo 48 kHz PCM
  control socket MirrorDesk -> phone: reset video (key frame), touch, keys, text;
                 phone -> MirrorDesk messages are read and ignored.
"""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import ipaddress
import logging
import os
from pathlib import Path
import re
import secrets
import string
import struct
import subprocess
import time

import numpy as np

from . import androidtools
from .h264 import NAL_IDR, nal_type, split_annexb

log = logging.getLogger('mirrordesk.android')

CODEC_H264 = 0x68323634   # 'h264'
CODEC_RAW = 0x00726177    # 'raw'
FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY_FRAME = 1 << 61
PTS_MASK = FLAG_KEY_FRAME - 1
DEVICE_NAME_LENGTH = 64
MAX_PACKET = 32 * 1024 * 1024

DEVICE_SERVER_PATH = '/data/local/tmp/mirrordesk-scrcpy-server.jar'
POLL_SECONDS = 2.0
RETRY_SECONDS = 5.0
CONNECT_SECONDS = 15.0          # phone-side server start (first start on a slow phone)
STOP_SECONDS = 4.0
KEYFRAME_INTERVAL_SECONDS = 2   # encoder GOP: late joiners replay at most 2 s
LOG_LINES = 200
PAIRING_SECONDS = 120

# Control messages (scrcpy ControlMessage.java)
MSG_INJECT_KEYCODE, MSG_INJECT_TEXT, MSG_INJECT_TOUCH, MSG_INJECT_SCROLL = 0, 1, 2, 3
MSG_BACK_OR_SCREEN_ON, MSG_SET_DISPLAY_POWER, MSG_RESET_VIDEO = 4, 10, 17
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_GENERIC_FINGER = -2
INJECT_TEXT_MAX = 300
KEYCODES = {'home': 3, 'back': 4, 'volume_up': 24, 'volume_down': 25, 'power': 26, 'enter': 66,
            'backspace': 67, 'recents': 187, 'delete': 112, 'tab': 61, 'escape': 111,
            'left': 21, 'right': 22, 'up': 19, 'down': 20}

STATES = ('off', 'unavailable', 'installing', 'waiting', 'unauthorized', 'connecting', 'streaming', 'held', 'error')
WAITING = 'Connect an Android phone with a USB cable, with USB debugging turned on.'
HELD = 'Disconnected. Unplug the phone and plug it in again, or click Reconnect, to mirror it again.'
UNAUTHORIZED = ('Unlock the phone and tap Allow on "Allow USB debugging?" '
                '(tick "Always allow from this computer").')
_DEVICE_LINE = re.compile(r'^(?P<serial>\S.*?)\s+(?P<state>device|offline|unauthorized|authorizing|connecting|'
                          r'recovery|rescue|sideload|bootloader|host|unknown|no permissions[^\n]*?)'
                          r'(?:\s+(?P<rest>(?:\S+:\S*\s*)*))?$')


class AdbError(RuntimeError):
    """A user-facing adb failure."""


# Diagnostics get categories, never the message text (it can contain addresses or device names).
REASONS = (('tools_missing', ('Android tools are not installed', 'Install adb')),
           ('adb_failed', ('adb did not start', 'Could not run adb', 'did not answer in')),
           ('unauthorized', ('Allow USB debugging',)),
           ('no_permission', ('no permission',)),
           ('server_missing', ('scrcpy server file',)),
           ('push_failed', ('could not be copied to the phone',)),
           ('encoder', ('could not encode its screen', 'video encoder')),
           ('control_blocked', ('blocks remote control',)),
           ('start_refused', ('refused to start mirroring',)),
           ('start_timeout', ('did not start mirroring in time',)),
           ('connection_lost', ('connection to the phone was lost',)),
           ('stopped_on_phone', ('Mirroring stopped on the phone',)),
           ('video_format', ('unexpected video format',)))
REASON_NAMES = {name for name, _ in REASONS} | {'other', 'unplugged', 'stopped'}
BRANDS = ('google', 'samsung', 'xiaomi', 'redmi', 'poco', 'oneplus', 'motorola', 'oppo', 'vivo', 'realme',
          'huawei', 'honor', 'sony', 'nokia', 'asus', 'lenovo', 'nothing', 'fairphone', 'tcl', 'zte')
PAIRING_STATES = ('pairing', 'paired', 'connected', 'failed', 'waiting')


def reason_category(message):
    text = (message or '').lower()
    for name, needles in REASONS:
        if any(needle.lower() in text for needle in needles):
            return name
    return 'other'


def brand_category(manufacturer):
    name = (manufacturer or '').strip().lower()
    return name if name in BRANDS else ('unknown' if not name else 'other')


def is_wireless(serial):
    return ':' in serial or '._adb-tls-connect.' in serial


# ---------------------------------------------------------------------- adb
@dataclass
class Device:
    serial: str
    state: str
    model: str = ''
    product: str = ''
    transport_id: str = ''

    @property
    def transport(self):
        return 'wifi' if is_wireless(self.serial) else 'usb'

    @property
    def label(self):
        return (self.model or self.product or 'Android phone').replace('_', ' ')


def parse_devices(text):
    devices = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith('List of devices') or line.startswith('*'):
            continue
        match = _DEVICE_LINE.match(line)
        if not match:
            continue
        fields = dict(item.split(':', 1) for item in (match.group('rest') or '').split() if ':' in item)
        state = match.group('state')
        devices.append(Device(match.group('serial').strip(), 'no permissions' if state.startswith('no permissions')
                              else state, fields.get('model', ''), fields.get('product', ''),
                              fields.get('transport_id', '')))
    return devices


def parse_mdns(text):
    """`adb mdns services` -> [(instance, service type, 'ip:port')]."""
    services = []
    for line in text.splitlines():
        parts = line.strip().split('\t') if '\t' in line else line.split()
        if len(parts) >= 3 and parts[1].startswith('_adb'):
            services.append((parts[0].strip(), parts[1].strip().rstrip('.'), parts[2].strip()))
    return services


class Adb:
    """Thin async wrapper around the adb executable (one subprocess per command).

    command: the adb path, or an argv prefix list (tests run a fake adb with Python)."""

    def __init__(self, command, env=None):
        self.command = list(command) if isinstance(command, (list, tuple)) else [command]
        self.env = env
        self.flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0

    async def run(self, *args, serial=None, timeout=20.0, check=False):
        argv = self.command + (['-s', serial] if serial else []) + list(args)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, env=self.env, creationflags=self.flags)
        except OSError as exc:
            raise AdbError(f'Could not run adb ({exc}).')
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise AdbError(f'adb {args[0]} did not answer in {timeout:g} s.')
        except asyncio.CancelledError:
            # Shutting down mid-command: never leave an adb child (and its pipes) behind.
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        text = out.decode('utf-8', 'replace')
        if check and process.returncode != 0:
            raise AdbError(_adb_message(text) or f'adb {args[0]} failed.')
        return process.returncode, text

    async def version(self):
        code, text = await self.run('version', timeout=10)
        match = re.search(r'Version (\S+)', text)
        return match.group(1) if code == 0 and match else None

    async def start_server(self):
        """Start the adb daemon. Its output goes to NUL, not to pipes: on Windows the
        daemon inherits the client's pipes and keeps them open forever, so reading the
        output of `adb start-server` would never finish (seen with adb 37.0.1).
        Returns True if this call started the daemon (False: it was already running)."""
        running = await asyncio.to_thread(_server_listening, self.server_port())
        process = await asyncio.create_subprocess_exec(
            *self.command, 'start-server', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL, env=self.env, creationflags=self.flags)
        try:
            code = await asyncio.wait_for(process.wait(), 30)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise AdbError('adb did not start in 30 s. Security software may be blocking it.')
        if code != 0:
            raise AdbError('adb did not start. Security software may be blocking it; reinstall the Android tools.')
        return not running

    async def kill_server(self):
        process = await asyncio.create_subprocess_exec(
            *self.command, 'kill-server', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL, env=self.env, creationflags=self.flags)
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()

    def server_port(self):
        try:
            return int((self.env or os.environ).get('ANDROID_ADB_SERVER_PORT', 5037))
        except ValueError:
            return 5037

    async def devices(self):
        _, text = await self.run('devices', '-l', timeout=20)
        return parse_devices(text)

    async def push(self, serial, local, remote):
        await self.run('push', str(local), remote, serial=serial, timeout=60, check=True)

    async def forward(self, serial, remote):
        _, text = await self.run('forward', 'tcp:0', remote, serial=serial, check=True)
        match = re.search(r'^\s*(\d{2,5})\s*$', text, re.M)
        if not match:
            raise AdbError(_adb_message(text) or 'adb did not open a port to the phone.')
        return int(match.group(1))

    async def forward_remove(self, serial, port):
        try:
            await self.run('forward', '--remove', f'tcp:{port}', serial=serial, timeout=10)
        except AdbError:
            pass

    async def getprops(self, serial, names):
        script = '; '.join(f'getprop {name}' for name in names)
        code, text = await self.run('shell', script, serial=serial, timeout=10)
        values = text.splitlines() if code == 0 else []
        return {name: (values[i].strip() if i < len(values) else '') for i, name in enumerate(names)}

    async def spawn_shell(self, serial, args):
        argv = self.command + ['-s', serial, 'shell'] + list(args)
        return await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, env=self.env, creationflags=self.flags)

    async def pair(self, target, code):
        rc, text = await self.run('pair', target, code, timeout=30)
        ok = rc == 0 and 'Successfully paired' in text
        return ok, text.strip()

    async def connect(self, target):
        _, text = await self.run('connect', target, timeout=20)
        ok = text.strip().startswith(('connected to', 'already connected to'))
        return ok, text.strip()

    async def disconnect(self, target):
        await self.run('disconnect', target, timeout=10)

    async def mdns_services(self):
        _, text = await self.run('mdns', 'services', timeout=10)
        return parse_mdns(text)


def _server_listening(port):
    import socket
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False


def _adb_message(text):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('*')]
    return lines[-1] if lines else ''


# ---------------------------------------------------------------------- protocol
def read_name(data):
    return data.split(b'\0', 1)[0].decode('utf-8', 'replace').strip()


def parse_header(header):
    """12-byte header -> ('session', width, height) or ('media', pts_us, size, config, key)."""
    if header[0] & 0x80:
        _, width, height = struct.unpack('>III', header)
        return 'session', width, height
    flags_pts, size = struct.unpack('>QI', header)
    return ('media', flags_pts & PTS_MASK, size, bool(flags_pts & FLAG_CONFIG), bool(flags_pts & FLAG_KEY_FRAME))


def msg_reset_video():
    return bytes([MSG_RESET_VIDEO])


def _u16_fixed(value):
    value = min(max(float(value), 0.0), 1.0)
    return 0xFFFF if value >= 1.0 else int(value * 0x10000)


def _i16_fixed(value):
    value = min(max(float(value), -1.0), 1.0)
    return 0x7FFF if value >= 1.0 else int(value * 0x8000)


def msg_touch(action, pointer_id, x, y, width, height, pressure=1.0):
    # Finger events carry no mouse buttons (the phone clears them for touches anyway).
    return struct.pack('>BBqiiHHHii', MSG_INJECT_TOUCH, action, pointer_id, x, y, width, height,
                       _u16_fixed(pressure), 0, 0)


def msg_scroll(x, y, width, height, hscroll, vscroll):
    # The phone decodes i16 fixed point in [-1, 1] and multiplies by 16.
    return struct.pack('>BiiHHhhi', MSG_INJECT_SCROLL, x, y, width, height,
                       _i16_fixed(hscroll / 16), _i16_fixed(vscroll / 16), 0)


def msg_keycode(action, keycode, repeat=0, meta=0):
    return struct.pack('>BBiii', MSG_INJECT_KEYCODE, action, keycode, repeat, meta)


def msg_back_or_screen_on(action):
    return struct.pack('>BB', MSG_BACK_OR_SCREEN_ON, action)


def msg_text(text):
    data = text.encode('utf-8')[:INJECT_TEXT_MAX]
    # Never cut a multi-byte character in half.
    data = data.decode('utf-8', 'ignore').encode('utf-8')
    return struct.pack('>BI', MSG_INJECT_TEXT, len(data)) + data


def server_args(scid, settings, sdk=None):
    """Arguments after `app_process / com.genymobile.scrcpy.Server`."""
    args = [androidtools.SCRCPY_VERSION, f'scid={scid:08x}', 'log_level=info', 'video_codec=h264',
            'audio_codec=raw', f'audio={"true" if settings.audio_enabled else "false"}',
            f'max_fps={settings.android_fps}', f'video_bit_rate={settings.android_bit_rate * 1_000_000}',
            'tunnel_forward=true', 'control=true', 'clipboard_autosync=false',
            f'video_codec_options=i-frame-interval={KEYFRAME_INTERVAL_SECONDS}']
    if settings.android_max_size:
        args.append(f'max_size={settings.android_max_size}')
    if settings.audio_enabled and settings.android_audio_on_phone:
        args += ['audio_source=playback', 'audio_dup=true']   # Android 13+: sound stays on the phone too
    return args


# ---------------------------------------------------------------------- one session
class Session:
    """One phone, one scrcpy server run: start, stream, stop."""

    def __init__(self, adb, device, sink, settings, server_file):
        self.adb = adb
        self.device = device
        self.sink = sink
        self.settings = settings
        self.server_file = server_file
        self.scid = secrets.randbelow(0x7FFFFFFF)
        self.port = None
        self.process = None
        self.control = None          # StreamWriter of the control socket
        self.writers = []
        self.width = self.height = 0
        self.name = None
        self.audio_state = 'off'
        self.error = None
        self.unplugged = False
        self.stopping = False
        self.log = deque(maxlen=LOG_LINES)
        self.frames = 0
        self.audio_packets = 0
        self.last_keyframe_request = -1e9
        self.started = time.monotonic()

    # -- lifecycle ------------------------------------------------------------------
    async def run(self):
        serial = self.device.serial
        tasks = []
        try:
            self.sink.session_state('connecting', f'Starting mirroring on {self.device.label}…')
            await self.adb.push(serial, self.server_file, DEVICE_SERVER_PATH)
            self.port = await self.adb.forward(serial, f'localabstract:scrcpy_{self.scid:08x}')
            self.process = await self.adb.spawn_shell(
                serial, [f'CLASSPATH={DEVICE_SERVER_PATH}', 'app_process', '/', 'com.genymobile.scrcpy.Server']
                + server_args(self.scid, self.settings))
            self.sink.adopt(self.process)
            tasks.append(asyncio.create_task(self._read_log()))
            video_reader = await self._connect_first()
            audio_reader = await self._connect() if self.settings.audio_enabled else None
            control_reader = await self._connect()
            self.control = self.writers[-1]
            self.name = read_name(await asyncio.wait_for(video_reader.readexactly(DEVICE_NAME_LENGTH), 10))
            self.sink.device_name(self.name)
            streams = [asyncio.create_task(self._video(video_reader)), asyncio.create_task(self._drain(control_reader)),
                       asyncio.create_task(self.process.wait())]
            if audio_reader is not None:
                streams.append(asyncio.create_task(self._audio(audio_reader)))
            tasks += streams
            done, _ = await asyncio.wait(streams, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception() is not None and not self.stopping:
                    raise task.exception()
            if not self.stopping and self.error is None:
                self.error = self._explain('Mirroring stopped on the phone.')
        except (AdbError, androidtools.ToolsError) as exc:
            self.error = str(exc)
        except (OSError, asyncio.IncompleteReadError, ValueError, TimeoutError) as exc:
            if not self.stopping:
                self.error = self._explain(f'The connection to the phone was lost ({type(exc).__name__}).')
        finally:
            await self._cleanup(tasks)

    def stop(self):
        self.stopping = True
        for writer in self.writers:
            writer.close()

    async def _cleanup(self, tasks):
        self.stopping = True
        for writer in self.writers:
            writer.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.process is not None and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), STOP_SECONDS)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.port is not None:
            await self.adb.forward_remove(self.device.serial, self.port)

    # -- sockets ----------------------------------------------------------------------
    async def _open(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port, limit=4 * 1024 * 1024)
        self.writers.append(writer)
        return reader, writer

    async def _connect_first(self):
        """adb accepts the TCP connection at once and only then dials the phone's
        socket, so 'connected' proves nothing: the server's dummy byte does."""
        deadline = time.monotonic() + CONNECT_SECONDS
        while True:
            writer = None
            try:
                reader, writer = await self._open()
                await asyncio.wait_for(reader.readexactly(1), 2)
                return reader
            except (OSError, asyncio.IncompleteReadError, TimeoutError):
                if writer is not None:
                    writer.close()
                    self.writers.remove(writer)
                if self.process.returncode is not None:
                    await asyncio.sleep(0.2)   # let the log reader collect the reason
                    raise AdbError(self._explain('The phone refused to start mirroring.'))
                if time.monotonic() > deadline:
                    raise AdbError(self._explain('The phone did not start mirroring in time.'))
                await asyncio.sleep(0.1)

    async def _connect(self):
        reader, _ = await self._open()
        return reader

    # -- streams ------------------------------------------------------------------------
    async def _video(self, reader):
        codec = struct.unpack('>I', await reader.readexactly(4))[0]
        if codec != CODEC_H264:
            raise AdbError('The phone could not start its video encoder.' if codec in (0, 1)
                           else 'The phone sent an unexpected video format.')
        config = []
        while True:
            header = parse_header(await reader.readexactly(12))
            if header[0] == 'session':
                self.width, self.height = header[1], header[2]
                self.sink.video_size(self.width, self.height)
                continue
            _, pts, size, is_config, key = header
            if not 0 < size <= MAX_PACKET:
                raise ValueError(f'bad packet size {size}')
            data = await reader.readexactly(size)
            nals = split_annexb(data)
            if is_config:
                config = nals     # SPS/PPS travel with the next picture
                continue
            if config:
                nals, config = config + nals, []
            self.frames += 1
            self.sink.video(nals, pts, time.monotonic(), key or any(nal_type(n) == NAL_IDR for n in nals))

    async def _audio(self, reader):
        codec = struct.unpack('>I', await reader.readexactly(4))[0]
        if codec == 0:
            self.audio_state = 'unsupported'
            self.sink.audio_state('unsupported', self._explain_audio())
            await asyncio.Event().wait()   # video continues without sound
        if codec != CODEC_RAW:
            self.audio_state = 'error'
            self.sink.audio_state('error', 'The phone could not capture its sound.')
            await asyncio.Event().wait()
        self.audio_state = 'on'
        self.sink.audio_state('on', '')
        while True:
            header = parse_header(await reader.readexactly(12))
            if header[0] != 'media':
                continue
            _, _, size, is_config, _ = header
            if not 0 < size <= MAX_PACKET:
                raise ValueError(f'bad audio packet size {size}')
            data = await reader.readexactly(size)
            if is_config or size < 4:
                continue
            samples = np.frombuffer(data[:size - size % 4], dtype='<i2').astype(np.int16).reshape(1, -1)
            self.audio_packets += 1
            self.sink.audio(samples, time.monotonic())

    async def _drain(self, reader):
        while await reader.read(65536):
            pass   # clipboard/acks from the phone: not used

    async def _read_log(self):
        while True:
            line = await self.process.stdout.readline()
            if not line:
                return
            text = line.decode('utf-8', 'replace').rstrip()
            if text:
                self.log.append((time.time(), text))
                self.sink.server_log(text)

    def _explain(self, fallback):
        text = '\n'.join(line for _, line in self.log)
        if 'INJECT_EVENTS' in text:
            return ('The phone blocks remote control. On Xiaomi/Redmi/POCO phones also turn on '
                    '"USB debugging (Security settings)" in Developer options, then reconnect.')
        if re.search(r'ClassNotFoundException|Could not find class|No such file', text):
            return 'The mirroring helper could not be copied to the phone. Reconnect the cable and try again.'
        if 'Could not create default video encoder' in text or 'Capture/encoding error' in text:
            return 'The phone could not encode its screen. Try a lower size or bit rate in Settings.'
        for _, line in reversed(self.log):
            if line.startswith('ERROR:'):
                return f'{fallback} Phone says: {line[6:].strip()[:200]}'
        return fallback

    def _explain_audio(self):
        text = '\n'.join(line for _, line in self.log)
        if 'not supported before Android 11' in text:
            return 'Sound needs Android 11 or newer; mirroring continues without sound.'
        return ('The phone did not allow sound capture. On Android 11, unlock the phone, then reconnect. '
                'Mirroring continues without sound.')

    # -- control -------------------------------------------------------------------------
    def send(self, message):
        if self.control is None or self.control.is_closing():
            return False
        self.control.write(message)
        return True

    def request_keyframe(self):
        now = time.monotonic()
        if now - self.last_keyframe_request < 1.0:
            return False
        self.last_keyframe_request = now
        return self.send(msg_reset_video())


# ---------------------------------------------------------------------- watcher
class AndroidSource:
    """Watches adb for phones and runs one Session at a time; Wi-Fi pairing; control."""

    def __init__(self, store, hub, diagnostics=None, adb_finder=androidtools.find_adb, server_file=None,
                 containment=None):
        self.store = store
        self.hub = hub
        self.diagnostics = diagnostics
        self.adb_finder = adb_finder
        self.server_file = server_file
        self.containment = containment
        self.adb = None
        self.adb_version = None
        self.state = 'off'
        self.message = 'Android mirroring is off.'
        self.devices = []
        self.selected = None
        self.held = None             # serial disconnected from the page: not mirrored again until it goes away
        self.session = None
        self.session_task = None
        self.task = None
        self.loop = None
        self.retry_at = 0.0
        self.sessions = 0
        self.errors = 0
        self.device = None           # shown in the UI: name/model/size/transport/android
        self.audio_info = {'state': 'off', 'message': ''}
        self.pairing = {'state': 'idle', 'message': '', 'mode': None}
        self.pair_task = None
        self.install = {'state': 'idle', 'progress': 0.0, 'message': ''}
        self.log = deque(maxlen=LOG_LINES)
        self.audio_gap = True
        self.starting = False
        self.started_server = False
        self.control_note = None

    # -- lifecycle ------------------------------------------------------------------
    def _env(self):
        env = os.environ.copy()
        # Discover paired phones on the network (Wireless debugging) and reconnect them.
        env.setdefault('ADB_MDNS_AUTO_CONNECT', 'adb-tls-connect')
        return env

    async def start(self):
        self.loop = asyncio.get_running_loop()
        if self.task is not None and not self.task.done():
            return
        path = self.adb_finder()
        if path is None:
            self.adb = None
            self._set('unavailable', 'The Android tools are not installed yet.' + (
                ' Install them once with "Install Android tools" on the Devices page (Android).'
                if androidtools.installable() else ' Install adb (Linux: apt install adb) and restart MirrorDesk.'))
            return
        self.adb = Adb(path, self._env())
        self.retry_at = 0.0
        self.task = asyncio.create_task(self._watch())

    async def stop(self, state='off', message='Android mirroring is off.', shutdown=False):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_session()
        pair_task, self.pair_task = self.pair_task, None
        if pair_task is not None:
            pair_task.cancel()
            await asyncio.gather(pair_task, return_exceptions=True)
        if shutdown and self.adb is not None and (self.started_server or await asyncio.to_thread(self._daemon_is_ours)):
            # Leave nothing running that MirrorDesk started (an adb from this folder
            # would also keep the folder from being deleted), also one an earlier MirrorDesk
            # left behind. Never touch an adb daemon that runs for someone else.
            await self.adb.kill_server()
            self.started_server = False
        self._set(state, message)

    def _daemon_is_ours(self):
        """Windows: the adb daemon listening now runs from MirrorDesk's own adb, so an earlier
        MirrorDesk started it and did not stop it (seen: one left running for a day and a half
        across several MirrorDesk runs, each of which found it "already running")."""
        if os.name != 'nt' or self.adb is None:
            return False
        from .desktop import port_owners, process_image
        try:
            ours = Path(self.adb.command[0]).resolve()
        except OSError:
            return False
        for pid in port_owners(self.adb.server_port(), ()):
            image = process_image(pid)
            try:
                if image and Path(image).resolve() == ours:
                    return True
            except OSError:
                continue
        return False

    async def restart(self):
        await self._stop_session()
        self.retry_at = 0.0
        self.held = None
        if self.task is None or self.task.done():
            await self.start()

    async def disconnect_phone(self):
        """Stop mirroring the phone from the page. It stays connected to adb but is not mirrored
        again until it is unplugged (or its Wi-Fi connection drops) or Reconnect is clicked."""
        session = self.session
        if session is None:
            return False
        self.held = session.device.serial
        await self._stop_session()
        self.device = None
        self._set('held', HELD)
        return True

    async def _stop_session(self):
        session, task = self.session, self.session_task
        if session is not None:
            session.stop()
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), STOP_SECONDS + 2)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
        self.session = self.session_task = None
        self.hub.release('android')

    # -- tools ---------------------------------------------------------------------
    async def install_tools(self):
        if not androidtools.installable():
            raise AdbError('Install adb with the system package manager on this computer.')
        if self.install['state'] == 'running':
            return
        self.install = {'state': 'running', 'progress': 0.0, 'message': 'Downloading Android tools from Google…'}
        self._set('installing', self.install['message'])

        def progress(fraction):
            self.install['progress'] = round(fraction, 2)
        try:
            path = await asyncio.to_thread(androidtools.install, None, progress)
        except (androidtools.ToolsError, OSError) as exc:
            self.install = {'state': 'failed', 'progress': 0.0, 'message': f'Could not install: {exc}'}
            self._set('unavailable', self.install['message'])
            return
        self.install = {'state': 'done', 'progress': 1.0, 'message': f'Installed {os.path.basename(path)}.'}
        self.state = 'off'
        if self.store.settings.android_enabled:
            await self.start()

    # -- watching ------------------------------------------------------------------
    def _record(self, event, **fields):
        if self.diagnostics is not None:
            try:
                self.diagnostics.record(event, **fields)
            except ValueError:
                pass

    def _set(self, state, message):
        if state != self.state:
            fields = {'android_state': state, 'android_sessions': self.sessions}
            if state in ('error', 'unavailable', 'unauthorized'):
                fields['android_reason'] = reason_category(message)   # why, as a fixed category only
            self._record('android_state', **fields)
        self.state, self.message = state, message

    @property
    def pairing(self):
        return self._pairing

    @pairing.setter
    def pairing(self, value):
        previous = getattr(self, '_pairing', {}) or {}
        self._pairing = value
        if value.get('state') != previous.get('state') and value.get('state') in PAIRING_STATES:
            self._record('android_pairing', android_pairing_state=value['state'],
                         android_pairing_mode=value.get('mode') or 'code')

    async def _watch(self):
        try:
            self.adb_version = await self.adb.version()
            if self.adb_version is None:
                self._set('unavailable', 'adb did not start. Reinstall the Android tools.')
                return
            self.started_server = await self.adb.start_server() or self.started_server
        except AdbError as exc:
            self._set('unavailable', str(exc))
            return
        self._set('waiting', WAITING)
        while True:
            try:
                self.devices = await self.adb.devices()
            except AdbError as exc:
                self.devices = []
                log.debug('adb devices failed: %s', exc)
                try:   # the daemon died (or was killed): start it again the safe way
                    self.started_server = await self.adb.start_server() or self.started_server
                except AdbError:
                    pass
            self._watch_tick()
            await asyncio.sleep(POLL_SECONDS)

    def _candidate(self):
        ready = [d for d in self.devices if d.state == 'device' and d.serial != self.held]
        if self.selected:
            chosen = [d for d in ready if d.serial == self.selected]
            if chosen:
                return chosen[0]
        # The same phone on cable and Wi-Fi: the cable is steadier.
        ready.sort(key=lambda d: d.transport != 'usb')
        return ready[0] if ready else None

    def _watch_tick(self):
        if self.session is not None:
            if not any(d.serial == self.session.device.serial and d.state == 'device' for d in self.devices):
                self.session.unplugged = True
                self.session.stop()   # unplugged or Wi-Fi lost: _ended() cleans up
            return
        if self.held is not None and not any(d.serial == self.held and d.state == 'device' for d in self.devices):
            self.held = None   # unplugged (or Wi-Fi gone) since it was disconnected: mirror it again next time
        if self.starting or time.monotonic() < self.retry_at:
            return   # after an error its message stays up until the retry
        device = self._candidate()
        if device is None and self.held is not None:
            self._set('held', HELD)
            return
        if device is not None:
            self.starting = True
            asyncio.create_task(self._begin(device))
        elif any(d.state in ('unauthorized', 'authorizing') for d in self.devices):
            self._set('unauthorized', UNAUTHORIZED)
        elif any(d.state == 'no permissions' for d in self.devices):
            self._set('error', 'This computer may not open the phone over USB (no permission). On Linux add a '
                               'udev rule for the phone, or run the container with USB access.')
        elif any(d.state == 'offline' for d in self.devices):
            self._set('waiting', 'The phone is connected but offline. Unplug it and plug it in again.')
        else:
            self.device = None
            self._set('waiting', WAITING)

    async def _begin(self, device):
        try:
            await self._begin_session(device)
        finally:
            self.starting = False

    async def _begin_session(self, device):
        if self.session is not None:
            return
        try:
            server_file = androidtools.scrcpy_server(self.server_file)
        except androidtools.ToolsError as exc:
            self._set('error', str(exc))
            self.retry_at = time.monotonic() + 60
            return
        props = {}
        try:
            props = await self.adb.getprops(device.serial, ['ro.build.version.release', 'ro.build.version.sdk',
                                                            'ro.product.manufacturer'])
        except AdbError:
            pass
        settings = self.store.settings
        self.sessions += 1
        self.audio_gap = True
        self.control_note = None
        self.device = {'name': device.label, 'model': device.label, 'transport': device.transport,
                       'android': props.get('ro.build.version.release') or None,
                       'sdk': int(props['ro.build.version.sdk']) if props.get('ro.build.version.sdk', '').isdigit() else None,
                       'manufacturer': props.get('ro.product.manufacturer') or None,
                       'width': None, 'height': None}
        self.audio_info = {'state': 'starting' if settings.audio_enabled else 'off', 'message': ''}
        if settings.audio_enabled and self.device['sdk'] is not None and self.device['sdk'] < 30:
            self.audio_info = {'state': 'unsupported', 'message': 'Sound needs Android 11 or newer; video only.'}
        start = {'android_transport': device.transport, 'android_audio_requested': bool(settings.audio_enabled),
                 'android_brand': brand_category(self.device['manufacturer'])}
        if self.device['sdk'] is not None:
            start['android_sdk'] = self.device['sdk']
        self._record('android_session_start', **start)
        session = Session(self.adb, device, self, settings, server_file)
        self.session = session
        self.session_task = asyncio.create_task(self._run(session))

    async def _run(self, session):
        try:
            await session.run()
        finally:
            self._ended(session)

    def _ended(self, session):
        if session is not self.session:
            return
        self.session = self.session_task = None
        self.hub.release('android')
        result = ('unplugged' if session.unplugged else reason_category(session.error) if session.error else 'stopped')
        end = {'android_result': result, 'android_seconds': round(time.monotonic() - session.started, 1),
               'android_frames': session.frames, 'android_audio_packets': session.audio_packets,
               'android_audio_state': self.audio_info.get('state', 'off')}
        if session.width:
            end.update(android_width=session.width, android_height=session.height)
        self._record('android_session_end', **end)
        if session.error and not session.unplugged:
            self.errors += 1
            self.retry_at = time.monotonic() + RETRY_SECONDS
            self._set('error', session.error)
        else:
            self.device = None
            self._set('waiting', WAITING)

    # -- session callbacks (event loop) --------------------------------------------
    def adopt(self, process):
        if self.containment is not None:
            transport = getattr(process, '_transport', None)
            popen = transport.get_extra_info('subprocess') if transport is not None else None
            if popen is not None:
                self.containment.adopt(popen)

    def session_state(self, state, message):
        self._set(state, message)

    def device_name(self, name):
        if self.device is not None and name:
            self.device['name'] = name

    def video_size(self, width, height):
        if self.device is not None:
            self.device['width'], self.device['height'] = width, height

    def video(self, nals, pts_us, arrival, keyframe):
        session = self.session
        if session is None:
            return
        if self.state != 'streaming':
            self._set('streaming', f'Mirroring {self.device["name"] if self.device else "the phone"} '
                                   f'over {"Wi-Fi" if session.device.transport == "wifi" else "the USB cable"}.')
        if self.hub.input != 'android':
            if not self.hub.claim('android'):
                return   # an iPhone on USB is mirroring; this phone waits for it to stop
            self.hub.keyframe_requester = session.request_keyframe
            if not keyframe:
                session.request_keyframe()   # viewers need a fresh picture from this phone
        self.hub.push_unit(nals, arrival, (pts_us * 9 // 100) & 0xFFFFFFFF, source='android')

    def audio(self, samples, arrival):
        if self.session is None or self.hub.input != 'android':
            return
        self.hub.push_audio(samples, arrival, discontinuity=self.audio_gap, source='android')
        self.audio_gap = False

    def audio_state(self, state, message):
        self.audio_info = {'state': state, 'message': message}
        self._record('android_audio', android_audio_state=state)

    def server_log(self, line):
        self.log.append((time.time(), line))
        if 'INJECT_EVENTS' in line and self.control_note is None:
            self._record('android_control_blocked')
            # Mirroring keeps working, only taps/keys fail: say why while it is happening.
            self.control_note = ('The phone ignores remote control. Xiaomi/Redmi/POCO: turn on "USB debugging '
                                 '(Security settings)" in Developer options, then reconnect.')

    # -- wireless (Wireless debugging, Android 11+) --------------------------------------
    def _need_adb(self):
        if self.adb is None:
            raise AdbError('Install the Android tools first.')
        return self.adb

    async def pair_with_code(self, host, port, code):
        adb = self._need_adb()
        address, target = _target(host, port)
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code.strip()):
            raise AdbError('The pairing code has 6 digits.')
        code = code.strip()
        self.pairing = {'state': 'pairing', 'message': f'Pairing with {target}…', 'mode': 'code'}
        ok, text = await adb.pair(target, code)
        if not ok:
            self.pairing = {'state': 'failed', 'mode': 'code',
                            'message': 'Pairing failed: check the code and address on the phone, then try again.'
                            + (f' ({_adb_message(text)[:120]})' if text else '')}
            return self.pairing
        self.pairing = {'state': 'paired', 'mode': 'code',
                        'message': 'Paired. Connecting… If the phone does not appear, enter the address shown '
                                   'under "IP address & Port" on the Wireless debugging screen.'}
        self.pair_task = asyncio.create_task(self._connect_after_pairing(address))
        return self.pairing

    async def _connect_after_pairing(self, host):
        """adb normally reconnects paired phones by itself (mDNS); if it does not, dial the
        phone's announced connect service."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            if any(d.transport == 'wifi' and d.state == 'device' for d in self.devices):
                self.pairing = {'state': 'connected', 'mode': self.pairing.get('mode'), 'message': 'Connected over Wi-Fi.'}
                return
            try:
                services = await self.adb.mdns_services()
            except AdbError:
                continue
            for _, kind, address in services:
                if kind.startswith('_adb-tls-connect') and address.rsplit(':', 1)[0] == str(host):
                    ok, _ = await self.adb.connect(address)
                    if ok:
                        self.pairing = {'state': 'connected', 'mode': self.pairing.get('mode'),
                                        'message': 'Connected over Wi-Fi.'}
                        return

    async def connect_wifi(self, host, port):
        adb = self._need_adb()
        _, target = _target(host, port)
        ok, text = await adb.connect(target)
        if not ok:
            message = 'Could not connect to ' + target + '. '
            if 'failed to authenticate' in text:
                message += 'Pair the phone first (Pair device with pairing code).'
            else:
                message += 'Check that Wireless debugging is on and use the address shown on that screen.'
            raise AdbError(message)
        self.pairing = {'state': 'connected', 'mode': 'connect', 'message': f'Connected to {target}.'}
        return self.pairing

    async def disconnect_wifi(self, serial):
        adb = self._need_adb()
        if not is_wireless(serial or ''):
            raise AdbError('Only Wi-Fi connections can be disconnected here; unplug the cable instead.')
        if self.session is not None and self.session.device.serial == serial:
            await self._stop_session()
        await adb.disconnect(serial)

    async def qr_start(self):
        """Pair with the phone's "Pair device with QR code" (needs mDNS on the network)."""
        self._need_adb()
        if self.pair_task is not None and not self.pair_task.done():
            self.pair_task.cancel()
        name = 'mirrordesk-' + secrets.token_hex(3)
        password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
        payload = f'WIFI:T:ADB;S:{name};P:{password};;'
        self._qr = (name, password)   # never in status: the password pairs a phone with this computer
        self.pairing = {'state': 'waiting', 'mode': 'qr', 'message': 'Scan the code with the phone.'}
        self.pair_task = asyncio.create_task(self._qr_wait(name, password))
        return {'qr_svg': qr_svg(payload), 'expires_in': PAIRING_SECONDS}

    async def _qr_wait(self, name, password):
        deadline = time.monotonic() + PAIRING_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            try:
                services = await self.adb.mdns_services()
            except AdbError:
                continue
            match = next((address for instance, kind, address in services
                          if instance == name and kind.startswith('_adb-tls-pairing')), None)
            if match is None:
                continue
            self.pairing = {'state': 'pairing', 'mode': 'qr', 'message': 'Pairing…'}
            ok, text = await self.adb.pair(match, password)
            if not ok:
                self.pairing = {'state': 'failed', 'mode': 'qr', 'message': 'Pairing failed. Try again.'}
                return
            self.pairing = {'state': 'paired', 'mode': 'qr', 'message': 'Paired. Connecting…'}
            await self._connect_after_pairing(ipaddress.ip_address(match.rsplit(':', 1)[0]))
            return
        self.pairing = {'state': 'failed', 'mode': 'qr',
                        'message': 'The phone was not found. Make sure both are on the same Wi-Fi, or pair with '
                                   'a code instead (Windows may also ask to allow adb through the firewall).'}

    # -- control ----------------------------------------------------------------------
    def control_allowed(self, local):
        mode = self.store.settings.android_control
        return mode == 'all' or (mode == 'local' and local)

    def control(self, event):
        """Apply one browser input event (dict, already authorised). Returns True if sent."""
        session = self.session
        if session is None or self.hub.input != 'android' or not session.width:
            return False
        kind = event.get('t')
        width, height = session.width, session.height
        if kind == 'touch':
            action = {'down': ACTION_DOWN, 'up': ACTION_UP, 'move': ACTION_MOVE, 'cancel': ACTION_UP}.get(event.get('a'))
            if action is None:
                return False
            x, y = _point(event, width, height)
            pointer = event.get('id', POINTER_GENERIC_FINGER)
            pointer = pointer if type(pointer) is int and -2 <= pointer < 10 else POINTER_GENERIC_FINGER
            pressure = event.get('p', 1.0) if action != ACTION_UP else 0.0
            pressure = pressure if isinstance(pressure, (int, float)) else 1.0
            return session.send(msg_touch(action, pointer, x, y, width, height, pressure))
        if kind == 'scroll':
            x, y = _point(event, width, height)
            dx, dy = event.get('dx', 0), event.get('dy', 0)
            if not all(isinstance(v, (int, float)) for v in (dx, dy)):
                return False
            return session.send(msg_scroll(x, y, width, height, max(-16, min(16, dx)), max(-16, min(16, dy))))
        if kind == 'key':
            name = event.get('k')
            if name == 'back':   # also wakes the screen
                return session.send(msg_back_or_screen_on(ACTION_DOWN) + msg_back_or_screen_on(ACTION_UP))
            code = KEYCODES.get(name)
            if code is None:
                return False
            return session.send(msg_keycode(ACTION_DOWN, code) + msg_keycode(ACTION_UP, code))
        if kind == 'text':
            text = event.get('s')
            if not isinstance(text, str) or not text or len(text) > INJECT_TEXT_MAX:
                return False
            return session.send(msg_text(text))
        return False

    # -- status -----------------------------------------------------------------------
    def status(self, local=True):
        session = self.session
        return {
            'state': self.state, 'message': self.message, 'enabled': bool(self.store.settings.android_enabled),
            'held': self.held is not None,
            'tools': {'adb': self.adb is not None, 'adb_version': self.adb_version,
                      'installable': androidtools.installable(), 'install': dict(self.install),
                      'scrcpy': androidtools.SCRCPY_VERSION},
            'device': dict(self.device) if self.device else None,
            'devices': [{'serial': d.serial, 'name': d.label, 'state': d.state, 'transport': d.transport,
                         'active': session is not None and session.device.serial == d.serial}
                        for d in self.devices],
            'audio': dict(self.audio_info),
            'pairing': dict(self.pairing),
            'control': self.control_allowed(local) and session is not None and self.hub.input == 'android',
            'control_note': self.control_note,
            'sessions': self.sessions, 'errors': self.errors,
            'frames': session.frames if session else 0,
        }

    def log_lines(self):
        return [{'t': t, 'line': line} for t, line in self.log]


def _target(host, port):
    """Validated (address, 'host:port') from user input; never passes text to adb unchecked."""
    try:
        address = ipaddress.ip_address(str(host).strip())
    except ValueError:
        raise AdbError('Enter the phone\'s IP address, for example 192.168.1.23.')
    if type(port) is not int or not 1 <= port <= 65535:
        raise AdbError('Enter the port shown on the phone (a number up to 65535).')
    return address, (f'[{address}]:{port}' if address.version == 6 else f'{address}:{port}')


def _point(event, width, height):
    x, y = event.get('x'), event.get('y')
    if not all(isinstance(v, (int, float)) for v in (x, y)):
        x = y = 0.5
    return (int(min(max(x, 0.0), 1.0) * (width - 1)), int(min(max(y, 0.0), 1.0) * (height - 1)))


def qr_svg(payload, scale=6):
    """A standalone SVG document. The page shows it as an <img> (data: URL), and an SVG image
    needs its namespace: segno's svg_inline() leaves it out (for SVG inside HTML), so the
    pairing code showed up as a broken image in every browser."""
    import io
    import segno
    out = io.BytesIO()
    segno.make(payload, error='m').save(out, kind='svg', scale=scale, border=2, dark='#000', light='#fff',
                                        xmldecl=False, svgns=True, nl=False)
    return out.getvalue().decode('utf-8')
