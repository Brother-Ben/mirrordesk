"""Persistent, validated settings editable from the browser."""
from dataclasses import dataclass, asdict, fields
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading

RESOLUTIONS = {
    # UxPlay: the requested HEIGHT controls the stream; width follows the phone's
    # aspect ratio (portrait iPhone at 1080 -> ~498x1080, landscape -> 1920x~886).
    '720p': (1280, 720),
    '1080p': (1920, 1080),
    '1440p': (2560, 1440),   # experimental: client may cap H.264 at 1080p
}
FPS_CHOICES = (30, 60)
VIDEO_MODES = ('auto', 'passthrough', 'compat')
AV_SYNC_MODES = ('low_latency', 'synced')
COMPAT_HEIGHTS = (480, 720, 1080)
# Android (androidsource.py): longest screen side sent by the phone (0 = the
# phone's own size), encoder bit rate in Mbit/s, frame-rate cap, and who may
# control the phone from the browser.
ANDROID_SIZES = (0, 2560, 1920, 1600, 1280, 1024)
ANDROID_BIT_RATES = (2, 4, 8, 12, 16, 24)
ANDROID_CONTROL = ('off', 'local', 'all')
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'()-]{0,39}$")
_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def new_pin():
    return f'{secrets.randbelow(10000):04d}'


def new_access_code():
    return ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


@dataclass
class Settings:
    receiver_name: str = 'MirrorDesk Web'
    resolution: str = '1080p'
    fps: int = 60
    pin: str = ''
    autostart_receiver: bool = True
    video_mode: str = 'auto'
    max_viewers: int = 4
    audio_enabled: bool = True
    av_sync: str = 'low_latency'
    compat_max_height: int = 1080
    access_code: str = ''          # viewer code: other devices may only watch
    owner_code: str = ''           # full control from other devices (security.py)
    require_code_on_localhost: bool = False
    airplay_ports: str = ''        # '' = UxPlay default; else "n" or "n1,n2,n3"
    usb_enabled: bool = True       # mirror a phone plugged in by cable (usbsource.py)
    android_enabled: bool = True   # Android phones over USB debugging / Wireless debugging (androidsource.py)
    android_max_size: int = 1920
    android_bit_rate: int = 8
    android_fps: int = 60
    android_control: str = 'local'  # tap/type on the phone from: nobody, this computer, every signed-in device
    android_audio_on_phone: bool = False   # Android 13+: keep the sound playing on the phone too
    confirm_quit: bool = True      # "Quit MirrorDesk?" first (its "Don't ask me again" turns it off)
    uxplay_path: str = ''
    msys2_root: str = ''

    # Fields the browser may change, with validators.
    EDITABLE = ('receiver_name', 'resolution', 'fps', 'autostart_receiver', 'video_mode', 'max_viewers',
                'audio_enabled', 'av_sync', 'compat_max_height', 'require_code_on_localhost',
                'airplay_ports', 'usb_enabled', 'android_enabled', 'android_max_size', 'android_bit_rate',
                'android_fps', 'android_control', 'android_audio_on_phone', 'confirm_quit')
    # Changing these restarts a running Android session.
    ANDROID_FIELDS = ('android_max_size', 'android_bit_rate', 'android_fps', 'android_audio_on_phone',
                      'audio_enabled')
    # Changing these requires a receiver restart to take effect.
    RECEIVER_FIELDS = ('receiver_name', 'resolution', 'fps', 'audio_enabled', 'av_sync',
                       'airplay_ports', 'pin')

    def public(self, include_secrets=False):
        data = asdict(self)
        if not include_secrets:
            data.pop('access_code', None)
            data.pop('owner_code', None)
            data.pop('pin', None)
        data.pop('uxplay_path', None)
        data.pop('msys2_root', None)
        return data

    @property
    def size(self):
        return RESOLUTIONS[self.resolution]


