"""Stand-in for Google's adb plus a phone running scrcpy-server 5.0.1 (tests only).

Implements the adb commands MirrorDesk uses, with the output formats of real
adb 37.0.1, and plays the phone side of the scrcpy protocol (see
mirrordesk/androidsource.py) with synthetic libx264 video and a sine tone.

State lives in FAKE_ADB_STATE (a directory): devices.json (the phone list),
forwards.json, commands.log (every invocation) and control.log (hex control
messages received). Knobs: FAKE_ADB_PAIR_CODE, FAKE_ADB_MDNS (JSON list of
[name, type, address]), FAKE_SCRCPY_SIZE (WxH), FAKE_SCRCPY_PROFILE,
FAKE_SCRCPY_AUDIO=disabled, FAKE_SCRCPY_FAIL=encoder, FAKE_SCRCPY_ROTATE_AFTER (s).
MirrorDesk fixes adb's environment when it starts, so knobs that change during a
test are files in the state directory: pair-code.json, mdns.json, fail-encoder.
"""
import json
import math
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

STATE = Path(os.environ.get('FAKE_ADB_STATE') or Path(__file__).resolve().parent / '.fake-adb')
DEFAULT_DEVICES = [{'serial': 'FAKEPIXEL01', 'state': 'device', 'model': 'Pixel_8', 'product': 'shiba',
                    'release': '14', 'sdk': '34', 'manufacturer': 'Google'}]
FLAG_CONFIG, FLAG_KEY = 1 << 62, 1 << 61


