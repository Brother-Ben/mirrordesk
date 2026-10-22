"""MirrorDesk's change to UxPlay (third_party/uxplay/mirrordesk): the AirPlay audio buffer, and
the timestamps of forwarded video.

UxPlay waited for audio packets the phone skipped after a pause, seek or new video until 256
packets had queued up behind them (~2.8 s of AAC-ELD), so the sound cut out that long and then
came all at once (a tester's iPhone: 2.76 s of sound dropped after every seek). The harness
(tests/uxplay/raop_buffer_harness.c) runs UxPlay's own buffer code, original and changed, with
the packet patterns of a pause or seek. Skipped without a C compiler.
"""
import difflib
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UXPLAY = ROOT / 'third_party' / 'uxplay'
OVERLAY = UXPLAY / 'mirrordesk'
COMMIT = 'd29dfabe102aba137170121077f55390e875310c'
TARBALL = UXPLAY / f'UxPlay-{COMMIT}.tar.gz'
CHANGED = ('lib/raop_buffer.c', 'lib/raop_buffer.h', 'lib/raop_rtp.c', 'renderers/video_renderer.c')


def compiler():
    candidates = [r'C:\msys64\ucrt64\bin\gcc.exe'] if os.name == 'nt' else []
    candidates += [shutil.which('gcc'), shutil.which('cc'), shutil.which('clang')]
    return next((c for c in candidates if c and Path(c).is_file()), None)


@pytest.fixture(scope='module')
def source(tmp_path_factory):
    out = tmp_path_factory.mktemp('uxplay')
    with tarfile.open(TARBALL) as tar:
        members = [m for m in tar.getmembers() if m.isfile() and ('/lib/' in m.name or '/renderers/' in m.name)]
        tar.extractall(out, members=members, filter='data')
    return next(out.iterdir())


def test_overlay_changes_only_the_audio_buffer_and_video_timestamps(source):
    files = sorted(p.relative_to(OVERLAY).as_posix() for p in OVERLAY.rglob('*') if p.is_file())
    assert files == sorted(CHANGED)
    for name in CHANGED:
        assert (source / name).is_file(), f'{name} is not in UxPlay {COMMIT[:7]}: rebase the overlay'


def test_patch_file_matches_the_overlay(source):
    """third_party/uxplay/mirrordesk.patch documents the change (GPL corresponding source)."""
    expected = []
    for name in CHANGED:
        before = (source / name).read_text(encoding='utf-8').splitlines(keepends=True)
        after = (OVERLAY / name).read_text(encoding='utf-8').splitlines(keepends=True)
        expected += difflib.unified_diff(before, after, f'a/{name}', f'b/{name}')
    assert (UXPLAY / 'mirrordesk.patch').read_text(encoding='utf-8') == ''.join(expected)


def _run(cc, source, out, patched):
    exe = out / ('patched' if patched else 'original')
    lib = source / 'lib'
    args = [cc, '-O2', '-o', str(exe), str(ROOT / 'tests' / 'uxplay' / 'raop_buffer_harness.c'),
            str((OVERLAY / 'lib' if patched else lib) / 'raop_buffer.c'), str(lib / 'byteutils.c')]
    args[1:1] = (['-DMIRRORDESK_PATCHED', f'-I{OVERLAY / "lib"}'] if patched else []) + [f'-I{lib}']
    if os.name == 'nt':
        args.append('-lws2_32')
    env = dict(os.environ)
    env['PATH'] = str(Path(cc).parent) + os.pathsep + env.get('PATH', '')
    built = subprocess.run(args, capture_output=True, text=True, env=env)
    if built.returncode != 0:
        pytest.skip('the UxPlay buffer harness does not build here: ' + built.stderr[-400:])
    ran = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=60)
    assert ran.returncode == 0, ran.stderr
    results = {}
    for line in ran.stdout.splitlines():
        name, *fields = line.split()
        results[name] = {k: (v if k == 'order' else int(v)) for k, v in (f.split('=') for f in fields)}
    return results


