"""`python -m mirrordesk` — run the browser-first MirrorDesk server."""
import argparse
import asyncio
import contextlib
import logging
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import webbrowser

from aiohttp import web

from . import __version__, desktop, netinfo
from .server import MirrorDeskServer

ROOT = Path(__file__).resolve().parents[1]


def default_data_dir():
    if os.environ.get('MIRRORDESK_DATA'):
        return Path(os.environ['MIRRORDESK_DATA'])
    return ROOT / '.runtime'


def parse_args(argv=None):
    env = os.environ.get
    parser = argparse.ArgumentParser(prog='mirrordesk', description='MirrorDesk browser-first AirPlay receiver.')
    parser.add_argument('--host', default=env('MIRRORDESK_HOST', '0.0.0.0'),
                        help='Bind address. 0.0.0.0 = all interfaces (LAN/Tailscale/USB, access code required '
                             'for other devices); 127.0.0.1 = this computer only.')
    parser.add_argument('--port', type=int, default=int(env('MIRRORDESK_PORT', '8765')))
    parser.add_argument('--data', type=Path, default=default_data_dir(), help='Settings, keys and diagnostics.')
    parser.add_argument('--video-port', type=int, default=int(env('MIRRORDESK_VIDEO_PORT', '5004')))
    parser.add_argument('--audio-port', type=int, default=int(env('MIRRORDESK_AUDIO_PORT', '5006')))
    parser.add_argument('--no-receiver', action='store_true', help='Do not start UxPlay (media from another source).')
    parser.add_argument('--no-usb', action='store_true', default=env('MIRRORDESK_USB') == '0',
                        help='Never touch USB devices (no cable mirroring). Also MIRRORDESK_USB=0.')
    parser.add_argument('--open-browser', action='store_true', help='Open the local browser once ready.')
    parser.add_argument('--quit', action='store_true',
                        help='Quit the MirrorDesk running on this computer (same --port and --data) and exit.')
    parser.add_argument('--replace', action='store_true',
                        help='Close an older MirrorDesk (Python) that holds the ports without asking.')
    parser.add_argument('--background', action='store_true',
                        help='Desktop app mode, used by MirrorDesk.pyw: no console; output goes to '
                             '<data>/logs/mirrordesk.log; Quit and start-at-sign-in in the browser. Windows also '
                             'shows an icon next to the clock.')
    parser.add_argument('--public-host', default=env('MIRRORDESK_PUBLIC_HOST'),
                        help='Extra host name browsers may use (reverse proxy / DNS name).')
    parser.add_argument('--log-level', default=env('MIRRORDESK_LOG_LEVEL', 'WARNING'))
    return parser.parse_args(argv)


class _SharedPort(asyncio.Protocol):
    """The web port also carries TURN (turn.py), so a viewer who can open the page can reach the
    relay as well: a TURN client's first byte is 0x00 or 0x01 (a STUN message), a browser's
    request starts with a letter."""
    FIRST_DATA_SECONDS = 30

    def __init__(self, http, turn):
        self.http, self.turn = http, turn
        self.transport = None
        self.timer = None

    def connection_made(self, transport):
        self.transport = transport
        self.timer = asyncio.get_running_loop().call_later(self.FIRST_DATA_SECONDS, transport.close)

    def data_received(self, data):
        self.timer.cancel()
        turn = self.turn is not None and data[:1] in (b'\x00', b'\x01')
        protocol = (self.turn if turn else self.http)()
        self.transport.set_protocol(protocol)
        protocol.connection_made(self.transport)
        protocol.data_received(data)

    def connection_lost(self, exc):
        if self.timer is not None:
            self.timer.cancel()


class SharedPortSite(web.TCPSite):
    def __init__(self, runner, host, port, turn=None):
        super().__init__(runner, host, port)
        self._turn = turn

    async def start(self):
        await web.BaseSite.start(self)
        server = self._runner.server
        self._server = await asyncio.get_running_loop().create_server(
            lambda: _SharedPort(server, self._turn), self._host, self._port, backlog=self._backlog,
            reuse_address=self._reuse_address, reuse_port=self._reuse_port)


def serve(app, host, port, turn=None, shutdown_timeout=5):
    """aiohttp's web.run_app, quiet, with the web port shared with the TURN relay."""
    async def run():
        runner = web.AppRunner(app, access_log=None, handle_signals=True, shutdown_timeout=shutdown_timeout)
        await runner.setup()
        try:
            await SharedPortSite(runner, host, port, turn).start()
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()

    loop = asyncio.new_event_loop()
    main_task = loop.create_task(run())
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main_task)
    except (web.GracefulExit, KeyboardInterrupt):
        pass
    finally:
        try:
            main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                loop.run_until_complete(main_task)
        finally:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()


