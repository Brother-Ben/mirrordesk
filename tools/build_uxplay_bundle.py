"""Build a self-contained UxPlay folder from an MSYS2 UCRT64 build (developer tool).

    .venv\\Scripts\\python tools\\build_uxplay_bundle.py [--uxplay PATH] [--msys C:\\msys64] [--out build\\uxplay]

Result (no MSYS2 needed at runtime):
    <out>/bin/uxplay.exe, gst-plugin-scanner.exe, gst-inspect-1.0.exe and every DLL they
          and the plugins load (found by walking PE imports; Windows system DLLs excluded)
    <out>/lib/gstreamer-1.0/  only the GStreamer plugins MirrorDesk's UxPlay pipelines use
    <out>/licenses/           the license files of every MSYS2 package that provided a file
    <out>/BUNDLE.json         files, SHA-256, owning MSYS2 package + version, UxPlay source commit
    <out>/SOURCES.txt         where the corresponding source code is (GPL/LGPL)

MirrorDesk runs UxPlay with -vrtp/-artp, so the pipelines are
  video: appsrc ! queue ! h264parse ! rtph264pay ! udpsink
  audio: appsrc ! queue ! avdec_aac|avdec_alac ! audioconvert ! audioresample ! volume
         ! audioconvert ! rtpL16pay ! udpsink
(UxPlay renderers/video_renderer.c, audio_renderer.c; cover art / HLS are not enabled).
UxPlay also refuses to start unless the plugins in UXPLAY_REQUIRED are installed
(check_plugins in audio_renderer.c), whether its pipelines use them or not.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PLUGINS = ['coreelements', 'app', 'videoparsersbad', 'rtp', 'udp', 'libav', 'audioconvert', 'audioresample',
           'volume', 'typefindfunctions', 'playback', 'autodetect']
# UxPlay's startup check (audio_renderer.c check_plugins): without one of these it prints
# "Required gstreamer plugin ... not found" and exits, although `uxplay -h` still works.
UXPLAY_REQUIRED = ['app', 'libav', 'playback', 'autodetect', 'videoparsersbad']
ELEMENTS = ['appsrc', 'queue', 'h264parse', 'rtph264pay', 'udpsink', 'avdec_aac', 'avdec_alac', 'audioconvert',
            'audioresample', 'volume', 'rtpL16pay', 'fakesink', 'capsfilter']
TOOLS = ['gst-inspect-1.0.exe']
SCANNER_DIRS = ['libexec/gstreamer-1.0', 'lib/gstreamer-1.0']
# MirrorDesk's change to UxPlay's audio buffer and video timestamps (the files it replaces;
# mirrordesk.patch is the diff).
OVERLAY = Path(__file__).resolve().parents[1] / 'third_party' / 'uxplay' / 'mirrordesk'
CHANGE = 'mirrordesk.3'


def run(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', **kwargs)


def imports(objdump, path):
    result = run([str(objdump), '-p', str(path)])
    return re.findall(r'DLL Name:\s*(\S+)', result.stdout)


def owner(pacman, path, msys):
    """MSYS2 package that owns a file: (name, version) or (None, None)."""
    relative = '/' + Path(path).resolve().relative_to(Path(msys).resolve()).as_posix()
    result = run([str(pacman), '-Qo', relative], env={**os.environ, 'PATH': str(Path(msys) / 'usr' / 'bin') + os.pathsep + os.environ['PATH']})
    match = re.search(r'is owned by (\S+) (\S+)', result.stdout)
    return (match.group(1), match.group(2)) if match else (None, None)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rebuild(source, msys, build_dir):
    """Build UxPlay from `source` (left untouched) into build_dir with the options of the
    development build (Release, USE_MDNS=ON, USE_DNS_SD=OFF) plus -ffile-prefix-map, so
    the binary carries no local paths (__FILE__ in assertions would include the user name)."""
    source, msys, build_dir = Path(source).resolve(), Path(msys), Path(build_dir).resolve()
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    env = dict(os.environ)
    env['PATH'] = str(msys / 'ucrt64' / 'bin') + os.pathsep + env['PATH']
    maps = []
    for path, name in ((source, 'uxplay'), (build_dir, 'uxplay-build')):
        for form in {str(path), path.as_posix()}:
            maps.append(f'-ffile-prefix-map={form}={name}')
    flags = ' '.join(maps)
    cmake = str(msys / 'ucrt64' / 'bin' / 'cmake.exe')
    configure = run([cmake, '-S', str(source), '-B', str(build_dir), '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release',
                     '-DUSE_MDNS=ON', '-DUSE_DNS_SD=OFF', f'-DCMAKE_C_FLAGS={flags}', f'-DCMAKE_CXX_FLAGS={flags}'], env=env)
    if configure.returncode != 0:
        raise SystemExit('cmake configure failed:\n' + configure.stdout[-3000:] + configure.stderr[-3000:])
    built = run([cmake, '--build', str(build_dir), '-j', str(os.cpu_count() or 4)], env=env)
    if built.returncode != 0:
        raise SystemExit('UxPlay build failed:\n' + built.stdout[-3000:] + built.stderr[-3000:])
    executable = build_dir / 'uxplay.exe'
    run([str(msys / 'ucrt64' / 'bin' / 'strip.exe'), str(executable)], env=env)
    return executable


def patched_copy(source, target):
    """The UxPlay checkout `source` (left untouched) with MirrorDesk's change copied over it."""
    source, target = Path(source).resolve(), Path(target)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('.git', 'build', 'build-*'))
    shutil.copytree(OVERLAY, target, dirs_exist_ok=True)
    return target


