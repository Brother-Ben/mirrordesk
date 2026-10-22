"""Windows-only receiver orchestration; no installs or changes to original UxPlay."""
import argparse
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
import threading
from diagnostics import DiagnosticSession, receiver_signal
from backend import runtime_env, uxplay_args
import discovery_probe
import discovery_selftest
import ble_discovery

ROOT = Path(__file__).resolve().parent

# Phone-discovery readiness: read-only, fail-open, never fatal. Full probe
# (8 s subprocess timeout) and, only if it fails, the light probe without
# firewall data (6 s). Hard bound on added startup time: 10 + 8 = 18 s.
DISCOVERY_FULL_BOUND_SECONDS = 10
DISCOVERY_LIGHT_BOUND_SECONDS = 8
DISCOVERY_CHECK_BOUND_SECONDS = DISCOVERY_FULL_BOUND_SECONDS + DISCOVERY_LIGHT_BOUND_SECONDS
DISCOVERY_WATCH_SECONDS = 15
# One-shot path evidence: 3s after startup + <=4s work = <=7s; no media delay.
DISCOVERY_PATH_DELAY_SECONDS = 3


def check_discovery_path(discovery, diagnostics, receiver_running, probe=None,
                         bound=discovery_selftest.BOUND_SECONDS):
    """Fail-open even if an injected probe hangs; never persist raw replies."""
    box = {}
    def work():
        try:
            args = uxplay_args('unused')
            name = args[args.index('-n') + 1]
            box['result'] = (probe or discovery_selftest.run_selftest)(discovery, name, receiver_running)
        except Exception:
            pass
    worker = threading.Thread(target=work, name='mirrordesk-path-check', daemon=True)
    worker.start()
    worker.join(max(0, min(bound, discovery_selftest.BOUND_SECONDS)))
    result = box.get('result')
    if not isinstance(result, discovery_selftest.PathResult):
        result = discovery_selftest.PathResult(receiver_running=receiver_running,
                                               reason='timeout' if worker.is_alive() else 'socket_error')
    for line in discovery_selftest.summary_lines(result):
        print(line, flush=True)
    if diagnostics:
        for fields in discovery_selftest.diagnostic_records(result):
            diagnostics.record('discovery_path', **fields)
    return result


def delayed_discovery_path(discovery, diagnostics, stop, receiver_started=None):
    delay = DISCOVERY_PATH_DELAY_SECONDS
    if receiver_started is not None:
        delay = max(0, delay - (time.monotonic() - receiver_started))
    if not stop.wait(delay):
        check_discovery_path(discovery, diagnostics, True)


def _bounded_probe(probe, executable, full, bound):
    box = {}
    def work():
        try:
            box['result'] = probe(executable, full=full)
        except Exception:
            pass
    worker = threading.Thread(target=work, name='mirrordesk-discovery-check', daemon=True)
    worker.start()
    worker.join(bound)
    result = box.get('result')
    return result if isinstance(result, discovery_probe.DiscoveryResult) else discovery_probe.UNKNOWN


def check_discovery(executable, diagnostics, probe=None, full_bound=DISCOVERY_FULL_BOUND_SECONDS,
                    light_bound=DISCOVERY_LIGHT_BOUND_SECONDS):
    """Print which adapter MirrorDesk Web is announced on and the fix. Never raises."""
    probe = probe or discovery_probe.run_probe
    mode = 'full'
    result = _bounded_probe(probe, executable, True, full_bound)
    if not result.ok:
        # Keep the adapter/profile answer even if firewall enumeration is slow.
        mode = 'light'
        result = _bounded_probe(probe, None, False, light_bound)
    for line in discovery_probe.summary_lines(result):
        print(line, flush=True)
    if diagnostics:
        diagnostics.record('discovery_check', **discovery_probe.diagnostic_fields(result, mode))
    return result


