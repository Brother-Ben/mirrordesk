"""Tester packages (tools/make_tester_package.py) and the installers' pins: the Linux
zip, Unix permissions in the macOS and Linux zips, and checksums/versions that
must agree across the Dockerfile, the installers and the bundled UxPlay source."""
import hashlib
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import make_tester_package as packager  # noqa: E402

from mirrordesk import __version__  # noqa: E402

UXPLAY = ROOT / 'third_party' / 'uxplay' / 'UxPlay-d29dfabe102aba137170121077f55390e875310c.tar.gz'
SCRIPTS = [ROOT / 'packaging' / 'linux' / 'install.sh', ROOT / 'packaging' / 'macos' / 'install.sh', ROOT / 'packaging' / 'macos' / 'Install MirrorDesk.command',
           ROOT / 'packaging' / 'macos' / 'Uninstall MirrorDesk.command', ROOT / 'packaging' / 'unix' / 'start-mirrordesk.sh',
           ROOT / 'docker' / 'entrypoint.sh', ROOT / 'docker' / 'zimaos-install.sh']


def _pin(path, name):
    match = re.search(rf'^\s*(?:ARG\s+)?\$?{name}\s*=\s*\'?([0-9a-z.]+)', path.read_text(encoding='utf-8'), re.M)
    assert match, f'{name} not found in {path}'
    return match.group(1)


def test_uxplay_source_matches_every_pin():
    digest = hashlib.sha256(UXPLAY.read_bytes()).hexdigest()
    for path in (ROOT / 'Dockerfile', ROOT / 'packaging' / 'linux' / 'install.sh', ROOT / 'packaging' / 'macos' / 'install.sh'):
        assert _pin(path, 'UXPLAY_SHA256') == digest, path
        assert _pin(path, 'UXPLAY_COMMIT') == 'd29dfabe102aba137170121077f55390e875310c', path
    assert digest in (ROOT / 'third_party' / 'uxplay' / 'SOURCE.txt').read_text(encoding='utf-8')


def test_uv_version_is_the_same_everywhere():
    windows = re.search(r"\$UvVersion = '([0-9.]+)'", (ROOT / 'packaging' / 'windows' / 'setup' / 'setup.ps1')
                        .read_text(encoding='utf-8')).group(1)
    assert _pin(ROOT / 'packaging' / 'linux' / 'install.sh', 'UV_VERSION') == windows
    assert _pin(ROOT / 'packaging' / 'macos' / 'install.sh', 'UV_VERSION') == windows


def test_image_tags_follow_the_version():
    assert f'org.opencontainers.image.version="{__version__}"' in (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert f'IMAGE=mirrordesk:{__version__}' in (ROOT / 'docker' / 'zimaos-install.sh').read_text(encoding='utf-8')


def test_pasted_stacks_download_this_version_from_github():
    """Both stack files name the image the GitHub workflow publishes, so pasting one into Dockge
    is enough; nothing may stop Docker from downloading it."""
    image = packager.compose_image()
    assert re.fullmatch(rf'ghcr\.io/[a-z0-9-]+/mirrordesk:{re.escape(__version__)}', image), image
    for path in (ROOT / 'docker' / 'compose.yaml', ROOT / 'docker' / 'dockge' / 'compose.yaml'):
        text = path.read_text(encoding='utf-8')
        assert f'    image: {image}\n' in text, path
        assert 'pull_policy' not in text and 'build:' not in text, path
    workflow = (ROOT / '.github' / 'workflows' / 'image.yml').read_text(encoding='utf-8')
    assert 'images: ghcr.io/${{ github.repository_owner }}/mirrordesk' in workflow
    assert 'platforms: linux/amd64,linux/arm64' in workflow


def test_linux_installer_gives_its_build_the_stacks_image_name():
    installer = (ROOT / 'packaging' / 'linux' / 'install.sh').read_text(encoding='utf-8')
    assert 'dk build -t "$IMAGE" -t "$STACK_IMAGE"' in installer
    assert 'sed -e "s#^    image: .*#    image: $STACK_IMAGE#"' in installer


@pytest.mark.parametrize('path', SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_are_posix_sh_with_unix_line_ends(path):
    data = path.read_bytes()
    assert b'\r' not in data, 'CRLF breaks sh'
    assert data.startswith(b'#!/bin/sh\n')


def test_usb_lines_in_compose_match_the_installers_sed_patterns():
    compose = (ROOT / 'docker' / 'compose.yaml').read_text(encoding='utf-8').splitlines()
    assert '      # - /dev/bus/usb:/dev/bus/usb' in compose
    assert '    # device_cgroup_rules:' in compose and '    #   - "c 189:* rmw"' in compose


def _stage_small(tmp_path):
    out = tmp_path / 'MirrorDesk'
    (out / 'app' / 'mirrordesk').mkdir(parents=True)
    (out / 'app' / 'mirrordesk' / '__init__.py').write_text('x = 1\n')
    (out / 'install.sh').write_text('#!/bin/sh\necho installed "$@"\n')
    (out / 'Install MirrorDesk.command').write_text('#!/bin/sh\n')
    return out


def test_linux_package_is_a_zip_with_the_program_the_installer_and_the_guides(tmp_path, monkeypatch):
    """A tester opening the Linux package sees the program, install.sh and the guides, like the
    Windows and macOS zips (it was one self-extracting .sh, which looked like it had no files)."""
    monkeypatch.setattr(packager, 'write_icons', lambda app, kind: None)
    target = packager.build('linux', tmp_path / 'staging', tmp_path / 'dist')
    assert target is not None and target.name == packager.package_name('linux', 'zip')
    with zipfile.ZipFile(target) as archive:
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in archive.infolist()}
        guide = archive.read('MirrorDesk/INSTALL-GUIDE.html').decode('utf-8')
        readme = archive.read('MirrorDesk/README.txt')
    assert modes['MirrorDesk/install.sh'] == 0o755 and modes['MirrorDesk/app/mirrordesk/server.py'] == 0o644
    for name in ('MirrorDesk/app/Dockerfile', 'MirrorDesk/app/third_party/uxplay/SOURCE.txt', 'MirrorDesk/TESTER-GUIDE.html',
                 'MirrorDesk/NOTICE.txt', 'MirrorDesk/VERSION.txt'):
        assert name in modes, name
    assert target.name in guide and '__LINUX_FILE__' not in guide and 'Linux.sh' not in guide
    assert b'\r' not in readme and b'MirrorDesk/install.sh' in readme


def test_macos_zip_keeps_execute_permissions(tmp_path):
    out = _stage_small(tmp_path)
    target = packager.make_zip(out, tmp_path / 'mac.zip', unix=True)
    with zipfile.ZipFile(target) as archive:
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in archive.infolist()}
    assert modes['MirrorDesk/Install MirrorDesk.command'] == 0o755 and modes['MirrorDesk/install.sh'] == 0o755
    assert modes['MirrorDesk/app/mirrordesk/__init__.py'] == 0o644


def test_privacy_gate_rejects_private_files_and_strings(tmp_path, monkeypatch):
    out = _stage_small(tmp_path)
    (out / 'app' / '.runtime').mkdir()
    (out / 'app' / '.runtime' / 'settings.json').write_text('{}')
    (out / 'app' / 'notes.txt').write_text('built by Secret-PC-42')
    monkeypatch.setattr(packager, 'private_needles', lambda: {'secret-pc-42': 'computer name'})
    problems, _ = packager.scan(out)
    assert any('.runtime/settings.json' in p for p in problems)
    assert any('notes.txt: contains the computer name' in p for p in problems)
