"""UxPlay supervision: platform setup, arguments, lifecycle and output parsing.

The browser controls the receiver through this module, so no console window
is needed for the PIN, start/stop or troubleshooting. Raw receiver output is
kept only in a bounded in-memory ring for authenticated users; it is never
written to diagnostics (it can contain device names and IDs).
"""
import asyncio
from collections import deque
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from .settings import parse_ports

ROOT = Path(__file__).resolve().parents[1]
# Developer machines: the UCRT64 build inside MSYS2. Installed copies: the
# self-contained folder made by tools/build_uxplay_bundle.py (no MSYS2 needed).
WINDOWS_DEFAULT_UXPLAY = rf'C:\msys64\home\{os.environ.get("USERNAME", "")}\mirrordesk-uxplay\build\uxplay.exe'
WINDOWS_DEFAULT_MSYS2 = r'C:\msys64'
BUNDLE_DIR = Path(os.environ.get('MIRRORDESK_UXPLAY_BUNDLE') or ROOT / 'uxplay')
BUNDLE_ELEMENTS = ('appsrc', 'h264parse', 'rtph264pay', 'rtpL16pay', 'udpsink', 'audioconvert', 'audioresample',
                   'volume', 'avdec_aac', 'avdec_alac')
BUNDLE_FIRST_PROBE_SECONDS = 120   # first start scans the plugin registry
PIDFILE = 'uxplay.pid'             # macOS/Linux: the running receiver, for clean-up after a crash
HOMEBREW_GSTDBUF = ('/opt/homebrew/bin/gstdbuf', '/usr/local/bin/gstdbuf') if sys.platform == 'darwin' else ()
LOG_LINES = 300
RESTART_BACKOFF = (1, 2, 4, 8, 16)
FAILURE_WINDOW = 120
FAILURE_LIMIT = 5
STOP_TIMEOUT = 5
_CONNECT = re.compile(r'connection request from (.{1,80}?) \((.{0,60}?)\) with deviceID')
# UxPlay's mirror thread logs this when the phone stops mirroring (Stop Mirroring in Control
# Center sends TEARDOWN; iOS 17+ without stream types), and when MirrorDesk ends it.
_MIRROR_STOPPED = 'raop_rtp_mirror->running is no longer true'
_LOST = 'lost connection with client'
# MirrorDesk's UxPlay change (third_party/uxplay/mirrordesk) reports the phone's FLUSH: it paused,
# seeked or started another video. The sound viewers still have queued is stale from then on.
_AUDIO_FLUSH = 'MirrorDesk: audio flush'
_DISCOVERY_FAIL = ('dnssd_register_raop failed', 'dnssd_register_airplay failed',
                   'could not initialize dnssd', 'no dns-sd server found')
# Every AirPlay session has its own code. A session lasts while MirrorDesk runs: phones that entered
# its code may disconnect and come back without it. It ends when MirrorDesk starts (after Quit or a
# restart), when the owner asks for a new code, or when no phone has been connected for
# SESSION_IDLE_SECONDS (a computer that never quits, like a ZimaOS box, still gets a new code the
# next day); then UxPlay starts again with a new code, a new key and an empty register (-reg: only
# phones that entered this session's code may skip it), so every phone has to enter the new code.
# (0.12.0 ended a session 30 s after the phone left: stopping mirroring for a moment meant a new code.)
SESSION_IDLE_SECONDS = 4 * 3600
SESSION_CHECK_SECONDS = 30
KEY_FILE = 'uxplay-key.pem'
REGISTER_FILE = 'uxplay-register.txt'
_VERSION = re.compile(r'UxPlay(?: version)? (\d+\.\d+(?:\.\d+)?)')