def watch_discovery(baseline, diagnostics, stop, probe=None):
    """Light re-probe every DISCOVERY_WATCH_SECONDS; one warning per change. Never raises."""
    if not baseline.ok or baseline.verdict == 'unknown':
        return  # nothing reliable to compare against
    baseline = discovery_probe.as_light(baseline)  # compare like with like (no firewall)
    last = baseline
    while not stop.wait(DISCOVERY_WATCH_SECONDS):
        try:
            current = (probe or discovery_probe.run_probe)(program=None, full=False)
        except Exception:
            continue
        if stop.is_set():
            return
        if not isinstance(current, discovery_probe.DiscoveryResult) or not current.ok:
            continue  # a failed probe is not a network change
        if current.key() != last.key():
            # Warn only while the state differs from what the receiver bound at
            # start; returning to that exact adapter/IP is recorded, not warned.
            if current.key() != baseline.key():
                print(discovery_probe.CHANGE_LINE, flush=True)
            if diagnostics:
                diagnostics.record('discovery_change', **discovery_probe.change_fields(last, current))
            last = current


def check_ports(http=8765, media=(5004,5005,5006,5007)):
    for port, kind in [(http, socket.SOCK_STREAM)] + [(p, socket.SOCK_DGRAM) for p in media]:
        with socket.socket(socket.AF_INET, kind) as s:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                s.bind(('127.0.0.1', port))
            except OSError as exc:
                raise RuntimeError(f'Port {port} occupied. Close the earlier MirrorDesk Web session; no process was killed.') from exc


# Categorical probe outcomes for saved diagnostics. Distinguishes a hung
# native probe (timeout) from an OS/loader failure (missing DLL, permission)
# from a completed-but-failing probe (nonzero exit) from success. Test 4
# evidence: a stage-2 failure at ~15.045s recorded only a generic RuntimeError
# with no completion event, so these three failure modes were NOT
# distinguishable from saved diagnostics alone.
PROBE_OUTCOMES = ('success', 'nonzero_exit', 'timeout', 'os_error')
PROBE_TIMEOUT_SECONDS = 15
FIRST_GSTREAMER_PROBE_TIMEOUT_SECONDS = 120


def probe_dependency(command, diagnostics, stage, cwd, env, timeout, run=None,
                     probe_name=None, creationflags=0):
    """Run one dependency probe with categorical, privacy-safe diagnostics.

    Records probe_name/stage_index, monotonic duration and a PROBE_OUTCOMES
    category before raising. Never records raw exception text, command
    output, or filesystem paths (which can contain a local username).
    """
    started = time.monotonic()
    if diagnostics:
        diagnostics.record('preflight', stage_index=stage, probe_name=probe_name, ok=False)

    def finish(outcome, **extra):
        duration = time.monotonic() - started
        if diagnostics:
            diagnostics.record('preflight', stage_index=stage, probe_name=probe_name,
                              probe_outcome=outcome, duration=duration,
                              ok=(outcome == 'success'), **extra)
        return duration

    try:
        completed = (run or subprocess.run)(command, env=env, cwd=cwd, capture_output=True, timeout=timeout, creationflags=creationflags)
    except subprocess.TimeoutExpired as exc:
        finish('timeout')
        raise RuntimeError(f'Dependency probe timed out after {timeout}s: {probe_name or command[0]}. '
                          'A timeout alone does not establish a DLL failure. '
                          'Startup stopped; probe not retried. Verify probe/scanner shutdown before relaunch.') from exc
    except OSError as exc:
        finish('os_error')
        # {exc} (e.g. "[Errno 2] No such file or directory") is shown ONLY in
        # this local console-facing RuntimeError, preserving the original
        # Prototype 4 user-visible detail. It is never passed to
        # diagnostics.record()/finish() above, which only ever receives the
        # sanitized 'os_error' category.
        raise RuntimeError(f'Dependency probe could not start: {probe_name or command[0]}: {exc}. '
                          'Check UCRT64 runtime, not CLANG64.') from exc
    output = (completed.stdout+completed.stderr).decode('utf-8', 'replace')
    if completed.returncode:
        finish('nonzero_exit', exit_code=completed.returncode)
        raise RuntimeError(f'Dependency probe exit {completed.returncode} (0x{completed.returncode & 0xffffffff:08x}): {probe_name or command[0]}\n{output[-4000:]}\nUCRT64 DLL loading must work before streaming; no firewall changes will fix this.')
    finish('success', exit_code=completed.returncode)
    return output


