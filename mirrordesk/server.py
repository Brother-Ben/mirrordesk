"""aiohttp application: browser UI, control API, WebRTC signalling, diagnostics."""
import asyncio
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import platform
import re
import secrets
import sys
import time

from aiohttp import web

from . import INSTALL_ID, __version__
from . import netinfo
from .androidsource import AdbError, AndroidSource
from .media import MediaHub
from .receiver import ReceiverSupervisor
from .security import COOKIE, OWNER, SESSION_SECONDS, VIEWER, AccessControl
from .settings import ANDROID_BIT_RATES, ANDROID_CONTROL, ANDROID_SIZES, RESOLUTIONS, SettingsStore, validate_update
from .turn import TurnRelay
from .usbsource import UsbSource
from .webrtc import ViewerLimit, ViewerManager

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(__file__).resolve().parent / 'web'
log = logging.getLogger('mirrordesk.server')
SAMPLE_SECONDS = 5
MAX_JSON = 256 * 1024
CONTROL_EVENTS_PER_SECOND = 400
QUIT_TOKEN_FILE = 'quit-token'
mimetypes.add_type('application/manifest+json', '.webmanifest')
mimetypes.add_type('image/svg+xml', '.svg')
mimetypes.add_type('text/javascript', '.js')


ASSET_LINK = re.compile(r'((?:src|href)=")(/app/[^"?#]+)(")')


def build_tag(web=WEB):
    """Changes whenever the page's files do (a new version, or an edit while developing)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(web).rglob('*') if p.is_file()):
        digest.update(path.relative_to(web).as_posix().encode())
        digest.update(path.read_bytes())
    return f'{__version__}-{digest.hexdigest()[:10]}'


def render_page(name, tag, web=WEB):
    """A page whose script and style links name this build, plus the build itself for app.js.
    A browser kept the old app.js after an update (no cache rule told it not to): the new page
    with the old script showed only the Mirror tab, a player bar and nothing that worked."""
    html = (Path(web) / name).read_text(encoding='utf-8')
    html = ASSET_LINK.sub(lambda m: f'{m.group(1)}{m.group(2)}?v={tag}{m.group(3)}', html)
    return html.replace('</head>', f'<meta name="mirrordesk-build" content="{tag}">\n</head>', 1)


def _diagnostics(data_dir):
    sys.path.insert(0, str(ROOT))
    try:
        from diagnostics import DiagnosticSession
        return DiagnosticSession.create(Path(data_dir) / 'diagnostics')
    except (ImportError, OSError) as exc:  # diagnostics must never stop the product
        log.warning('Diagnostics unavailable: %s', exc)
        return None


class LoopMonitor:
    """How late the event loop wakes up. The 20 ms audio clock and all packet sending run on
    it, so a computer too busy for the phone's video shows here first (as a USB iPhone's
    high-bitrate stream re-encoded for a browser did: choppy sound, then lagging video)."""
    PERIOD = 0.02
    STALL_MS = 50

    def __init__(self):
        self.max_ms = 0.0
        self.stalls = 0
        self.last = {'lag_max_ms': 0, 'stalls': 0}

    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            await asyncio.sleep(self.PERIOD)
            late = (loop.time() - start - self.PERIOD) * 1000
            self.max_ms = max(self.max_ms, late)
            if late > self.STALL_MS:
                self.stalls += 1

    def take(self):
        """The worst lateness and the stalls since the last call (one sample window)."""
        self.last = {'lag_max_ms': round(self.max_ms), 'stalls': self.stalls}
        self.max_ms, self.stalls = 0.0, 0
        return self.last


def _write_private(path, text):
    """A file only this user can read (POSIX 0600; Windows: the user's own data folder)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            stream.write(text)
    except OSError as exc:
        log.warning('Could not write %s: %s', path, exc)


async def _json(request, limit=MAX_JSON):
    if request.content_length is not None and request.content_length > limit:
        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=request.content_length)
    try:
        async with asyncio.timeout(5):
            data = await request.json()
    except (ValueError, TimeoutError):
        raise web.HTTPBadRequest(text='Expected a JSON body.')
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text='Expected a JSON object.')
    return data


