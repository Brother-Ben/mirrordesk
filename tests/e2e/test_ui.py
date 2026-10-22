"""Playwright UI automation for the browser-first MirrorDesk (Prototype 7).

Every test drives the real server + fake UxPlay + real browser engine. Media
is synthetic; passing here is browser/OS compatibility evidence, not phone
acceptance.
"""
import io
import json
import os
import time
import zipfile

import pytest

from e2e_support import close_quietly, require_webrtc, show_controls

pytestmark = pytest.mark.e2e

PLAYING = """() => { const v = document.getElementById('video');
  return v.videoWidth > 0 && v.readyState >= 2 && !v.paused; }"""


def open_viewer(page, server, browser_name):
    page.goto(server.url + '/')
    require_webrtc(page, browser_name)
    page.wait_for_function(PLAYING, timeout=45000)


def viewer_state(page):
    return page.evaluate("""() => { const v = document.getElementById('video'); const s = window.mirrordesk.viewer;
      return {w: v.videoWidth, h: v.videoHeight, mode: s.mode, codec: s.codec,
              decoded: s.last && s.last.decoded, fps: s.last && s.last.fps,
              vjb: s.last && s.last.vjb, vjbn: s.last && s.last.vjbn}; }""")


def decoded_after(page, seconds):
    first = viewer_state(page)['decoded'] or 0
    time.sleep(seconds)
    return (viewer_state(page)['decoded'] or 0) - first


def test_viewer_auto_connects_and_plays_full_phone_resolution(page, live_server, browser_name):
    open_viewer(page, live_server, browser_name)
    state = viewer_state(page)
    assert (state['w'], state['h']) == (498, 1080), state
    if browser_name == 'chromium':
        assert state['mode'] == 'passthrough', 'Chromium must receive the phone stream untouched'
    frames = decoded_after(page, 3.0)
    print(f'{browser_name}: mode={state["mode"]} codec={state["codec"]} frames/3s={frames}')
    assert frames >= 3 * 40, f'expected ~60 fps synthetic source, decoded {frames} in 3 s'
    assert not page.errors, page.errors


def test_status_chips_pin_and_idle_overlay(page, live_server, browser_name):
    open_viewer(page, live_server, browser_name)
    status = live_server.status()
    page.wait_for_function("() => document.querySelector('#chip-phone .value').textContent === 'Mirroring · Wi-Fi'")
    assert page.locator('#chip-receiver .value').inner_text() == 'Ready'
    assert page.locator('#rx-pin').inner_text() == status['receiver']['pin']
    assert page.locator('#idle').is_hidden()
    assert 'Test iPhone' in page.locator('#rx-phone').inner_text()
    assert '498×1080' in page.locator('#rx-source').inner_text()


def test_latency_stays_bounded_during_soak(page, live_server, browser_name):
    """Regression for P6's growing jitter-buffer delay (0.6 s/frame after minutes)."""
    open_viewer(page, live_server, browser_name)
    samples = []
    for _ in range(8):
        time.sleep(2.5)
        s = viewer_state(page)
        samples.append((s['vjb'], s['vjbn']))
    per_interval = []
    for (a, an), (b, bn) in zip(samples, samples[1:]):
        if None not in (a, an, b, bn) and bn > an:
            per_interval.append((b - a) / (bn - an) * 1000)
    if not per_interval:
        pytest.skip(f'{browser_name} does not expose jitterBufferDelay')
    print(f'{browser_name}: video jitter-buffer ms per interval {[round(x) for x in per_interval]}')
    assert max(per_interval) < 250
    assert per_interval[-1] < per_interval[0] + 100, 'delay must not keep growing'


def audio_counters(page):
    return page.evaluate("""() => { const s = window.mirrordesk.viewer.last || {};
      return {samples: s.asamples, concealed: s.aconcealed, discarded: s.adiscarded, packets: s.apackets}; }""")


@pytest.mark.browsers('chromium', 'firefox')
def test_usb_sized_audio_blocks_play_without_dropouts(page, server_factory, browser_name):
    """Regression: the iPhone's USB audio arrives as ~85 ms blocks; aiortc gave every Opus
    packet cut from one block the same RTP timestamp and Chromium discarded 3 of 4
    (measured on hardware: 76% of the sound concealed). Sent here as 4096-sample blocks."""
    server = server_factory(env={'FAKE_UXPLAY_AUDIO_SAMPLES': '4096'})
    server.wait_streaming()
    open_viewer(page, server, browser_name)
    time.sleep(8)   # past the browser's start-up fill
    first = audio_counters(page)
    if first['samples'] is None or first['concealed'] is None:
        pytest.skip(f'{browser_name} does not report audio concealment')
    time.sleep(15)
    last = audio_counters(page)
    played = last['samples'] - first['samples']
    concealed = last['concealed'] - first['concealed']
    print(f'{browser_name}: played {played} samples, concealed {concealed}, '
          f'discarded {last["discarded"]}, packets {last["packets"] - first["packets"]}')
    assert played > 15 * 48000 * 0.9, 'sound keeps flowing'
    assert concealed / played < 0.01, 'no dropouts (was 76% before the fix)'
    if last['discarded'] is not None:
        assert last['discarded'] - (first['discarded'] or 0) == 0, 'no packet discarded as a duplicate'


