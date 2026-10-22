"""Build the tester packages for Windows, macOS and Linux (developer tool).

    .venv\\Scripts\\python tools\\make_tester_package.py [--target windows|macos|linux|all]
    (Windows needs build_uxplay_bundle.py and build_wdi_simple.py first)

Creates in dist/ (one top folder "MirrorDesk" in each archive):
  MirrorDesk-Tester-<version>-<date>-Windows.zip   the app, the self-contained UxPlay (build/uxplay),
        the cable-driver tool (build/usb-driver), setup/uninstall scripts (packaging/windows).
        Python, its libraries, uv and Google's adb are downloaded by setup on the tester's PC.
  MirrorDesk-Tester-<version>-<date>-macOS.zip     app/ + install.sh and the .command files
        (packaging/macos); the installer builds UxPlay from third_party/uxplay on the Mac.
  MirrorDesk-Tester-<version>-<date>-Linux.zip     app/ + install.sh (packaging/linux): desktop,
        systemd service or Docker/Dockge; app/ is also the Docker build context.
  MirrorDesk-Tester-<version>-<date>-compose.yaml  the Docker Compose / Dockge stack (docker/compose.yaml)
Every package has INSTALL-GUIDE.html, TESTER-GUIDE.html and NOTICE.txt (packaging/common).

Privacy gate: the build FAILS if a package would contain this PC's private data -
settings/keys/pairing records/logs/diagnostics/tests, or (anywhere, text or binary)
the Windows user name, the computer name, the e-mail address, LAN addresses, the
current access code or PIN, or the development iPhone's serial number.
"""
import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

APP_FILES = ['diagnostics.py', 'discovery_probe.py', 'discovery_selftest.py', 'MirrorDesk.pyw', 'pyproject.toml',
             'uv.lock', 'THIRD_PARTY_NOTICES.md']
APP_DIRS = ['mirrordesk', 'licenses', 'third_party/scrcpy', 'third_party/uxplay']
WINDOWS_FILES = ['Start-MirrorDesk.cmd', 'Start-MirrorDesk-Console.cmd']
DOCKER_FILES = ['Dockerfile', '.dockerignore', 'requirements-docker.txt', 'docker/entrypoint.sh', 'docker/compose.yaml']
PACKAGING = ROOT / 'packaging'
TARGETS = ('windows', 'macos', 'linux')
FORBIDDEN_NAMES = re.compile(r'(^|/)(\.runtime|\.venv|tests|test-results|__pycache__|usb-pairing|diagnostics|logs)(/|$)'
                             r'|\.(pem|key|log|jsonl|pyc)$|(^|/)(settings|sessions)\.json$', re.I)
# Shell scripts and other text that must have Unix line ends on macOS/Linux.
UNIX_TEXT = re.compile(r'\.(sh|command|py|pyw|yaml|yml|toml|lock|txt|md|html|css|js|json|svg|webmanifest)$'
                       r'|(^|/)(Dockerfile|\.dockerignore)$')
EXECUTABLE = re.compile(r'\.(sh|command)$')


def private_needles():
    """Strings that must never appear in the package."""
    needles = {}
    user = os.environ.get('USERNAME') or os.environ.get('USER')
    if user and len(user) >= 3:
        needles[user] = 'Windows user name'
    host = socket.gethostname()
    if host and len(host) >= 3:
        needles[host] = 'computer name'
    for path in (ROOT / '.runtime' / 'settings.json',):
        try:
            settings = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if settings.get('access_code'):
            needles[settings['access_code']] = 'access code'
        if settings.get('owner_code'):
            needles[settings['owner_code']] = 'owner code'
        if settings.get('pin') and len(settings['pin']) >= 4:
            needles[f'"{settings["pin"]}"'] = 'PIN (as a JSON string)'
    pairing = ROOT / '.runtime' / 'usb-pairing'
    for record in pairing.glob('*.plist') if pairing.is_dir() else []:
        needles[record.stem] = 'iPhone serial number'
        if len(record.stem) == 24 and '-' not in record.stem:   # the same ID as usbmuxd writes it
            needles[f'{record.stem[:8]}-{record.stem[8:]}'] = 'iPhone serial number'
    # More private text known only to this computer, e.g. the addresses and login of a server
    # MirrorDesk was tried on: one per line in .runtime/private-needles.txt, "text<TAB>what it is".
    try:
        for line in (ROOT / '.runtime' / 'private-needles.txt').read_text(encoding='utf-8').splitlines():
            text, _, label = line.partition('\t')
            if len(text.strip()) >= 4 and not text.startswith('#'):
                needles[text.strip()] = label.strip() or 'private text'
    except OSError:
        pass
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and not ip.ip.startswith(('127.', '169.254.')):
                    needles[ip.ip] = 'LAN address of this PC'
    except Exception:  # noqa: BLE001 - best effort
        pass
    email = os.environ.get('MIRRORDESK_PRIVATE_EMAIL')
    if email:
        needles[email] = 'e-mail address'
    return needles