def banner(server, args):
    local = f'http://127.0.0.1:{args.port}/'
    lines = [f'MirrorDesk {__version__} ready.', f'  This computer: {local}']
    if args.host not in ('127.0.0.1', 'localhost'):
        try:
            for item in netinfo.viewer_urls(netinfo.list_interfaces(), args.port):
                lines.append(f'  {item["kind"]:<10} {item["url"]}')
        except OSError:
            pass
    if args.host not in ('127.0.0.1', 'localhost') or not server.access.trust_localhost:
        who = 'other devices' if server.access.trust_localhost else 'every browser (container: no localhost trust)'
        lines.append(f'  Owner code (full control) for {who}: {server.store.settings.owner_code}')
        lines.append(f'  Viewer code (watch only) for {who}: {server.store.settings.access_code}')
    lines.append(f'  AirPlay name on the phone: {server.store.settings.receiver_name}')
    if args.background:
        where = {'windows': 'the MirrorDesk icon next to the clock', 'macos': 'MirrorDesk in the Dock',
                 'linux': '"Quit MirrorDesk" in the app menu'}[desktop.desktop_os()]
        lines.append(f'  Running in the background: quit from {where} or the page.')
    else:
        lines.append('  Press Ctrl+C to stop.')
    return '\n'.join(lines)


def main(argv=None):
    args = parse_args(argv)
    local_url = f'http://127.0.0.1:{args.port}/'
    if args.quit:
        return desktop.quit_running(args.port, args.data, notify=desktop.notify if args.background else None,
                                    udp_ports=(args.video_port, args.audio_port))
    found = desktop.probe_instance(args.port)
    if found == 'current':
        # Already running (e.g. started at sign-in): show it instead of failing on the busy port,
        # in its own window when it has one (so Quit closes this one too).
        print(f'MirrorDesk is already running: {local_url}', flush=True)
        if args.open_browser and not desktop.show_running(args.port, args.data):
            webbrowser.open(local_url)
        return 0
    if args.background:
        try:
            desktop.redirect_output(args.data)
        except OSError as exc:
            desktop.show_error(f'MirrorDesk could not write its log in {args.data}: {exc}')
            return 1
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    fail = desktop.show_error if args.background else (lambda text: print(text, file=sys.stderr, flush=True))
    udp_ports = (args.video_port, args.audio_port)
    if found is None and not desktop.ports_free(args.host, args.port, udp_ports):
        # Nothing answers, yet the ports are taken: usually a MirrorDesk that is still closing (Quit, then
        # MirrorDesk again at once). It lets go of them within seconds; ask about it only if it does not.
        desktop.wait_ports_free(args.host, args.port, udp_ports, 15)
    if found or not desktop.ports_free(args.host, args.port, udp_ports):
        # An older MirrorDesk (no single-instance answer), a console copy or the
        # Prototype 6 launcher holds the ports: offer to close it instead of failing.
        confirm = (lambda question: True) if args.replace else (
            desktop.ask_yes_no if args.background else desktop.ask_console)
        ok, why = desktop.take_over(args.host, args.port, udp_ports, found, confirm)
        if not ok:
            fail(why)
            return 1
    # WebRTC host candidates on every usable interface even when the HTTP UI is
    # local-only: Firefox never pairs with a loopback-only server (measured with
    # Firefox 157), and ICE/DTLS already require this session's credentials.
    # MIRRORDESK_ICE_ADDRESSES=loopback restores the P6 loopback-only behaviour.
    ice_scope = os.environ.get('MIRRORDESK_ICE_ADDRESSES') or 'all'
    # Quit / start-at-sign-in from the browser: Windows always (plus the icon next to the clock in
    # background mode); macOS and Linux desktops in background mode (the installed app). Servers,
    # Docker and consoles on macOS/Linux stop with their service manager or Ctrl+C instead.
    controls = (desktop.Desktop(local_url, tray=args.background, data_dir=args.data)
                if os.name == 'nt' or args.background else None)
    server = MirrorDeskServer(args.data, port=args.port, video_port=args.video_port, audio_port=args.audio_port,
                              start_receiver=not args.no_receiver, public_host=args.public_host, ice_scope=ice_scope,
                              desktop=controls, usb=not args.no_usb)
    app = server.app()

    async def announce(app):
        print(banner(server, args), flush=True)
        if controls is not None:
            controls.attach(asyncio.get_running_loop())
        if args.open_browser:
            show = controls.open if controls is not None else (lambda: webbrowser.open(local_url))
            threading.Timer(0.5, show).start()
    app.on_startup.append(announce)
    try:
        serve(app, args.host, args.port, server.turn.protocol)
    except OSError as exc:
        fail(f'ERROR: could not listen on {args.host}:{args.port}: {exc}')
        return 1
    except RuntimeError as exc:
        fail(f'ERROR: {exc}')
        return 1
    finally:
        if controls is not None and controls.tray is not None:
            controls.tray.stop()
    return 0


if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())