@pytest.mark.browsers('chromium', 'firefox')
def test_wifi_stalls_do_not_cut_the_sound(page, server_factory, browser_name):
    """Measured on a user's ZimaOS box (0.11.1, iPhone over Wi-Fi): now and then the phone's sound
    stalled ~0.1 s, then came all at once, and each stall cost 40-60 ms of missing sound plus 60 ms
    skipped. Sent here as a 100 ms stall every 2 s: once the first one has set the backlog, the
    browser has next to nothing to fill in and nothing is skipped."""
    server = server_factory(env={'FAKE_UXPLAY_AUDIO_BURST': '2,0.1'})
    server.wait_streaming()
    open_viewer(page, server, browser_name)
    time.sleep(7)   # past the browser's start-up fill and the first stalls
    first, before = audio_counters(page), server.status()['source']
    if first['samples'] is None or first['concealed'] is None:
        pytest.skip(f'{browser_name} does not report audio concealment')
    time.sleep(16)
    last, after = audio_counters(page), server.status()['source']
    played = last['samples'] - first['samples']
    concealed = last['concealed'] - first['concealed']
    print(f'{browser_name}: concealed {concealed / played:.2%} of {played} samples; backlog '
          f'{after["audio_backlog_ms"]} ms, jitter {after["audio_jitter_ms"]} ms, missed '
          f'{after["audio_missed_ms"] - before["audio_missed_ms"]} ms, skipped {after["audio_dropped_ms"] - before["audio_dropped_ms"]} ms')
    assert played > 16 * 48000 * 0.9, 'sound keeps flowing'
    assert concealed / played < 0.005, 'stalls no longer leave holes (0.11.1: about 3%)'
    assert after['audio_dropped_ms'] == before['audio_dropped_ms'], 'no sound that came late is skipped'
    assert 80 <= after['audio_backlog_ms'] <= 200, 'the backlog covers the stall, no more'


@pytest.mark.browsers('chromium')
def test_seeking_on_the_phone_never_skips_the_new_sound(page, server_factory, browser_name):
    """A seek on the phone: MirrorDesk's UxPlay reports the FLUSH, no sound for a moment, then the
    new sound's first 0.17 s at once. 0.11.1 kept playing the old sound, then skipped all but 40 ms
    of the new burst and ran dry until the phone caught up (a user's diagnostics: 120-300 ms
    skipped and as much missing after every seek). Now the old sound stops at the FLUSH and the
    new sound plays from its first moment, without holes."""
    server = server_factory(env={'FAKE_UXPLAY_AUDIO_SEEK': '3,0.3'})
    server.wait_streaming()
    open_viewer(page, server, browser_name)
    time.sleep(4)
    before = server.status()['source']
    time.sleep(13)
    after = server.status()['source']
    flushes = after['audio_flushes'] - before['audio_flushes']
    print(f'{browser_name}: {flushes} seeks; skipped {after["audio_dropped_ms"] - before["audio_dropped_ms"]} ms, '
          f'missed {after["audio_missed_ms"] - before["audio_missed_ms"]} ms, stopped at the seek '
          f'{after["audio_flushed_ms"] - before["audio_flushed_ms"]} ms')
    assert flushes >= 3
    assert after['audio_flushed_ms'] > before['audio_flushed_ms'], 'the old sound stopped at the seek'
    assert after['audio_dropped_ms'] == before['audio_dropped_ms'], 'nothing of the new sound is skipped'
    assert after['audio_missed_ms'] - before['audio_missed_ms'] <= 60, 'and it runs without holes'
    assert page.evaluate(PLAYING)


@pytest.mark.browsers('chromium')
def test_still_phone_screen_is_not_resent_over_and_over(page, server_factory, browser_name):
    """Regression (ZimaOS over Tailscale): a still iPhone screen sends nothing; Chromium asks
    for a keyframe 3 s after its last frame and every request replayed the whole cached GOP,
    which restarted that timer (2.4 MB every ~4 s, measured, for as long as it stood still)."""
    server = server_factory(env={'FAKE_UXPLAY_STILL_AFTER': '1.5'})
    server.wait_streaming()
    open_viewer(page, server, browser_name)
    time.sleep(5)  # past Chromium's 3 s timer
    packets = "() => window.mirrordesk.viewer.last && window.mirrordesk.viewer.last.vpackets"
    first = page.evaluate(packets)
    time.sleep(12)
    resent = page.evaluate(packets) - first
    print(f'{browser_name}: {resent} video packets in 12 s of a still screen')
    assert resent < 50, 'nothing new on the phone, so nothing is sent again'
    assert page.evaluate(PLAYING), 'the picture stays up'