def git_commit(source):
    git = shutil.which('git') or str(Path('C:/msys64/usr/bin/git.exe'))
    try:
        commit = run([git, '-C', str(source), 'rev-parse', 'HEAD']).stdout.strip()
        describe = run([git, '-C', str(source), 'describe', '--tags']).stdout.strip()
        dirty = run([git, '-C', str(source), 'status', '--porcelain', '--untracked-files=no']).stdout.strip()
        return commit, describe, bool(dirty)
    except OSError:
        return None, None, None


def build(uxplay, msys, out, source_dir=None):
    msys, uxplay, out = Path(msys), Path(uxplay), Path(out)
    source_dir = Path(source_dir) if source_dir else uxplay.parents[1]
    ucrt = msys / 'ucrt64'
    objdump, pacman = ucrt / 'bin' / 'objdump.exe', msys / 'usr' / 'bin' / 'pacman.exe'
    if out.exists():
        shutil.rmtree(out)
    bindir, plugdir, licdir = out / 'bin', out / 'lib' / 'gstreamer-1.0', out / 'licenses'
    for d in (bindir, plugdir, licdir):
        d.mkdir(parents=True)
    roots = []
    shutil.copy2(uxplay, bindir / 'uxplay.exe')
    roots.append(bindir / 'uxplay.exe')
    scanner = next((ucrt / d / 'gst-plugin-scanner.exe' for d in SCANNER_DIRS if (ucrt / d / 'gst-plugin-scanner.exe').is_file()), None)
    if scanner is None:
        raise SystemExit('gst-plugin-scanner.exe not found under ucrt64')
    shutil.copy2(scanner, bindir / 'gst-plugin-scanner.exe')
    roots.append(bindir / 'gst-plugin-scanner.exe')
    for tool in TOOLS:
        shutil.copy2(ucrt / 'bin' / tool, bindir / tool)
        roots.append(bindir / tool)
    sources = {bindir / 'uxplay.exe': uxplay, bindir / 'gst-plugin-scanner.exe': scanner}
    sources.update({bindir / t: ucrt / 'bin' / t for t in TOOLS})
    for plugin in PLUGINS:
        name = f'libgst{plugin}.dll'
        source = ucrt / 'lib' / 'gstreamer-1.0' / name
        if not source.is_file():
            raise SystemExit(f'missing GStreamer plugin {name}')
        shutil.copy2(source, plugdir / name)
        roots.append(plugdir / name)
        sources[plugdir / name] = source
    # Walk the import graph; a DLL found in ucrt64/bin is bundled, anything else is a Windows DLL.
    seen, queue, system = set(), list(roots), set()
    while queue:
        current = queue.pop()
        for dll in imports(objdump, current):
            key = dll.lower()
            if key in seen:
                continue
            seen.add(key)
            candidate = ucrt / 'bin' / dll
            if not candidate.is_file():
                matches = [p for p in (ucrt / 'bin').iterdir() if p.name.lower() == key]
                candidate = matches[0] if matches else None
            if candidate is None:
                system.add(dll)
                continue
            target = bindir / candidate.name
            shutil.copy2(candidate, target)
            sources[target] = candidate
            queue.append(target)
    # Provenance and licenses.
    files, packages = [], {}
    for target, source in sorted(sources.items()):
        if source == uxplay:
            package, version = 'uxplay (built from source)', None
        else:
            package, version = owner(pacman, source, msys)
        if package and version:
            packages[package] = version
        files.append({'path': target.relative_to(out).as_posix(), 'sha256': sha256(target), 'size': target.stat().st_size,
                      'package': package, 'version': version})
    for package in packages:
        short = package.removeprefix('mingw-w64-ucrt-x86_64-')
        for candidate in (ucrt / 'share' / 'licenses' / short, msys / 'usr' / 'share' / 'licenses' / package):
            if candidate.is_dir():
                shutil.copytree(candidate, licdir / short, dirs_exist_ok=True)
    commit, describe, dirty = git_commit(source_dir)
    if (source_dir / 'LICENSE').is_file():
        shutil.copy2(source_dir / 'LICENSE', licdir / 'UxPlay-LICENSE.txt')
    manifest = {'uxplay': {'repository': 'https://github.com/FDH2/UxPlay', 'commit': commit, 'describe': describe,
                           'local_changes': dirty, 'mirrordesk_change': CHANGE},
                'plugins': PLUGINS, 'elements': ELEMENTS, 'msys2_packages': packages,
                'windows_dlls': sorted(system, key=str.lower), 'files': files,
                'total_bytes': sum(f['size'] for f in files)}
    (out / 'BUNDLE.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    lines = ['Source code for the programs in this folder', '',
             f'UxPlay (GPL-3.0-or-later): https://github.com/FDH2/UxPlay  commit {commit} ({describe})'
             + ('  [with local changes]' if dirty else ''),
             f"  with MirrorDesk's change to its audio and video handling ({CHANGE}): third_party/uxplay/mirrordesk.patch",
             '  in the MirrorDesk package (the same commit is third_party/uxplay/UxPlay-<commit>.tar.gz).',
             '',
             'All other files are unmodified binaries from MSYS2 UCRT64 packages (versions below).',
             'Their sources: https://github.com/msys2/MINGW-packages (build recipes) and https://repo.msys2.org/mingw/sources/',
             '(source tarballs per package and version). License texts are in licenses/.', '']
    lines += [f'  {name} {version}' for name, version in sorted(packages.items())]
    (out / 'SOURCES.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return manifest


def verify(out, data_dir):
    """Run the bundle with a clean environment: no MSYS2 on PATH."""
    out = Path(out)
    env = {k: v for k, v in os.environ.items() if k.upper() not in ('PATH', 'GST_PLUGIN_PATH', 'GST_PLUGIN_SYSTEM_PATH',
                                                                   'GST_PLUGIN_SCANNER', 'GST_REGISTRY')}
    env['PATH'] = str(out / 'bin') + os.pathsep + os.environ.get('SystemRoot', r'C:\Windows') + r'\System32'
    env['GST_PLUGIN_SYSTEM_PATH'] = str(out / 'lib' / 'gstreamer-1.0')
    env['GST_PLUGIN_PATH'] = env['GST_PLUGIN_SYSTEM_PATH']
    env['GST_PLUGIN_SCANNER'] = str(out / 'bin' / 'gst-plugin-scanner.exe')
    env['GST_REGISTRY'] = str(Path(data_dir) / 'gst-registry-verify.bin')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    help_text = run([str(out / 'bin' / 'uxplay.exe'), '-h'], env=env, timeout=60, creationflags=flags)
    ok = help_text.returncode == 0 and '-vrtp' in help_text.stdout + help_text.stderr
    print('uxplay -h:', 'OK' if ok else f'FAILED ({help_text.returncode})')
    failures = []
    for element in ELEMENTS:
        result = run([str(out / 'bin' / 'gst-inspect-1.0.exe'), element], env=env, timeout=120, creationflags=flags)
        if result.returncode != 0:
            failures.append(element)
    print('elements:', 'all found' if not failures else f'MISSING {failures}')
    missing = [plugin for plugin in UXPLAY_REQUIRED
               if run([str(out / 'bin' / 'gst-inspect-1.0.exe'), plugin], env=env, timeout=120, creationflags=flags).returncode != 0]
    print('plugins UxPlay checks at startup:', 'all found' if not missing else f'MISSING {missing}')
    return ok and not failures and not missing


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    user = os.environ.get('USERNAME', '')
    parser.add_argument('--uxplay', default=rf'C:\msys64\home\{user}\mirrordesk-uxplay\build\uxplay.exe')
    parser.add_argument('--msys', default=r'C:\msys64')
    parser.add_argument('--out', default=str(Path(__file__).resolve().parents[1] / 'build' / 'uxplay'))
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--no-rebuild', action='store_true',
                        help='Bundle --uxplay as is (its binary may contain local build paths).')
    options = parser.parse_args()
    if not options.verify_only:
        source = Path(options.uxplay).parents[1]
        uxplay = options.uxplay
        if not options.no_rebuild:
            print(f"Rebuilding UxPlay from {source} with MirrorDesk's change, without local paths…", flush=True)
            # CMake splits CFLAGS on spaces: build where the path has none (the project folder may).
            import tempfile
            temp = Path(tempfile.gettempdir())
            patched = patched_copy(source, temp / 'mirrordesk-uxplay-src')
            uxplay = rebuild(patched, options.msys, temp / 'mirrordesk-uxplay-build')
        manifest = build(uxplay, options.msys, options.out, source_dir=source)
        print(f"Bundled {len(manifest['files'])} files, {manifest['total_bytes'] / 1e6:.1f} MB, "
              f"{len(manifest['msys2_packages'])} MSYS2 packages; UxPlay {manifest['uxplay']['describe']}")
        print('Windows DLLs used from the system:', ', '.join(manifest['windows_dlls']))
    temp = Path(options.out).parent / 'verify-data'
    temp.mkdir(parents=True, exist_ok=True)
    return 0 if verify(options.out, temp) else 1


if __name__ == '__main__':
    sys.exit(main())
