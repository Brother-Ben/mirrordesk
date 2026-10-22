"""Access control for a LAN/Tailscale-reachable server.

* Same-computer browsers (loopback peer AND loopback Host, no proxy headers)
  are trusted as the owner unless `require_code_on_localhost` is set.
* Every other client signs in once with a code shown in the UI / server log and
  receives an HttpOnly, SameSite=Strict session cookie. The owner code gives
  full control; the access code ("viewer code") only lets a browser watch: the
  page, its own WebRTC viewer and a reduced status (VIEWER_PATHS). Sessions
  survive server restarts (only SHA-256 hashes of the tokens are stored), so a
  daily start/quit does not sign every device out.
* Host allowlist (IP literals, localhost, this host's names, *.ts.net and
  MIRRORDESK_ALLOWED_HOSTS) defeats DNS rebinding; state-changing requests
  need the X-MirrorDesk header and a same-origin Origin, defeating CSRF.
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import tempfile
import time

from aiohttp import web

log = logging.getLogger('mirrordesk.security')
COOKIE = 'mirrordesk_session'
SESSION_SECONDS = 30 * 24 * 3600
MAX_SESSIONS = 256
LOGIN_WINDOW = 60
LOGIN_ATTEMPTS = 10
PROXY_HEADERS = ('X-Forwarded-For', 'Forwarded', 'X-Real-IP', 'Tailscale-User-Login', 'X-Forwarded-Host')
PUBLIC_PATHS = ('/login', '/api/login', '/api/health', '/app/login.js', '/app/theme.js', '/app/app.css',
                '/app/icon.svg', '/app/manifest.webmanifest')
OWNER, VIEWER = 'owner', 'viewer'
# Everything a viewer may reach; every other path (current and future) is owner-only.
VIEWER_PATHS = ('/', '/api/status', '/api/logout', '/api/viewers')
VIEWER_PREFIXES = ('/app/', '/api/viewers/')
LOCAL_HOSTNAMES = {'localhost', '127.0.0.1', '[::1]', '::1'}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _host_only(host_header):
    host = (host_header or '').strip().lower()
    if host.startswith('['):
        end = host.find(']')
        return host[:end + 1] if end != -1 else host
    return host.rsplit(':', 1)[0] if host.count(':') == 1 else host


def _is_ip(value):
    try:
        ipaddress.ip_address(value.strip('[]'))
        return True
    except ValueError:
        return False


def default_trust_localhost():
    """Same-computer trust is a desktop convenience. In a container (or when
    MIRRORDESK_TRUST_LOCALHOST=0) a 'local' peer is usually a loopback
    forwarder - userspace Tailscale, nginx proxy_pass, socat, ssh -R - that
    relays remote users without proxy headers, so everyone needs the code."""
    env = os.environ.get('MIRRORDESK_TRUST_LOCALHOST')
    if env is not None:
        return env.strip().lower() not in ('0', 'false', 'no', 'off')
    return not (os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv'))


class AccessControl:
    def __init__(self, store, extra_hosts=None, trust_localhost=None, sessions_path=None):
        self.store = store
        self.trust_localhost = default_trust_localhost() if trust_localhost is None else trust_localhost
        self.sessions_path = Path(sessions_path) if sessions_path else None
        self.sessions = self._load_sessions()  # sha256(code, token) -> (expiry epoch seconds, role)
        self.attempts = {}
        self.quit_token = None   # desktop apps: lets the local `--quit` command quit (server.py)
        names = {socket.gethostname().lower()}
        try:
            names.add(socket.getfqdn().lower())
        except OSError:
            pass
        names |= {f'{n}.local' for n in list(names) if n and not n.endswith('.local')}
        env = os.environ.get('MIRRORDESK_ALLOWED_HOSTS', '')
        names |= {h.strip().lower() for h in env.split(',') if h.strip()}
        names |= {h.lower() for h in (extra_hosts or ())}
        self.allowed_names = {n for n in names if n}

    # -- classification -----------------------------------------------------
    def host_allowed(self, request):
        host = _host_only(request.host)
        return (host in LOCAL_HOSTNAMES or _is_ip(host) or host in self.allowed_names
                or host.endswith('.ts.net'))

    @staticmethod
    def peer_ip(request):
        peer = request.transport.get_extra_info('peername') if request.transport else None
        return peer[0] if peer else None

    def is_local(self, request):
        peer = self.peer_ip(request)
        try:
            loopback = peer is not None and ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            return False
        if _host_only(request.host) not in LOCAL_HOSTNAMES:
            return False  # e.g. `tailscale serve` or another reverse proxy on this host
        return not any(h in request.headers for h in PROXY_HEADERS)

    def trusted_local(self, request):
        return (self.trust_localhost and not self.store.settings.require_code_on_localhost
                and self.is_local(request))

    def role(self, request):
        """OWNER, VIEWER, or None (not signed in)."""
        if self.trusted_local(request):
            return OWNER
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        for role in (OWNER, VIEWER):
            key = self._token_hash(token, role)
            entry = self.sessions.get(key)
            if entry is None:
                continue
            if entry[0] < time.time() or entry[1] != role:
                del self.sessions[key]
                self._save_sessions()
                return None
            return role
        return None

    def authorized(self, request):
        return self.role(request) is not None

    def quit_request(self, request):
        """The local launcher: `--quit` (app menu "Quit MirrorDesk", start-mirrordesk.sh stop) and a
        second start asking for MirrorDesk's window. It proves it runs as this user by reading a
        token from MirrorDesk's private data folder, so it works even when this computer must sign
        in with a code."""
        given = request.headers.get('X-MirrorDesk-Quit')
        return bool(self.quit_token and given and request.method == 'POST'
                    and request.path in ('/api/desktop/quit', '/api/desktop/window')
                    and self.is_local(request) and hmac.compare_digest(given.encode(), self.quit_token.encode()))

    @staticmethod
    def viewer_allowed(path):
        return path in VIEWER_PATHS or path.startswith(VIEWER_PREFIXES)

    # -- login -------------------------------------------------------------------
    def check_code(self, request, code):
        """Rate-limited code check: OWNER, VIEWER, or None for a wrong code."""
        peer = self.peer_ip(request) or 'unknown'
        now = time.time()
        recent = [t for t in self.attempts.get(peer, []) if now - t < LOGIN_WINDOW]
        if len(recent) >= LOGIN_ATTEMPTS:
            self.attempts[peer] = recent
            raise web.HTTPTooManyRequests(text='Too many attempts; wait a minute.')
        recent.append(now)
        self.attempts[peer] = recent
        if len(self.attempts) > 1024:
            self.attempts = {k: v for k, v in self.attempts.items() if v and now - v[-1] < LOGIN_WINDOW}
        given = (code or '').strip().upper().replace(' ', '').replace('-', '')
        if not given:
            return None
        settings = self.store.settings
        # Both comparisons always run, so timing never tells which code was close.
        owner = hmac.compare_digest(given.encode(), settings.owner_code.encode())
        viewer = hmac.compare_digest(given.encode(), settings.access_code.encode())
        return OWNER if owner else VIEWER if viewer else None

    def new_session(self, role=VIEWER):
        token = secrets.token_urlsafe(32)
        self.sessions[self._token_hash(token, role)] = (time.time() + SESSION_SECONDS, role)
        if len(self.sessions) > MAX_SESSIONS:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] > now}
        self._save_sessions()
        return token

    def _token_hash(self, token, role):
        # Keyed by the role's current code: changing a code (UI or MIRRORDESK_ACCESS_CODE /
        # MIRRORDESK_OWNER_CODE + restart) invalidates every session made with it. Viewer
        # sessions keep the original format, so devices signed in before owner codes
        # existed stay signed in, as viewers.
        if role == OWNER:
            return hashlib.sha256(f'owner:{self.store.settings.owner_code}:{token}'.encode()).hexdigest()
        return hashlib.sha256(f'{self.store.settings.access_code}:{token}'.encode()).hexdigest()

    def end_session(self, token):
        if not token:
            return
        removed = [self.sessions.pop(self._token_hash(token, role), None) for role in (OWNER, VIEWER)]
        if any(entry is not None for entry in removed):
            self._save_sessions()

    def revoke(self, role=None):
        """Sign out every session of one role (None: all)."""
        self.sessions = {k: v for k, v in self.sessions.items() if role is not None and v[1] != role}
        self._save_sessions()

    def revoke_all(self):
        self.revoke()

    # -- persistence (hashes only: the file never holds a usable cookie) ----------
    def _load_sessions(self):
        if self.sessions_path is None:
            return {}
        try:
            data = json.loads(self.sessions_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning('Ignoring unreadable sessions file (devices sign in again): %s', exc)
            return {}
        now = time.time()
        if not isinstance(data, dict):
            return {}
        sessions = {}
        for key, value in data.items():
            if isinstance(value, (int, float)):
                value = [value, VIEWER]  # written before owner codes existed: every device was a viewer
            if (isinstance(key, str) and len(key) == 64 and isinstance(value, list) and len(value) == 2
                    and isinstance(value[0], (int, float)) and value[0] > now and value[1] in (OWNER, VIEWER)):
                sessions[key] = (float(value[0]), value[1])
        return dict(sorted(sessions.items(), key=lambda kv: kv[1][0])[-MAX_SESSIONS:])

    def _save_sessions(self):
        if self.sessions_path is None:
            return
        try:
            self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.sessions_path.parent, prefix='.sessions-', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(self.sessions, stream)
                os.replace(temporary, self.sessions_path)
            except OSError:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError as exc:  # sign-in keeps working in memory; it just won't survive a restart
            log.warning('Could not save sessions: %s', exc)

    # -- middleware ------------------------------------------------------------------
    def middleware(self):
        @web.middleware
        async def guard(request, handler):
            if not self.host_allowed(request):
                raise web.HTTPMisdirectedRequest(text='Unknown host name. Add it to MIRRORDESK_ALLOWED_HOSTS.')
            if request.headers.get('Content-Encoding'):
                raise web.HTTPUnsupportedMediaType()
            if request.method not in ('GET', 'HEAD'):
                origin = request.headers.get('Origin')
                if request.headers.get('X-MirrorDesk') != '1':
                    raise web.HTTPForbidden(text='Missing request header.')
                if origin is not None and origin.split('://', 1)[-1].rstrip('/').lower() != (request.host or '').lower():
                    raise web.HTTPForbidden(text='Cross-origin request refused.')
            public = request.path in PUBLIC_PATHS
            role = OWNER if self.quit_request(request) else self.role(request)
            request['role'] = role
            if not public and role is None:
                if request.path.startswith('/api/'):
                    raise web.HTTPUnauthorized(text='Sign in with the access code.')
                raise web.HTTPFound('/login')
            if not public and role == VIEWER and not self.viewer_allowed(request.path):
                raise web.HTTPForbidden(text='This device can only watch. Sign in with the owner code to change anything.')
            response = await handler(request)
            response.headers.setdefault('Content-Security-Policy', CSP)
            response.headers.setdefault('X-Content-Type-Options', 'nosniff')
            response.headers.setdefault('Referrer-Policy', 'no-referrer')
            response.headers.setdefault('X-Frame-Options', 'DENY')
            response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
            if request.path.startswith('/api/') or request.path in ('/', '/login'):
                response.headers.setdefault('Cache-Control', 'no-store')
            elif request.path.startswith('/app/'):
                # Kept, but checked on every load (a quick "not modified" on the network): an
                # update's page never runs with an older script or style.
                response.headers.setdefault('Cache-Control', 'no-cache')
            return response
        return guard