def test_switch_to_compatibility_mode_and_back(page, live_server, browser_name):
    open_viewer(page, live_server, browser_name)
    show_controls(page)
    page.select_option('#mode', 'compat')
    page.wait_for_function("() => window.mirrordesk.viewer.mode === 'compat'", timeout=20000)
    page.wait_for_function(PLAYING, timeout=30000)
    assert decoded_after(page, 2.0) > 20
    show_controls(page)
    page.select_option('#mode', 'auto')
    page.wait_for_function("() => window.mirrordesk.viewer.mode && window.mirrordesk.viewer.mode !== 'compat'"
                           " || window.mirrordesk.viewer.codec && window.mirrordesk.viewer.mode === 'compat'", timeout=20000)
    page.wait_for_function(PLAYING, timeout=30000)


def test_stop_and_start_receiver_from_browser(page, live_server, browser_name):
    open_viewer(page, live_server, browser_name)
    page.locator('#nav-connect').click()
    page.once('dialog', lambda dialog: dialog.accept())
    page.locator('#rx-stop').click()
    page.wait_for_function("() => document.querySelector('#chip-receiver .value').textContent === 'Stopped'", timeout=15000)
    page.wait_for_function("() => !document.getElementById('idle').hidden", timeout=15000)
    page.locator('#rx-start').click()
    page.wait_for_function("() => document.querySelector('#chip-receiver .value').textContent === 'Ready'", timeout=30000)
    page.wait_for_function("() => document.querySelector('#chip-phone .value').textContent.startsWith('Mirroring')", timeout=30000)
    page.wait_for_function(PLAYING, timeout=30000)
    assert decoded_after(page, 2.0) > 20, 'video resumes after a receiver restart'


def test_settings_change_applies_new_resolution(page, live_server, browser_name):
    open_viewer(page, live_server, browser_name)
    page.locator('#nav-settings').click()
    page.locator('#stab-video').click()
    page.wait_for_function("() => document.querySelector('[name=resolution]').options.length > 0")
    page.select_option('[name=resolution]', '720p')
    page.select_option('[name=fps]', '30')
    page.locator('#settings-form button[type=submit]').click()
    page.wait_for_function("() => /restarting|Saved/.test(document.getElementById('settings-msg').textContent)")
    page.wait_for_function("() => document.getElementById('video').videoHeight === 720", timeout=45000)
    state = viewer_state(page)
    assert (state['w'], state['h']) == (332, 720), state
    saved = json.loads((live_server.data_dir / 'settings.json').read_text(encoding='utf-8'))
    assert saved['resolution'] == '720p' and saved['fps'] == 30


def test_two_viewers_share_the_stream(context, browser, live_server, browser_name):
    """Two devices (separate browser contexts) watching at once; closing one frees its slot."""
    other = browser.new_context(**({'ignore_https_errors': True}))
    try:
        first, second = context.new_page(), other.new_page()
        for page in (first, second):
            open_viewer(page, live_server, browser_name)
        # Viewers are the other devices, not this browser itself.
        first.wait_for_function("() => document.querySelector('#chip-viewers .value').textContent === '1 viewer'",
                                timeout=15000)
        counts = [decoded_after(page, 2.0) for page in (first, second)]
        assert min(counts) > 20, (counts, [viewer_state(p) for p in (first, second)], live_server.status()['viewers'])
        first.locator('#nav-share').click()
        first.wait_for_function("() => document.querySelectorAll('#watchers li:not(.empty)').length === 1")
        assert first.locator('#watch-count').inner_text() == '1'
        second.close()
        first.wait_for_function("() => document.querySelector('#chip-viewers .value').textContent === '0 viewers'",
                                timeout=20000)
        first.wait_for_function("() => document.querySelector('#watchers .empty').textContent === 'No viewers'")
    finally:
        close_quietly(other)


def test_stats_overlay_reports_stream(page, live_server, browser_name):
    open_viewer(page, live_server, browser_name)
    show_controls(page)
    page.locator('#btn-stats').click()
    page.wait_for_function("() => document.getElementById('stats').textContent.includes('498×1080')", timeout=10000)
    text = page.locator('#stats').inner_text()
    assert 'mode' in text and 'bitrate' in text


BAR_SHOWN = "() => getComputedStyle(document.getElementById('controls')).visibility === 'visible'"
BAR_HIDDEN = "() => getComputedStyle(document.getElementById('controls')).visibility === 'hidden'"