@pytest.fixture(scope='module')
def harness(source, tmp_path_factory):
    cc = compiler()
    if cc is None:
        pytest.skip('no C compiler')
    out = tmp_path_factory.mktemp('harness')
    return _run(cc, source, out, patched=False), _run(cc, source, out, patched=True)


def test_original_holds_sound_back_after_a_seek(harness):
    original, _ = harness
    # 255 packets of 10.9 ms waited behind the skipped ones: the 2.8 s cutout testers hear.
    for scenario in ('gap', 'flush', 'flush_late'):
        assert original[scenario]['held_after_gap'] >= 200, original[scenario]


def test_changed_buffer_plays_new_sound_at_once(harness):
    _, patched = harness
    for scenario, result in patched.items():
        assert result['lost'] == 0 and result['order'] == 'ok', (scenario, result)
    for scenario in ('gap', 'flush', 'flush_late', 'wrap', 'long_gap'):
        assert patched[scenario]['held_after_gap'] <= 1, (scenario, patched[scenario])
    # A single lost packet still gets its chance to be resent and played in order.
    assert patched['resend']['released'] == 100 and patched['resend']['max_held'] <= 8
    assert patched['steady']['max_held'] == 1


def test_flush_is_reported_in_the_words_mirrordesk_listens_for():
    """The phone's FLUSH (pause, seek, next video) reaches MirrorDesk as one line on stdout, pushed
    out at once (into a pipe, stdout is block-buffered: Windows has no stdbuf)."""
    from mirrordesk import receiver
    rtp = (OVERLAY / 'lib' / 'raop_rtp.c').read_text(encoding='utf-8')
    flush = rtp[rtp.index('if (flush != NO_FLUSH)'):]
    assert f'"{receiver._AUDIO_FLUSH}"' in flush[:flush.index('audio_flush(raop_rtp->callbacks.cls)')]
    assert 'fflush(stdout);' in flush[:flush.index('audio_flush(raop_rtp->callbacks.cls)')]


def test_forwarded_video_carries_the_phones_frame_time(source):
    """Only frames forwarded as RTP get the phone's frame time (UxPlay's own display path is
    untouched), counted from the stream's first frame and started again with every stream."""
    original = (source / 'renderers' / 'video_renderer.c').read_text(encoding='utf-8')
    changed = (OVERLAY / 'renderers' / 'video_renderer.c').read_text(encoding='utf-8')
    render = changed[changed.index('uint64_t video_renderer_render_buffer('):]
    render = render[:render.index('gst_buffer_fill(buffer, 0, data, *data_len);')]
    assert 'if (sync) {\n            GST_BUFFER_PTS(buffer) = pts;\n        } else if (rtp_output) {' in render
    assert 'GST_BUFFER_PTS(buffer) = pts - rtp_first_frame_time;' in render
    start = changed[changed.index('void video_renderer_start() {'):]
    assert 'rtp_first_frame_time = 0;' in start[:start.index('\n}\n')]
    assert 'rtp_output = rtp;' in changed
    # UxPlay's own display path (sync) keeps its timestamps as they were.
    assert original.count('GST_BUFFER_PTS(buffer) = pts;') == changed.count('GST_BUFFER_PTS(buffer) = pts;')


def test_every_build_applies_the_overlay():
    """Docker image, Linux and macOS installers, and the Windows bundle tool copy the overlay over
    the extracted source, and mark the build so installs with the old receiver rebuild it."""
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert 'third_party/uxplay/mirrordesk' in dockerfile
    for script in (ROOT / 'packaging' / 'linux' / 'install.sh', ROOT / 'packaging' / 'macos' / 'install.sh'):
        text = script.read_text(encoding='utf-8')
        assert 'third_party/uxplay/mirrordesk/.' in text and 'UXPLAY_BUILD=' in text, script
    assert "'mirrordesk'" in (ROOT / 'tools' / 'build_uxplay_bundle.py').read_text(encoding='utf-8')
    assert sys.version_info >= (3, 12)   # tarfile's extraction filter
