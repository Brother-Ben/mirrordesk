"""Android in the browser: mirroring, phone control and the Phones page (Playwright).

The phone is tests/fake_adb.py (adb + scrcpy-server 5.0.1 protocol, synthetic
H.264 and a tone). Browser/UI evidence only, not real-phone acceptance.
"""
import json
import sys
import time
from pathlib import Path

import pytest

from e2e_support import require_webrtc, show_controls

pytestmark = pytest.mark.e2e
FAKE_ADB = str(Path(__file__).resolve().parents[1] / 'fake_adb.py')
PLAYING = """() => { const v = document.getElementById('video');
  return v.videoWidth > 0 && v.readyState >= 2 && !v.paused; }"""


def android_server(server_factory, tmp_path, **env):
    state = tmp_path / 'fake-adb'
    settings = env.pop('settings', None)
    server = server_factory(settings=settings, env={
        'MIRRORDESK_ANDROID': '1', 'MIRRORDESK_ADB_CMD': json.dumps([sys.executable, FAKE_ADB]),
        'FAKE_ADB_STATE': str(state), 'FAKE_SCRCPY_SIZE': '360x800', 'FAKE_UXPLAY_NO_MEDIA': '1', **env})
    server.adb_state = state
    return server


def wait_android(server, state='streaming', timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = server.status()
        if status['android']['state'] == state and (state != 'streaming' or status['source']['width']):
            return status
        time.sleep(0.25)
    raise AssertionError(f'android never reached {state}: {server.status()["android"]}')


def control_log(server):
    path = server.adb_state / 'control.log'
    return ''.join(path.read_text().split()) if path.exists() else ''


@pytest.mark.browsers('chromium', 'firefox')
def test_android_phone_mirrors_and_is_controlled_from_the_page(page, server_factory, tmp_path, browser_name):
    server = android_server(server_factory, tmp_path)
    status = wait_android(server)
    assert status['source']['input'] == 'android' and (status['source']['width'], status['source']['height']) == (360, 800)
    page.goto(server.url + '/')
    require_webrtc(page, browser_name)
    page.wait_for_function(PLAYING, timeout=45000)
    assert page.evaluate("() => [document.getElementById('video').videoWidth, document.getElementById('video').videoHeight]") == [360, 800]
    page.wait_for_function("() => document.querySelector('#chip-phone .value').textContent === 'Mirroring · Android'")
    page.wait_for_function("() => !document.getElementById('phone-keys').hidden")
    # Navigation buttons and a tap in the middle of the picture reach the phone.
    show_controls(page)
    page.locator('#phone-keys [data-key="home"]').click()
    box = page.locator('#video').bounding_box()
    page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.keyboard.type('hi')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not ('00000000030000' in control_log(server) and '0201' in control_log(server)):
        time.sleep(0.2)
    log = control_log(server)
    assert '000000000300000000' in log, 'Home key (keycode 3) sent'
    assert '0200fffffffffffffffe' in log and '0201fffffffffffffffe' in log, 'tap = finger down + up'
    assert '01000000016869' in log or ('0100000001' + 'h'.encode().hex()) in log, 'typed text sent'
    # The Phones page opens on the Android tab (the phone that mirrors) and describes it.
    page.locator('#nav-connect').click()
    assert page.locator('#tab-android').get_attribute('aria-selected') == 'true'
    assert page.locator('#android-card').is_visible() and page.locator('#now-card').is_visible()
    page.wait_for_function("() => document.getElementById('android-state').textContent === 'Mirroring'")
    assert 'Pixel 8' in page.locator('#android-device').inner_text()
    assert page.locator('#android-transport').inner_text() == 'USB cable'
    assert page.locator('#android-audio').inner_text() == 'On'
    assert not page.errors, page.errors


@pytest.mark.browsers('chromium')
def test_view_only_setting_turns_phone_control_off(page, server_factory, tmp_path, browser_name):
    server = android_server(server_factory, tmp_path, settings={'android_control': 'off'})
    wait_android(server)
    page.goto(server.url + '/')
    require_webrtc(page, browser_name)
    page.wait_for_function(PLAYING, timeout=45000)
    assert page.evaluate("() => document.getElementById('phone-keys').hidden")
    status = page.evaluate("() => fetch('/api/status').then(r => r.json())")
    assert status['android']['control'] is False
    refused = page.evaluate("""() => new Promise((resolve) => {
      const ws = new WebSocket('ws://' + location.host + '/api/android/control');
      ws.onopen = () => resolve('open'); ws.onerror = () => resolve('refused'); })""")
    assert refused == 'refused'


@pytest.mark.browsers('chromium')
def test_missing_tools_offer_the_download_and_pairing_validates(page, server_factory, tmp_path):
    server = server_factory(env={'MIRRORDESK_ANDROID': '1', 'MIRRORDESK_ADB': str(tmp_path / 'no-adb.exe'),
                                 'FAKE_UXPLAY_NO_MEDIA': '1'})
    page.goto(server.url + '/')
    page.locator('#nav-connect').click()
    page.locator('#tab-android').click()
    page.wait_for_function("() => document.getElementById('android-state').textContent === 'Not set up'")
    assert page.locator('#btn-android-install').is_visible()
    assert 'Install Android tools' in page.locator('#android-message').inner_text()
    # Wi-Fi pairing form: input is validated on the server, the reason is shown.
    page.locator('#android-wifi summary').click()
    page.fill('#android-pair-form [name=host]', '192.168.1.23')
    page.fill('#android-pair-form [name=port]', '37123')
    page.fill('#android-pair-form [name=code]', '123456')
    page.locator('#android-pair-form button[value=pair]').click()
    page.wait_for_function("() => /Install the Android tools first/.test(document.getElementById('android-pair-msg').textContent)")