def test_player_bar_hides_after_5_seconds_even_with_the_pointer_on_the_picture(page, live_server):
    """Someone screen-sharing this window leaves the pointer resting on the picture: the bar
    (Disconnect, Sound, ...) still goes away after 5 s, and while hidden it takes no clicks."""
    page.goto(live_server.url + '/')
    page.wait_for_selector('#controls')
    show_controls(page)
    time.sleep(4)
    assert page.evaluate(BAR_SHOWN), 'still there before 5 s'
    page.wait_for_function(BAR_HIDDEN, timeout=4000)
    hit = page.evaluate("""() => { const r = document.getElementById('btn-full').getBoundingClientRect();
      return !!document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2).closest('#controls'); }""")
    assert not hit, 'a hidden bar must not catch clicks meant for the picture'
    show_controls(page)
    page.wait_for_function(BAR_SHOWN, timeout=2000)
    # With the mouse resting on the bar itself it stays.
    box = page.locator('#btn-stats').bounding_box()
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    time.sleep(6.5)
    assert page.evaluate(BAR_SHOWN), 'the bar stays under the mouse'


def test_mark_issue_and_export_diagnostics(page, live_server, browser_name):
    open_viewer(page, live_server, browser_name)
    page.locator('#nav-developer').click()
    page.select_option('#issue', 'no_mirrordesk_option')
    page.locator('#btn-mark').click()
    page.wait_for_function("() => document.getElementById('diag-msg').textContent === 'Issue marked.'")
    with page.expect_download() as download_info:
        page.locator('#btn-export').click()
    download = download_info.value
    assert download.suggested_filename.endswith('.zip')
    data = zipfile.ZipFile(io.BytesIO(open(download.path(), 'rb').read()))
    names = data.namelist()
    assert 'manifest.json' in names
    log = data.read('backend.jsonl').decode()
    assert 'no_mirrordesk_option' in log and '"viewer_start"' in log
    assert live_server.status()['receiver']['pin'] not in log.replace('"source_', '')


def test_network_panel_shows_urls_and_access_code(page, live_server, browser_name):
    page.goto(live_server.url + '/')
    page.locator('#nav-share').click()
    page.wait_for_function("() => document.querySelectorAll('#urls li').length > 0", timeout=15000)
    code = page.locator('#access-code').inner_text()
    assert len(code) == 6 and code.isalnum()


@pytest.mark.parametrize('device', ['iPhone 13', 'Pixel 7', 'iPad (gen 7)'])
def test_mobile_layouts_have_no_horizontal_scroll(pw, browser, browser_name, live_server, device):
    if browser_name == 'firefox':
        pytest.skip('Firefox does not support Playwright mobile emulation (isMobile)')
    descriptor = dict(pw.devices[device])
    descriptor.pop('default_browser_type', None)
    ctx = browser.new_context(**descriptor)
    try:
        page = ctx.new_page()
        page.goto(live_server.url + '/')
        page.wait_for_selector('#controls')
        assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth + 1')
        for selector in ('#btn-full', '#btn-sound', '#mode', '#nav-settings'):
            assert page.locator(selector).is_visible(), selector
        if page.evaluate('() => typeof RTCPeerConnection === "function"'):
            page.wait_for_function(PLAYING, timeout=45000)
    finally:
        close_quietly(ctx)


def test_appearance_theme_and_color_are_remembered(page, live_server, browser_name):
    page.goto(live_server.url + '/')
    root = "() => [document.documentElement.dataset.theme, document.documentElement.dataset.color]"
    background = "() => getComputedStyle(document.querySelector('.topbar')).backgroundColor"
    page.locator('#nav-settings').click()
    page.locator('#theme-options [data-value="light"]').click()
    assert page.evaluate(root) == ['light', 'none']
    plain = page.evaluate(background)
    page.locator('#color-options [data-value="forest"]').click()
    assert page.evaluate(root) == ['light', 'forest']
    assert page.locator('#color-options [data-value="forest"]').get_attribute('aria-checked') == 'true'
    assert page.evaluate(background) != plain, 'a color theme tints the surfaces'
    page.reload()
    assert page.evaluate(root) == ['light', 'forest'], 'kept in this browser'
    page.locator('#theme-options [data-value="onyx"]').click()
    page.locator('#btn-theme').click()
    assert page.evaluate(root)[0] == 'light'
    page.locator('#btn-theme').click()
    assert page.evaluate(root)[0] == 'onyx', 'the quick switch returns to the chosen dark theme'
    assert not page.errors, page.errors


def test_settings_sections_hold_their_own_fields_and_the_owner_code(page, live_server, browser_name):
    """Settings is split into sections; the owner code lives under Access and loads there (it used
    to load only with the Share page)."""
    page.goto(live_server.url + '/')
    page.locator('#nav-settings').click()
    page.wait_for_function("() => document.querySelector('[name=resolution]').options.length > 0")
    assert page.locator('[name=receiver_name]').is_visible() and page.locator('#theme-options').is_visible()
    assert page.locator('[name=resolution]').is_hidden(), 'one section at a time'
    page.locator('#stab-access').click()
    assert page.locator('[name=max_viewers]').is_visible() and page.locator('#theme-options').is_hidden()
    code = json.loads((live_server.data_dir / 'settings.json').read_text(encoding='utf-8'))['owner_code']
    page.wait_for_function(f"() => document.getElementById('owner-code').textContent === '{code}'", timeout=10000)
    assert page.locator('#owner-code').is_visible()
    assert not page.errors, page.errors


def test_settings_panel_keeps_the_video_playing(page, live_server, browser_name):
    open_viewer(page, live_server, browser_name)
    page.locator('#nav-settings').click()
    assert page.locator('#panel').is_visible() and page.locator('#stage').is_visible()
    assert page.locator('#panel-title').inner_text() == 'Settings'
    assert decoded_after(page, 2.0) > 20, 'video keeps playing beside the settings'
    page.keyboard.press('Escape')
    assert page.locator('#panel').is_hidden()
    assert page.locator('#nav-mirror').get_attribute('aria-current') == 'page'


def test_usb_card_says_when_cable_mirroring_is_unavailable(page, live_server, browser_name):
    page.goto(live_server.url + '/')
    page.locator('#nav-connect').click()
    page.wait_for_function("() => document.getElementById('usb-state').textContent === 'Not available'")
    assert page.locator('#btn-usb-reconnect').is_disabled()


def remote_sign_in(page, server):
    page.goto(server.url + '/')
    assert page.url.endswith('/login')
    code = json.loads((server.data_dir / 'settings.json').read_text(encoding='utf-8'))['access_code']
    page.fill('#code', code)
    page.click('button[type=submit]')
    page.wait_for_url(server.url + '/', timeout=30000)


def went_off(view, timeout=10):
    """The page closed itself (browsers allow it for a window with one page in its history) or shows
    that MirrorDesk is off."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if view.is_closed():
            return True
        try:
            if view.evaluate("() => document.getElementById('idle-title').textContent") == 'MirrorDesk is off':
                return True
        except Exception:   # closed meanwhile
            return True
        time.sleep(0.2)
    return False


@pytest.mark.skipif(os.name != 'nt', reason='Quit / start with Windows are Windows desktop features')
def test_quit_asks_first_then_everything_closes_and_a_restart_brings_it_back(page, context, browser, browser_name,
                                                                           server_factory):
    """Quit (bottom left) asks "Quit MirrorDesk?" with "Don't ask me again"; MirrorDesk then shuts down
    completely, another open page shows it went off and closes itself where the browser allows it,
    and after a restart remote devices are still signed in and Quit no longer asks."""
    server = server_factory()
    server.wait_streaming()
    open_viewer(page, server, browser_name)
    other = context.new_page()
    other.goto(server.url + '/')
    other.wait_for_function("() => document.querySelector('#chip-receiver .value').textContent === 'Ready'")
    remote = browser.new_context(extra_http_headers={'X-Forwarded-For': '192.168.1.77'})
    try:
        remote_page = remote.new_page()
        remote_sign_in(remote_page, server)
        assert remote_page.locator('#btn-quit').is_hidden(), 'only on this computer'
        page.locator('#nav-settings').click()
        page.wait_for_selector('#app-card:not([hidden])')
        assert page.locator('#autostart').is_visible() and page.locator('[name=confirm_quit]').is_checked()
        box = page.locator('#btn-quit').bounding_box()
        assert box['x'] < 120 and box['y'] > 400, 'Quit sits at the bottom left'
        page.locator('#btn-quit').click()
        page.wait_for_selector('#quit-dialog[open]')
        page.locator('#quit-dialog .btn:not(.danger)').click()             # Cancel: nothing happens
        time.sleep(1)
        assert server.process.poll() is None
        page.locator('#btn-quit').click()
        page.locator('#quit-dont-ask').check()
        page.locator('#quit-confirm').click()
        assert server.process.wait(timeout=20) == 0, 'graceful exit'
        for view in (page, other):
            assert went_off(view), 'each open page closed itself or says MirrorDesk is off'

        again = server_factory(port=server.port, data_dir=server.data_dir)
        again.wait_streaming()
        assert json.loads((server.data_dir / 'settings.json').read_text(encoding='utf-8'))['confirm_quit'] is False
        fresh = context.new_page()
        open_viewer(fresh, again, browser_name)
        assert decoded_after(fresh, 2.0) > 20
        remote_page.reload()
        assert not remote_page.url.endswith('/login'), 'remote sign-in survives a restart'
        status = remote_page.evaluate("() => fetch('/api/status').then(r => r.json())")
        assert status['client']['local'] is False
        fresh.locator('#btn-quit').click()                                # "Don't ask me again": no question
        assert again.process.wait(timeout=20) == 0
    finally:
        close_quietly(remote)


def test_viewer_code_device_only_watches(browser, browser_name, server_factory):
    """Regression: a friend watching over Tailscale saw Connect and Settings and could press
    Stop. With the viewer code a device gets the picture and its own player controls only."""
    server = server_factory()
    server.wait_streaming()
    ctx = browser.new_context(extra_http_headers={'X-Forwarded-For': '100.64.0.7'})
    try:
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        remote_sign_in(page, server)
        require_webrtc(page, browser_name)
        page.wait_for_function(PLAYING, timeout=45000)
        for selector in ('#nav-connect', '#nav-share', '#nav-settings', '#nav-developer', '#chip-receiver', '#panel',
                         '#btn-quit'):
            assert page.locator(selector).is_hidden(), selector
        show_controls(page)
        for selector in ('#nav-owner', '#btn-sound', '#btn-full', '#chip-phone'):
            assert page.locator(selector).is_visible(), selector
        assert page.locator('#btn-disconnect').is_hidden(), 'a viewer cannot disconnect the phone'
        stop = "() => fetch('/api/receiver/stop', {method: 'POST', headers: {'X-MirrorDesk': '1'}}).then(r => r.status)"
        assert page.evaluate(stop) == 403
        assert server.status()['receiver']['state'] == 'running'
        page.locator('#nav-owner').click()
        page.wait_for_function("() => document.getElementById('login-title').textContent === 'Sign in as the owner'")
        page.fill('#code', json.loads((server.data_dir / 'settings.json').read_text(encoding='utf-8'))['owner_code'])
        page.click('button[type=submit]')
        page.wait_for_url(server.url + '/', timeout=30000)
        page.locator('#nav-settings').click()
        page.wait_for_function("() => document.querySelector('[name=resolution]').options.length > 0")
        assert not errors, errors
    finally:
        close_quietly(ctx)


def test_remote_device_must_sign_in(browser, browser_name, server_factory):
    """A non-local Host (as a LAN/Tailscale device would send) requires the access code."""
    server = server_factory()
    ctx = browser.new_context(extra_http_headers={'X-Forwarded-For': '192.168.1.77'})
    try:
        page = ctx.new_page()
        page.goto(server.url + '/')
        assert page.url.endswith('/login')
        page.fill('#code', 'WRONG1')
        page.click('button[type=submit]')
        page.wait_for_function("() => document.getElementById('login-msg').textContent.includes('not right')")
        code = json.loads((server.data_dir / 'settings.json').read_text(encoding='utf-8'))['access_code']
        page.fill('#code', code.lower())
        page.click('button[type=submit]')
        page.wait_for_url(server.url + '/', timeout=30000)
        page.wait_for_selector('#chip-phone')
        status = page.evaluate("() => fetch('/api/status').then(r => r.json())")
        assert status['client'] == {'local': False, 'role': 'viewer'}
    finally:
        close_quietly(ctx)


def test_invite_link_signs_a_viewer_in_directly(browser, browser_name, server_factory):
    """The Share page's invitation carries the viewer code: whoever opens it watches at once."""
    server = server_factory()
    server.wait_streaming()
    owner = browser.new_context()
    guest = browser.new_context(extra_http_headers={'X-Forwarded-For': '100.64.0.7'})
    try:
        page = owner.new_page()
        page.goto(server.url + '/')
        page.locator('#nav-share').click()
        page.wait_for_function("() => document.getElementById('invite-url').value.includes('login?code=')", timeout=15000)
        page.wait_for_function("() => document.getElementById('invite-qr').src.startsWith('data:image/svg+xml')", timeout=10000)
        assert page.evaluate("() => document.getElementById('invite-qr').naturalWidth") > 100, 'the QR code renders'
        link = page.locator('#invite-url').input_value()
        code = json.loads((server.data_dir / 'settings.json').read_text(encoding='utf-8'))['access_code']
        assert link.endswith('login?code=' + code)
        viewer_page = guest.new_page()
        # The test server listens on loopback; keep the path and query of the invitation.
        viewer_page.goto(server.url + '/' + link.split('/', 3)[3])
        viewer_page.wait_for_url(server.url + '/', timeout=30000)   # no typing, no button
        require_webrtc(viewer_page, browser_name)
        viewer_page.wait_for_function(PLAYING, timeout=45000)
    finally:
        close_quietly(guest)
        close_quietly(owner)


def test_no_phone_no_viewer_no_player_bar_no_tap_prompt(page, server_factory, browser_name):
    """Regressions from a tester: with no phone connected the page counted itself as a viewer
    ("1 of 4"), showed the player bar, and could show "Tap to start video"."""
    server = server_factory(env={'FAKE_UXPLAY_NO_MEDIA': '1'})
    page.goto(server.url + '/')
    page.wait_for_function("() => document.querySelector('#chip-receiver .value').textContent === 'Ready'", timeout=30000)
    time.sleep(3)
    assert page.evaluate("() => window.mirrordesk.viewer.pc") is None, 'no video connection without a phone'
    assert server.status()['viewers']['count'] == 0
    assert page.locator('#chip-viewers .value').inner_text() == '0 viewers'
    show_controls(page)
    assert page.locator('#controls').is_hidden(), 'no player bar without a phone'
    assert page.locator('#tap').is_hidden()
    assert page.locator('#idle').is_visible() and page.locator('#idle-steps').is_visible()
    assert not page.errors, page.errors


def test_phone_stopping_mirroring_returns_the_page_to_ready(page, server_factory, browser_name):
    """Stop Mirroring on the iPhone: the page goes back to "Ready to mirror" at once (it showed the
    frozen last frame for a minute) and this browser stops holding a viewer place."""
    server = server_factory(env={'FAKE_UXPLAY_STOP_AFTER': '7'})
    server.wait_streaming()
    open_viewer(page, server, browser_name)
    assert server.status()['viewers']['count'] == 1
    page.wait_for_function("() => !document.getElementById('idle').hidden", timeout=15000)
    page.wait_for_function("() => document.getElementById('toast').textContent === 'The device stopped mirroring.'", timeout=5000)
    assert page.locator('#idle-title').inner_text() == 'Ready to mirror'
    assert server.status()['source']['state'] == 'idle'
    page.wait_for_function("() => window.mirrordesk.viewer.pc === null", timeout=10000)
    deadline = time.monotonic() + 10
    while server.status()['viewers']['count'] and time.monotonic() < deadline:
        time.sleep(0.25)
    assert server.status()['viewers']['count'] == 0
    assert page.locator('#controls').is_hidden()


def test_owner_disconnect_button_drops_the_phone_but_keeps_the_code(page, live_server, browser_name):
    """Disconnect stops that phone's mirroring; the session (its code) goes on (the user, 2026-10-21:
    the code changes every session, not every time someone disconnects)."""
    open_viewer(page, live_server, browser_name)
    pin = live_server.status()['receiver']['pin']
    show_controls(page)
    page.locator('#btn-disconnect').click()
    page.wait_for_function("() => document.getElementById('toast').textContent === 'The device was disconnected.'", timeout=15000)
    starts = """() => fetch('/api/receiver/log').then(r => r.json())
      .then(log => log.lines.filter(l => l.line.includes('Initialized server socket')).length)"""
    page.wait_for_function(f'async () => (await ({starts})()) >= 2', timeout=15000)   # AirPlay restarted: the phone was dropped
    assert live_server.status()['receiver']['pin'] == pin


@pytest.mark.browsers('chromium', 'firefox')
def test_late_viewer_gets_a_picture_from_an_iphone_like_stream(page, server_factory, browser_name):
    """Regression (a tester's iPhone): one keyframe, then only P-frames for minutes. Someone who
    joined after the picture cache gave up saw nothing, in either mode, until the phone's next
    keyframe. Now the shared decoder follows the long GOP and the late viewer starts re-encoded."""
    server = server_factory(env={'FAKE_UXPLAY_ENDLESS_GOP': '1', 'MIRRORDESK_REPLAY_MAX_UNITS': '60'})
    server.wait_streaming()
    deadline = time.monotonic() + 20
    while not server.status()['source']['warm'] and time.monotonic() < deadline:
        time.sleep(0.25)
    status = server.status()
    assert status['source']['warm'] and status['source']['keyframes'] == 1, status['source']
    open_viewer(page, server, browser_name)
    state = viewer_state(page)
    assert state['mode'] == 'compat', state
    assert decoded_after(page, 3.0) > 30, 'the late viewer keeps getting pictures'


FREEZES = """async () => { let out = null; const pc = window.mirrordesk.viewer.pc; if (!pc) return null;
  (await pc.getStats()).forEach(s => { if (s.type === 'inbound-rtp' && s.kind === 'video')
    out = {freezes: s.freezeCount, decoded: s.framesDecoded}; }); return out; }"""


@pytest.mark.browsers('chromium')
@pytest.mark.parametrize('mode', ['auto', 'compat'])
def test_wifi_stalls_in_the_video_no_longer_freeze_the_picture(page, server_factory, browser_name, mode):
    """Measured on a user's iPhone (0.12.0): every viewer froze for a moment every 15-20 s, the
    computer's own screen included, while a steady test stream never froze. Wi-Fi held the phone's
    video back and then delivered it all at once. Sent here as a 150 ms stall every 2 s: once the
    first one has set the delay, frames go out on the phone's own schedule and nothing freezes."""
    server = server_factory(env={'FAKE_UXPLAY_VIDEO_BURST': '2,0.15', 'FAKE_UXPLAY_ORIENTATION': 'landscape'})
    server.wait_streaming()
    page.goto(server.url + '/')
    if mode != 'auto':
        page.select_option('#mode', mode)
    require_webrtc(page, browser_name)
    page.wait_for_function(PLAYING, timeout=45000)
    time.sleep(6)                                   # the first stalls set the delay
    first = page.evaluate(FREEZES)
    time.sleep(14)
    last, source = page.evaluate(FREEZES), server.status()['source']
    print(f'{browser_name}/{mode}: freezes +{last["freezes"] - first["freezes"]}, decoded +{last["decoded"] - first["decoded"]}, '
          f'delay {source["video_delay_ms"]} ms, stalls {source["video_stalls"]}, late {source["video_late_frames"]}')
    assert source['video_timed'] and source['video_stalls'] >= 3   # (the test encoder spaces some bursts out)
    assert source['video_delay_ms'] >= 150, 'the delay covers the stall'
    assert last['freezes'] - first['freezes'] == 0, 'no freeze once the stall is known'
    assert last['decoded'] - first['decoded'] > 14 * 25


@pytest.mark.browsers('chromium', 'firefox')
def test_switching_to_original_mid_stream_shows_the_picture_at_once(page, server_factory, browser_name):
    """Regression (a user's ZimaOS box, 0.12.0): choosing Original a while into mirroring showed
    nothing until the iPhone's next keyframe (331 s apart on that phone), and its replay of a long
    picture cache made Chromium drop most frames. The viewer now gets the current picture re-encoded
    at once, in the same connection, and the phone's own video from its next keyframe on."""
    server = server_factory(env={'FAKE_UXPLAY_ENDLESS_GOP': '1', 'MIRRORDESK_REPLAY_MAX_UNITS': '60'})
    server.wait_streaming()
    deadline = time.monotonic() + 20
    while not server.status()['source']['warm'] and time.monotonic() < deadline:
        time.sleep(0.25)
    open_viewer(page, server, browser_name)
    page.select_option('#mode', 'passthrough')
    page.wait_for_function("() => window.mirrordesk.viewer.mode === 'passthrough'", timeout=20000)
    page.wait_for_function(PLAYING, timeout=15000)
    assert decoded_after(page, 4.0) > 4 * 25, 'Original plays right away'
    source = server.status()['source']
    assert source['video_bridged'] > 50 and source['keyframes'] == 1, source
    assert server.status()['loop']['lag_max_ms'] < 120


@pytest.mark.browsers('chromium')
def test_viewer_without_a_direct_connection_watches_through_the_relay(browser, browser_name, server_factory):
    """Friends of a user opened MirrorDesk's page through Tailscale on their ZimaOS box but never got a
    picture: the page came through, the video's own connection did not. Pages now list MirrorDesk itself
    as a TURN relay on the page's own port; forced here to use only that relay."""
    server = server_factory()
    server.wait_streaming()
    ctx = browser.new_context()
    ctx.add_init_script("localStorage.setItem('mirrordesk.relayOnly', 'true')")
    try:
        page = ctx.new_page()
        page.goto(server.url + '/')
        require_webrtc(page, browser_name)
        page.wait_for_function(PLAYING, timeout=45000)
        assert decoded_after(page, 3.0) > 3 * 40, 'the full frame rate through the relay'
        kind = page.evaluate("""async () => { const r = await window.mirrordesk.viewer.pc.getStats(); let kind = null;
          r.forEach(s => { if (s.type === 'candidate-pair' && s.state === 'succeeded' && (s.nominated || s.selected)) {
            const l = r.get(s.localCandidateId); if (l) kind = l.candidateType + '/' + (l.relayProtocol || ''); } });
          return kind; }""")
        assert kind == 'relay/tcp', kind
    finally:
        close_quietly(ctx)


@pytest.mark.browsers('chromium')
def test_audio_stays_live_when_held_sound_arrives_all_at_once(page, server_factory, browser_name):
    """Regression (a tester's iPhone, 0.10.0): after a pause or seek UxPlay released the sound
    it had held back all at once; the browser's audio buffer grew to 1.5-1.8 s (the "sync"
    issue he marked). The audio buffer must stay small however the sound arrives."""
    server = server_factory(env={'FAKE_UXPLAY_AUDIO_BURST': '3,1.2'})
    server.wait_streaming()
    open_viewer(page, server, browser_name)
    samples = []
    for _ in range(16):
        time.sleep(1)
        value = page.evaluate("() => window.mirrordesk.viewer.last && window.mirrordesk.viewer.last.ajbms")
        if value is not None:
            samples.append(value)
    print(f'{browser_name}: audio jitter buffer ms per second {[round(v) for v in samples]}')
    assert len(samples) >= 8
    # The browser's own start-up takes the first seconds (also with a perfect source);
    # 0.10.0 measured with this source: ~1100-1200 ms from second 4 on, and it stayed there.
    steady = samples[3:]
    assert max(steady) < 300, 'the browser never holds a backlog of late sound'
    assert sorted(steady[-5:])[2] < 150
    status = server.status()['source']
    assert status['audio_dropped_ms'] > 1500, 'the late sound was skipped at the server'