def load(name, default):
    try:
        return json.loads((STATE / name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def save(name, value):
    STATE.mkdir(parents=True, exist_ok=True)
    temporary = STATE / f'.{name}.{os.getpid()}.tmp'
    temporary.write_text(json.dumps(value), encoding='utf-8')
    os.replace(temporary, STATE / name)


def devices():
    return load('devices.json', DEFAULT_DEVICES)


def find(serial):
    return next((d for d in devices() if d['serial'] == serial), None)


def out(text, code=0):
    sys.stdout.write(text + ('\n' if text and not text.endswith('\n') else ''))
    sys.stdout.flush()
    return code


# ------------------------------------------------------------------ phone side
def nal_units(annexb):
    from synthetic_media import nal_units as split
    return split(annexb)


def clip(width, height):
    from synthetic_media import encode_clip
    units = encode_clip(width, height, frames=60, fps=30, gop=30, profile=os.environ.get('FAKE_SCRCPY_PROFILE', 'baseline'))
    config, frames = [], []
    for unit in units:
        nals = nal_units(unit)
        if not config:
            config = [n for n in nals if n[0] & 0x1F in (7, 8)]
        body = [n for n in nals if n[0] & 0x1F not in (7, 8, 6)]   # like MediaCodec: parameter sets only in the config packet
        if body:
            frames.append((b''.join(b'\x00\x00\x00\x01' + n for n in body), any(n[0] & 0x1F == 5 for n in body)))
    return b''.join(b'\x00\x00\x00\x01' + n for n in config), frames


def media(pts, payload, config=False, key=False):
    flags = FLAG_CONFIG if config else (pts | (FLAG_KEY if key else 0))
    return struct.pack('>QI', flags, len(payload)) + payload


def scrcpy(serial, args):
    options = dict(a.split('=', 1) for a in args[1:] if '=' in a)
    if args[0] != '5.0.1':
        return out(f'ERROR: The server version (5.0.1) does not match the client ({args[0]})', 1)
    if os.environ.get('FAKE_SCRCPY_FAIL') == 'encoder' or (STATE / 'fail-encoder').exists():
        out('INFO: Device: [Google] google Pixel 8 (Android 14)')
        return out('ERROR: Could not create default video encoder for h264', 1)
    scid = options.get('scid', '')
    port = load('forwards.json', {}).get(serial, {}).get(f'scrcpy_{scid}')
    if port is None:
        return out('ERROR: no forward for this server', 1)
    out('INFO: Device: [Google] google Pixel 8 (Android 14)')
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    time.sleep(float(os.environ.get('FAKE_SCRCPY_START_DELAY', '0.3')))   # the phone-side server takes a moment
    server.bind(('127.0.0.1', port))
    server.listen(4)
    server.settimeout(20)
    audio_on = options.get('audio') == 'true'
    video, _ = server.accept()
    video.sendall(b'\0')
    audio = server.accept()[0] if audio_on else None
    control, _ = server.accept()
    server.close()
    name = b'Pixel 8'
    video.sendall(name + b'\0' * (64 - len(name)))
    width, height = (int(v) for v in os.environ.get('FAKE_SCRCPY_SIZE', '360x800').split('x'))
    reset = threading.Event()
    stop = threading.Event()

    def control_reader():
        log = STATE / 'control.log'
        while not stop.is_set():
            try:
                data = control.recv(4096)
            except OSError:
                break
            if not data:
                break
            with log.open('a', encoding='utf-8') as stream:
                stream.write(data.hex() + '\n')
            if 17 in data[:1]:
                reset.set()
            if data[:1] == b'\x02' and (STATE / 'deny-inject').exists():   # like a Xiaomi without the extra setting
                out('ERROR: java.lang.SecurityException: Injecting input events requires the caller (or the '
                    'source of the instrumentation, if any) to have the INJECT_EVENTS permission.')
        stop.set()

    def audio_writer():
        if os.environ.get('FAKE_SCRCPY_AUDIO') == 'disabled':
            audio.sendall(b'\0\0\0\0')
            out('WARN: Audio disabled: it is not supported before Android 11')
            return
        audio.sendall(struct.pack('>I', 0x00726177))
        start, sent = time.monotonic(), 0
        while not stop.is_set():
            t = (sent + __import__('numpy').arange(1024)) / 48000
            tone = (__import__('numpy').sin(2 * math.pi * 440 * t) * 6000).astype('<i2')
            payload = __import__('numpy').repeat(tone, 2).tobytes()
            try:
                audio.sendall(media(sent * 1_000_000 // 48000, payload))
            except OSError:
                break
            sent += 1024
            delay = start + sent / 48000 - time.monotonic()
            if delay > 0:
                stop.wait(delay)

    threading.Thread(target=control_reader, daemon=True).start()
    if audio is not None:
        threading.Thread(target=audio_writer, daemon=True).start()
    rotate_after = float(os.environ.get('FAKE_SCRCPY_ROTATE_AFTER', '0') or 0)
    started = time.monotonic()
    pts = 0
    try:
        video.sendall(struct.pack('>I', 0x68323634))
        while not stop.is_set():
            config, frames = clip(width, height)
            video.sendall(struct.pack('>III', 0x80000000, width, height) + media(0, config, config=True))
            reset.clear()
            index = 0
            frame_start = time.monotonic()
            while not stop.is_set() and not reset.is_set():
                payload, key = frames[index % len(frames)]
                video.sendall(media(pts, payload, key=key))
                index += 1
                pts += 33_333
                if rotate_after and time.monotonic() - started > rotate_after:
                    rotate_after = 0
                    width, height = height, width
                    out('INFO: Display rotated')
                    break
                delay = frame_start + index / 30 - time.monotonic()
                if delay > 0:
                    stop.wait(delay)
    except OSError:
        pass
    stop.set()
    for sock in (video, audio, control):
        try:
            sock.close()
        except (OSError, AttributeError):
            pass
    return 0


# ------------------------------------------------------------------ adb commands
def main(argv):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'commands.log').open('a', encoding='utf-8') as log:
        log.write(json.dumps(argv) + '\n')
    serial = None
    if argv[:1] == ['-s']:
        serial, argv = argv[1], argv[2:]
    if not argv:
        return out('adb: usage', 1)
    command, args = argv[0], argv[1:]
    if command == 'version':
        return out('Android Debug Bridge version 1.0.41\nVersion 37.0.1-fake\nInstalled as fake_adb.py')
    if command in ('start-server', 'kill-server'):
        return 0
    if command == 'devices':
        lines = ['List of devices attached']
        for d in devices():
            extra = '' if d['state'] != 'device' else f" product:{d.get('product', 'x')} model:{d.get('model', 'x')} device:x"
            lines.append(f"{d['serial']:<22} {d['state']}{extra} transport_id:1")
        return out('\n'.join(lines) + '\n')
    if command == 'pair':
        target, code = args[0], args[1]
        expected = load('pair-code.json', None) or os.environ.get('FAKE_ADB_PAIR_CODE', '123456')
        if code != expected:
            return out('Failed: Wrong password or connection was dropped.', 1)
        return out(f'Successfully paired to {target} [guid=adb-FAKEWIFI-abc123]')
    if command == 'connect':
        target = args[0]
        if os.environ.get('FAKE_ADB_CONNECT') == 'refuse':
            return out(f'cannot connect to {target}: No connection could be made (10061)')
        listed = devices()
        if not any(d['serial'] == target for d in listed):
            listed.append({'serial': target, 'state': 'device', 'model': 'Pixel_8', 'product': 'shiba',
                           'release': '14', 'sdk': '34', 'manufacturer': 'Google'})
            save('devices.json', listed)
            return out(f'connected to {target}')
        return out(f'already connected to {target}')
    if command == 'disconnect':
        save('devices.json', [d for d in devices() if d['serial'] != args[0]])
        return out(f'disconnected {args[0]}')
    if command == 'mdns':
        lines = ['List of discovered mdns services']
        for name, kind, address in load('mdns.json', None) or json.loads(os.environ.get('FAKE_ADB_MDNS', '[]')):
            lines.append(f'{name}\t{kind}\t{address}')
        return out('\n'.join(lines))
    device = find(serial) if serial else (devices()[0] if devices() else None)
    if device is None:
        return out(f"adb.exe: device '{serial}' not found", 1)
    if device['state'] != 'device':
        return out(f"adb.exe: device {device['state']}", 1)
    if command == 'push':
        if not Path(args[0]).is_file():
            return out(f"adb: error: cannot stat '{args[0]}': No such file or directory", 1)
        return out(f'{args[0]}: 1 file pushed, 0 skipped. 50.0 MB/s (733930 bytes in 0.014s)')
    if command == 'forward':
        forwards = load('forwards.json', {})
        if args[0] == '--remove':
            port = int(args[1].split(':')[1])
            forwards[device['serial']] = {k: v for k, v in forwards.get(device['serial'], {}).items() if v != port}
            save('forwards.json', forwards)
            return 0
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        forwards.setdefault(device['serial'], {})[args[1].split(':', 1)[1]] = port
        save('forwards.json', forwards)
        return out(str(port))
    if command == 'shell':
        if 'app_process' in args:
            index = args.index('com.genymobile.scrcpy.Server')
            return scrcpy(device['serial'], args[index + 1:])
        script = ' '.join(args)
        values = {'ro.build.version.release': device.get('release', '14'), 'ro.build.version.sdk': device.get('sdk', '34'),
                  'ro.product.manufacturer': device.get('manufacturer', 'Google')}
        names = [part.split('getprop', 1)[1].strip() for part in script.split(';') if 'getprop' in part]
        return out('\n'.join(values.get(n, '') for n in names))
    return out(f'adb: unknown command {command}', 1)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
