"""Local feasibility bridge. AirPlay itself is supplied by unmodified UxPlay."""
import argparse
import asyncio
from collections import deque
import ipaddress
import multiprocessing as mp
import os
from pathlib import Path
import queue
import socket
import time
from fractions import Fraction

import aioice.ice
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCConfiguration, RTCSessionDescription
import av
import numpy as np
from diagnostics import DiagnosticSession, ISSUES, BROWSER_FIELDS, safe_fields

ROOT = Path(__file__).resolve().parent
# Pinned aioice 0.10.2 internal hook: don't open ICE sockets on LAN adapters.
# No STUN/TURN. Same-computer browser only. Covered by socket/media smoke.
aioice.ice.get_host_addresses = lambda use_ipv4, use_ipv6: ['127.0.0.1']


NETWORK_RTT_PATHS = ('router', 'phone', 'loopback')


def measure_tcp_connect_rtt(host, port, path, timeout=1.0):
    """Bounded, low-overhead, explicitly path-labelled TCP-connect RTT probe.

    Test 4 correction (item 5): supporting network diagnostics must
    distinguish laptop->router, laptop->phone (optional/unknown target) and
    backend->browser loopback, and must never be labelled phone Wi-Fi
    latency or used to derive end-to-end touch latency. This measures ONLY
    the TCP handshake time to an EXPLICITLY supplied target; it never
    auto-discovers a target (no netsh wlan, no Windows location services, no
    broad scan). A missing target is 'target_not_configured'; a
    non-responding target is inconclusive, never a fabricated 0ms and never a
    claim the target is definitively offline (silent drops are common).
    """
    if path not in NETWORK_RTT_PATHS:
        raise ValueError(f'Unknown network RTT path label: {path!r}; expected one of {NETWORK_RTT_PATHS}')
    if not host or not port:
        return {'path': path, 'reachable': False, 'rtt_ms': None,
                'inconclusive_reason': 'target_not_configured'}
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except (OSError, socket.timeout):
        return {'path': path, 'reachable': False, 'rtt_ms': None,
                'inconclusive_reason': 'no_response_or_refused'}
    return {'path': path, 'reachable': True,
            'rtt_ms': (time.monotonic() - started) * 1000, 'inconclusive_reason': None}


def uxplay_args(executable):
    return [str(executable), '-n', 'MirrorDesk Web', '-nh', '-s', '1280x720@30',
            '-vs', 'fakesink', '-as', 'fakesink',
            '-vrtp', 'pt=96 config-interval=1 ! udpsink host=127.0.0.1 port=5004 sync=false async=false',
            '-artp', 'pt=97 ! udpsink host=127.0.0.1 port=5006 sync=false async=false']


def runtime_env(original, root, project):
    env = original.copy()
    env['PATH'] = root + '/ucrt64/bin;' + original.get('PATH', '')
    env['GST_PLUGIN_PATH'] = root + '/ucrt64/lib/gstreamer-1.0'
    env['GST_PLUGIN_SYSTEM_PATH'] = env['GST_PLUGIN_PATH']
    env['GST_PLUGIN_SCANNER'] = root + '/ucrt64/lib/gstreamer-1.0/gst-plugin-scanner.exe'
    env['GST_REGISTRY'] = project + '/.runtime/gstreamer-registry.bin'
    # UxPlay reads ~/.uxplayrc. Isolate that config from the original project.
    env['HOME'] = project + '/.runtime/home'
    env['USERPROFILE'] = env['HOME']
    return env