def preflight(executable, msys, project, diagnostics=None):
    executable, msys, project = Path(executable), Path(msys), Path(project)
    if not executable.is_file():
        raise RuntimeError(f'UxPlay executable missing: {executable}. Set MIRRORDESK_UXPLAY to the existing UCRT64 build; do not rebuild the original.')
    if not (msys/'ucrt64/bin/gst-inspect-1.0.exe').is_file():
        raise RuntimeError(f'UCRT64 GStreamer missing under {msys}. This launcher does not install system packages.')
    (project/'.runtime/home').mkdir(parents=True, exist_ok=True)
    env = runtime_env(os.environ, msys.as_posix(), project.as_posix())
    # Upstream checks these before HOME. Never inherit original receiver policy.
    env.pop('UXPLAYRC', None)
    env.pop('XDG_CONFIG_HOMEDIR', None)
    for name in ('.uxplayrc', '.config/uxplayrc'):
        config = project/'.runtime/home'/name
        if config.exists() or config.is_symlink():
            raise RuntimeError('Scoped UxPlay config exists; move it aside manually before launching. No config was read or modified.')
    scanners = [msys/'ucrt64/lib/gstreamer-1.0/gst-plugin-scanner.exe', msys/'ucrt64/libexec/gstreamer-1.0/gst-plugin-scanner.exe']
    scanner = next((p for p in scanners if p.is_file()), None)
    if scanner is None:
        raise RuntimeError('Missing UCRT64 gst-plugin-scanner.exe (checked lib and libexec). Repair the matching MSYS2 package; never download random DLLs.')
    env['GST_PLUGIN_SCANNER'] = str(scanner)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    stage = 0
    def probe(command, name, timeout=PROBE_TIMEOUT_SECONDS):
        nonlocal stage
        stage += 1
        return probe_dependency(command, diagnostics, stage, project/'.runtime/home', env, timeout,
                                probe_name=name, creationflags=flags)
    help_text = probe([str(executable), '-h'], 'uxplay_help')
    # Reuse the existing help probe; no extra native invocation for BLE.
    env[ble_discovery.CAPABILITY_ENV] = '1' if ble_discovery.help_has_ble(help_text) else '0'
    if '-vrtp' not in help_text or '-artp' not in help_text:
        raise RuntimeError('Existing UxPlay help lacks -vrtp/-artp. Stop: this build cannot forward; no original-source changes made.')
    if not re.search(r'^\s*-pin(?=\s|\[|$)', help_text, re.MULTILINE):
        raise RuntimeError('PIN capability missing from existing UxPlay help. Refusing unprotected launch; no fallback or original-source changes.')
    # The first inspect may initialize/rebuild the scoped registry, even if
    # its file already exists. Allow that scan once per preflight, not once
    # per plugin; a timeout remains fatal with no unsafe scanner-tree retry.
    print('Checking GStreamer: a cold plugin registry scan may take time; '
          f'waiting up to {FIRST_GSTREAMER_PROBE_TIMEOUT_SECONDS}s for the first probe. '
          f'Later probes retain {PROBE_TIMEOUT_SECONDS}s deadlines.', flush=True)
    for index, plugin in enumerate(('appsrc', 'h264parse', 'rtph264pay', 'rtpL16pay', 'udpsink', 'audioconvert', 'audioresample', 'avdec_aac', 'avdec_alac', 'fakesink')):
        timeout = FIRST_GSTREAMER_PROBE_TIMEOUT_SECONDS if index == 0 else PROBE_TIMEOUT_SECONDS
        probe([str(msys/'ucrt64/bin/gst-inspect-1.0.exe'), plugin], f'gst_inspect_{plugin}', timeout)
    print('PASS: original UxPlay -h under scoped UCRT64; forwarding, PIN option and required plugins found. Pairing is not hardware-verified.', flush=True)
    return env


