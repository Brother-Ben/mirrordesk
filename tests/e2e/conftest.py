"""Playwright fixtures: a live MirrorDesk server fed by the fake UxPlay, plus browsers.

Run:  .venv\\Scripts\\python -m pytest -m e2e tests/e2e            (all browsers)
      .venv\\Scripts\\python -m pytest -m e2e tests/e2e --browser chromium
Install browsers once:  .venv\\Scripts\\python -m playwright install chromium firefox webkit

Media is synthetic (libx264 + sine via tests/fake_uxplay.py), never iPhone evidence.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright_api = pytest.importorskip('playwright.sync_api')

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_support import close_quietly  # noqa: E402
RESULTS = ROOT / 'test-results' / 'p7-e2e'
BROWSERS = ('chromium', 'firefox', 'webkit')
LAUNCH = {
    'chromium': {'args': ['--autoplay-policy=no-user-gesture-required']},
    # Autoplay prefs only: ICE stays at Firefox defaults (mDNS-obfuscated host
    # candidates, no loopback), exactly what real users run.
    'firefox': {'firefox_user_prefs': {'media.autoplay.default': 0, 'media.autoplay.blocking_policy': 0}},
    'webkit': {},
}


def pytest_addoption(parser):
    parser.addoption('--browser', action='append', default=None, choices=BROWSERS,
                     help='Browser(s) for e2e tests (default: all three).')


def pytest_generate_tests(metafunc):
    if 'browser_name' in metafunc.fixturenames:
        names = metafunc.config.getoption('--browser') or list(BROWSERS)
        marker = metafunc.definition.get_closest_marker('browsers')
        if marker:
            names = [n for n in names if n in marker.args]
        metafunc.parametrize('browser_name', names, scope='session')


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, 'rep_' + report.when, report)


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, data_dir, host='127.0.0.1', env=None, settings=None, port=None):
        self.port = port or free_port()
        self.video_port = free_port(socket.SOCK_DGRAM)
        self.audio_port = free_port(socket.SOCK_DGRAM)
        self.host = host
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if settings:
            (self.data_dir / 'settings.json').write_text(json.dumps(settings), encoding='utf-8')
        environment = os.environ.copy()
        environment['MIRRORDESK_UXPLAY_CMD'] = json.dumps([sys.executable, str(ROOT / 'tests' / 'fake_uxplay.py')])
        environment.setdefault('FAKE_UXPLAY_CONNECT_DELAY', '0.5')
        # Loopback-only ICE keeps test sockets off the LAN; Firefox needs 'all'
        # because it never offers loopback candidates.
        environment.setdefault('MIRRORDESK_ICE_ADDRESSES', 'loopback')
        environment['MIRRORDESK_USB'] = '0'  # never switch a real plugged-in phone into capture mode
        environment.update(env or {})
        self.log = (self.data_dir / 'server.log').open('w', encoding='utf-8')
        self.process = subprocess.Popen(
            [sys.executable, '-m', 'mirrordesk', '--host', host, '--port', str(self.port), '--data', str(self.data_dir),
             '--video-port', str(self.video_port), '--audio-port', str(self.audio_port)],
            cwd=ROOT, env=environment, stdout=self.log, stderr=subprocess.STDOUT)
        self.url = f'http://127.0.0.1:{self.port}'
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'server exited early; see {self.data_dir / "server.log"}')
            try:
                with urllib.request.urlopen(self.url + '/api/health', timeout=1) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError('server did not become healthy')

    def status(self):
        with urllib.request.urlopen(self.url + '/api/status', timeout=5) as response:
            return json.loads(response.read())

    def wait_streaming(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status['source']['state'] == 'streaming' and status['source']['width']:
                return status
            time.sleep(0.25)
        raise AssertionError(f'no synthetic stream reached the hub: {self.status()}')

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.log.close()


@pytest.fixture
def server_factory(tmp_path, browser_name):
    servers = []

    def make(**kwargs):
        if browser_name == 'firefox':
            kwargs['env'] = {'MIRRORDESK_ICE_ADDRESSES': 'all', **(kwargs.get('env') or {})}
        data_dir = kwargs.pop('data_dir', None) or tmp_path / f'server{len(servers)}'
        server = LiveServer(data_dir, **kwargs)
        servers.append(server)
        return server
    yield make
    for server in servers:
        server.stop()


@pytest.fixture
def live_server(server_factory):
    server = server_factory()
    server.wait_streaming()
    return server


@pytest.fixture(scope='session')
def pw():
    with playwright_api.sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope='session')
def browser(pw, browser_name):
    try:
        instance = getattr(pw, browser_name).launch(headless=True, **LAUNCH[browser_name])
    except Exception as exc:  # browser not installed on this machine
        pytest.skip(f'{browser_name} unavailable: {exc}')
    yield instance
    instance.close()


@pytest.fixture
def context(browser, browser_name, request):
    ctx = browser.new_context(viewport={'width': 1280, 'height': 860}, accept_downloads=True)
    ctx.tracing.start(screenshots=True, snapshots=True)
    yield ctx
    failed = getattr(request.node, 'rep_call', None) is not None and request.node.rep_call.failed
    if failed:
        RESULTS.mkdir(parents=True, exist_ok=True)
        name = request.node.name.replace('[', '-').replace(']', '').replace('/', '_')
        for index, page in enumerate(ctx.pages):
            try:
                page.screenshot(path=str(RESULTS / f'{name}-{index}.png'))
            except Exception:
                pass
        ctx.tracing.stop(path=str(RESULTS / f'{name}-trace.zip'))
    else:
        ctx.tracing.stop()
    close_quietly(ctx)


@pytest.fixture
def page(context, browser_name):
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.errors = errors
    return page