def build_args(settings, video_port, audio_port, key_path=None, register_path=None):
    width, height = settings.size
    sync = 'true' if settings.av_sync == 'synced' else 'false'
    args = ['-n', settings.receiver_name, '-nh',
            '-s', f'{width}x{height}@{settings.fps}', '-fps', str(settings.fps),
            '-vs', 'fakesink',
            '-vrtp', f'pt=96 config-interval=1 ! udpsink host=127.0.0.1 port={video_port} sync={sync} async=false']
    if settings.audio_enabled:
        args += ['-as', 'fakesink',
                 '-artp', f'pt=97 ! udpsink host=127.0.0.1 port={audio_port} sync={sync} async=false']
    else:
        args += ['-as', '0']
    args += ['-pin', settings.pin]
    if key_path is not None:
        args += ['-key', str(key_path)]
    if register_path is not None:
        args += ['-reg', str(register_path)]
    # A phone that connects takes over from one that is still connected: the same phone coming back
    # after its connection dropped is not refused until UxPlay notices. It needs this session's code.
    args.append('-nohold')
    ports = parse_ports(settings.airplay_ports)
    if ports:
        args += ['-p', ','.join(str(p) for p in ports)]
    return args


def line_buffered(command, os_name=os.name, which=shutil.which):
    """Prefix stdbuf on Linux and macOS (gstdbuf from Homebrew's coreutils): into a pipe,
    UxPlay's log is block-buffered, so lines such as a phone connecting or a rejected
    AirPlay code would only arrive when UxPlay exits."""
    stdbuf = None
    if os_name != 'nt':
        # MirrorDesk.app starts MirrorDesk with macOS's minimal PATH, without Homebrew's folder.
        stdbuf = which('stdbuf') or which('gstdbuf') or next(
            (path for path in HOMEBREW_GSTDBUF if Path(path).is_file()), None)
    return [stdbuf, '-oL', '-eL', *command] if stdbuf else list(command)


def help_capabilities(text):
    version = _VERSION.search(text)
    return {
        'version': version.group(1) if version else None,
        'vrtp': '-vrtp' in text,
        'artp': '-artp' in text,
        'pin': re.search(r'^\s*-pin(?=\s|\[|$)', text, re.MULTILINE) is not None,
        'h265': '-h265' in text,
        'key': re.search(r'^\s*-key\b', text, re.MULTILINE) is not None,
        'reg': re.search(r'^\s*-reg\b', text, re.MULTILINE) is not None,
    }