def capture_output(stream, diagnostics, component):
    # Bound each read; arbitrary sender-controlled text never reaches saved logs.
    last = {}
    try:
        while True:
            chunk = stream.readline(1024)
            if not chunk:
                break
            text = chunk.decode('utf-8', 'replace')
            signal = receiver_signal(text)
            now = time.monotonic()
            if signal and now-last.get(signal, -100) >= 1:
                diagnostics.record('receiver_signal', component=component, signal=signal)
                last[signal] = now
            # Preserve existing local console diagnostics/PIN display, not the bundle.
            print(text, end='', flush=True)
    finally:
        stream.close()


def main():
    diagnostics = DiagnosticSession.create(ROOT/'.diagnostics', component='launcher')
    print(f'Diagnostic session: {diagnostics.directory.name}', flush=True)
    try:
        return run(diagnostics)
    except Exception as exc:
        name = type(exc).__name__
        diagnostics.record('launcher_error', error=name if name in {'OSError','RuntimeError','TimeoutError','ValueError'} else 'other')
        raise
    finally:
        diagnostics.record('session_end', component='launcher')
        try:
            print(f'Diagnostics saved: {diagnostics.save_export()}', flush=True)
        except OSError:
            print('WARNING: diagnostics could not be saved; check writable folder/free disk.', flush=True)