def copy_tree(source, target):
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if any(part == '__pycache__' for part in relative.parts) or path.suffix == '.pyc':
            continue
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def version_text():
    from mirrordesk import __version__
    return f'MirrorDesk {__version__} tester build, {date.today().isoformat()}\n'


def stage_app(target, extra_files=()):
    for name in [*APP_FILES, *extra_files]:
        copy_file(ROOT / name, target / name)
    for name in APP_DIRS:
        copy_tree(ROOT / name, target / name)


HTML_HEAD = ('<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n'
             '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')


def compose_image():
    """The image the Docker Compose / Dockge stack names (published on GitHub)."""
    return re.search(r'^\s+image:\s*(\S+)', (ROOT / 'docker' / 'compose.yaml').read_text(encoding='utf-8'), re.M).group(1)


def render_guide(text):
    """packaging/common/*.html are page bodies (also publishable as-is): fill in this build's
    file names and make them complete documents for opening from disk."""
    from mirrordesk import __version__
    names = {'__WINDOWS_FILE__': package_name('windows', 'zip'), '__MACOS_FILE__': package_name('macos', 'zip'),
             '__LINUX_FILE__': package_name('linux', 'zip'), '__COMPOSE_FILE__': package_name('compose', 'yaml'),
             '__IMAGE__': compose_image(), '__VERSION__': __version__}
    for token, value in names.items():
        text = text.replace(token, value)
    return text if text.lstrip().lower().startswith('<!doctype') else HTML_HEAD + text


def stage_docs(out):
    for path in sorted((PACKAGING / 'common').iterdir()):
        if path.suffix == '.html':
            (out / path.name).write_text(render_guide(path.read_text(encoding='utf-8')), encoding='utf-8', newline='\n')
        else:
            copy_file(path, out / path.name)
    (out / 'VERSION.txt').write_text(version_text(), encoding='utf-8', newline='\n')


def write_icons(app, kind):
    """The app icon (mirrordesk/web/icon.svg, drawn by desktop.icon_image) for the launchers."""
    from mirrordesk.desktop import icon_image
    if kind == 'macos':
        icon_image(1024).save(app / 'mirrordesk.icns', format='ICNS')
    else:
        icon_image(256).save(app / 'mirrordesk.png', format='PNG')


