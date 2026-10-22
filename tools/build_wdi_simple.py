"""Build wdi-simple.exe (libwdi) with the libusb-win32 1.2.6.0 driver embedded (developer tool).

    .venv\\Scripts\\python tools\\build_wdi_simple.py [--msys C:\\msys64] [--out build\\usb-driver]

MirrorDesk's "Install the iPhone cable driver" button runs this tool once, elevated:
it binds the plugged-in iPhone (its composite parent device) to libusb-win32, the
same driver package Zadig creates, so pyusb can switch the phone into its screen
capture configuration. The libusb0.sys/libusb0.dll embedded here are byte-identical
to the ones verified with the development iPhone (SHA-256 checked below).

libwdi: https://github.com/pbatard/libwdi tag v1.5.1 (LGPL-3.0; wdi-simple example GPL-3.0)
libusb-win32: https://sourceforge.net/projects/libusb-win32/ 1.2.6.0 (driver LGPL/GPL, see its COPYING files)
Built with MSYS2 UCRT64 gcc, x64 only (no 32-bit or ARM64 Windows).
"""
import argparse
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

LIBWDI_REPO = 'https://github.com/pbatard/libwdi.git'
LIBWDI_TAG, LIBWDI_COMMIT = 'v1.5.1', '9b23b82a2dd1cbffc16d46c212f92c6bf8c0c602'
LIBUSB_URL = ('https://downloads.sourceforge.net/project/libusb-win32/libusb-win32-releases/1.2.6.0/'
              'libusb-win32-bin-1.2.6.0.zip')
LIBUSB_SHA256 = 'ac19715e6ace496d1a41df3714038ab0ad9b8059834ed65c55ff4e0b9ba0d2e9'
# The driver verified on the development PC (C:\Windows\System32\drivers\libusb0.sys, 1.2.6.0).
LIBUSB0_SYS_SHA256 = '8058f2afe6ef96a7d2ded432997fd8655970c9ea75a938ee4557d6a2cb4cc989'
CFLAGS = ['-O2', '-m64', '-D_WIN64', '-DWINVER=0x0601', '-D_WIN32_WINNT=0x0601', '-D_WIN32_IE=0x0700',
          '-DCOBJMACROS', '-Wno-stringop-truncation', '-Wno-int-to-pointer-cast', '-Wno-pointer-to-int-cast']
LIB_SOURCES = ['logging.c', 'tokenizer.c', 'vid_data.c', 'pki.c', 'libwdi_dlg.c', 'libwdi.c']


def sh(command, cwd, env):
    print('  $', ' '.join(str(c) for c in command[:6]), '…' if len(command) > 6 else '', flush=True)
    result = subprocess.run([str(c) for c in command], cwd=cwd, env=env, capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
    if result.returncode != 0:
        print(result.stdout[-3000:], result.stderr[-6000:])
        raise SystemExit(f'failed: {command[0]}')
    return result


def fetch_libusb(work):
    data = urllib.request.urlopen(urllib.request.Request(LIBUSB_URL, headers={'User-Agent': 'Wget/1.21'}), timeout=60).read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != LIBUSB_SHA256:
        raise SystemExit(f'libusb-win32 download checksum mismatch: {digest}')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(work)
    root = work / 'libusb-win32-bin-1.2.6.0'
    if hashlib.sha256((root / 'bin' / 'amd64' / 'libusb0.sys').read_bytes()).hexdigest() != LIBUSB0_SYS_SHA256:
        raise SystemExit('libusb0.sys differs from the verified driver')
    return root


def fetch_libwdi(work):
    """The source archive of the pinned commit (GitHub serves exactly that tree)."""
    url = f'https://codeload.github.com/pbatard/libwdi/zip/{LIBWDI_COMMIT}'
    data = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'mirrordesk-build'}), timeout=60).read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(work)
    source = work / f'libwdi-{LIBWDI_COMMIT}'
    if not (source / 'libwdi' / 'libwdi.c').is_file():
        raise SystemExit('unexpected libwdi archive layout')
    return source