DECODER_STAGES = ('opening', 'demux', 'decode', 'convert', 'closed')
DECODER_REASONS = ('none', 'eof', 'invalid_data', 'timeout', 'fatal', 'corruption_budget', 'no_frames', 'stopped', 'close_error')
DECODER_STAGE = 0
DECODER_REASON = 1
DECODER_CORRUPT = 2
DECODER_DEMUXED = 3
DECODER_DECODED = 4
DECODER_QUEUE_EVICTED = 5
DECODER_QUEUE_REJECTED = 6
AUDIO_SAMPLE_RATE = 12
AUDIO_FRAME_SAMPLES = 13
DECODER_PROGRESS_FIELDS = 14
AUDIO_SAMPLES_ENQUEUED = 7
AUDIO_SAMPLES_RETURNED = 8
AUDIO_GAP_COUNT = 9
AUDIO_SAMPLES_EVICTED = 10
AUDIO_SAMPLES_REJECTED = 11
VIDEO_QUEUE_CHUNKS = 2
AUDIO_MAX_FRAME_SAMPLES = 2048
AUDIO_QUEUE_MAX_SAMPLES = 32768
AUDIO_QUEUE_CHUNKS = AUDIO_QUEUE_MAX_SAMPLES // AUDIO_MAX_FRAME_SAMPLES
# Test 4 evidence (MirrorDesk-test4-media-analysis.md line 120): saved real
# decoded audio frames were ~133 samples, far smaller than AUDIO_MAX_FRAME_SAMPLES.
# A queue sized ONLY by AUDIO_QUEUE_CHUNKS (a fixed slot count derived from the
# 2048-sample assumption) then buffers only ~AUDIO_QUEUE_CHUNKS*133 samples
# (~48ms), not the advertised AUDIO_QUEUE_MAX_SAMPLES (~743ms) duration -- a
# 93%+ capacity shortfall that explains rapid eviction growth within one
# generation while timestamp counters stay at zero (samples_evicted growing is
# a queue-capacity symptom, not a timestamp-repair symptom).
AUDIO_MIN_FRAME_SAMPLES = 128  # Conservative floor informed directly by that evidence.
AUDIO_QUEUE_MAX_SLOTS_CAP = 512  # Bound worst-case multiprocessing.Queue depth/memory.
QUEUE_DELAY_WINDOW = 128  # Bounded recent-sample window for queue-residence distribution.


def audio_queue_capacity_slots(min_frame_samples):
    """Slot count so the queue retains close to AUDIO_QUEUE_MAX_SAMPLES worth of
    audio even when real frames are much smaller than AUDIO_MAX_FRAME_SAMPLES,
    bounded by AUDIO_QUEUE_MAX_SLOTS_CAP so a tiny/degenerate frame size cannot
    make the queue unboundedly deep. Never narrower than the historical
    AUDIO_QUEUE_CHUNKS so normal/larger frames keep their existing headroom.
    """
    frame_floor = max(1, min(min_frame_samples, AUDIO_MAX_FRAME_SAMPLES))
    slots = AUDIO_QUEUE_MAX_SAMPLES // frame_floor
    return max(AUDIO_QUEUE_CHUNKS, min(slots, AUDIO_QUEUE_MAX_SLOTS_CAP))


def progress_get(progress, index, default=0):
    try:
        return progress[index]
    except (IndexError, TypeError):
        return default


def progress_set(progress, index, value):
    try:
        progress[index] = value
    except IndexError:
        pass


def progress_inc(progress, index, amount=1):
    progress_set(progress, index, min(progress_get(progress, index) + amount, 2**31 - 1))


