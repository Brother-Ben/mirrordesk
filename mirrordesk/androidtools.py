"""Android tools: Google's adb (platform-tools) and the bundled scrcpy server.

* adb is Google's Android Debug Bridge. Its license does not allow MirrorDesk to
  ship it, so it is downloaded from Google on request (Setup-MirrorDesk.cmd or
  the "Install Android tools" button) into <project>/tools/platform-tools, and
  every download is checked against a pinned SHA-256 before it is unpacked.
  An adb already on PATH (Android Studio, Linux packages, the Docker image) is
  used when present; MIRRORDESK_ADB=<path> overrides everything.
* scrcpy-server (Apache-2.0, Genymobile/scrcpy) runs on the phone and streams
  its screen and sound; the exact release is bundled in third_party/scrcpy and
  checked against its published SHA-256 before every use.

`python -m mirrordesk.androidtools install` downloads adb; `... status` shows what is used.
"""
import hashlib
import io
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(os.environ.get('MIRRORDESK_TOOLS') or ROOT / 'tools')

SCRCPY_VERSION = '5.0.1'
SCRCPY_SERVER = ROOT / 'third_party' / 'scrcpy' / f'scrcpy-server-v{SCRCPY_VERSION}'
SCRCPY_SERVER_SHA256 = '764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536'

PLATFORM_TOOLS_VERSION = '37.0.1'   # the adb scrcpy 5.0 was released with
PLATFORM_TOOLS_BASE = 'https://dl.google.com/android/repository/'
# SHA-256 computed from Google's archives; each one also matched the size and
# SHA-1 published in Google's repository2-3.xml manifest.
PLATFORM_TOOLS = {
    'windows': ('platform-tools_r37.0.1-win.zip', 8044989,
                '45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99'),
    'linux': ('platform-tools_r37.0.1-linux.zip', 9054187,
              'd230f13842f60f782a8645f9c813f8f845bf36089ea7289f28c48f17979313f1'),
    'darwin': ('platform-tools_r37.0.1-darwin.zip', 16110554,
               'ee39ad5967e95c2a07f04dbcbde96b1a0c916ba376096db5d2f498b7727a5d1d'),
}
DOWNLOAD_TIMEOUT = 60


class ToolsError(RuntimeError):
    """A user-facing reason the Android tools are not usable."""


def host_os():
    if os.name == 'nt':
        return 'windows'
    return 'darwin' if sys.platform == 'darwin' else 'linux'


def adb_name():
    return 'adb.exe' if os.name == 'nt' else 'adb'


def bundled_adb(tools_dir=None):
    return Path(tools_dir or TOOLS_DIR) / 'platform-tools' / adb_name()


def find_adb(tools_dir=None):
    """Path of the adb MirrorDesk uses, or None. Order: MIRRORDESK_ADB, the copy
    Setup downloaded, then adb on PATH. (MIRRORDESK_ADB_CMD, a JSON argv list,
    is for tests: it runs tests/fake_adb.py.)"""
    command = os.environ.get('MIRRORDESK_ADB_CMD')
    if command:
        import json
        value = json.loads(command)
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return value
    override = os.environ.get('MIRRORDESK_ADB')
    if override:
        return override if Path(override).is_file() or shutil.which(override) else None
    local = bundled_adb(tools_dir)
    if local.is_file():
        return str(local)
    return shutil.which('adb')


def installable():
    """Whether the download button makes sense here (not in a container or on an
    unsupported CPU: Google ships adb for x86-64 Windows/Linux and macOS only)."""
    if os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv'):
        return False
    machine = platform.machine().lower()
    if host_os() == 'linux' and machine not in ('x86_64', 'amd64'):
        return False
    return True


def scrcpy_server(path=None):
    """The verified scrcpy-server file. Raises ToolsError when missing or altered."""
    path = Path(path or SCRCPY_SERVER)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise ToolsError(f'The scrcpy server file is missing ({path.name}). Reinstall MirrorDesk.')
    if digest != SCRCPY_SERVER_SHA256:
        raise ToolsError('The scrcpy server file is damaged (checksum mismatch). Reinstall MirrorDesk.')
    return path


def _download(url, size, progress=None):
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
        buffer = io.BytesIO()
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            buffer.write(chunk)
            if buffer.tell() > size * 2:
                raise ToolsError('The download is larger than expected; stopped.')
            if progress:
                progress(min(1.0, buffer.tell() / size))
    return buffer.getvalue()


def install(tools_dir=None, progress=None, fetch=None):
    """Download Google's platform-tools for this OS, verify, unpack. Returns the adb path.

    fetch(url, size, progress) -> bytes is replaceable in tests (no network)."""
    name, size, sha256 = PLATFORM_TOOLS[host_os()]
    data = (fetch or _download)(PLATFORM_TOOLS_BASE + name, size, progress)
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        raise ToolsError(f'The downloaded Android tools failed the checksum check ({digest[:12]}…); '
                         'nothing was installed. Try again later.')
    target = Path(tools_dir or TOOLS_DIR)
    target.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.platform-tools-', dir=target))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                # Only the platform-tools folder, never absolute or parent paths.
                parts = Path(member.filename).parts
                if not parts or parts[0] != 'platform-tools' or '..' in parts or Path(member.filename).is_absolute():
                    continue
                archive.extract(member, staging)
                if host_os() != 'windows' and not member.is_dir():
                    mode = (member.external_attr >> 16) & 0o777
                    if mode:
                        os.chmod(staging / member.filename, mode)
        adb = staging / 'platform-tools' / adb_name()
        if not adb.is_file():
            raise ToolsError('The Android tools archive did not contain adb.')
        if host_os() != 'windows':
            adb.chmod(adb.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        final = target / 'platform-tools'
        if final.exists():
            shutil.rmtree(final)
        (staging / 'platform-tools').replace(final)
        (final / 'MIRRORDESK-SOURCE.txt').write_text(
            f'Downloaded from {PLATFORM_TOOLS_BASE}{name}\nSHA-256 {sha256}\n'
            'Android SDK Platform-Tools by Google, under the Android Software Development Kit License.\n',
            encoding='utf-8')
        return str(final / adb_name())
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='python -m mirrordesk.androidtools',
                                     description='Android tools for MirrorDesk (Google adb + scrcpy server).')
    parser.add_argument('command', choices=['install', 'status'], nargs='?', default='status')
    parser.add_argument('--if-missing', action='store_true', help='install: do nothing when an adb is found.')
    options = parser.parse_args(argv)
    if options.command == 'install':
        found = find_adb()
        if options.if_missing and found:
            print(f'Android tools already available: {found}')
            return 0
        print(f'Downloading Android platform-tools {PLATFORM_TOOLS_VERSION} from Google…', flush=True)
        try:
            path = install(progress=None)
        except (ToolsError, OSError) as exc:
            print(f'Could not install the Android tools: {exc}')
            return 1
        print(f'Installed: {path}')
        return 0
    adb = find_adb()
    print(f'adb: {adb or "not installed (run: python -m mirrordesk.androidtools install)"}')
    try:
        print(f'scrcpy server: {scrcpy_server()} (v{SCRCPY_VERSION}, checksum OK)')
    except ToolsError as exc:
        print(f'scrcpy server: {exc}')
    return 0 if adb else 1


if __name__ == '__main__':
    sys.exit(main())