def build(msys, out):
    msys, out = Path(msys), Path(out).resolve()
    work = out / 'work'
    if out.exists():
        shutil.rmtree(out)
    work.mkdir(parents=True)
    env = dict(os.environ)
    env['PATH'] = str(msys / 'ucrt64' / 'bin') + os.pathsep + str(msys / 'usr' / 'bin') + os.pathsep + env['PATH']
    gcc, windres = msys / 'ucrt64' / 'bin' / 'gcc.exe', msys / 'ucrt64' / 'bin' / 'windres.exe'
    libusb = fetch_libusb(work)
    source = fetch_libwdi(work)
    lib = source / 'libwdi'
    helper = work / 'helper'
    helper.mkdir()
    config = work / 'config'
    config.mkdir()
    (config / 'config.h').write_text('\n'.join([
        '#pragma once', '#define _GNU_SOURCE 1', '#define OPT_M64 1',
        f'#define LIBUSB0_DIR "{libusb.as_posix()}"', f'#define SOLUTIONDIR "{helper.as_posix()}"',
        '#define WDF_VER 1011', '#define COINSTALLER_DIR "wdf"', '#define X64_DIR "x64"',
        '#define ENABLE_LOGGING 1', '#define INCLUDE_DEBUG_LOGGING 1', '']), encoding='utf-8')
    include = ['-I' + str(config), '-I' + str(source), '-I' + str(lib)]
    # No local paths in the binaries (__FILE__ would carry the build folder, i.e. the user name).
    for path, name in ((source, 'libwdi'), (work, 'build'), (libusb, 'libusb-win32')):
        for form in {str(path), path.as_posix()}:
            include.append(f'-ffile-prefix-map={form}={name}')
    print('installer helper')
    sh([gcc, *CFLAGS, *include, lib / 'installer.c', '-o', helper / 'installer_x64.exe', '-static',
        '-lsetupapi', '-lnewdev', '-lole32'], lib, env)
    print('embedder')
    sh([gcc, '-O2', *include, lib / 'embedder.c', '-o', work / 'embedder.exe'], lib, env)
    sh([work / 'embedder.exe', 'embedded.h'], lib, env)
    print('libwdi')
    objects = []
    for name in LIB_SOURCES:
        obj = work / (Path(name).stem + '.o')
        sh([gcc, *CFLAGS, *include, '-c', lib / name, '-o', obj], lib, env)
        objects.append(obj)
    print('wdi-simple')
    examples = source / 'examples'
    rc_obj = work / 'wdi-simple_rc.o'
    sh([windres, '-I', examples, '-I', lib, '-i', examples / 'wdi-simple.rc', '-o', rc_obj], examples, env)
    exe = out / 'wdi-simple.exe'
    sh([gcc, *CFLAGS, *include, examples / 'wdi-simple.c', *objects, rc_obj, '-o', exe, '-static',
        '-lsetupapi', '-lole32', '-lntdll', '-luuid', '-lcomctl32', '-lshell32', '-lcrypt32', '-lnewdev', '-lgdi32'], examples, env)
    # Licenses and provenance travel with the binary.
    licenses = out / 'licenses'
    licenses.mkdir()
    shutil.copy2(source / 'COPYING-LGPL', licenses / 'libwdi-COPYING-LGPL.txt')
    shutil.copy2(source / 'COPYING', licenses / 'libwdi-wdi-simple-COPYING-GPL.txt')
    for name in ('COPYING_GPL.txt', 'COPYING_LGPL.txt', 'installer_license.txt', 'AUTHORS.txt'):
        shutil.copy2(libusb / name, licenses / f'libusb-win32-{name}')
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (out / 'SOURCES.txt').write_text(
        f'wdi-simple.exe  SHA-256 {digest}\n'
        f'Built from libwdi {LIBWDI_TAG} ({LIBWDI_REPO}, commit {LIBWDI_COMMIT}), unmodified,\n'
        f'with the libusb-win32 1.2.6.0 driver embedded ({LIBUSB_URL}, SHA-256 {LIBUSB_SHA256}).\n'
        'Compiler: MSYS2 UCRT64 gcc. Build script: tools/build_wdi_simple.py in MirrorDesk.\n', encoding='utf-8')
    shutil.rmtree(work)
    print(f'Built {exe} ({exe.stat().st_size / 1e6:.1f} MB), SHA-256 {digest}')
    return exe


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--msys', default=r'C:\msys64')
    parser.add_argument('--out', default=str(Path(__file__).resolve().parents[1] / 'build' / 'usb-driver'))
    options = parser.parse_args()
    build(options.msys, options.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