class MirrorDeskServer:
    def __init__(self, data_dir, port=8765, video_port=5004, audio_port=5006, start_receiver=True,
                 receiver_command=None, public_host=None, ice_scope='all', desktop=None, usb=None, android=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.ice_scope = ice_scope if ice_scope in ('all', 'loopback') else 'all'
        self.store = SettingsStore(self.data_dir / 'settings.json')
        # Starting MirrorDesk starts a new AirPlay session: a new code, and the receiver starts with
        # a new key and an empty register, so no phone from before is trusted (receiver.py).
        self.store.regenerate_pin()
        self.diagnostics = _diagnostics(self.data_dir)
        self.hub = MediaHub(video_port, audio_port, compat_max_height=self.store.settings.compat_max_height)
        self.receiver = ReceiverSupervisor(self.store, self.data_dir, self.hub, self.diagnostics, receiver_command)
        # USB cable input. usb: None = on unless MIRRORDESK_USB=0 (tests, --no-usb);
        # False = off; or a UsbSource instance.
        if usb is None:
            usb = os.environ.get('MIRRORDESK_USB', '1') != '0'
        self.usb = (UsbSource(self.store, self.hub, self.diagnostics, data_dir=self.data_dir) if usb is True
                    else (usb or None))
        # Android phones (adb + scrcpy). android: None = on unless MIRRORDESK_ANDROID=0; False = off; or an instance.
        if android is None:
            android = os.environ.get('MIRRORDESK_ANDROID', '1') != '0'
        self.android = (AndroidSource(self.store, self.hub, self.diagnostics, containment=self.receiver.containment)
                        if android is True else (android or None))
        self.access = AccessControl(self.store, extra_hosts=[public_host] if public_host else None,
                                    sessions_path=self.data_dir / 'sessions.json')
        self.start_receiver = start_receiver
        self.desktop = desktop  # desktop.Desktop on Windows (quit, start at sign-in); None in Docker/tests
        self.quitting = False   # Quit was chosen: open pages show that MirrorDesk is going off
        if hasattr(desktop, 'quit_hooks'):
            desktop.quit_hooks.append(lambda: setattr(self, 'quitting', True))
        if desktop is not None:
            # `--quit` (the app menu's Quit entry, start-mirrordesk.sh stop) reads this from the
            # private data folder: it works even when this computer must sign in with a code.
            self.access.quit_token = secrets.token_urlsafe(24)
            _write_private(self.data_dir / QUIT_TOKEN_FILE, self.access.quit_token)
        self.interfaces = []
        self.interfaces_at = 0
        self.tailscale = None
        self.tailscale_at = 0
        self.viewers = ViewerManager(self.hub, self.store, self.diagnostics, addresses=self._candidate_addresses)
        # TURN over TCP on the web port (__main__ shares it): viewers whose direct connection fails.
        self.turn = TurnRelay(self.viewers.ice_sockets, self._relay_host)
        self.tasks = []
        self.build = build_tag()
        self.pages = {name: render_page(name, self.build) for name in ('index.html', 'login.html')}
        self.loop_monitor = LoopMonitor()
        self.last_mark = 0.0
        self.last_export = 0.0
        self.last_browser = {}
        self.last_receiver_state = None

    # -- helpers -------------------------------------------------------------
    def _refresh_interfaces(self, max_age=10):
        if time.monotonic() - self.interfaces_at > max_age:
            try:
                self.interfaces = netinfo.list_interfaces()
            except OSError:
                self.interfaces = []
            self.interfaces_at = time.monotonic()
        return self.interfaces

    def _candidate_addresses(self, local):
        """WebRTC host candidates: every reachable interface (LAN, Tailscale, USB
        tether, and local browsers that never use loopback candidates, like
        Firefox), or loopback only when MIRRORDESK_ICE_ADDRESSES=loopback (the
        P5/P6 behaviour, proven with Chrome/Edge on the same computer)."""
        if self.ice_scope == 'loopback':
            return ['127.0.0.1']
        return netinfo.candidate_addresses(self._refresh_interfaces(), include_loopback=True)

    def _relay_host(self):
        """Where TURN's relayed sockets are announced: an address of this computer that MirrorDesk's
        ICE sockets reach, not loopback when there is another (Firefox ignores loopback candidates)."""
        return next((a for a in self._candidate_addresses(False) if a != '127.0.0.1'), '127.0.0.1')

    def _record(self, event, **fields):
        if self.diagnostics is not None:
            try:
                self.diagnostics.record(event, **fields)
            except ValueError:
                pass

    async def _sampler(self):
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            source = self.hub.status()
            receiver = self.receiver.status()
            loop = self.loop_monitor.take()
            skew = self._usb_skew()
            usb = {} if skew is None else {'usb_skew_ppm': skew}
            if receiver['state'] != self.last_receiver_state:
                self.last_receiver_state = receiver['state']
                self._record('receiver_state', receiver_state=receiver['state'], restarts=receiver['restarts'])
            self._record('hub_sample', source_state=source['state'], phone_state=receiver['phone'],
                         source_input=source['input'],
                         source_width=source['width'], source_height=source['height'],
                         source_fps=source['fps'], source_bitrate_kbps=source['bitrate_kbps'],
                         source_keyframes=source['keyframes'],
                         source_keyframe_interval_s=source['keyframe_interval_s'],
                         source_lost_packets=source['lost_packets'], source_dropped_nals=source['dropped_nals'],
                         source_gop_overflows=source['gop_overflows'], source_stream_resets=source['stream_resets'],
                         source_audio_discontinuities=source['audio_discontinuities'],
                         source_audio_dropped_ms=source['audio_dropped_ms'],
                         source_audio_missed_ms=source['audio_missed_ms'],
                         source_audio_rebases=source['audio_rebases'], source_warm=source['warm'],
                         source_replayable=source['replayable'], source_sessions_ended=source['sessions_ended'],
                         source_profile=source['profile'] or 'other', viewer_count=len(self.viewers.viewers),
                         compat_decoded=source['compat_decoded'], compat_errors=source['compat_errors'],
                         compat_queue=source['compat_queue'], compat_dropped=source['compat_dropped'],
                         source_audio_input_ms=source['audio_input_ms'],
                         source_audio_jitter_ms=source['audio_jitter_ms'],
                         source_audio_backlog_ms=source['audio_backlog_ms'],
                         source_audio_trimmed_ms=source['audio_trimmed_ms'],
                         source_audio_held_ms=source['audio_held_ms'],
                         source_audio_flushes=source['audio_flushes'],
                         source_audio_flushed_ms=source['audio_flushed_ms'],
                         source_video_timed=source['video_timed'], source_video_jitter_ms=source['video_jitter_ms'],
                         source_video_delay_ms=source['video_delay_ms'],
                         source_video_late_frames=source['video_late_frames'],
                         source_video_stalls=source['video_stalls'], source_video_bridged=source['video_bridged'],
                         turn_allocations=self.turn.allocations, turn_relayed_kb=self.turn.relayed_bytes // 1024,
                         loop_lag_max_ms=loop['lag_max_ms'], loop_stalls=loop['stalls'], **usb)

    def _usb_skew(self):
        try:
            return self.usb.skew_ppm() if self.usb is not None and self.hub.input == 'usb' else None
        except AttributeError:   # a stand-in USB source (tests)
            return None

    # -- lifecycle -----------------------------------------------------------------
    async def on_startup(self, app):
        await self.hub.start()
        self.tasks.append(asyncio.create_task(self._sampler()))
        self.tasks.append(asyncio.create_task(self.loop_monitor.run()))
        self.tasks.append(asyncio.create_task(self.receiver.watch_sessions()))
        if self.start_receiver and self.store.settings.autostart_receiver:
            self.tasks.append(asyncio.create_task(self.receiver.start()))
        if self.usb is not None and self.store.settings.usb_enabled:
            await self.usb.start()
        if self.android is not None and self.store.settings.android_enabled:
            await self.android.start()

    async def on_cleanup(self, app):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.turn.close()
        await self.viewers.close_all()
        if self.android is not None:
            await self.android.stop(shutdown=True)  # the phone-side server exits when its sockets close
        if self.usb is not None:
            await self.usb.stop()  # hands the phone back in its normal USB mode
        await self.receiver.close()
        await self.hub.stop()
        if self.access.quit_token:
            try:
                (self.data_dir / QUIT_TOKEN_FILE).unlink()
            except OSError:
                pass
        self._record('session_end', component='backend')
        if self.diagnostics is not None:
            try:
                self.diagnostics.save_export()
            except OSError:
                pass

    # -- pages ------------------------------------------------------------------------
    async def index(self, request):
        return web.Response(text=self.pages['index.html'], content_type='text/html')

    async def login_page(self, request):
        # A viewer may open it to sign in again with the owner code.
        if request['role'] == OWNER:
            raise web.HTTPFound('/')
        return web.Response(text=self.pages['login.html'], content_type='text/html')

    async def health(self, request):
        # 'app' lets a second launch find this one; 'install' tells it whether that is itself.
        return web.json_response({'ok': True, 'app': 'mirrordesk', 'install': INSTALL_ID})

    # -- auth -------------------------------------------------------------------------
    async def login(self, request):
        data = await _json(request, 4096)
        role = self.access.check_code(request, data.get('code'))
        if role is None:
            raise web.HTTPForbidden(text='Wrong access code.')
        response = web.json_response({'ok': True, 'role': role})
        self._set_session(request, response, role)
        return response

    def _set_session(self, request, response, role):
        old = request.cookies.get(COOKIE)
        if old:
            self.access.end_session(old)   # e.g. a viewer signing in again with the owner code
        secure = request.secure or request.headers.get('X-Forwarded-Proto') == 'https'
        response.set_cookie(COOKIE, self.access.new_session(role), max_age=SESSION_SECONDS, httponly=True,
                            samesite='Strict', secure=secure, path='/')

    async def logout(self, request):
        self.access.end_session(request.cookies.get(COOKIE))
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE, path='/')
        return response

    # -- status ------------------------------------------------------------------------------
    async def status(self, request):
        local = self.access.is_local(request)
        if request['role'] == VIEWER:
            return web.json_response(self._viewer_status(local))
        return web.json_response({
            'version': __version__,
            'build': self.build,
            'platform': {'os': platform.system(), 'python': platform.python_version(),
                         'container': os.path.exists('/.dockerenv')},
            'client': {'local': local, 'role': OWNER},
            'receiver': self.receiver.status(include_private=True),
            'usb': self.usb.status() if self.usb is not None else {'state': 'unavailable', 'enabled': False,
                                                                    'message': 'USB cable input is not available.'},
            'android': self.android.status(local) if self.android is not None else {
                'state': 'unavailable', 'enabled': False, 'message': 'Android input is not available on this server.'},
            'source': self.hub.status(),
            'loop': self.loop_monitor.last,
            'viewers': {'count': len(self.viewers.viewers), 'max': self.store.settings.max_viewers,
                        'list': self.viewers.status()},
            'settings': self.store.settings.public(),
            'turn': self.turn.credentials(),
            'quitting': self.quitting,
        })

    def _viewer_status(self, local):
        """What a watch-only device needs to show the picture: no pairing code, phone or
        device names, settings or addresses."""
        receiver = self.receiver.status()
        usb = self.usb.status() if self.usb is not None else {'state': 'unavailable', 'enabled': False}
        android = self.android.status(local) if self.android is not None else {'state': 'unavailable', 'enabled': False}
        return {
            'version': __version__,
            'build': self.build,
            'client': {'local': local, 'role': VIEWER},
            'receiver': {'state': receiver['state'], 'phone': receiver['phone']},
            'usb': {'state': usb['state'], 'enabled': usb['enabled']},
            'android': {'state': android['state'], 'enabled': android['enabled'], 'control': False},
            'source': self.hub.status(),
            'viewers': {'count': len(self.viewers.viewers), 'max': self.store.settings.max_viewers},
            # The way in through this page's own connection (turn.py), for when the direct one fails.
            'turn': self.turn.credentials(),
            'quitting': self.quitting,
        }

    async def receiver_log(self, request):
        return web.json_response({'lines': self.receiver.log_lines()})

    # -- receiver control -----------------------------------------------------------------------
    async def receiver_action(self, request):
        action = request.match_info['action']
        if action == 'start':
            asyncio.create_task(self.receiver.start())
        elif action == 'stop':
            await self.receiver.stop()
        elif action == 'restart':
            asyncio.create_task(self.receiver.restart())
        elif action == 'forget':
            await self.receiver.new_session()
            self._record('settings_change', setting='paired_phones')
        else:
            raise web.HTTPNotFound()
        await asyncio.sleep(0.05)
        return web.json_response(self.receiver.status(include_private=True))

    async def phone_action(self, request):
        """Disconnect whichever phone is mirroring (the owner's Disconnect button)."""
        if request.match_info['action'] != 'disconnect':
            raise web.HTTPNotFound()
        source = self.hub.input
        if source == 'usb' and self.usb is not None:
            done = await self.usb.disconnect_phone()
        elif source == 'android' and self.android is not None:
            done = await self.android.disconnect_phone()
        elif self.receiver.phone_state() != 'idle' or self.hub.source_state() != 'idle':
            await self.receiver.disconnect_phone()
            done = True
        else:
            done = False
        if not done:
            raise web.HTTPConflict(text='No phone is mirroring.')
        self._record('phone_disconnect', source_input=source)
        return web.json_response({'disconnected': source})

    async def usb_action(self, request):
        if self.usb is None:
            raise web.HTTPNotFound(text='USB cable input is not available on this server.')
        action = request.match_info['action']
        if action in ('driver-install', 'driver-restore'):
            # Windows shows its permission prompt on this computer's screen, so only
            # the browser on this computer may start it.
            if not self.access.is_local(request):
                raise web.HTTPForbidden(text='Do this in the browser on the computer the phone is plugged into.')
            from . import usbdriver
            if not usbdriver.available():
                raise web.HTTPNotFound(text='The cable driver installer is not part of this MirrorDesk.')
            asyncio.create_task(self.usb.driver_action('install' if action == 'driver-install' else 'restore',
                                                       self.data_dir))
            await asyncio.sleep(0.05)
            return web.json_response(self.usb.status())
        if action != 'reconnect':
            raise web.HTTPNotFound()
        if not self.store.settings.usb_enabled:
            raise web.HTTPConflict(text='USB cable mirroring is turned off in Settings.')
        await self.usb.restart()
        return web.json_response(self.usb.status())

    # -- android ------------------------------------------------------------------------------------
    def _need_android(self):
        if self.android is None:
            raise web.HTTPNotFound(text='Android input is not available on this server.')
        return self.android

    async def android_action(self, request):
        android = self._need_android()
        action = request.match_info['action']
        data = await _json(request, 4096) if request.can_read_body else {}
        try:
            if action == 'install-tools':
                asyncio.create_task(android.install_tools())
                await asyncio.sleep(0.05)
            elif action == 'reconnect':
                if not self.store.settings.android_enabled:
                    raise web.HTTPConflict(text='Android mirroring is turned off in Settings.')
                await android.restart()
            elif action == 'select':
                serial = data.get('serial')
                if not isinstance(serial, str) or not any(d.serial == serial for d in android.devices):
                    raise web.HTTPBadRequest(text='Unknown phone.')
                android.selected = serial
                if android.session is not None and android.session.device.serial != serial:
                    await android.restart()
            elif action == 'pair':
                result = await android.pair_with_code(data.get('host'), data.get('port'), data.get('code'))
                return web.json_response(result)
            elif action == 'connect':
                return web.json_response(await android.connect_wifi(data.get('host'), data.get('port')))
            elif action == 'disconnect':
                await android.disconnect_wifi(data.get('serial'))
            elif action == 'qr':
                return web.json_response(await android.qr_start())
            elif action == 'key':
                if not android.control_allowed(self.access.is_local(request)):
                    raise web.HTTPForbidden(text='Controlling the phone from this device is turned off in Settings.')
                if not android.control({'t': 'key', 'k': data.get('key')}):
                    raise web.HTTPConflict(text='No Android phone is mirroring.')
            else:
                raise web.HTTPNotFound()
        except AdbError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(android.status(self.access.is_local(request)))

    async def android_log(self, request):
        return web.json_response({'lines': self._need_android().log_lines()})

    async def android_control(self, request):
        """WebSocket for touch/keyboard input. Browsers send no custom headers on a
        WebSocket handshake, so CSRF protection is a strict same-origin check here
        (a page on another site must never drive a trusted local session)."""
        android = self._need_android()
        origin = request.headers.get('Origin')
        if not origin or origin.split('://', 1)[-1].rstrip('/').lower() != (request.host or '').lower():
            raise web.HTTPForbidden(text='Cross-origin request refused.')
        local = self.access.is_local(request)
        if not android.control_allowed(local):
            raise web.HTTPForbidden(text='Controlling the phone from this device is turned off in Settings.')
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8192)
        await ws.prepare(request)
        window, count = time.monotonic(), 0
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                continue
            now = time.monotonic()
            if now - window >= 1:
                window, count = now, 0
            count += 1
            if count > CONTROL_EVENTS_PER_SECOND or not android.control_allowed(local):
                continue
            try:
                event = json.loads(message.data)
            except ValueError:
                continue
            if isinstance(event, dict):
                android.control(event)
        return ws

    # -- settings -----------------------------------------------------------------------------------
    async def get_settings(self, request):
        return web.json_response({'settings': self.store.settings.public(),
                                  'choices': {'resolution': list(RESOLUTIONS), 'fps': [30, 60],
                                              'video_mode': ['auto', 'passthrough', 'compat'],
                                              'av_sync': ['low_latency', 'synced'],
                                              'compat_max_height': [480, 720, 1080],
                                              'android_max_size': list(ANDROID_SIZES),
                                              'android_bit_rate': list(ANDROID_BIT_RATES),
                                              'android_fps': [30, 60],
                                              'android_control': list(ANDROID_CONTROL)}})

    async def put_settings(self, request):
        try:
            clean = validate_update(await _json(request, 8192))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        changed = self.store.update(clean)
        for key in sorted(changed):
            self._record('settings_change', setting=key)
        if 'compat_max_height' in changed:
            self.hub.compat_max_height = self.store.settings.compat_max_height
            if self.hub._compat is not None:
                self.hub._compat.max_height = self.store.settings.compat_max_height
        restart = bool(changed & set(self.store.settings.RECEIVER_FIELDS)) and self.receiver.want_running
        if restart:
            asyncio.create_task(self.receiver.restart())
        if self.usb is not None and 'usb_enabled' in changed:
            await (self.usb.start() if self.store.settings.usb_enabled else self.usb.stop())
        elif self.usb is not None and 'audio_enabled' in changed and self.usb.capture is not None:
            asyncio.create_task(self.usb.restart())
        if self.android is not None and 'android_enabled' in changed:
            await (self.android.start() if self.store.settings.android_enabled else self.android.stop())
        elif self.android is not None and changed & set(self.store.settings.ANDROID_FIELDS) and self.android.session:
            asyncio.create_task(self.android.restart())   # new size / bit rate / sound take effect now
        return web.json_response({'settings': self.store.settings.public(), 'changed': sorted(changed),
                                  'receiver_restarting': restart})

    async def regenerate(self, request):
        what = request.match_info['what']
        if what == 'pin':
            # A new code ends the session: every phone, also one trusted a moment ago, has to enter it.
            await self.receiver.new_session()
            self._record('settings_change', setting='pin')
            return web.json_response({'pin': self.store.settings.pin})
        if what == 'access-code':
            try:
                code = self.store.regenerate_access_code()
            except ValueError as exc:
                raise web.HTTPConflict(text=str(exc))
            self.access.revoke(VIEWER)
            self._record('settings_change', setting='access_code')
            return web.json_response({'access_code': code})
        if what == 'owner-code':
            try:
                code = self.store.regenerate_owner_code()
            except ValueError as exc:
                raise web.HTTPConflict(text=str(exc))
            self.access.revoke(OWNER)
            self._record('settings_change', setting='owner_code')
            response = web.json_response({'owner_code': code})
            if not self.access.trusted_local(request):
                self._set_session(request, response, OWNER)   # the device that asked stays signed in
            return response
        raise web.HTTPNotFound()

    async def qr_code(self, request):
        """A QR code (SVG) of a link the owner shares: the Viewers page's invitation."""
        data = await _json(request, 2048)
        text = data.get('text')
        if not isinstance(text, str) or not 0 < len(text) <= 512 or not text.startswith(('http://', 'https://')):
            raise web.HTTPBadRequest(text='Expected a link.')
        from .androidsource import qr_svg
        return web.json_response({'svg': await asyncio.to_thread(qr_svg, text, 5)})

    async def access_info(self, request):
        return web.json_response({'access_code': self.store.settings.access_code,
                                  'pinned': self.store.access_code_pinned,
                                  'owner_code': self.store.settings.owner_code,
                                  'owner_pinned': self.store.owner_code_pinned,
                                  'trust_localhost': self.access.trust_localhost,
                                  'require_code_on_localhost': self.store.settings.require_code_on_localhost})

    # -- network ------------------------------------------------------------------------------------------
    async def network(self, request):
        interfaces = self._refresh_interfaces(max_age=0 if request.query.get('refresh') else 10)
        if time.monotonic() - self.tailscale_at > 30 or request.query.get('refresh'):
            self.tailscale = await asyncio.to_thread(netinfo.tailscale_status)
            self.tailscale_at = time.monotonic()
        scheme = 'https' if request.secure else 'http'
        return web.json_response({
            'port': self.port,
            'interfaces': [i.as_dict() for i in interfaces],
            'urls': netinfo.viewer_urls(interfaces, self.port, scheme, self.tailscale),
            'tailscale': self.tailscale,
            'usb_tether': [i.as_dict() for i in interfaces if i.kind == 'usb_tether'],
        })

    async def network_check(self, request):
        """Windows-only read-only discovery evidence from the existing probes."""
        if os.name != 'nt':
            return web.json_response({'available': False,
                                      'lines': ['Discovery self-test is available on the Windows receiver. On macOS '
                                                'run `dns-sd -B _airplay._tcp`, on Linux `avahi-browse -r '
                                                '_airplay._tcp` on another computer of the same network.']})
        def run():
            sys.path.insert(0, str(ROOT))
            import discovery_probe
            import discovery_selftest
            result = discovery_probe.run_probe(None, full=True)
            lines = list(discovery_probe.summary_lines(result))
            running = self.receiver.state == 'running'
            path = discovery_selftest.run_selftest(result, self.store.settings.receiver_name, running)
            lines += list(discovery_selftest.summary_lines(path))
            return lines
        try:
            lines = await asyncio.wait_for(asyncio.to_thread(run), timeout=25)
        except Exception as exc:  # evidence collection is best effort
            lines = [f'Discovery check could not complete ({type(exc).__name__}).']
        return web.json_response({'available': True, 'lines': lines})

    # -- desktop app (Windows; macOS/Linux desktop installs) ---------------------------------------------
    def _need_desktop(self):
        if self.desktop is None:
            raise web.HTTPNotFound(text='Not available on this server (Docker or Linux service: stop the container '
                                        'or service instead).')
        return self.desktop

    async def desktop_info(self, request):
        return web.json_response(self._need_desktop().status())

    async def desktop_autostart(self, request):
        desktop = self._need_desktop()
        data = await _json(request, 1024)
        if set(data) != {'enabled'} or not isinstance(data['enabled'], bool):
            raise web.HTTPBadRequest(text='Expected {"enabled": true|false}.')
        try:
            await asyncio.to_thread(desktop.set_autostart, data['enabled'])
        except OSError as exc:
            raise web.HTTPInternalServerError(text=f'Could not change the start-at-sign-in setting: {exc}')
        return web.json_response(desktop.status())

    async def desktop_quit(self, request):
        desktop = self._need_desktop()
        # Answer first; MirrorDesk's windows close, and the graceful shutdown (receiver stopped,
        # diagnostics saved) follows once open pages have seen it coming.
        from .desktop import QUIT_DELAY
        desktop.quit(QUIT_DELAY)
        return web.json_response({'quitting': True})

    async def desktop_window(self, request):
        """Open MirrorDesk's window on this computer (a second start of MirrorDesk asks for it)."""
        if not self.access.is_local(request):
            raise web.HTTPForbidden(text='Only on the computer MirrorDesk runs on.')
        await asyncio.to_thread(self._need_desktop().open)
        return web.json_response({'opened': True})

    # -- viewers --------------------------------------------------------------------------------------------
    async def create_viewer(self, request):
        data = await _json(request)
        if data.get('type') != 'offer' or not isinstance(data.get('sdp'), str) or not data['sdp'].startswith('v=0'):
            raise web.HTTPBadRequest(text='Expected an SDP offer.')
        if self.hub.source_state() == 'idle':
            # Pages join only while a phone mirrors; one left open from an older version joined at
            # once and held a viewer place ("Watching 1 of 4" with no phone) until it was reloaded.
            raise web.HTTPConflict(text='Nothing is being mirrored right now.')
        local = self.access.is_local(request)
        kind = self._viewer_kind(request, data.get('client'))
        try:
            answer = await self.viewers.create(data['sdp'], str(data.get('mode') or 'auto'),
                                               self.access.peer_ip(request), local,
                                               prefer_vp8=data.get('codec') == 'vp8', role=request['role'] or OWNER,
                                               kind=kind)
        except ViewerLimit as exc:
            raise web.HTTPConflict(text=str(exc))
        except (ValueError, TimeoutError) as exc:
            raise web.HTTPBadRequest(text=f'Could not negotiate media: {exc}')
        self._record('viewer_start', viewer_mode=answer['mode'], viewer_local=local, **kind)
        return web.json_response(answer)

    def _viewer_kind(self, request, client):
        """Diagnostics categories for a viewer: its browser and device (as the page names them,
        from fixed lists) and how it reached this server (the address it used, by kind)."""
        from diagnostics import ENUMS
        client = client if isinstance(client, dict) else {}
        kind = {key: client[key] for key in ('browser', 'device')
                if isinstance(client.get(key), str) and client[key] in ENUMS[key]}
        host = (request.host or '').lower()
        host = host[1:host.index(']')] if host.startswith('[') and ']' in host else host.rsplit(':', 1)[0]
        if host in ('localhost', '127.0.0.1', '::1'):
            kind['path'] = 'local'
        elif host.endswith('.ts.net'):
            kind['path'] = 'tailscale'
        elif host.endswith('.local'):
            kind['path'] = 'name'
        else:
            found = next((i.kind for i in self._refresh_interfaces() if host in i.ipv4), None)
            kind['path'] = {'ethernet': 'lan', 'wifi': 'lan', 'usb_tether': 'lan', 'tailscale': 'tailscale',
                            'vpn': 'vpn'}.get(found, 'other')
        return kind

    async def delete_viewer(self, request):
        closed = await self.viewers.close(request.match_info['viewer_id'])
        return web.json_response({'closed': closed})

    async def viewer_keyframe(self, request):
        resync, stuck = self.viewers.request_keyframe(request.match_info['viewer_id'])
        return web.json_response({'resync': resync, 'stuck': stuck})

    async def viewer_stats(self, request):
        viewer_id = request.match_info['viewer_id']
        if viewer_id not in self.viewers.viewers:
            raise web.HTTPNotFound()
        now = time.monotonic()
        if now - self.last_browser.get(viewer_id, 0) < 1:
            raise web.HTTPTooManyRequests()
        self.last_browser[viewer_id] = now
        if len(self.last_browser) > 64:
            self.last_browser = {k: v for k, v in self.last_browser.items() if k in self.viewers.viewers}
        data = await _json(request, 8192)
        from diagnostics import P7_BROWSER_FIELDS, safe_fields
        fields = safe_fields({k: v for k, v in data.items() if k in P7_BROWSER_FIELDS})
        viewer = self.viewers.viewers.get(viewer_id)
        if viewer is not None and isinstance(data.get('document_hidden'), bool):
            viewer.hidden = data['document_hidden']
        self._record('browser', **fields)
        return web.json_response({'recorded': True})

    # -- diagnostics ----------------------------------------------------------------------------------------
    async def mark_issue(self, request):
        from diagnostics import ISSUES
        data = await _json(request, 1024)
        if set(data) != {'issue'} or data['issue'] not in ISSUES:
            raise web.HTTPBadRequest(text='Unknown issue category.')
        if time.monotonic() - self.last_mark < 1:
            raise web.HTTPTooManyRequests(text='Wait a second between markers.')
        self.last_mark = time.monotonic()
        self._record('issue', issue=data['issue'])
        return web.json_response({'marked': True})

    async def export_diagnostics(self, request):
        if self.diagnostics is None:
            raise web.HTTPServiceUnavailable(text='Diagnostics are unavailable on this server.')
        if time.monotonic() - self.last_export < 3:
            raise web.HTTPTooManyRequests(text='Export again in a few seconds.')
        self.last_export = time.monotonic()
        data = await asyncio.to_thread(self.diagnostics.export)
        return web.Response(body=data, content_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="MirrorDesk-diagnostics-{self.diagnostics.directory.name}.zip"'})

    # -- app ------------------------------------------------------------------------------------------------------
    def app(self):
        app = web.Application(middlewares=[self.access.middleware()], client_max_size=MAX_JSON)
        r = app.router
        r.add_get('/', self.index)
        r.add_get('/login', self.login_page)
        r.add_get('/api/health', self.health)
        r.add_post('/api/login', self.login)
        r.add_post('/api/logout', self.logout)
        r.add_get('/api/status', self.status)
        r.add_get('/api/receiver/log', self.receiver_log)
        r.add_post('/api/receiver/{action}', self.receiver_action)
        r.add_post('/api/phone/{action}', self.phone_action)
        r.add_post('/api/usb/{action}', self.usb_action)
        r.add_get('/api/android/log', self.android_log)
        r.add_get('/api/android/control', self.android_control)
        r.add_post('/api/android/{action}', self.android_action)
        r.add_get('/api/settings', self.get_settings)
        r.add_put('/api/settings', self.put_settings)
        r.add_post('/api/regenerate/{what}', self.regenerate)
        r.add_get('/api/access', self.access_info)
        r.add_post('/api/qr', self.qr_code)
        r.add_get('/api/network', self.network)
        r.add_post('/api/network/check', self.network_check)
        r.add_get('/api/desktop', self.desktop_info)
        r.add_post('/api/desktop/autostart', self.desktop_autostart)
        r.add_post('/api/desktop/quit', self.desktop_quit)
        r.add_post('/api/desktop/window', self.desktop_window)
        r.add_post('/api/viewers', self.create_viewer)
        r.add_delete('/api/viewers/{viewer_id}', self.delete_viewer)
        r.add_post('/api/viewers/{viewer_id}/keyframe', self.viewer_keyframe)
        r.add_post('/api/viewers/{viewer_id}/stats', self.viewer_stats)
        r.add_post('/api/diagnostics/mark', self.mark_issue)
        r.add_post('/api/diagnostics/export', self.export_diagnostics)
        r.add_static('/app/', WEB, show_index=False, append_version=False)
        app.on_startup.append(self.on_startup)
        app.on_cleanup.append(self.on_cleanup)
        return app