class ProcessContainment:
    """Kill receiver children if the server dies (Windows job / Linux pdeathsig)."""

    def __init__(self):
        self.job = None
        if os.name == 'nt':
            try:
                self.job = self._create_job()
            except OSError:
                self.job = None

    @staticmethod
    def _create_job():
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimit), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError('CreateJobObject failed')
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError('SetInformationJobObject failed')
        return (kernel32, job)

    def adopt(self, process):
        if self.job is not None:
            kernel32, job = self.job
            try:
                kernel32.AssignProcessToJobObject(job, int(process._handle))
            except (OSError, AttributeError):
                pass

    @staticmethod
    def preexec():
        if sys.platform.startswith('linux'):
            def child():
                try:
                    import ctypes
                    ctypes.CDLL('libc.so.6', use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
                except OSError:
                    pass
            return child
        return None


class ReceiverSupervisor:
    """Owns at most one UxPlay process and restarts it after crashes."""

    def __init__(self, store, data_dir, hub, diagnostics=None, command_override=None):
        self.store = store
        self.data_dir = Path(data_dir)
        self.hub = hub
        self.diagnostics = diagnostics
        self.command_override = command_override if command_override is not None else _env_command()
        self.containment = ProcessContainment()
        self.state = 'stopped'
        self.message = 'Wireless mirroring is not started.'
        self.capabilities = {}
        self.process = None
        self.want_running = False
        self.log = deque(maxlen=LOG_LINES)
        self.failures = deque(maxlen=FAILURE_LIMIT)
        self.phone = {'state': 'idle', 'name': None, 'model': None, 'since': None}
        self.discovery_error = None
        self.started_at = None
        self.restarts = 0
        self.loop = None
        self._task = None
        self._lock = asyncio.Lock()
        self._env = None
        self._cwd = None
        self._prepared_for = None
        self._stop_generation = 0
        self.new_identity = True       # the next start begins a session: new key, empty register
        self.session_used = False      # a phone connected in this session
        self.unused_since = None       # since when no phone is connected (session in use)

    # -- discovery of the executable --------------------------------------
    def executable(self):
        if self.command_override:
            return self.command_override
        settings = self.store.settings
        path = settings.uxplay_path or os.environ.get('MIRRORDESK_UXPLAY')
        # Installed copies: Windows' self-contained folder, or (macOS, Linux) the UxPlay the
        # installer built from the bundled source into the same place.
        if not path and self._bundled().is_file():
            path = self._bundled()
        if not path:
            path = WINDOWS_DEFAULT_UXPLAY if os.name == 'nt' else shutil.which('uxplay')
        if not path or not Path(path).is_file():
            return None
        return [str(path)]

    @staticmethod
    def _bundled():
        return BUNDLE_DIR / 'bin' / ('uxplay.exe' if os.name == 'nt' else 'uxplay')

    def platform(self):
        if self.command_override:
            return 'custom'
        command = self.executable()
        bundled = bool(command) and Path(command[0]).resolve() == self._bundled().resolve()
        if os.name != 'nt':
            return 'posix-bundle' if bundled else 'posix'
        return 'windows-bundle' if bundled else 'windows-msys2'

    async def _prepare(self, command):
        """Return (env, cwd) after a capability probe; raise RuntimeError on failure."""
        home = self.data_dir / 'home'
        home.mkdir(parents=True, exist_ok=True)
        platform = self.platform()
        if platform == 'windows-msys2':
            import launcher  # existing, reviewed Windows UCRT64 preflight
            msys = self.store.settings.msys2_root or os.environ.get('MIRRORDESK_MSYS2', WINDOWS_DEFAULT_MSYS2)
            env = await asyncio.to_thread(launcher.preflight, command[0], msys, ROOT, self.diagnostics)
            cwd = ROOT / '.runtime' / 'home'
        elif platform == 'windows-bundle':
            env = bundle_env(os.environ, BUNDLE_DIR, self.data_dir)
            cwd = home
            await asyncio.to_thread(_check_bundle, env, cwd)
        else:
            env = os.environ.copy()
            env['HOME'] = str(home)
            env.pop('UXPLAYRC', None)
            cwd = home
        cwd = _safe_cwd(cwd)
        probe = await asyncio.to_thread(_run_probe, command + ['-h'], env, cwd)
        caps = help_capabilities(probe)
        self.capabilities = caps
        if not (caps['vrtp'] and caps['artp']):
            raise RuntimeError('This UxPlay build lacks -vrtp/-artp forwarding (needs UxPlay 1.73 or newer).')
        if not caps['pin']:
            raise RuntimeError('This UxPlay build lacks -pin; refusing to run an unprotected receiver.')
        return env, cwd

    # -- lifecycle -----------------------------------------------------------
    async def start(self, expect_stop_generation=None):
        self.loop = asyncio.get_running_loop()
        async with self._lock:
            if expect_stop_generation is not None and expect_stop_generation != self._stop_generation:
                return  # a newer explicit Stop arrived while this restart was pending
            self.want_running = True
            if self.process is not None and self.process.poll() is None:
                return
            command = self.executable()
            if command is None:
                self._set('unavailable', 'UxPlay (the wireless receiver) was not found. Run the MirrorDesk '
                                         'installer again, or set its path in MIRRORDESK_UXPLAY (UxPlay 1.74+).')
                self.want_running = False
                return
            if self._env is None or self._prepared_for != command:
                self._set('checking', 'Getting wireless mirroring ready (the first start can take up to 2 minutes)…')
                try:
                    self._env, self._cwd = await self._prepare(command)
                    self._prepared_for = command
                except (RuntimeError, OSError) as exc:
                    self._env = None
                    self._set('failed', str(exc))
                    self.want_running = False
                    return
            await self._spawn(command)

    def _forget_phones(self):
        """A new receiver key and an empty register: phones paired before must enter the code."""
        for name in (KEY_FILE, REGISTER_FILE):
            try:
                (self.data_dir / name).unlink()
            except FileNotFoundError:
                pass
        self.new_identity = False
        self.session_used = False
        self.unused_since = None

    async def _spawn(self, command):
        settings = self.store.settings
        if self.new_identity:
            self._forget_phones()     # MirrorDesk just started: a new session
        key_path = self.data_dir / KEY_FILE if self.capabilities.get('key') else None
        register_path = self.data_dir / REGISTER_FILE if self.capabilities.get('reg') else None
        args = line_buffered(command) + build_args(settings, self.hub.video_port, self.hub.audio_port, key_path,
                                                   register_path)
        flags = 0
        if os.name == 'nt':
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        else:
            await asyncio.to_thread(reap_orphan, self.data_dir / PIDFILE)
        try:
            self.process = subprocess.Popen(
                args, cwd=self._cwd, env=self._env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=flags, preexec_fn=ProcessContainment.preexec(),
                start_new_session=(os.name != 'nt'))
        except OSError as exc:
            self._set('failed', f'Could not start UxPlay: {exc}')
            self.want_running = False
            return
        self.containment.adopt(self.process)
        if os.name != 'nt':
            _write_pidfile(self.data_dir / PIDFILE, self.process.pid)
        self.started_at = time.monotonic()
        self.discovery_error = None
        self._record('process_start', component='receiver')
        self._set('running', f'Phones on this network can find "{settings.receiver_name}".')
        process = self.process
        threading.Thread(target=self._reader, args=(process,), name='mirrordesk-receiver-output', daemon=True).start()
        self._task = asyncio.create_task(self._watch(process))

    async def _watch(self, process):
        code = await asyncio.to_thread(process.wait)
        if process is not self.process:
            return
        if os.name != 'nt':
            try:
                (self.data_dir / PIDFILE).unlink()
            except OSError:
                pass
        self._record('process_exit', component='receiver', exit_code=code)
        self.phone.update(state='idle', name=None, model=None, since=None)
        self.hub.end_session('airplay')
        if not self.want_running:
            self._set('stopped', 'Wireless mirroring is stopped.')
            return
        now = time.monotonic()
        self.failures.append(now)
        recent = [t for t in self.failures if now - t < FAILURE_WINDOW]
        if len(recent) >= FAILURE_LIMIT:
            self.want_running = False
            self._set('failed', f'UxPlay exited {len(recent)} times in {FAILURE_WINDOW}s (last code {code}). '
                                'See the receiver log on the Developer page.')
            return
        delay = RESTART_BACKOFF[min(len(recent) - 1, len(RESTART_BACKOFF) - 1)]
        self._set('restarting', f'UxPlay exited (code {code}); restarting in {delay}s.')
        self.restarts += 1
        await asyncio.sleep(delay)
        async with self._lock:
            if self.want_running and process is self.process:
                command = self.executable()
                if command is None:
                    self._set('unavailable', 'UxPlay executable disappeared.')
                    return
                await self._spawn(command)

    async def stop(self):
        async with self._lock:
            self._stop_generation += 1
            self.want_running = False
            process = self.process
            if process is None or process.poll() is not None:
                self._set('stopped', 'Wireless mirroring is stopped.')
                return
            self._set('stopping', 'Stopping wireless mirroring…')
            await asyncio.to_thread(_terminate, process)
            self.phone.update(state='idle', since=time.time())
            self.hub.end_session('airplay')
            self._set('stopped', 'Wireless mirroring is stopped.')

    async def restart(self):
        await self.stop()
        generation = self._stop_generation
        self.failures.clear()
        await self.start(expect_stop_generation=generation)

    async def disconnect_phone(self):
        """End the phone's mirroring from here. UxPlay cannot drop one client, so it restarts
        (about a second) with the same code and key: the session goes on, and the phone may come
        back without the code. A new code (new_session) is what locks every phone out."""
        self._record('receiver_signal', component='receiver', signal='kicked')
        if self.want_running:
            await self.restart()

    async def new_session(self):
        """End the AirPlay session now: a new code, a new receiver key and an empty register, so
        every phone has to enter the new code (also one that was trusted a moment ago)."""
        was_running = self.want_running
        await self.stop()
        self._forget_phones()
        self.store.regenerate_pin()
        self._record('receiver_signal', component='receiver', signal='new_session')
        if was_running:
            self.failures.clear()
            await self.start(expect_stop_generation=self._stop_generation)

    async def watch_sessions(self):
        """End a session once no phone has been connected for SESSION_IDLE_SECONDS."""
        while True:
            await asyncio.sleep(SESSION_CHECK_SECONDS)
            if not self.session_used or self.state != 'running' or self.phone_state() != 'idle':
                self.unused_since = None
                continue
            now = time.monotonic()
            if self.unused_since is None:
                self.unused_since = now
            elif now - self.unused_since >= SESSION_IDLE_SECONDS:
                self.unused_since = None
                await self.new_session()

    async def close(self):
        await self.stop()
        if self._task is not None:
            self._task.cancel()

    # -- output ----------------------------------------------------------------
    def _reader(self, process):
        stream = process.stdout
        try:
            while True:
                chunk = stream.readline(2048)
                if not chunk:
                    break
                text = chunk.decode('utf-8', 'replace').rstrip()
                if self.loop is not None and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self.on_line, text)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def on_line(self, text):
        if _AUDIO_FLUSH in text:
            self.hub.audio_flush()
            return
        self.log.append((time.time(), text))
        lowered = text.lower()
        match = _CONNECT.search(text)
        if match:
            self.phone.update(state='connecting', name=match.group(1).strip(), model=match.group(2).strip(),
                              since=time.time())
            self.session_used = True
            self._record('receiver_signal', component='receiver', signal='connection')
        elif _MIRROR_STOPPED in text or _LOST in lowered:
            # The phone is gone: pages go back to "Ready to mirror" now, not after the
            # minute a still screen is allowed to stay silent.
            self.phone.update(state='idle', since=time.time())
            self.hub.end_session('airplay')
            self._record('receiver_signal', component='receiver',
                         signal='mirror_stop' if _MIRROR_STOPPED in text else 'disconnect')
        elif any(token in lowered for token in _DISCOVERY_FAIL):
            self.discovery_error = text[:200]
            self._record('receiver_signal', component='receiver', signal='error')
        elif 'blocked client' in lowered:
            self._record('receiver_signal', component='receiver', signal='warning')

    # -- status --------------------------------------------------------------------
    def _set(self, state, message):
        self.state = state
        self.message = message

    def _record(self, event, **fields):
        if self.diagnostics is not None:
            try:
                self.diagnostics.record(event, **fields)
            except ValueError:
                pass

    def phone_state(self):
        source = self.hub.source_state()
        if self.state != 'running' and source != 'streaming':
            return 'idle'  # no receiver, no phone session: never report a stale 'paused'
        if source == 'streaming':
            return 'mirroring'
        if source in ('paused', 'audio_only'):
            return source
        if self.phone['state'] == 'connecting' and self.phone['since'] and time.time() - self.phone['since'] < 60:
            return 'connecting'
        return 'idle'

    def status(self, include_private=False):
        settings = self.store.settings
        data = {
            'state': self.state,
            'message': self.message,
            'name': settings.receiver_name,
            'platform': self.platform(),
            'version': self.capabilities.get('version'),
            'h265_capable': bool(self.capabilities.get('h265')),
            'restarts': self.restarts,
            'uptime_s': round(time.monotonic() - self.started_at) if self.started_at and self.state == 'running' else None,
            'phone': self.phone_state(),
            'discovery_error': self.discovery_error,
            'resolution': settings.resolution,
            'fps': settings.fps,
        }
        if include_private:
            data['pin'] = settings.pin
            data['phone_name'] = self.phone['name'] if data['phone'] != 'idle' else None
            data['phone_model'] = self.phone['model'] if data['phone'] != 'idle' else None
        return data

    def log_lines(self, limit=200):
        return [{'t': t, 'line': line} for t, line in list(self.log)[-limit:]]


