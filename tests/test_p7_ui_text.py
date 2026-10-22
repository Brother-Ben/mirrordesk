"""The browser interface's wording: plain language, no marketing or AI buzzwords,
and every element the scripts look up actually exists."""
from pathlib import Path
import re

WEB = Path(__file__).resolve().parents[1] / 'mirrordesk' / 'web'
BUZZWORDS = re.compile(r'\b(AI|A\.I\.|artificial intelligence|smart|magic(al)?|seamless(ly)?|effortless(ly)?|'
                       r'supercharge[sd]?|unleash|revolutionary|cutting[- ]edge|leverage|intelligent|'
                       r'next[- ]gen(eration)?|game[- ]chang(er|ing)|delightful|powered by)\b', re.I)


def visible_text(html):
    html = re.sub(r'<!--.*?-->', ' ', html, flags=re.S)
    html = re.sub(r'<(script|style|svg)\b.*?</\1>', ' ', html, flags=re.S)
    return re.sub(r'<[^>]+>', ' ', html)


def js_strings(js):
    return ' '.join(m.group(2) for m in re.finditer(r"(['`])((?:\\.|(?!\1).)*)\1", js))


def test_no_buzzwords_in_the_interface():
    for name in ('index.html', 'login.html'):
        found = BUZZWORDS.findall(visible_text((WEB / name).read_text(encoding='utf-8')))
        assert not found, f'{name}: {found}'
    for name in ('app.js', 'login.js', 'theme.js'):
        found = BUZZWORDS.findall(js_strings((WEB / name).read_text(encoding='utf-8')))
        assert not found, f'{name}: {found}'


def test_wording_the_user_asked_for():
    """2026-10-20: "0 viewers" instead of "Watching: nobody else", "Audio off/on" instead of
    "Sound off", a "Viewers" list that says "No viewers", and no "A new phone takes over" switch."""
    html = visible_text((WEB / 'index.html').read_text(encoding='utf-8'))
    js = js_strings((WEB / 'app.js').read_text(encoding='utf-8'))
    for gone in ('Nobody else', 'Sound off', 'Sound on', 'Watching now', 'Nobody is watching', 'takes over'):
        assert gone not in html and gone not in js, gone
    for wanted in ('Audio off', 'Audio on', ' viewers', 'No viewers'):
        assert wanted in js, wanted
    assert '>Viewers<' in (WEB / 'index.html').read_text(encoding='utf-8')


def test_wording_of_the_2026_10_21_round():
    """"Devices" instead of "Phones", Quit at the bottom left with an "are you sure?" that has
    "Don't ask me again", "Play audio on phone and MirrorDesk", and the two viewer problems."""
    raw = (WEB / 'index.html').read_text(encoding='utf-8')
    html = visible_text(raw)
    assert '>Devices<' in raw and 'data-title="Devices"' in raw and '>Phones<' not in raw
    assert 'Also play the audio on the phone' not in html and 'Play audio on phone and MirrorDesk' in html
    assert "Don't ask me again" in html and 'Quit MirrorDesk?' in html
    nav = raw[raw.index('<nav'):raw.index('</nav>')]
    assert 'id="btn-quit"' in nav[nav.index('nav-bottom'):], 'Quit sits at the bottom of the side bar'
    header = raw[raw.index('<header class="topbar">'):raw.index('</header>')]
    assert 'btn-quit' not in header
    for value, text in (('viewer_cannot_connect', 'Viewer cannot connect'), ('viewer_cannot_see', 'Viewer cannot see the screen')):
        assert f'<option value="{value}">{text}</option>' in raw
    import diagnostics
    assert {'viewer_cannot_connect', 'viewer_cannot_see'} <= diagnostics.ISSUES


def test_every_element_the_script_uses_exists():
    html = (WEB / 'index.html').read_text(encoding='utf-8')
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r"\$\('([a-z0-9-]+)'\)", (WEB / 'app.js').read_text(encoding='utf-8')))
    assert used - ids == set()


def test_pages_and_navigation_match():
    html = (WEB / 'index.html').read_text(encoding='utf-8')
    pages = set(re.findall(r'id="page-([a-z]+)"', html))
    targets = set(re.findall(r'data-page="([a-z]+)"', html)) - {'mirror'}
    assert pages == targets == {'connect', 'share', 'settings', 'developer'}


def test_no_inline_script_or_style():
    """The Content-Security-Policy allows neither; a slip would silently break the page."""
    for name in ('index.html', 'login.html'):
        html = (WEB / name).read_text(encoding='utf-8')
        assert not re.search(r'<script(?![^>]*\bsrc=)[^>]*>', html), name
        assert '<style' not in html and ' style="' not in html, name
        assert not re.search(r'\son[a-z]+="', html), name