def run(diagnostics):
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Only executable/plugin probes, port checks and discovery evidence; no receiver')
    parser.add_argument('--bluetooth', action='store_true', help='Opt in to upstream Bluetooth LE discovery; PIN and media still use the LAN')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('Windows launcher only. On Linux run .venv/bin/python backend.py for the RTP transport fixture.')
    exe = Path(os.environ.get('MIRRORDESK_UXPLAY', rf'C:\msys64\home\{os.environ.get("USERNAME", "")}\mirrordesk-uxplay\build\uxplay.exe'))
    msys = Path(os.environ.get('MIRRORDESK_MSYS2', r'C:\msys64'))
    check_ports()
    env = preflight(exe, msys, ROOT, diagnostics)
    discovery = check_discovery(exe, diagnostics)
    ble_plan = ble_discovery.prepare(args.bluetooth, env.get(ble_discovery.CAPABILITY_ENV) == '1', discovery, ROOT)
    ble_discovery.report(ble_plan, diagnostics)
    if args.check:
        check_discovery_path(discovery, diagnostics, False)
        return
    discovery_stop = threading.Event()
    discovery_watch = None
    discovery_path = None
    children = []
    readers = []
    backend = None
    beacon = None
    beacon_exit_reported = False
    backend_healthy = False
    opener = None
    backend_env = os.environ.copy()
    backend_env["MIRRORDESK_DIAGNOSTICS"] = str(diagnostics.directory)
    def watch(child, component):
        diagnostics.record("process_start", component=component)
        reader = threading.Thread(target=capture_output, args=(child.stdout, diagnostics, component), daemon=True)
        reader.start()
        readers.append(reader)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        backend = subprocess.Popen([sys.executable, str(ROOT/'backend.py')], cwd=ROOT, creationflags=flags, env=backend_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        children.append(backend)
        watch(backend, "backend")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(60):
            if backend.poll() is not None:
                raise RuntimeError(f'Web backend exited: {backend.returncode}')
            try:
                with opener.open('http://127.0.0.1:8765/status', timeout=.5) as response:
                    if response.status == 200:
                        backend_healthy = True
                        break
            except OSError:
                pass
            time.sleep(.1)
        else:
            raise RuntimeError('Backend did not become healthy within startup deadline.')
        activated = ble_discovery.activate(ble_plan)
        if activated.status != ble_plan.status:
            ble_discovery.report(activated, diagnostics)
        ble_plan = activated
        receiver_args = uxplay_args(exe) + ['-pin']
        if ble_plan.enabled:
            receiver_args += ['-ble', str(ble_plan.path)]
        receiver = subprocess.Popen(receiver_args, cwd=ROOT/'.runtime/home', env=env, creationflags=flags, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        receiver_started = time.monotonic()
        children.append(receiver)
        watch(receiver, 'receiver')
        # Early loader/config failure should be visible before opening browser.
        try:
            code = receiver.wait(timeout=1)
            raise RuntimeError(f'UxPlay exited immediately: {code}. Read diagnostics above.')
        except subprocess.TimeoutExpired:
            pass
        if backend.poll() is not None:
            raise RuntimeError(f'Web backend exited: {backend.returncode}')
        # Schedule before optional BLE setup: one-shot deadline is anchored to
        # receiver spawn, not to the later browser opening or beacon startup.
        discovery_path = threading.Thread(target=delayed_discovery_path,
                                          args=(discovery, diagnostics, discovery_stop, receiver_started),
                                          name='mirrordesk-discovery-path', daemon=True)
        discovery_path.start()
        if ble_plan.enabled:
            try:
                beacon_env = dict(env, PYTHONUNBUFFERED='1')
                beacon = subprocess.Popen(ble_discovery.command(ble_plan, sys.executable),
                                          cwd=ROOT/'.runtime/home', env=beacon_env, creationflags=flags,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                children.append(beacon)  # owned, cleaned up first; optional in monitor
                diagnostics.record('ble_process_start', ble_enabled=True)
                reader = threading.Thread(target=ble_discovery.capture_output, args=(beacon.stdout, diagnostics), daemon=True)
                reader.start()
                readers.append(reader)
            except OSError:
                diagnostics.record('ble_signal', ble_status='spawn_error')
                print('Bluetooth discovery could not start; continuing with network discovery only.', flush=True)
        print('Random first-pairing PIN requested: read it in this console, not the browser or saved diagnostics. Returning clients may not be prompted.', flush=True)
        print('Receiver running, NOT proof of iPhone connection. Trusted LAN only. Ctrl+C here stops owned processes.', flush=True)
        discovery_watch = threading.Thread(target=watch_discovery, args=(discovery, diagnostics, discovery_stop),
                                           name='mirrordesk-discovery-watch', daemon=True)
        discovery_watch.start()
        webbrowser.open('http://127.0.0.1:8765/')
        while True:
            for child in children:
                if child is beacon:
                    if child.poll() is not None and not beacon_exit_reported:
                        beacon_exit_reported = True
                        diagnostics.record('ble_process_exit', ble_status='exited', ble_exit_code=child.returncode)
                        print('WARNING: Bluetooth discovery stopped; mirroring and network discovery keep running. '
                              'Check PC Bluetooth before the next launch.', flush=True)
                    continue  # never stop media for the optional beacon's death
                if child.poll() is not None:
                    raise RuntimeError(f'Owned process exited ({child.returncode}); stopping this session.')
            time.sleep(.3)
    except KeyboardInterrupt:
        print('Stopping MirrorDesk Web owned processes.')
    finally:
        discovery_stop.set()  # daemon watcher; never joined before child cleanup
        # Ask aiohttp to close its workers before TerminateProcess on Windows.
        try:
            req = urllib.request.Request('http://127.0.0.1:8765/stop', method='POST', headers={'Origin':'http://127.0.0.1:8765', 'X-MirrorDesk':'1'})
            if backend_healthy and backend is not None and backend.poll() is None:
                opener.open(req, timeout=5).close()
        except Exception:
            pass
        cleanup_failed = False
        for child in reversed(children):
            try:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_failed = True
                    diagnostics.record('launcher_error', error='RuntimeError')
                    print('WARNING: an owned process could not be reaped; verify shutdown before relaunch.', flush=True)
            if child.returncode is not None:
                if child is beacon:
                    if not beacon_exit_reported:
                        diagnostics.record('ble_process_exit', ble_status='exited', ble_exit_code=child.returncode)
                else:
                    diagnostics.record("process_exit", component="backend" if child is backend else "receiver", exit_code=child.returncode)
        for reader in readers:
            reader.join(timeout=2)
        if discovery_watch is not None:
            discovery_watch.join(timeout=discovery_probe.LIGHT_TIMEOUT_SECONDS + 1)
        # Path thread is daemonized and cancellation wakes its delay; never join
        # active socket work or put it ahead of owned-child cleanup.
        if discovery_path is not None:
            discovery_path.join(timeout=0)
        if cleanup_failed and sys.exc_info()[0] is None:
            raise RuntimeError('An owned process could not be reaped; verify shutdown before relaunch.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