def _env_command():
    raw = os.environ.get('MIRRORDESK_UXPLAY_CMD')
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = shlex.split(raw, posix=(os.name != 'nt'))
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return value
    return None


def bundle_env(original, bundle, data_dir):
    """Environment for the self-contained UxPlay folder: its own DLLs first, only its
    GStreamer plugins, a registry in the data folder, and an isolated HOME (UxPlay
    reads ~/.uxplayrc; the user's own settings must never change its behaviour)."""
    bundle, data_dir = Path(bundle), Path(data_dir)
    env = {k: v for k, v in original.items() if not k.upper().startswith('GST_')}
    system = os.path.join(original.get('SystemRoot', r'C:\Windows'), 'System32')
    env['PATH'] = os.pathsep.join([str(bundle / 'bin'), system, original.get('PATH', '')])
    env['GST_PLUGIN_SYSTEM_PATH'] = str(bundle / 'lib' / 'gstreamer-1.0')
    env['GST_PLUGIN_PATH'] = env['GST_PLUGIN_SYSTEM_PATH']
    env['GST_PLUGIN_SCANNER'] = str(bundle / 'bin' / 'gst-plugin-scanner.exe')
    env['GST_REGISTRY'] = str(data_dir / 'gstreamer-registry.bin')
    env['HOME'] = env['USERPROFILE'] = str(data_dir / 'home')
    for name in ('UXPLAYRC', 'XDG_CONFIG_HOMEDIR'):
        env.pop(name, None)
    return env