def validate_update(update):
    """Return a cleaned dict or raise ValueError with a user-facing message."""
    if not isinstance(update, dict) or not update:
        raise ValueError('Expected a JSON object with settings to change.')
    clean = {}
    for key, value in update.items():
        if key not in Settings.EDITABLE:
            raise ValueError(f'Setting "{key}" cannot be changed from the browser.')
        if not isinstance(value, (str, int, bool)):
            raise ValueError(f'"{key}" has an invalid type.')  # lists/dicts are unhashable in the lookups below
        if key == 'receiver_name':
            if not isinstance(value, str) or not _NAME.match(value.strip()):
                raise ValueError('Receiver name: 1-40 letters, digits, spaces or . _ \' ( ) -, not starting with a symbol.')
            value = value.strip()
        elif key == 'resolution':
            if value not in RESOLUTIONS:
                raise ValueError(f'Resolution must be one of {", ".join(RESOLUTIONS)}.')
        elif key == 'fps':
            if value not in FPS_CHOICES or type(value) is not int:
                raise ValueError('Frame rate must be 30 or 60.')
        elif key == 'video_mode':
            if value not in VIDEO_MODES:
                raise ValueError('Video mode must be auto, passthrough or compat.')
        elif key == 'av_sync':
            if value not in AV_SYNC_MODES:
                raise ValueError('A/V sync must be low_latency or synced.')
        elif key == 'max_viewers':
            if type(value) is not int or not 1 <= value <= 16:
                raise ValueError('Max viewers must be between 1 and 16.')
        elif key == 'compat_max_height':
            if value not in COMPAT_HEIGHTS or type(value) is not int:
                raise ValueError('Compatibility height must be 480, 720 or 1080.')
        elif key == 'airplay_ports':
            if not isinstance(value, str) or not parse_ports(value, check_only=True):
                raise ValueError('AirPlay ports: empty, one port (1024-65533) or three comma-separated ports.')
            value = value.strip()
        elif key == 'android_max_size':
            if type(value) is not int or value not in ANDROID_SIZES:
                raise ValueError('Android size must be one of ' + ', '.join(str(s) for s in ANDROID_SIZES) + '.')
        elif key == 'android_bit_rate':
            if type(value) is not int or value not in ANDROID_BIT_RATES:
                raise ValueError('Android bit rate must be one of ' + ', '.join(str(s) for s in ANDROID_BIT_RATES) + ' Mbit/s.')
        elif key == 'android_fps':
            if type(value) is not int or value not in FPS_CHOICES:
                raise ValueError('Android frame rate must be 30 or 60.')
        elif key == 'android_control':
            if value not in ANDROID_CONTROL:
                raise ValueError('Phone control must be off, local or all.')
        else:  # booleans
            if type(value) is not bool:
                raise ValueError(f'"{key}" must be true or false.')
        clean[key] = value
    return clean


def parse_ports(text, check_only=False):
    text = (text or '').strip()
    if not text:
        return True if check_only else []
    parts = [p.strip() for p in text.split(',')]
    if len(parts) not in (1, 3) or not all(p.isdigit() for p in parts):
        return False if check_only else None
    ports = [int(p) for p in parts]
    if len(ports) == 1:
        ports = [ports[0], ports[0] + 1, ports[0] + 2]
    if not all(1024 <= p <= 65535 for p in ports) or len(set(ports)) != 3:
        return False if check_only else None
    return True if check_only else ports


class SettingsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.access_code_pinned = False
        self.owner_code_pinned = False
        self.settings = self._load()

    def _load(self):
        settings = Settings()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raw = {}
        known = {f.name for f in fields(Settings)}
        for key, value in (raw.items() if isinstance(raw, dict) else ()):
            if key not in known:
                continue
            try:
                if key in Settings.EDITABLE:
                    value = validate_update({key: value})[key]
                elif key == 'pin' and not (isinstance(value, str) and re.fullmatch(r'\d{4}', value)):
                    continue
                elif key in ('access_code', 'owner_code') and not (
                        isinstance(value, str) and re.fullmatch(r'[A-Z0-9]{6,32}', value)):
                    continue
                elif key in ('uxplay_path', 'msys2_root') and not isinstance(value, str):
                    continue
            except (ValueError, TypeError):
                continue
            setattr(settings, key, value)
        changed = False
        if not settings.pin:
            settings.pin, changed = new_pin(), True
        if not settings.access_code:
            settings.access_code, changed = new_access_code(), True
        env_code = (os.environ.get('MIRRORDESK_ACCESS_CODE') or '').strip().upper()
        if re.fullmatch(r'[A-Z0-9]{6,32}', env_code):
            # Pinned by the deployment: shown as such and never "rotated" in the
            # UI, because a rotation would silently revert on the next restart.
            settings.access_code = env_code
            self.access_code_pinned = True
        env_owner = (os.environ.get('MIRRORDESK_OWNER_CODE') or '').strip().upper()
        if re.fullmatch(r'[A-Z0-9]{6,32}', env_owner) and env_owner != settings.access_code:
            settings.owner_code = env_owner
            self.owner_code_pinned = True
        # Installs from before owner codes get a new one here; their access code
        # (which everyone signed in with) becomes the watch-only viewer code.
        while not settings.owner_code or settings.owner_code == settings.access_code:
            settings.owner_code, changed = new_access_code(), True
        self.settings = settings
        if changed:
            self.save()
        return settings

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(asdict(self.settings), indent=2)
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.settings-', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(data)
                os.replace(temporary, self.path)
            except OSError:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise

    def update(self, clean):
        """Apply validated values; return the set of keys whose value changed."""
        changed = set()
        for key, value in clean.items():
            if getattr(self.settings, key) != value:
                setattr(self.settings, key, value)
                changed.add(key)
        if changed:
            self.save()
        return changed

    def regenerate_pin(self):
        self.settings.pin = new_pin()
        self.save()
        return self.settings.pin

    def regenerate_access_code(self):
        if self.access_code_pinned:
            raise ValueError('The viewer code is set by MIRRORDESK_ACCESS_CODE; change it there and restart.')
        code = new_access_code()
        while code == self.settings.owner_code:
            code = new_access_code()
        self.settings.access_code = code
        self.save()
        return code

    def regenerate_owner_code(self):
        if self.owner_code_pinned:
            raise ValueError('The owner code is set by MIRRORDESK_OWNER_CODE; change it there and restart.')
        code = new_access_code()
        while code == self.settings.access_code:
            code = new_access_code()
        self.settings.owner_code = code
        self.save()
        return code