def stage(out, kind):
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if kind == 'windows':
        stage_app(out, WINDOWS_FILES)
        copy_tree(PACKAGING / 'windows', out)
        uxplay, driver = ROOT / 'build' / 'uxplay', ROOT / 'build' / 'usb-driver'
        if not (uxplay / 'bin' / 'uxplay.exe').is_file():
            raise SystemExit('Run tools/build_uxplay_bundle.py first (build/uxplay is missing).')
        sys.path.insert(0, str(ROOT / 'tools'))
        from build_uxplay_bundle import CHANGE
        built = json.loads((uxplay / 'BUNDLE.json').read_text(encoding='utf-8')).get('uxplay', {}).get('mirrordesk_change')
        if built != CHANGE:
            raise SystemExit(f"build/uxplay has MirrorDesk's UxPlay change {built!r}, not {CHANGE!r}: "
                             'run tools/build_uxplay_bundle.py again.')
        if not (driver / 'wdi-simple.exe').is_file():
            raise SystemExit('Run tools/build_wdi_simple.py first (build/usb-driver is missing).')
        copy_tree(uxplay, out / 'uxplay')
        copy_tree(driver, out / 'tools' / 'usb-driver')
    else:
        app = out / 'app'
        stage_app(app, DOCKER_FILES if kind == 'linux' else ())
        copy_file(PACKAGING / 'unix' / 'start-mirrordesk.sh', app / 'start-mirrordesk.sh')
        write_icons(app, kind)
        copy_tree(PACKAGING / kind, out)
        for path in out.rglob('*'):
            if path.is_file() and UNIX_TEXT.search(path.relative_to(out).as_posix()):
                data = path.read_bytes()
                if b'\r\n' in data:
                    path.write_bytes(data.replace(b'\r\n', b'\n'))
    stage_docs(out)


def scan(out):
    """Return a list of problems (empty = clean)."""
    problems = []
    needles = private_needles()
    encoded = [(text, label, text.encode('utf-8'), text.encode('utf-16-le')) for text, label in needles.items()]
    for path in sorted(p for p in out.rglob('*') if p.is_file()):
        relative = path.relative_to(out).as_posix()
        if FORBIDDEN_NAMES.search(relative):
            problems.append(f'{relative}: file must not be shipped')
            continue
        data = path.read_bytes()
        lowered = data.lower()
        for text, label, utf8, utf16 in encoded:
            if utf8.lower() in lowered or utf16.lower() in lowered:
                problems.append(f'{relative}: contains the {label}')
    return problems, sorted(needles.values())


def _mode(relative, is_dir):
    if is_dir:
        return 0o755
    return 0o755 if EXECUTABLE.search(relative) else 0o644


def make_zip(out, target, unix=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(out.rglob('*')):
            if not path.is_file():
                continue
            relative = path.relative_to(out).as_posix()
            if not unix:
                archive.write(path, 'MirrorDesk/' + relative)
                continue
            info = zipfile.ZipInfo('MirrorDesk/' + relative, date_time=time.localtime(path.stat().st_mtime)[:6])
            info.create_system = 3      # Unix: Archive Utility and unzip keep the permissions
            info.external_attr = (0o100000 | _mode(relative, False)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return target


def package_name(kind, suffix):
    from mirrordesk import __version__
    label = {'windows': 'Windows', 'macos': 'macOS', 'linux': 'Linux', 'compose': 'compose'}[kind]
    return f'MirrorDesk-Tester-{__version__}-{date.today().isoformat()}-{label}.{suffix}'


def build(kind, staging, dist):
    out = staging / kind / 'MirrorDesk'
    stage(out, kind)
    problems, checked = scan(out)
    print(f'[{kind}] privacy scan checked for:', ', '.join(checked))
    if problems:
        print(f'[{kind}] PRIVACY GATE FAILED - nothing was packaged:')
        for problem in problems[:50]:
            print('  ' + problem)
        return None
    target = make_zip(out, dist / package_name(kind, 'zip'), unix=(kind != 'windows'))
    files = sum(1 for p in out.rglob('*') if p.is_file())
    print(f'[{kind}] clean. {files} files -> {target} ({target.stat().st_size / 1e6:.1f} MB)')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', choices=[*TARGETS, 'all'], default='all')
    parser.add_argument('--out', default=str(ROOT / 'build' / 'package'), help='staging folder')
    parser.add_argument('--dist', default=str(ROOT / 'dist'))
    options = parser.parse_args()
    staging, dist = Path(options.out), Path(options.dist)
    kinds = TARGETS if options.target == 'all' else (options.target,)
    failed = False
    for kind in kinds:
        failed |= build(kind, staging, dist) is None
    if 'linux' in kinds and not failed:
        compose = dist / package_name('compose', 'yaml')
        compose.write_bytes((ROOT / 'docker' / 'compose.yaml').read_bytes().replace(b'\r\n', b'\n'))
        print(f'[compose] {compose}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