def audio_item_samples(item):
    """Recover sample count without assuming packed stereo representation."""
    data = item[2]
    layout = item[4] if len(item) > 4 else 'stereo'
    try:
        channels = len(av.AudioLayout(layout).channels)
    except (TypeError, ValueError):
        channels = 1
    return max(1, data.size // max(1, channels))


def decode_worker(kind, output, progress=None, stop_event=None):
    """Killable reader; only packet-local InvalidDataError is recoverable.

    Shared numeric progress: stage, reason, corrupt packets, demuxed packets,
    decoded frames. No raw exception text or media in diagnostics.
    """
    progress = progress if progress is not None else [0] * DECODER_PROGRESS_FIELDS
    container = None
    first = None
    failures = 0
    last_frame = time.monotonic()
    stopped = lambda: stop_event is not None and stop_event.is_set()
    try:
        container = av.open(str(ROOT / 'config' / (kind + '.sdp')), format='sdp', options={
            'protocol_whitelist': 'file,udp,rtp', 'localaddr': '127.0.0.1',
            'buffer_size': '262144', 'reorder_queue_size': '32',
            'analyzeduration': '0', 'probesize': '32768'}, timeout=(10, 1))
        progress[0] = 1
        for packet in container.demux():
            if stopped():
                progress[1] = 7
                break
            progress_inc(progress, DECODER_DEMUXED)
            progress[0] = 2
            try:
                frames = packet.decode()
            except av.InvalidDataError:
                progress_inc(progress, DECODER_CORRUPT)
                failures += 1
                if failures >= 32:
                    progress[1] = 5
                    break
                frames = ()
            for frame in frames:
                if stopped():
                    break
                if frame.pts is None or frame.time_base is None:
                    continue
                progress[0] = 3
                stamp = float(frame.pts * frame.time_base)
                if first is None:
                    first = stamp
                if kind == 'video':
                    if frame.width > 1920 or frame.height > 1920:
                        continue
                    item = (time.monotonic(), stamp-first, frame.to_ndarray(format='yuv420p'), frame.width, frame.height)
                else:
                    item = (time.monotonic(), stamp-first, frame.to_ndarray(), frame.format.name, frame.layout.name, frame.sample_rate)
                    progress_set(progress, AUDIO_SAMPLE_RATE, frame.sample_rate)
                    progress_set(progress, AUDIO_FRAME_SAMPLES, frame.samples)
                progress_inc(progress, DECODER_DECODED)
                if kind == 'audio':
                    samples = frame.samples
                    progress_inc(progress, AUDIO_SAMPLES_ENQUEUED, samples)
                last_frame = time.monotonic()
                failures = 0
                try:
                    output.put_nowait(item)
                except queue.Full:
                    if kind == 'audio':
                        # Audio keeps a finite continuity window; eviction is
                        # explicit and measured rather than hidden as a frame drop.
                        try:
                            evicted = output.get_nowait()
                            progress_inc(progress, DECODER_QUEUE_EVICTED)
                            evicted_samples = audio_item_samples(evicted)
                            progress_inc(progress, AUDIO_SAMPLES_EVICTED, evicted_samples)
                        except queue.Empty:
                            pass
                    else:
                        try:
                            output.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        output.put_nowait(item)
                    except queue.Full:
                        progress_inc(progress, DECODER_QUEUE_REJECTED)
                        if kind == 'audio':
                            progress_inc(progress, AUDIO_SAMPLES_REJECTED, samples)
            if stopped():
                progress[1] = 7
                break
            if time.monotonic() - last_frame > 10:
                progress[1] = 6
                break
            progress[0] = 1
        else:
            progress[1] = 1
    except Exception as exc:
        progress[1] = (2 if isinstance(exc, av.InvalidDataError) else
                       3 if isinstance(exc, (av.error.TimeoutError, TimeoutError)) else 4)
    finally:
        # Preserve failure stage; exit code 0 is not evidence of media success.
        if stopped():
            progress[1] = 7
        if container is not None:
            try:
                container.close()
            except Exception:
                if progress[1] in (0, 1, 7):
                    progress[1] = 8
        if progress[1] in (1, 7):
            progress[0] = 4


class InputTrack(MediaStreamTrack):
    def __init__(self, kind, state, diagnostics=None):
        super().__init__()
        self.kind = kind
        self.state = state
        self.diagnostics = diagnostics
        self.frames = 0  # Historical meaning: dequeued, NOT returned to aiortc.
        self.returned = 0
        self.timestamp_dropped = 0
        self.timestamp_rebased = 0
        self.source_delta = None
        self.source_stamp = None
        self.outgoing_pts = None
        self.restarts = -1
        self.audio_samples_returned = 0
        self.audio_expected_pts = None
        self.audio_gap_count = 0
        self.failure_streak = 0
        self.restart_delay = .2
        self.queue_delay = 0
        # Bounded queue-residence DISTRIBUTION, not just the latest item. A
        # single last-item value can look tiny while a much larger residence
        # occurred earlier in the same burst (Test 4 evidence). Track-lifetime
        # scope; a fixed-size deque keeps memory bounded regardless of run length.
        self.queue_delay_samples = deque(maxlen=QUEUE_DELAY_WINDOW)
        self.ctx = mp.get_context('spawn')
        self.origin = time.monotonic()
        self.offset = None
        self.last_stamp = -1.0
        self.start_worker()

    def start_worker(self):
        self.restarts += 1
        if self.diagnostics:
            self.diagnostics.record("worker_start", kind=self.kind)
        # Audio slot count is derived from AUDIO_MIN_FRAME_SAMPLES (the observed
        # short real frame size), not the larger AUDIO_MAX_FRAME_SAMPLES
        # assumption, so real small frames still retain close to the advertised
        # AUDIO_QUEUE_MAX_SAMPLES duration instead of evicting almost immediately.
        queue_size = (VIDEO_QUEUE_CHUNKS if self.kind == 'video'
                     else audio_queue_capacity_slots(AUDIO_MIN_FRAME_SAMPLES))
        self.queue = self.ctx.Queue(maxsize=queue_size)
        self.decoder_progress = self.ctx.Array('i', DECODER_PROGRESS_FIELDS, lock=False)
        self.worker_stop = self.ctx.Event()
        self.worker_started_at = time.monotonic()
        self.worker_closed = False
        self.worker = self.ctx.Process(target=decode_worker,
                                      args=(self.kind, self.queue, self.decoder_progress, self.worker_stop), daemon=True)
        self.worker.start()
        self.offset = None
        self.source_stamp = None
        self.source_delta = None
        self.non_increasing = 0
        self.repairing_video_clock = False
        self.last_produced_at = None

    def diagnostic_fields(self):
        values = {'frames': self.frames, 'dequeued': self.frames,
                  'returned': self.returned, 'timestamp_dropped': self.timestamp_dropped,
                  'timestamp_rebased': self.timestamp_rebased,
                  'queue_delay': self.queue_delay,
                  # Bounded recent-window distribution, not just the latest value.
                  'queue_delay_max': max(self.queue_delay_samples) if self.queue_delay_samples else None,
                  'queue_delay_mean': (sum(self.queue_delay_samples) / len(self.queue_delay_samples)
                                       if self.queue_delay_samples else None),
                  'source_delta': self.source_delta,
                  'outgoing_pts': self.outgoing_pts, 'generation': self.restarts,
                  'restarts': self.restarts, 'alive': self.worker.is_alive(),
                  'decoder_stage': DECODER_STAGES[progress_get(self.decoder_progress, DECODER_STAGE)],
                  'decoder_reason': DECODER_REASONS[progress_get(self.decoder_progress, DECODER_REASON)],
                  'corrupt_packets': progress_get(self.decoder_progress, DECODER_CORRUPT),
                  'demux_packets': progress_get(self.decoder_progress, DECODER_DEMUXED),
                  'decoded': progress_get(self.decoder_progress, DECODER_DECODED), 'restart_delay': self.restart_delay,
                   'queue_evicted': progress_get(self.decoder_progress, DECODER_QUEUE_EVICTED),
                   'queue_rejected': progress_get(self.decoder_progress, DECODER_QUEUE_REJECTED)}
        if self.kind == 'audio':
            # Audio-only sample accounting. Worker scope resets with each decoder
            # generation; track scope accumulates for the whole viewer lifetime.
            values.update({
                'samples_enqueued': progress_get(self.decoder_progress, AUDIO_SAMPLES_ENQUEUED),
                'samples_returned_worker': progress_get(self.decoder_progress, AUDIO_SAMPLES_RETURNED),
                'gap_count_worker': progress_get(self.decoder_progress, AUDIO_GAP_COUNT),
                'samples_returned_track': self.audio_samples_returned,
                'gap_count_track': self.audio_gap_count,
                'sample_rate': progress_get(self.decoder_progress, AUDIO_SAMPLE_RATE),
                'frame_samples': progress_get(self.decoder_progress, AUDIO_FRAME_SAMPLES),
                'samples_evicted': progress_get(self.decoder_progress, AUDIO_SAMPLES_EVICTED),
                'samples_rejected': progress_get(self.decoder_progress, AUDIO_SAMPLES_REJECTED)})
        return {self.kind + '_' + key: value for key, value in values.items()
                if value is not None}

    def close_worker(self):
        if self.worker_closed:
            return
        self.worker_closed = True
        self.worker_stop.set()
        if self.worker.is_alive():
            self.worker.join(timeout=.1)
        if self.worker.is_alive():
            self.worker.terminate()
        self.worker.join(timeout=2)
        if self.worker.is_alive():
            self.worker.kill()
            self.worker.join(timeout=2)
        # Prototype 5 r1 added a get_nowait() drain loop here to count queued
        # items discarded at close time. REVERTED in r2: Hermes's independent
        # review (test5-hermes-review-input/close-probe.py, reproduced by this
        # session) showed a real, reproducible shutdown hang, not merely a
        # theoretical risk. A multiprocessing.Queue's feeder thread can leave
        # a PARTIALLY WRITTEN item in the underlying pipe -- get_nowait() (and
        # get(timeout=...)) first polls readability, then performs a *blocking*
        # read of the rest of that item's bytes once any data is available.
        # After the worker has been terminated/killed (immediately above),
        # nothing is left alive to finish writing that partial item, so the
        # read blocks forever. This reproduced deterministically with a large
        # (1080p) video frame and is not specific to synthetic tests: it is a
        # property of multiprocessing.Queue itself, independent of frame
        # content, and it directly regresses ordinary Stop/shutdown/recovery
        # for real HD video. Finite item capacity bounds memory, not time: it
        # does not bound how long a partial read can block. Clean process exit
        # does not make this safe either -- decode_worker calls
        # cancel_join_thread(), so even a normally-exiting worker can leave a
        # partially flushed item behind. Accounting for residual queued items
        # at close time is therefore DEFERRED as an unmeasured, known gap
        # rather than solved with a same-process drain; see PROTOTYPE-5-RESULTS.md.
        self.queue.cancel_join_thread()
        self.queue.close()
        if self.worker.is_alive():
            super().stop()
            raise RuntimeError('Decoder worker did not stop; refusing another reader')

    async def recv(self):
        while self.readyState == 'live':
            if not self.worker.is_alive():
                if self.diagnostics:
                    self.diagnostics.record("worker_exit", kind=self.kind, exit_code=self.worker.exitcode,
                                            **self.diagnostic_fields())
                # Fast failing workers back off; long productive generations
                # reset the rate budget. Reconnect remains possible indefinitely.
                if progress_get(self.decoder_progress, DECODER_DECODED) >= 30 and time.monotonic() - self.worker_started_at >= 10:
                    self.failure_streak = 0
                self.restart_delay = min(5, .2 * 2**min(self.failure_streak, 5))
                self.failure_streak = min(self.failure_streak + 1, 6)
                self.close_worker()
                await asyncio.sleep(self.restart_delay)
                if self.readyState != 'live':
                    break
                self.start_worker()
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(.005)
                continue
            produced_at, stamp, data, *meta = item
            self.state[self.kind] = produced_at
            self.frames += 1
            self.queue_delay = max(0, time.monotonic() - produced_at)
            self.queue_delay_samples.append(self.queue_delay)
            self.source_delta = None if self.source_stamp is None else max(-3600, min(3600, stamp - self.source_stamp))
            self.source_stamp = stamp
            # Decoder generations restart RTP timestamps. Keep outgoing time
            # increasing across source restarts instead of rewinding WebRTC.
            if self.offset is None:
                self.offset = max(time.monotonic() - self.origin, self.last_stamp + .04) - stamp
            source_stamp = stamp
            stamp += self.offset
            if self.kind == 'video' and self.last_produced_at is not None:
                production_step = max(0, produced_at - self.last_produced_at)
                if stamp <= self.last_stamp:
                    self.non_increasing += 1
                    # Discard one isolated reorder/duplicate. Two consecutive
                    # regressions establish a discontinuity, not endless loss.
                    if self.non_increasing < 2 and not self.repairing_video_clock:
                        self.timestamp_dropped += 1
                        continue
                    self.repairing_video_clock = True
                else:
                    self.non_increasing = 0
                    self.repairing_video_clock = False
                # A source jump is suspect only when it exceeds the actual
                # decoded-production gap by >1s (preserve legitimate idle gaps).
                if self.repairing_video_clock or stamp - self.last_stamp > production_step + 1:
                    stamp = self.last_stamp + min(.2, max(1/120, production_step))
                    self.offset = stamp - source_stamp
                    self.timestamp_rebased += 1
            if self.kind == 'audio' and stamp <= self.last_stamp:
                # The source timeline regressed (generation restart, duplicate or
                # reorder). Audio must not rewind WebRTC, and clamping only this
                # frame's pts would let the next frame of the new epoch rewind
                # again. Rebase the whole epoch onto the END of the audio emitted
                # so far, so the new frames neither rewind nor OVERLAP the samples
                # already sent. Advancing by a single tick would keep PTS monotonic
                # while still overlapping almost a full frame.
                # Forward source gaps never reach here and remain observable.
                sample_rate = meta[2] if len(meta) > 2 and meta[2] else 48000
                repaired = (self.audio_expected_pts / sample_rate
                            if self.audio_expected_pts is not None else self.last_stamp)
                if repaired <= self.last_stamp:
                    # Defensive: never regress below what was already emitted.
                    repaired = self.last_stamp
                self.offset += repaired - stamp
                stamp = repaired
                self.timestamp_rebased += 1
            if stamp <= self.last_stamp:
                self.timestamp_dropped += 1
                continue
            self.last_stamp = stamp
            if self.kind == 'video':
                frame = av.VideoFrame.from_ndarray(data, format='yuv420p')
                frame.time_base = Fraction(1, 90000)
                # Float monotonicity does not imply distinct 90kHz ticks.
                frame.pts = max(round(stamp * 90000), (self.outgoing_pts + 1) if self.outgoing_pts is not None else 0)
            else:
                fmt, layout, rate = meta
                frame = av.AudioFrame.from_ndarray(data, format=fmt, layout=layout)
                frame.sample_rate = rate
                frame.time_base = Fraction(1, rate)
                samples = frame.samples
                candidate_pts = round(stamp * rate)
                if self.audio_expected_pts is not None and candidate_pts != self.audio_expected_pts:
                    # Every discontinuity stays visible as a gap, including resets.
                    self.audio_gap_count += 1
                    progress_inc(self.decoder_progress, AUDIO_GAP_COUNT)
                frame.pts = candidate_pts
                self.audio_expected_pts = frame.pts + samples
                self.audio_samples_returned += samples
                progress_inc(self.decoder_progress, AUDIO_SAMPLES_RETURNED, samples)
            self.last_produced_at = produced_at
            self.returned += 1
            self.outgoing_pts = frame.pts
            return frame
        from aiortc.mediastreams import MediaStreamError
        raise MediaStreamError

    def stop(self):
        if self.readyState == 'ended':
            return
        super().stop()
        self.close_worker()


def sender_fields(reports):
    """Project only numeric outbound counters; never retain IDs/addresses/SDP."""
    result = {}
    for report in reports.values():
        kind = getattr(report, 'kind', None)
        if report.type != 'outbound-rtp' or kind not in ('video', 'audio'):
            continue
        for attr, suffix in (('packetsSent', 'packets'), ('bytesSent', 'bytes')):
            key = kind + '_sender_' + suffix
            value = safe_fields({key: getattr(report, attr, None)}).get(key)
            if value is not None:
                result[key] = result.get(key, 0) + value
    return safe_fields(result)


def create_app(port=8765, diagnostics=None):
    diagnostics = diagnostics or DiagnosticSession.create(ROOT / ".diagnostics")
    origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    state = {'video': None, 'audio': None}
    peer = None
    tracks = []
    lease = None
    viewer_generation = 0  # Session-local counter; no persistent viewer identity.

    @web.middleware
    async def boundary(request, handler):
        addr = request.transport.get_extra_info('peername') if request.transport else None
        if not addr or not ipaddress.ip_address(addr[0]).is_loopback:
            raise web.HTTPForbidden()
        if request.host not in hosts or (request.headers.get('Origin') and request.headers['Origin'] not in origins):
            raise web.HTTPForbidden()
        if request.method != 'GET' and (request.headers.get('Origin') not in origins or request.headers.get('X-MirrorDesk') != '1'):
            raise web.HTTPForbidden()
        if request.headers.get('Content-Encoding'):
            raise web.HTTPUnsupportedMediaType()
        response = await handler(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"})
        return response

    app = web.Application(middlewares=[boundary], client_max_size=65536)

    async def stop():
        nonlocal peer, lease
        current, peer = peer, None
        if lease:
            lease.cancel(); lease = None
        if current:
            await current.close()
        for track in tracks:
            track.stop()
        tracks.clear()
        state.update(video=None, audio=None)
        diagnostics.record("viewer_stop")

    async def status(request):
        now = time.monotonic()
        return web.json_response({'source': 'UxPlay RTP (not connected)' if state['video'] is None else 'RTP input (sender identity not verified)',
            'video_age': None if state['video'] is None else now-state['video'],
            'audio_age': None if state['audio'] is None else now-state['audio'],
            'peer': peer.connectionState if peer else 'none',
            'viewer_generation': viewer_generation,
            **{key: value for track in tracks for key, value in track.diagnostic_fields().items()},
            'diagnostic_session': diagnostics.directory.name, 'diagnostic_write_failed': diagnostics.failed})

    async def offer(request):
        nonlocal peer, lease, viewer_generation
        try:
            async with asyncio.timeout(5):
                params = await request.json()
            if not isinstance(params, dict) or params.get('type') != 'offer' or not isinstance(params.get('sdp'), str) or not params['sdp'].startswith('v=0'):
                raise ValueError('Expected SDP offer')
        except (ValueError, TimeoutError):
            raise web.HTTPBadRequest(text='Expected JSON SDP offer')
        if peer:
            raise web.HTTPConflict(text='One viewer only. Stop the current session first.')
        # Never resolve remote SDP mDNS names / probe non-loopback candidates.
        lines = []
        for line in params['sdp'].splitlines():
            if line.startswith('a=candidate:'):
                parts = line.split()
                try:
                    if not ipaddress.ip_address(parts[4]).is_loopback:
                        continue
                except (ValueError, IndexError):
                    continue
            lines.append(line)
        viewer_generation += 1
        diagnostics.record("offer", viewer_generation=viewer_generation)
        peer = pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        try:
            async with asyncio.timeout(12):
                await pc.setRemoteDescription(RTCSessionDescription(sdp='\r\n'.join(lines)+'\r\n', type='offer'))
                for kind in ('video', 'audio'):
                    track = InputTrack(kind, state, diagnostics)
                    tracks.append(track)
                    pc.addTrack(track)
                await pc.setLocalDescription(await pc.createAnswer())
            async def expire_unconnected():
                await asyncio.sleep(20)
                if pc.connectionState != 'connected' and peer is pc:
                    await stop()
            lease = asyncio.create_task(expire_unconnected())
            @pc.on('connectionstatechange')
            async def changed():
                if pc.connectionState in ('failed', 'closed') and peer is pc:
                    await stop()
            return web.json_response({'sdp': pc.localDescription.sdp, 'type': pc.localDescription.type,
                                      'viewer_generation': viewer_generation})
        except Exception:
            diagnostics.record("offer_error")
            await stop()
            raise web.HTTPBadRequest(text='Invalid or unsupported SDP; see local diagnostics')

    async def stop_route(request):
        await stop()
        return web.json_response({'stopped': True})

    async def index(request):
        return web.FileResponse(ROOT / 'web/index.html')

    async def monitor(app):
        while True:
            now = time.monotonic()
            fields = {f'{kind}_age': None if state[kind] is None else now-state[kind] for kind in ('video','audio')}
            fields['peer'] = peer.connectionState if peer else 'none'
            fields['viewer_generation'] = viewer_generation
            for track in tracks:
                fields.update(track.diagnostic_fields())
            current = peer
            if current:
                try:
                    async with asyncio.timeout(1):
                        reports = await current.getStats()
                    if peer is current:
                        fields.update(sender_fields(reports))
                except Exception:
                    pass  # Missing stats remain absent, never fabricated as zero.
            diagnostics.record('sample', **fields)
            await asyncio.sleep(2)

    monitor_task = None
    async def startup(app):
        nonlocal monitor_task
        monitor_task = asyncio.create_task(monitor(app))

    async def cleanup(app):
        if monitor_task:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        await stop()
        diagnostics.record('session_end', component='backend')
        try:
            diagnostics.save_export()
        except OSError:
            diagnostics.failed = True

    last_browser = 0.0
    last_mark = 0.0
    last_export = 0.0
    async def diagnostic_input(request):
        try:
            async with asyncio.timeout(3):
                data = await request.json()
        except (ValueError, TimeoutError):
            raise web.HTTPBadRequest()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest()
        return data

    async def mark_issue(request):
        nonlocal last_mark
        data = await diagnostic_input(request)
        if set(data) != {'issue'} or not isinstance(data['issue'], str) or data['issue'] not in ISSUES:
            raise web.HTTPBadRequest()
        if time.monotonic()-last_mark < 1:
            raise web.HTTPTooManyRequests()
        last_mark = time.monotonic()
        diagnostics.record('issue', **data)
        return web.json_response({'marked':True})

    async def browser_stats(request):
        nonlocal last_browser
        data = await diagnostic_input(request)
        if not data or not set(data) <= BROWSER_FIELDS or safe_fields(data) != data:
            raise web.HTTPBadRequest()
        if time.monotonic()-last_browser < 1:
            raise web.HTTPTooManyRequests()
        last_browser = time.monotonic()
        diagnostics.record('browser', **data)
        return web.json_response({'recorded':True})

    async def export_diagnostics(request):
        nonlocal last_export
        if time.monotonic()-last_export < 3:
            raise web.HTTPTooManyRequests()
        last_export = time.monotonic()
        data = diagnostics.export()
        return web.Response(body=data, content_type='application/zip', headers={
            'Content-Disposition':f'attachment; filename="MirrorDesk-diagnostics-{diagnostics.directory.name}.zip"'})

    app.router.add_get('/', index)
    app.router.add_get('/status', status)
    app.router.add_post('/offer', offer)
    app.router.add_post('/stop', stop_route)
    app.router.add_static('/web/', ROOT / 'web', show_index=False)
    app.router.add_post('/diagnostics/mark', mark_issue)
    app.router.add_post('/diagnostics/browser', browser_stats)
    app.router.add_post('/diagnostics/export', export_diagnostics)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == '__main__':
    mp.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    shared = os.environ.get('MIRRORDESK_DIAGNOSTICS')
    diagnostics = DiagnosticSession(shared) if shared else None
    web.run_app(create_app(args.port, diagnostics), host='127.0.0.1', port=args.port, access_log=None)
