"""Performance benchmark (not a pass/fail test): server + fake UxPlay + one Chromium viewer.

Measures, per scenario, the hub's received fps/bitrate, the browser's decoded
fps/resolution/jitter-buffer delay, and the server process CPU/RSS. Synthetic
libx264 media on this machine only — not iPhone hardware evidence.

    .venv\\Scripts\\python tests\\e2e\\bench_performance.py [--seconds 15]
Writes test-results/p7-perf/bench-<utc>.json.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    import psutil  # optional (present via the Windows BLE extras); CPU/RSS omitted without it
except ImportError:
    psutil = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from conftest import LAUNCH, LiveServer  # noqa: E402

ROOT = HERE.parents[1]
SCENARIOS = [
    ('portrait-1080p60-passthrough', 'portrait', 'auto'),
    ('landscape-1080p60-passthrough', 'landscape', 'auto'),
    ('portrait-1080p60-compat', 'portrait', 'compat'),
    ('landscape-1080p60-compat', 'landscape', 'compat'),
]
STATS = """async () => { const v = window.mirrordesk.viewer; const out = {mode: v.mode, codec: v.codec};
  if (v.pc) { (await v.pc.getStats()).forEach(s => { if (s.type === 'inbound-rtp' && s.kind === 'video')
    Object.assign(out, {decoded: s.framesDecoded, w: s.frameWidth, h: s.frameHeight, jb: s.jitterBufferDelay,
                        jbn: s.jitterBufferEmittedCount, bytes: s.bytesReceived, dropped: s.framesDropped}); }); }
  return out; }"""


def server_process(pid):
    if psutil is None:
        return None
    proc = psutil.Process(pid)

    def is_server(q):
        args = q.cmdline()
        # Exactly `python -m mirrordesk ...` (the project folder name also contains
        # "mirrordesk", so a substring match would catch the fake receiver too).
        return any(a == '-m' and b == 'mirrordesk' for a, b in zip(args, args[1:]))
    candidates = [q for q in [proc] + proc.children(recursive=True) if is_server(q)]
    return candidates[-1] if candidates else proc  # the real interpreter behind the venv launcher


def run(seconds, data_root):
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **LAUNCH['chromium'])
        for name, orientation, mode in SCENARIOS:
            server = LiveServer(data_root / name, env={'FAKE_UXPLAY_ORIENTATION': orientation})
            try:
                server.wait_streaming()
                page = browser.new_page()
                page.goto(server.url + '/')
                if mode != 'auto':
                    page.evaluate(f"() => {{ const m = document.getElementById('mode'); m.value = '{mode}'; "
                                  "m.dispatchEvent(new Event('change')); }")
                page.wait_for_function("() => document.getElementById('video').videoWidth > 0 && "
                                       f"window.mirrordesk.viewer.mode === '{'compat' if mode == 'compat' else 'passthrough'}'",
                                       timeout=45000)
                time.sleep(2)
                main = server_process(server.process.pid)
                if main is not None:
                    main.cpu_percent(None)
                first, t0 = page.evaluate(STATS), time.monotonic()
                hub = []
                for _ in range(int(seconds)):
                    time.sleep(1)
                    hub.append(server.status()['source'])
                last, t1 = page.evaluate(STATS), time.monotonic()
                cpu = main.cpu_percent(None) / psutil.cpu_count() if main is not None else None
                elapsed = t1 - t0
                result = {
                    'scenario': name, 'mode': last['mode'], 'codec': last['codec'],
                    'source': f"{hub[-1]['width']}x{hub[-1]['height']} {hub[-1]['profile']}",
                    'hub_fps_avg': round(sum(h['fps'] for h in hub) / len(hub), 1),
                    'hub_bitrate_kbps_avg': round(sum(h['bitrate_kbps'] for h in hub) / len(hub)),
                    'browser_resolution': f"{last['w']}x{last['h']}",
                    'browser_decoded_fps': round((last['decoded'] - first['decoded']) / elapsed, 1),
                    'browser_received_kbps': round((last['bytes'] - first['bytes']) * 8 / elapsed / 1000),
                    'browser_jitter_buffer_ms': round((last['jb'] - first['jb']) / max(1, last['jbn'] - first['jbn']) * 1000, 1),
                    'browser_frames_dropped': last.get('dropped'),
                    'server_cpu_percent_of_machine': None if cpu is None else round(cpu, 1),
                    'server_rss_mb': None if main is None else round(main.memory_info().rss / 2**20),
                }
                print(json.dumps(result), flush=True)
                results.append(result)
                page.close()
            finally:
                server.stop()
        browser.close()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=15)
    args = parser.parse_args()
    out_dir = ROOT / 'test-results' / 'p7-perf'
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    results = run(args.seconds, out_dir / f'data-{stamp}')
    report = {'utc': stamp, 'machine': {'cpus': psutil.cpu_count() if psutil else None, 'python': sys.version.split()[0]},
              'note': 'synthetic libx264 media, Chromium headless, loopback - not iPhone hardware evidence',
              'results': results}
    path = out_dir / f'bench-{stamp}.json'
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('saved', path)


if __name__ == '__main__':
    main()
