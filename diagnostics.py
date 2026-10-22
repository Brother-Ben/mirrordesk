"""Local, structured diagnostics. Never persist arbitrary logs, SDP or media."""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from importlib.metadata import version, PackageNotFoundError

EVENTS = {'session_start', 'session_end', 'issue', 'browser', 'sample', 'viewer_stop',
          'offer', 'offer_error', 'worker_start', 'worker_exit', 'preflight',
          'process_start', 'process_exit', 'receiver_signal', 'launcher_error'}
ISSUES = {'freeze', 'audio', 'delay', 'sync', 'disconnect', 'other',
          # User-reported discovery failures (fixed values; markable before Connect).
          # A user report only: not a measured network path or adapter identity.
          'no_connection_ethernet', 'no_connection_wifi', 'no_mirrordesk_option'}
NUMBERS = {'video_frames', 'audio_frames', 'video_age', 'audio_age', 'video_restarts',
           'audio_restarts', 'queue_delay', 'video_width', 'video_height', 'video_dropped',
           'video_fps', 'video_packets', 'audio_packets', 'audio_energy', 'audio_samples',
           'video_jitter', 'audio_jitter', 'video_lost', 'audio_lost', 'audio_jitter_buffer_delay',
           'audio_jitter_buffer_emitted', 'audio_concealed_samples', 'audio_silent_concealed_samples',
           'video_jitter_buffer_delay', 'video_jitter_buffer_emitted', 'video_concealed_samples',
           'video_silent_concealed_samples', 'exit_code',
           'duration', 'stage_index', 'audio_sample_rate', 'audio_frame_samples'}
BOOLS = {'video_alive', 'audio_alive', 'paused', 'hidden', 'muted', 'ok'}
ENUMS = {'issue': ISSUES, 'kind': {'video', 'audio'},
         'component': {'backend', 'receiver', 'launcher'},
         'peer': {'none', 'new', 'connecting', 'connected', 'disconnected', 'failed', 'closed'},
         'signal': {'connection', 'disconnect', 'warning', 'error', 'timeout'},
         'error': {'OSError', 'RuntimeError', 'TimeoutError', 'ValueError', 'other'},
         # Categorical startup preflight probe outcome; never raw exception text.
         'probe_outcome': {'success', 'nonzero_exit', 'timeout', 'os_error'},
         # Fixed allowlisted probe identities only; never a raw argv/path string.
         'probe_name': {'uxplay_help'} | {f'gst_inspect_{p}' for p in
                        ('appsrc', 'h264parse', 'rtph264pay', 'rtpL16pay', 'udpsink',
                         'audioconvert', 'audioresample', 'avdec_aac', 'avdec_alac', 'fakesink')}}
# Launcher-only phone-discovery readiness (discovery_probe.py). Categorical
# prediction from source analysis + read-only Windows state; never adapter
# alias/description, address, MAC, SSID, program path or raw PowerShell text.
EVENTS |= {'discovery_check', 'discovery_change'}
_DISCOVERY_VERDICTS = {'ready', 'public_network', 'firewall_blocked', 'vpn_or_virtual_adapter', 'no_adapter', 'unknown'}
ENUMS.update({'discovery_mode': {'full', 'light'},
              'discovery_verdict': _DISCOVERY_VERDICTS,
              'discovery_previous_verdict': _DISCOVERY_VERDICTS,
              'discovery_kind': {'ethernet', 'wifi', 'vpn_or_virtual', 'other'},
              'discovery_profile': {'private', 'public', 'domain', 'unknown'},
              'discovery_firewall': {'allow', 'block', 'no_rule', 'off', 'unknown'}})
BOOLS |= {'discovery_ok', 'discovery_both_up', 'discovery_adapter_changed', 'discovery_address_changed'}
NUMBERS |= {'discovery_adapters_up', 'discovery_candidates'}
# Readback path evidence: categories/counts only, never responder identities.
EVENTS |= {'discovery_path'}
ENUMS.update({'path_scope': {'summary', 'adapter'},
              'path_verdict': {'path_ok', 'no_lan_replies', 'receiver_silent', 'inconclusive'},
              'path_kind': {'ethernet', 'wifi', 'vpn_or_virtual', 'other'},
              'path_reason': {'none', 'no_candidates', 'no_ipv4', 'unicast_disabled', 'unicast_unknown', 'socket_error', 'timeout', 'limit'}})
BOOLS |= {'path_receiver_running', 'path_own_answer', 'path_own_a_matches'}
NUMBERS |= {'path_adapters', 'path_index', 'path_other_responders', 'path_other_airplay'}
# Optional upstream Bluetooth beacon: no raw stdout/addresses/paths.
EVENTS |= {'ble_preflight', 'ble_process_start', 'ble_signal', 'ble_process_exit'}
ENUMS['ble_status'] = {'off', 'ready', 'capability_missing', 'deps_missing', 'no_ipv4',
                       'config_present', 'unsafe_path', 'file_error', 'beacon_missing',
                       'spawn_error', 'started', 'aborted_no_bluetooth', 'import_error', 'exited'}
BOOLS |= {'ble_requested', 'ble_enabled'}
NUMBERS |= {'ble_exit_code'}
# Keep browser input separate from backend-only instrumentation.
BROWSER_FIELDS = {'video_frames', 'audio_frames', 'video_width', 'video_height',
                  'video_dropped', 'video_fps', 'video_packets', 'audio_packets',
                  'audio_energy', 'audio_samples', 'video_jitter', 'audio_jitter',
                  'video_lost', 'audio_lost', 'paused', 'hidden', 'muted', 'peer'}
# Reviewed browser playout counters. Collected only when the browser reports
# them; missing values stay absent rather than being recorded as zero. These are
# arrival/playout statistics, NOT end-to-end latency or audible-quality proof.
BROWSER_PLAYOUT_FIELDS = {f'{kind}_{field}' for kind in ('video', 'audio')
                          for field in ('jitter_buffer_delay', 'jitter_buffer_emitted',
                                        'concealed_samples', 'silent_concealed_samples')}
BROWSER_FIELDS |= BROWSER_PLAYOUT_FIELDS
# Numeric RTP stream identity (SSRC) per kind, for interval-safety when
# computing jitter-buffer residence across two samples: a changed value
# signals a stream reset rather than a continuous interval. Not a device
# fingerprint, address, PIN or media content.
STREAM_IDENTITY_FIELDS = {f'{kind}_stream_id' for kind in ('video', 'audio')}
BROWSER_FIELDS |= STREAM_IDENTITY_FIELDS
NUMBERS |= STREAM_IDENTITY_FIELDS
# Explicitly path-labelled: backend<->browser LOOPBACK only (same computer,
# one HTTP hop). Never phone Wi-Fi latency and never a touch-to-display proxy.
BROWSER_FIELDS |= {'backend_loopback_rtt_ms'}
NUMBERS |= {'backend_loopback_rtt_ms'}
NUMBERS |= BROWSER_PLAYOUT_FIELDS
NUMBERS |= {'viewer_generation'} | {
    f'{kind}_{field}' for kind in ('video', 'audio')
    for field in ('dequeued', 'returned', 'timestamp_dropped', 'timestamp_rebased',
                  'queue_delay', 'queue_delay_max', 'queue_delay_mean',
                  'source_delta', 'outgoing_pts', 'generation')
}
BROWSER_FIELDS |= {'browser_generation', 'video_presented', 'rendered_supported', 'document_hidden', 'output_state'}
NUMBERS |= {'browser_generation', 'video_presented'} | {
    f'{kind}_{field}' for kind in ('video', 'audio')
    for field in ('corrupt_packets', 'demux_packets', 'decoded', 'restart_delay', 'sender_packets', 'sender_bytes',
                  'queue_evicted', 'queue_rejected',
                  'samples_enqueued', 'samples_returned', 'gap_count', 'samples_evicted', 'samples_rejected',
                  # Explicit scopes: worker = current decoder generation, track = viewer lifetime.
                  'samples_returned_worker', 'gap_count_worker',
                  'samples_returned_track', 'gap_count_track')
}
BOOLS |= {'rendered_supported', 'document_hidden'}
ENUMS['output_state'] = {'starting', 'progress', 'input_stale', 'output_stalled', 'paused',
                         'peer_disconnected', 'tab_hidden', 'metrics_unavailable', 'backend_unavailable',
                         'decoder_recovering'}
for _kind in ('video', 'audio'):
    ENUMS[_kind + '_decoder_stage'] = {'opening', 'demux', 'decode', 'convert', 'closed'}
    ENUMS[_kind + '_decoder_reason'] = {'none', 'eof', 'invalid_data', 'timeout', 'fatal',
                                       'corruption_budget', 'no_frames', 'stopped', 'close_error'}
# Prototype 7 browser-first server (mirrordesk/): numbers and categories only.
# Never device names, addresses, PINs, access codes or raw receiver output.
EVENTS |= {'hub_sample', 'viewer_start', 'settings_change', 'receiver_state'}
NUMBERS |= {'source_width', 'source_height', 'source_fps', 'source_bitrate_kbps', 'source_keyframes',
            'source_keyframe_interval_s', 'source_lost_packets', 'source_dropped_nals', 'source_gop_overflows',
            'source_stream_resets', 'source_audio_discontinuities', 'viewer_count', 'compat_decoded',
            'compat_errors', 'browser_rtt_ms', 'video_bitrate_kbps', 'video_freeze_count',
            'video_jitter_buffer_ms', 'audio_jitter_buffer_ms', 'video_frames_decoded', 'restarts'}
ENUMS.update({'source_state': {'idle', 'streaming', 'paused', 'audio_only'},
              'source_profile': {'baseline', 'main', 'extended', 'high', 'high10', 'high422', 'high444', 'other'},
              'receiver_state': {'stopped', 'checking', 'running', 'restarting', 'stopping', 'failed', 'unavailable'},
              'phone_state': {'idle', 'connecting', 'mirroring', 'paused', 'audio_only'},
              'viewer_mode': {'passthrough', 'compat'},
              'setting': {'receiver_name', 'resolution', 'fps', 'autostart_receiver', 'video_mode', 'max_viewers',
                          'audio_enabled', 'av_sync', 'nohold', 'compat_max_height', 'require_code_on_localhost',
                          'airplay_ports', 'pin', 'access_code', 'owner_code', 'usb_enabled'}})
BOOLS |= {'viewer_local'}
# USB cable input (mirrordesk/usbsource.py): states and counters only, never device names or serials.
EVENTS |= {'usb_state'}
NUMBERS |= {'usb_sessions'}
ENUMS.update({'usb_state': {'off', 'unavailable', 'waiting', 'driver', 'connecting', 'pairing', 'streaming', 'error'},
              'source_input': {'airplay', 'usb'}})
ISSUES |= {'no_connection_usb'}
# Android input (mirrordesk/androidsource.py): states and counters only, never serials, names or addresses.
EVENTS |= {'android_state'}
NUMBERS |= {'android_sessions'}
ENUMS['android_state'] = {'off', 'unavailable', 'installing', 'waiting', 'unauthorized', 'connecting',
                          'streaming', 'error'}
ENUMS['source_input'] = ENUMS['source_input'] | {'android'}
ENUMS['setting'] = ENUMS['setting'] | {'android_enabled', 'android_max_size', 'android_bit_rate', 'android_fps',
                                       'android_control', 'android_audio_on_phone'}
ISSUES |= {'no_connection_android'}
ENUMS['issue'] = ISSUES
# Why an Android session failed and what it was, as fixed categories and numbers only (androidsource.REASONS).
EVENTS |= {'android_session_start', 'android_session_end', 'android_audio', 'android_pairing',
           'android_control_blocked'}
_ANDROID_REASONS = {'tools_missing', 'adb_failed', 'unauthorized', 'no_permission', 'server_missing', 'push_failed',
                    'encoder', 'control_blocked', 'start_refused', 'start_timeout', 'connection_lost',
                    'stopped_on_phone', 'video_format', 'other', 'unplugged', 'stopped'}
ENUMS.update({'android_reason': _ANDROID_REASONS, 'android_result': _ANDROID_REASONS,
              'android_transport': {'usb', 'wifi'},
              'android_audio_state': {'off', 'starting', 'on', 'unsupported', 'error'},
              'android_pairing_state': {'pairing', 'paired', 'connected', 'failed', 'waiting'},
              'android_pairing_mode': {'code', 'qr', 'connect'},
              'android_brand': {'google', 'samsung', 'xiaomi', 'redmi', 'poco', 'oneplus', 'motorola', 'oppo',
                                'vivo', 'realme', 'huawei', 'honor', 'sony', 'nokia', 'asus', 'lenovo', 'nothing',
                                'fairphone', 'tcl', 'zte', 'other', 'unknown'}})
NUMBERS |= {'android_sdk', 'android_seconds', 'android_frames', 'android_audio_packets', 'android_width',
            'android_height'}
BOOLS |= {'android_audio_requested'}
# Separate from the legacy reviewed BROWSER_FIELDS contract (backend.py keeps
# accepting exactly what it did); only mirrordesk/server.py uses this superset.
P7_BROWSER_FIELDS = BROWSER_FIELDS | {'video_bitrate_kbps', 'browser_rtt_ms', 'video_freeze_count',
                                      'video_jitter_buffer_ms', 'audio_jitter_buffer_ms',
                                      'video_frames_decoded', 'viewer_mode'}
# Audio stability (P7): server-side drops/rebases and the browser's own playout
# counters (concealed = samples the browser had to invent because audio was late,
# lost or discarded). Numbers only.
P7_AUDIO_BROWSER_FIELDS = {'audio_concealed_pct', 'audio_concealment_events', 'audio_packets_discarded',
                           'audio_packets_received', 'audio_dropouts', 'audio_buffer_floor_ms'}
P7_BROWSER_FIELDS |= P7_AUDIO_BROWSER_FIELDS
NUMBERS |= P7_AUDIO_BROWSER_FIELDS | {'source_audio_dropped_ms', 'source_audio_rebases'}
# 0.11: audio ticks the source was late for, the shared decoder kept warm for long GOPs
# (iPhones), whether the GOP could still be replayed, phone sessions ended, and who
# disconnected a phone (the input only).
NUMBERS |= {'source_audio_missed_ms', 'source_sessions_ended'}
# 0.11.1: how late the server's event loop ran (its audio clock and packet sending), the sound
# the source delivered, and the re-encoder's backlog: a computer too busy for the phone's video.
NUMBERS |= {'loop_lag_max_ms', 'loop_stalls', 'source_audio_input_ms', 'compat_queue', 'compat_dropped'}
# The clock-rate answer last given to a USB iPhone, in ppm from real time (it obeys it).
NUMBERS |= {'usb_skew_ppm'}
# 0.12.0: the worst stall in the phone's sound over the last minute, the backlog viewers' sound
# keeps for it, sound taken out quietly (trimmed) or waited out (held), and the phone's FLUSHes
# (pause, seek, next video) with the stale sound they stopped.
NUMBERS |= {'source_audio_jitter_ms', 'source_audio_backlog_ms', 'source_audio_trimmed_ms',
            'source_audio_held_ms', 'source_audio_flushes', 'source_audio_flushed_ms'}
BOOLS |= {'source_warm', 'source_replayable'}
EVENTS |= {'phone_disconnect'}
ENUMS['signal'] = ENUMS['signal'] | {'mirror_stop', 'kicked', 'new_session'}
# 0.13.0: video Wi-Fi delivered late and in bursts (stalls), the delay that evens it out, frames
# that came later than that, whether the receiver sends frame times at all; Original viewers
# shown the picture re-encoded until the phone's next keyframe (bridged frames).
NUMBERS |= {'source_video_jitter_ms', 'source_video_delay_ms', 'source_video_late_frames',
            'source_video_stalls', 'source_video_bridged'}
BOOLS |= {'source_video_timed'}
# A viewer that never connected, and what kind of browser/device/way in it was (fixed categories
# from the page and the server; never the browser's own text, an address or a name).
EVENTS |= {'viewer_failed'}
ENUMS['browser'] = {'chrome', 'edge', 'firefox', 'safari', 'samsung', 'opera', 'brave', 'other'}
ENUMS['device'] = {'windows', 'mac', 'iphone', 'ipad', 'android', 'linux', 'chromeos', 'other'}
ENUMS['path'] = {'local', 'lan', 'tailscale', 'vpn', 'name', 'other'}
ENUMS['ice'] = {'new', 'checking', 'connected', 'completed', 'failed', 'disconnected', 'closed'}
BOOLS |= {'page_hidden'}
ISSUES |= {'viewer_cannot_connect', 'viewer_cannot_see'}
ENUMS['issue'] = ISSUES
# The TURN relay on the web port (mirrordesk/turn.py): allocations and relayed traffic, and whether
# a viewer's connection is direct or relayed.
NUMBERS |= {'turn_allocations', 'turn_relayed_kb'}
ENUMS['connection_kind'] = {'direct', 'relay'}
P7_BROWSER_FIELDS |= {'connection_kind'}
ENUMS['usb_state'] = ENUMS['usb_state'] | {'held'}
ENUMS['android_state'] = ENUMS['android_state'] | {'held'}
ENUMS['setting'] = ENUMS['setting'] | {'paired_phones', 'confirm_quit'}
OPTIONAL_HASHED = ('mirrordesk/__init__.py', 'mirrordesk/server.py', 'mirrordesk/media.py', 'mirrordesk/rtp.py',
                   'mirrordesk/h264.py', 'mirrordesk/webrtc.py', 'mirrordesk/receiver.py', 'mirrordesk/netinfo.py',
                   'mirrordesk/security.py', 'mirrordesk/settings.py', 'mirrordesk/quicktime.py',
                   'mirrordesk/usbsource.py', 'mirrordesk/usbmuxd.py', 'mirrordesk/lockdown.py',
                   'mirrordesk/androidsource.py', 'mirrordesk/androidtools.py', 'mirrordesk/turn.py',
                   'mirrordesk/desktop.py', 'mirrordesk/web/app.js', 'mirrordesk/web/app.css', 'mirrordesk/web/index.html')
FILES = ('manifest.json', 'backend.jsonl', 'backend.jsonl.1', 'launcher.jsonl', 'launcher.jsonl.1')


def safe_fields(fields):
    result = {}
    for key, value in fields.items():
        if key in NUMBERS and type(value) in (int, float) and abs(value) <= 1e15 and math.isfinite(value):
            result[key] = value
        elif key in BOOLS and type(value) is bool:
            result[key] = value
        elif key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            result[key] = value
    return result


class DiagnosticSession:
    def __init__(self, directory, component='backend', limit=524288):
        if component not in ('backend', 'launcher'):
            raise ValueError('Invalid diagnostic writer')
        self.directory = Path(directory)
        self.component = component
        self.limit = limit
        self.lock = threading.Lock()
        self.failed = False

    @classmethod
    def create(cls, root, component='backend', limit=524288):
        directory = Path(root) / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:12])
        directory.mkdir(parents=True, mode=0o700)
        session = cls(directory, component, limit)
        dependencies = {}
        for name in ('aiohttp','aiortc','av','numpy'):
            try:
                dependencies[name] = version(name)
            except PackageNotFoundError:
                dependencies[name] = 'unavailable'
        root_code = Path(__file__).resolve().parent
        # Files absent from a slim deployment (e.g. the P7 container image) are skipped.
        hashes = {name: hashlib.sha256((root_code/name).read_bytes()).hexdigest()
                  for name in ('backend.py','launcher.py','diagnostics.py','web/client.js','web/progress.js','web/index.html',
                               'config/video.sdp','config/audio.sdp','discovery_probe.py','discovery_selftest.py','ble_discovery.py',
                                'requirements-ble.txt','third_party/uxplay_beacon/uxplay-beacon.py',
                                'third_party/uxplay_beacon/uxplay_beacon_module_winrt.py')
                  if (root_code/name).is_file()}
        hashes.update({name: hashlib.sha256((root_code/name).read_bytes()).hexdigest()
                       for name in OPTIONAL_HASHED if (root_code/name).is_file()})
        manifest = {'schema':3, 'session':directory.name, 'created_utc':datetime.now(timezone.utc).isoformat(),
                    'os':platform.system(), 'os_release':platform.release(), 'architecture':platform.machine(),
                    'python':platform.python_version(), 'dependencies':dependencies, 'code_sha256':hashes,
                    'contains_media':False, 'automatic_upload':False,
                    'privacy':'Structured allowlisted fields only; no raw receiver output, SDP, PINs, names or addresses.',
                    'limits':'Two 512 KiB log segments per writer; oldest events rotate out. Session folders retained until manually removed.',
                    'freshness':'Decoded-frame production age, NOT packet-ingress age. No end-to-end latency measurement.'}
        (directory/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        session.record('session_start', component=component)
        return session

    def record(self, event, **fields):
        if event not in EVENTS:
            raise ValueError('Unknown diagnostic event')
        data = {'event':event, 'utc':datetime.now(timezone.utc).isoformat(),
                'monotonic':time.monotonic(), **safe_fields(fields)}
        payload = (json.dumps(data, allow_nan=False)+'\n').encode()
        with self.lock:
            try:
                path = self.directory/(self.component+'.jsonl')
                if path.exists() and path.stat().st_size + len(payload) > self.limit:
                    path.replace(path.with_suffix('.jsonl.1'))
                with path.open('ab') as stream:
                    stream.write(payload)
            except OSError:
                self.failed = True  # Diagnostics must not break media on full disk.

    def export(self):
        output = io.BytesIO()
        with self.lock, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in FILES:
                path = self.directory/name
                if path.is_file() and not path.is_symlink():
                    with path.open('rb') as stream:
                        archive.writestr(name, stream.read(524288 if name != 'manifest.json' else 65536))
        return output.getvalue()

    def save_export(self):
        payload = self.export()
        path = self.directory/'diagnostics.zip'
        temporary = self.directory/(self.component+'-export.tmp')
        temporary.write_bytes(payload)
        temporary.replace(path)
        return path


def export_latest(root):
    sessions = sorted((p for p in Path(root).glob('*') if p.is_dir() and not p.is_symlink() and (p/'manifest.json').is_file()),
                      key=lambda p: json.loads((p/'manifest.json').read_text(encoding='utf-8'))['created_utc'])
    if not sessions:
        raise RuntimeError('No diagnostic sessions found.')
    return DiagnosticSession(sessions[-1]).save_export()


def receiver_signal(line):
    """Classify text in memory; never write the raw text (may contain pairing PIN)."""
    text = line.lower()
    for token, label in [('error','error'), ('warn','warning'), ('timeout','timeout'),
                         ('disconnect','disconnect'), ('connect','connection')]:
        if token in text:
            return label
    return None


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Export saved diagnostics without starting any receiver or server.')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent/'.diagnostics')
    args = parser.parse_args()
    try:
        print(export_latest(args.root))
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f'Cannot export diagnostics: {exc}\n')