def _check_bundle(env, cwd):
    """Every GStreamer element MirrorDesk's pipelines need must load from the bundle.
    The first check builds the plugin registry (can take a while on a slow disk)."""
    inspect = Path(env['GST_PLUGIN_SCANNER']).with_name('gst-inspect-1.0.exe')
    if not inspect.is_file():
        return   # older bundle without the inspector: UxPlay reports missing elements itself
    missing = []
    for index, element in enumerate(BUNDLE_ELEMENTS):
        timeout = BUNDLE_FIRST_PROBE_SECONDS if index == 0 else 30
        try:
            completed = subprocess.run([str(inspect), element], env=env, cwd=cwd, capture_output=True, timeout=timeout,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                       stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('Checking the bundled wireless receiver took too long. Restart MirrorDesk.') from exc
        except OSError as exc:
            raise RuntimeError(f'The bundled wireless receiver could not start: {exc}') from exc
        if completed.returncode != 0:
            missing.append(element)
    if missing:
        raise RuntimeError('The bundled wireless receiver is incomplete (missing ' + ', '.join(missing)
                           + '). Reinstall MirrorDesk.')


def _safe_cwd(path):
    """CreateProcess rejects working directories longer than MAX_PATH (260).

    HOME already scopes UxPlay's files and the key path is absolute, so a deep
    project folder falls back to the system temp directory instead of failing.
    """
    if os.name == 'nt' and len(str(path)) >= 240:
        import tempfile
        return Path(tempfile.gettempdir())
    return path


def _run_probe(command, env, cwd, timeout=15):
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        completed = subprocess.run(command, env=env, cwd=cwd, capture_output=True, timeout=timeout,
                                   creationflags=flags, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'UxPlay capability probe timed out after {timeout}s.') from exc
    except OSError as exc:
        raise RuntimeError(f'UxPlay could not start: {exc}') from exc
    return (completed.stdout + completed.stderr).decode('utf-8', 'replace')


def _write_pidfile(path, pid):
    try:
        Path(path).write_text(f'{pid}\n', encoding='ascii')
    except OSError:
        pass


def reap_orphan(path, command_name=None, kill=os.kill):
    """macOS has no Linux-style "die with the parent": a UxPlay left running by a MirrorDesk
    that crashed would keep the AirPlay name and ports. Stop it, but only when the recorded
    process really is a uxplay (PIDs are reused)."""
    path = Path(path)
    try:
        pid = int(path.read_text(encoding='ascii').strip())
    except (OSError, ValueError):
        return False
    try:
        path.unlink()
    except OSError:
        pass
    name = (command_name or _command_name)(pid)
    if not name or Path(name).name != 'uxplay':
        return False
    try:
        kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline and (command_name or _command_name)(pid):
            time.sleep(0.2)
        if (command_name or _command_name)(pid):
            kill(pid, getattr(signal, 'SIGKILL', signal.SIGTERM))
    except OSError:
        pass
    return True


def _command_name(pid):
    try:
        text = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='], capture_output=True, text=True,
                              timeout=10, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return text or None


def _terminate(process):
    # Windows: TerminateProcess (a CREATE_NO_WINDOW child has its own hidden
    # console, so console control events cannot reach it). POSIX: SIGTERM so
    # UxPlay can deregister its DNS-SD services before exiting.
    try:
        process.terminate()
        process.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass
