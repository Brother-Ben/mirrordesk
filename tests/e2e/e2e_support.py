"""Shared helpers for the Playwright suite (kept out of conftest to avoid name clashes)."""
import pytest


def close_quietly(context):
    """Close a browser context, tolerating known Playwright-Firefox teardown errors
    (e.g. 'Browser.removeBrowserContext ... _maybeDontRestoreTabs') that occur after
    every assertion of the test has already passed."""
    try:
        context.close()
    except Exception as exc:  # cleanup only; never masks a product assertion
        print(f'context close ignored: {str(exc)[:160]}')


def show_controls(page):
    """The player bar hides 5 s after the pointer last moved: move it over the picture, as a person would."""
    box = page.locator('#stage').bounding_box()
    for offset in (0, 8):
        page.mouse.move(box['x'] + box['width'] / 3 + offset, box['y'] + box['height'] / 3 + offset)


def require_webrtc(page, browser_name):
    if not page.evaluate('() => typeof RTCPeerConnection === "function"'):
        pytest.skip(f'{browser_name} build on this OS has no WebRTC (e.g. Playwright WebKit on Windows)')
