"""Always-on media hub: UxPlay loopback RTP -> any number of WebRTC viewers.

Video is forwarded as the phone's own H.264 (no decode/re-encode) whenever the
viewer's browser can decode that profile; a shared decoder feeds re-encoding
("compat") viewers otherwise. A bounded cache of the current GOP lets a viewer
that joins mid-stream, overflows, or loses packets resynchronise without the
phone having to send a new keyframe (the receiver cannot request one). When
the GOP grows too long to replay, the shared decoder keeps following it, so a
viewer can still join at any moment.
"""
import asyncio
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
import logging
import math
import os
import queue
import socket
import threading
import time

import av
import numpy as np
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

from .h264 import NAL_PPS, NAL_SPS, join_annexb, nal_type, parse_sps
from .rtp import AccessUnit, H264Depacketizer, l16_to_s16, parse_rtp, parameter_sets

log = logging.getLogger('mirrordesk.media')

VIDEO_CLOCK = 90000
VIDEO_TIME_BASE = Fraction(1, VIDEO_CLOCK)
SOCKET_BUFFER = 8 * 1024 * 1024
GOP_MAX_BYTES = 48 * 1024 * 1024
GOP_MAX_UNITS = 36000           # ten minutes at 60 fps; the byte limit is the real bound
SUB_MAX_UNITS = 120             # ~2 s at 60 fps before a viewer is resynced
COMPAT_QUEUE_UNITS = 600        # ~10 s at 60 fps waiting for the shared decoder
# Every input is converted once, at ingest, to Opus's native format, and viewers
# get exactly one 20 ms frame per recv(): aiortc stamps every Opus packet that
# one encode() call returns with the SAME RTP timestamp, so a bigger frame (the
# iPhone's USB audio arrives as 85 ms blocks) made browsers discard 3 of every 4
# packets as duplicates (measured: 76% of USB audio concealed in Chromium).
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_FRAME_SAMPLES = AUDIO_RATE // 50   # 20 ms = one Opus packet
AUDIO_MAX_QUEUE_SECONDS = 0.5   # hard cap while no one pulls (newest audio wins)
AUDIO_GAP_SECONDS = 0.25        # arrival gap treated as a source discontinuity
# Viewers' audio leaves on a steady 20 ms clock (LiveAudioTrack), each frame when the source's
# own pace says it is due: the stream's usual arrival time for that position (ArrivalJitter)
# plus a backlog. The backlog covers the source's block size (UxPlay: ~8 ms packets; the
# iPhone's USB audio: 85 ms blocks, and it switches between 21 and 85 ms on its own) and its
# arrival jitter, or at least some slack. Sound more than AUDIO_EXCESS_FRAMES past its time
# is surplus.
AUDIO_PACE_WINDOW_SECONDS = 3.0
AUDIO_PACE_MAX_SECONDS = 0.15
AUDIO_TARGET_MIN_SECONDS = 0.04
AUDIO_SLACK_SECONDS = 0.03
AUDIO_EXCESS_FRAMES = 2
AUDIO_PREROLL_MAX_SECONDS = 0.25  # longest wait for the first frame's time when the clock (re)starts
AUDIO_IDLE_SECONDS = 0.4          # source silent this long: the clock stops until audio is back
AUDIO_CATCHUP_TICKS = 5           # a sender this many frames late sends what is due at once
# Wi-Fi delivers AirPlay sound unevenly: now and then nothing arrives for ~0.1 s, then all of it
# at once (the phone's power saving; a lost packet UxPlay waits for). A fixed 40 ms backlog ran
# dry in every such stall and the late sound was then skipped (measured on a user's ZimaOS box:
# 40-60 ms missing, then 60 ms skipped, every 5-15 s). The backlog now covers the worst stall
# of the last AUDIO_JITTER_WINDOW_SECONDS, up to AUDIO_JITTER_MAX_SECONDS.
AUDIO_JITTER_WINDOW_SECONDS = 60.0
AUDIO_JITTER_MAX_SECONDS = 0.15
AUDIO_JITTER_MARGIN_SECONDS = 0.025
AUDIO_USUAL_SECONDS = 0.6         # the usual pace: median arrival offset over this long
AUDIO_CONFIRM_SECONDS = 0.4       # a late arrival counts as a stall if the pace stays put this long
# When sound resumes after a pause or seek the phone sends its first ~0.2 s at once, ahead of
# time. That sound is the new video's first moment, not stale: it plays from its start. (0.11.1
# skipped all but 40 ms of it and then ran dry until the phone caught up: 120-300 ms skipped and
# as much missing right after every seek.) Chunks arriving back to back (a burst ahead, or the
# catch-up after a stall) do not count towards the pace.
AUDIO_PACED_GAP_SECONDS = 0.003
# Sound late beyond the backlog (it shrinks once Wi-Fi calms down; a phone clock a little fast)
# leaves one quiet frame at a time, its joint blended, so nobody hears a jump; sound more than
# AUDIO_SKIP_SECONDS late, or late at the start of a stream, is skipped at once. A clock that
# runs ahead of the source waits out a tick only where the sound is near silence.
AUDIO_TRIM_INTERVAL_SECONDS = 0.5
AUDIO_TRIM_CANDIDATES = 8
AUDIO_SKIP_SECONDS = 0.2
AUDIO_BLEND_SAMPLES = 96          # 2 ms at 48 kHz
AUDIO_QUIET_RMS = 60              # near silence (about -55 dBFS)
# Wi-Fi delivers the phone's video unevenly too: nothing for 0.1-0.3 s (a lost packet the phone
# sends again, its power saving while nobody touches it), then all of it at once, and every viewer
# froze for a moment (measured: a freeze every 15-20 s on every viewer at once, the computer's own
# screen included; a steady test stream showed none). VideoPacer sends frames on the phone's own
# schedule, a delay after their usual arrival that covers the worst stall of the last minute; the
# sound waits as long, so picture and sound stay together.
VIDEO_USUAL_SECONDS = 1.0
VIDEO_PACED_GAP_SECONDS = 0.004      # closer together than this: frames came in a burst
VIDEO_STALL_GAP_SECONDS = 0.08       # a gap this long, then a burst: a stall
VIDEO_JITTER_MAX_SECONDS = 0.35
VIDEO_JITTER_MARGIN_SECONDS = 0.02
VIDEO_DELAY_FALL_PER_SECOND = 0.05   # a shorter delay is reached slowly: no burst of frames
VIDEO_TIMELINE_JUMP_SECONDS = 3.0    # frame times this far off their arrivals: a new timeline
VIDEO_UNTIMED_FRAMES = 10            # this many frames with one timestamp: the receiver sends no frame times
SOURCE_IDLE_SECONDS = 3.0
SOURCE_PAUSED_SECONDS = 60.0
# After the phone stops mirroring, packets of its stream still in flight (UxPlay's pipeline, the
# socket buffer) are ignored this long: they made the ended session look alive again ('paused',
# the last frame frozen for a minute). A new phone takes seconds to connect.
SESSION_END_QUIET_SECONDS = 1.0
RESYNC_MIN_INTERVAL = 2.0
# Replaying a long GOP to a browser costs real network bandwidth and keeps its decoder busy;
# beyond these limits a passthrough viewer is shown the picture re-encoded until the phone's
# next keyframe (PassthroughVideoTrack's bridge), and new viewers start re-encoded from the
# warm shared decoder. Chromium holds at most 800 frames waiting to decode: a 20 s replay
# (1200 frames) overflowed it, it asked for a keyframe, got the replay again, and so on (an
# "Original" viewer froze and dropped most frames, and the server stalled for up to 0.24 s).
REPLAY_MAX_BYTES = 1536 * 1024
REPLAY_MAX_UNITS = int(os.environ.get('MIRRORDESK_REPLAY_MAX_UNITS') or 240)   # 4 s at 60 fps (tests lower it)
# The bridge's own H.264: about as many bits per pixel as the phone's stream while video plays.
BRIDGE_BITS_PER_PIXEL = 0.07
BRIDGE_MIN_BITRATE = 1_500_000
BRIDGE_MAX_BITRATE = 8_000_000
# Chromium asks for a keyframe 3 s after its last decoded frame whenever an RTP
# packet arrived in the last 5 s. On a still phone screen (which sends nothing)
# that viewer already shows the whole picture; replaying the GOP for it only
# restarted the same timer, so the cache went out again every few seconds for as
# long as the screen stood still (measured: a 3 MB GOP every 3-6 s). A PLI
# this long after the viewer's last frame, with nothing newer in the hub, is
# that timer, not damage (damage asks within ~1 s, while packets still flow).
STILL_PLI_SECONDS = 2.5


@dataclass
class AudioChunk:
    samples: object      # int16 ndarray shaped (1, n*channels)
    count: int           # samples per channel
    arrival: float
    discontinuity: bool
    position: float = None   # seconds into the stream (counted by the hub; None: the track counts)


class AudioConverter:
    """Interleaved s16 PCM at any rate/channel count -> the hub's 48 kHz stereo.

    Stateful (the resampler keeps its filter history between blocks); reset()
    after a discontinuity so stale history never bleeds into the new stream.
    Same-format input is passed through untouched (iPhone USB, Android).
    """

    def __init__(self, rate=AUDIO_RATE, channels=AUDIO_CHANNELS):
        self.rate = rate
        self.channels = channels
        self.resampler = None
        self.source = None

    def reset(self):
        self.resampler = None
        self.source = None

    def convert(self, samples, rate, channels):
        """samples: int16 ndarray (any shape, interleaved). Returns (1, n*channels) int16 or None."""
        samples = np.asarray(samples, dtype=np.int16).reshape(1, -1)
        usable = samples.shape[1] - samples.shape[1] % channels
        if usable <= 0 or channels not in (1, 2):
            return None
        samples = samples[:, :usable]
        if rate == self.rate and channels == self.channels:
            return samples
        if self.source != (rate, channels):
            self.source = (rate, channels)
            self.resampler = av.AudioResampler(format='s16', layout='stereo' if self.channels == 2 else 'mono',
                                               rate=self.rate)
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples), format='s16',
                                           layout='stereo' if channels == 2 else 'mono')
        frame.sample_rate = int(rate)
        out = [f.to_ndarray().reshape(1, -1) for f in self.resampler.resample(frame)]
        if not out:
            return None
        return out[0] if len(out) == 1 else np.concatenate(out, axis=1)


class _Datagram(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self.handler = handler

    def datagram_received(self, data, addr):
        self.handler(data, addr)

    def error_received(self, exc):  # e.g. Windows ICMP port-unreachable echoes
        pass


class RateWindow:
    """Events and bytes over the last `span` seconds (bounded memory)."""

    def __init__(self, span=1.0, maxlen=4096):
        self.span = span
        self.items = deque(maxlen=maxlen)

    def add(self, now, size=0):
        self.items.append((now, size))

    def _trim(self, now):
        while self.items and now - self.items[0][0] > self.span:
            self.items.popleft()

    def rate(self, now):
        self._trim(now)
        return len(self.items) / self.span

    def bytes_rate(self, now):
        self._trim(now)
        return sum(size for _, size in self.items) / self.span


class VideoSubscription:
    """Per-viewer bounded queue of access units with resync semantics."""

    def __init__(self, hub):
        self.hub = hub
        self.items = deque()
        self.live = 0          # live (non-replay) units queued; replays are byte-bounded
        self.event = asyncio.Event()
        self.waiting_keyframe = False
        self.closed = False
        self.resyncs = 0
        self.skipped = 0
        self.last_resync = -1e9
        self.last_given = None   # newest unit handed to the viewer's track

    def caught_up(self):
        """The viewer was handed the hub's newest unit and nothing is pending."""
        return (not self.items and not self.waiting_keyframe and self.last_given is not None
                and self.last_given is self.hub.newest_unit)

    def _replayable(self):
        hub = self.hub
        return bool(hub.gop_valid and hub.gop and hub.gop_bytes <= REPLAY_MAX_BYTES
                    and len(hub.gop) <= REPLAY_MAX_UNITS)

    def stuck(self):
        """Waiting for a keyframe that nothing can produce soon (an iPhone sends one only when
        its picture changes a lot): this viewer should switch to re-encoded video."""
        return (self.waiting_keyframe and not self._replayable()
                and getattr(self.hub, 'keyframe_requester', None) is None)

    def _load_cache(self):
        """Drop the backlog; replay the cache, or wait for the next keyframe."""
        self.items.clear()
        self.live = 0
        if self._replayable():
            self.items.extend((unit, True) for unit in self.hub.gop)
            self.waiting_keyframe = False
            self.resyncs += 1
            self.event.set()
            return True
        self.waiting_keyframe = True
        # Sources that can make a keyframe on demand (Android) spare the wait.
        request = getattr(self.hub, 'request_source_keyframe', None)
        if request is not None:
            request()
        return False

    def wait_for_keyframe(self):
        """The viewer's picture is damaged and the cache is too long to replay: drop the queue and
        take the next keyframe (PassthroughVideoTrack shows the picture re-encoded meanwhile)."""
        self.items.clear()
        self.live = 0
        self.waiting_keyframe = True
        self.event.set()

    def resync(self, force=False):
        """force: join/overflow (backlog must go). Otherwise a PLI/watchdog
        request, which only acts when the cache can actually be replayed - a
        healthy viewer must never be frozen waiting for a rare phone keyframe."""
        now = time.monotonic()
        if not force and (now - self.last_resync < RESYNC_MIN_INTERVAL or not self._replayable()):
            if not self._replayable():
                request = getattr(self.hub, 'request_source_keyframe', None)
                if request is not None:
                    request()   # the healthy queue is kept; a fresh keyframe follows from the source
            return False
        self.last_resync = now
        return self._load_cache()

    def push(self, unit):
        if self.closed:
            return
        if self.waiting_keyframe:
            if not unit.keyframe:
                self.skipped += 1
                return
            self.waiting_keyframe = False
        if self.live >= SUB_MAX_UNITS:
            # The viewer fell behind. The hub cache already contains this unit,
            # so replaying it restores the exact current decoder state. Only
            # live units count toward the limit, so a replay longer than the
            # limit cannot retrigger itself on every new frame.
            self.skipped += len(self.items)
            self.resync(force=True)
            return
        self.items.append((unit, False))
        self.live += 1
        self.event.set()

    async def get(self):
        while not self.items:
            if self.closed:
                raise MediaStreamError
            self.event.clear()
            await self.event.wait()
        unit, replay = self.items.popleft()
        if not replay:
            self.live -= 1
        self.last_given = unit
        return unit, replay

    def close(self):
        self.closed = True
        self.event.set()


class _Bridge:
    """The shared decoder's pictures, re-encoded for one passthrough viewer while it waits for
    the phone's next keyframe (PassthroughVideoTrack)."""

    def __init__(self, event):
        self.event = event
        self.latest = None
        self.codec = None
        self.force_keyframe = True
        self.frames = 0

    def offer(self, frame, at=None):   # CompatDecoder subscriber (event-loop thread)
        self.latest = (frame, time.monotonic() if at is None else at)
        self.event.set()

    def take(self):
        item, self.latest = self.latest, None
        return item

    def encode(self, frame):
        """Annex-B H.264 of one picture (executor thread): constrained baseline, which every
        browser that takes the phone's High profile also decodes."""
        size = (frame.width, frame.height)
        if self.codec is None or size != (self.codec.width, self.codec.height):
            self.codec = av.CodecContext.create('libx264', 'w')
            self.codec.width, self.codec.height = size
            self.codec.pix_fmt = 'yuv420p'
            self.codec.framerate = Fraction(60, 1)
            self.codec.time_base = Fraction(1, 60)
            self.codec.bit_rate = int(min(BRIDGE_MAX_BITRATE, max(BRIDGE_MIN_BITRATE,
                                                                   frame.width * frame.height * 60 * BRIDGE_BITS_PER_PIXEL)))
            self.codec.options = {'level': '42', 'preset': 'superfast', 'tune': 'zerolatency'}
            self.codec.profile = 'Baseline'
            self.force_keyframe = True
        frame.pict_type = av.video.frame.PictureType.I if self.force_keyframe else av.video.frame.PictureType.NONE
        self.force_keyframe = False
        frame.pts = self.frames
        frame.time_base = self.codec.time_base
        self.frames += 1
        return b''.join(bytes(p) for p in self.codec.encode(frame))


class PassthroughVideoTrack(MediaStreamTrack):
    """Emits av.Packet so aiortc packetizes the phone's H.264 untouched.

    A viewer can start, or recover from damage, only at a keyframe or from a replay of the
    cached GOP. An iPhone sends a keyframe only when it connects or its picture changes a lot
    (331 s apart on a user's phone), so when the cache is too long to replay, the viewer is
    shown the current picture re-encoded (_Bridge) until the phone's next keyframe, which then
    continues the phone's own video in the same stream. Before, an "Original" viewer stayed
    black until that keyframe (minutes), or its page gave up.
    """
    kind = 'video'

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.origin = time.monotonic()
        self.last_pts = -1
        self.last_sent_at = None
        self.sent = 0
        self.bridged = 0             # pictures sent re-encoded
        self.bridges = 0             # times the bridge started
        self.started = False
        self.bridge = None
        self.sub = hub.subscribe_video()
        self.sub.resync(force=True)

    def _can_bridge(self):
        """Waiting for a keyframe the phone may not send for minutes (sources that make one on
        request, Android, are asked instead)."""
        return (self.sub.waiting_keyframe and getattr(self.hub, 'keyframe_requester', None) is None
                and getattr(self.hub, 'newest_unit', None) is not None)

    def _repair(self):
        """The browser's picture is damaged or missing: replay the cache when it is short enough
        (rate-limited), else bridge until the phone's next keyframe."""
        if self.bridge is not None:
            self.bridge.force_keyframe = True
            return True
        if self.sub.resync():
            return True
        if not self.sub._replayable() and getattr(self.hub, 'keyframe_requester', None) is None:
            self.sub.wait_for_keyframe()
            return True
        return False

    def request_keyframe(self):
        """Client watchdog (packets but no picture)."""
        return self._repair()

    def stuck(self):
        return self.sub.stuck() and not self._can_bridge()

    def on_pli(self):
        """Browser PLI/FIR: the same, except for the still-screen timer (STILL_PLI_SECONDS)."""
        if (self.bridge is None and self.sub.caught_up() and self.last_sent_at is not None
                and time.monotonic() - self.last_sent_at >= STILL_PLI_SECONDS):
            return False
        return self._repair()

    def _start_bridge(self):
        self.bridge = _Bridge(self.sub.event)
        self.bridges += 1
        self.hub.compat_decoder().attach(self.bridge)

    def _stop_bridge(self):
        self.hub.compat_decoder().detach(self.bridge)
        self.bridge = None

    def _stamp(self, packet, at, replay=False):
        # Cached frames are decoded immediately (1-tick spacing) so the browser converges on the
        # live picture without added latency; others keep their time (paced or arrival).
        pts = self.last_pts + 1 if replay else max(self.last_pts + 1, round((at - self.origin) * VIDEO_CLOCK))
        self.last_sent_at = time.monotonic()
        self.last_pts = pts
        packet.pts = pts
        packet.time_base = VIDEO_TIME_BASE
        self.sent += 1
        return packet

    async def recv(self):
        if self.readyState != 'live':
            raise MediaStreamError
        if not self.started:
            # aiortc only starts pulling once DTLS is up (~0.5-1 s after the
            # offer). Live units queued meanwhile would go out as a burst with
            # old timestamps and leave the browser's jitter buffer holding
            # extra delay; restart from the cache (rendered immediately) instead.
            self.started = True
            self.origin = time.monotonic()
            self.sub.resync(force=True)
        while True:
            if self.sub.items:
                if self.bridge is not None:
                    self._stop_bridge()          # the phone's keyframe: its own video from here
                unit, replay = await self.sub.get()
                if replay:
                    await asyncio.sleep(0)       # the rest of the server runs between cached frames
                at = unit.release_at if unit.release_at is not None else unit.arrival
                return self._stamp(av.Packet(join_annexb(unit.nals)), at, replay)
            if self.bridge is None and self._can_bridge():
                self._start_bridge()
            if self.bridge is not None:
                item = self.bridge.take()
                if item is not None:
                    frame, at = item
                    bridge = self.bridge
                    data = await asyncio.get_running_loop().run_in_executor(None, bridge.encode, frame)
                    if data and bridge is self.bridge:
                        self.bridged += 1
                        self.hub.video_bridged = getattr(self.hub, 'video_bridged', 0) + 1
                        return self._stamp(av.Packet(data), at)
                    continue
            if self.sub.closed or self.readyState != 'live':
                raise MediaStreamError
            self.sub.event.clear()
            if not self.sub.items and (self.bridge is None or self.bridge.latest is None):
                await self.sub.event.wait()

    def stop(self):
        super().stop()
        if self.bridge is not None:
            self._stop_bridge()
        self.hub.unsubscribe_video(self.sub)


def _copy_frame(frame):
    """Independent frame: aiortc's encoder rewrites pts/time_base/pict_type in place."""
    return av.VideoFrame.from_ndarray(frame.to_ndarray(), format='yuv420p')


class CompatDecoder:
    """One shared decoder thread: frames for every re-encoding ("compat") viewer, and
    the way a viewer can start in the middle of a long GOP.

    An iPhone sends a new keyframe only when its picture changes a lot (every 45-70 s
    while a video played, measured on a tester's phone; never on a still screen), and a
    passthrough viewer can only start at a keyframe or from a replay of the cached GOP,
    which is capped (REPLAY_MAX_*). While the GOP is too long to replay, the hub keeps
    this decoder "warm": it follows the stream even with no compat viewer, so a viewer
    joining then gets the current picture at once, re-encoded. Before, a viewer who
    joined a minute into mirroring saw nothing until the phone's next keyframe.

    Units carry the hub's sequence numbers: after a late start or a full queue the
    decoder resumes from the cached GOP where it stands, instead of starting over from
    the keyframe (which on a slow computer with a long GOP could never catch up).
    """

    def __init__(self, hub, max_height):
        self.hub = hub
        self.max_height = max_height
        self.input = queue.Queue(maxsize=COMPAT_QUEUE_UNITS)
        self.subscribers = set()
        self.thread = None
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.reset_needed = threading.Event()
        self.warm = False
        self.last_frame = None      # newest picture, scaled, while viewers watch
        self.latest = None          # newest picture, unscaled, while warm with no viewer
        self.decoded = 0
        self.errors = 0
        self.dropped = 0
        self.resyncs = 0
        self.width = self.height = 0
        # Decoder thread only.
        self.codec = None
        self.gop_ref = None
        self.last_seq = 0

    def attach(self, subscriber):
        with self.lock:
            self.subscribers.add(subscriber)
            self._run_locked()
            last, latest = self.last_frame, self.latest
        frame = last if last is not None else (self._scale(latest) if latest is not None else None)
        if frame is not None:
            # A static phone screen sends nothing new: show the current picture now.
            subscriber.offer(_copy_frame(frame))

    def detach(self, subscriber):
        with self.lock:
            self.subscribers.discard(subscriber)

    def set_warm(self, warm):
        with self.lock:
            self.warm = warm
            if warm:
                self._run_locked()

    def reset(self):
        """Another stream begins (input switch, or the phone stopped): forget the picture."""
        self.last_frame = self.latest = None
        self.reset_needed.set()

    def stop(self):
        self.stop_flag.set()

    def _run_locked(self):
        if self.thread is None:
            self.stop_flag.clear()
            self.thread = threading.Thread(target=self._run, name='mirrordesk-compat-decoder', daemon=True)
            self.thread.start()

    def _drain(self):
        while True:
            try:
                self.input.get_nowait()
            except queue.Empty:
                return

    def push(self, unit):
        if not self.subscribers and not self.warm:
            return
        try:
            self.input.put_nowait(unit)
        except queue.Full:
            # Decoder fell behind: drop the backlog; it resumes from the cached GOP.
            self.dropped += self.input.qsize()
            self._drain()

    def _decode(self, unit):
        try:
            return self.codec.decode(av.Packet(join_annexb(unit.nals)))
        except (av.FFmpegError, ValueError):
            self.errors += 1
            return []

    def _scale(self, frame):
        if frame.height > self.max_height:
            width = max(2, int(frame.width * self.max_height / frame.height) // 2 * 2)
            return frame.reformat(width=width, height=self.max_height, format='yuv420p')
        if frame.format.name != 'yuv420p':
            return frame.reformat(format='yuv420p')
        return frame

    def _emit(self, frame, loop, at=None):
        """`at`: the picture's time (its unit's paced time); None: now."""
        with self.lock:
            watched = bool(self.subscribers)
        if not watched:
            self.latest, self.last_frame = frame, None   # scaled only when a viewer joins
            return
        frame = self._scale(frame)
        self.width, self.height = frame.width, frame.height
        self.latest, self.last_frame = None, frame
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._publish, frame, at)

    def _fresh(self):
        self.codec = av.CodecContext.create('h264', 'r')
        self.codec.thread_type = 'SLICE'
        self.resyncs += 1

    def _catch_up(self, loop):
        """Decode the cached GOP from where this decoder stands (from its keyframe with a
        fresh decoder when needed). True when it got through the cache."""
        hub = self.hub
        gop = hub.gop
        if not hub.gop_valid or not gop:
            return False
        if self.codec is None or self.gop_ref is not gop or self.last_seq < gop[0].seq:
            self._fresh()
            self.gop_ref = gop
            index = 0
        else:
            index = self.last_seq - gop[0].seq + 1
        newest = None
        while index < len(gop):          # the hub appends to the same list meanwhile
            if self.stop_flag.is_set() or self.reset_needed.is_set():
                return False
            unit = gop[index]
            index += 1
            for frame in self._decode(unit):
                self.decoded += 1
                newest = frame
            self.last_seq = unit.seq
            if hub.gop is not gop:
                break                    # a newer keyframe: the live queue takes it from here
        if newest is not None:
            self._emit(newest, loop)     # catch up instantly; only the newest picture matters
        return True

    def _run(self):
        loop = self.hub.loop
        while True:
            with self.lock:
                if self.stop_flag.is_set() or (not self.subscribers and not self.warm):
                    self.thread = None
                    self.codec = self.gop_ref = None
                    return
            if self.reset_needed.is_set():
                self.reset_needed.clear()
                self.codec = self.gop_ref = None
                self._drain()
            if self.codec is None and self._catch_up(loop):
                continue
            try:
                unit = self.input.get(timeout=0.5)
            except queue.Empty:
                continue
            if unit.seq <= self.last_seq:
                continue                                    # decoded already, from the cache
            if self.codec is None or unit.seq != self.last_seq + 1:
                if unit.keyframe:
                    self._fresh()
                    self.gop_ref = self.hub.gop if self.hub.gop and self.hub.gop[0] is unit else None
                elif self._catch_up(loop) and self.last_seq >= unit.seq:
                    continue                                # the cache had it, and everything before it
                elif self.codec is None:
                    continue                                # nothing to start from: wait for a keyframe
                # Otherwise units are missing and no longer cached: decode on. The picture
                # repairs itself at the next keyframe, which beats a frozen one.
            elif unit.keyframe and self.hub.gop and self.hub.gop[0] is unit:
                self.gop_ref = self.hub.gop
            for frame in self._decode(unit):
                self.decoded += 1
                self._emit(frame, loop, unit.release_at)
            self.last_seq = unit.seq

    def _publish(self, frame, at=None):
        subscribers = list(self.subscribers)
        if len(subscribers) == 1:
            subscribers[0].offer(frame, at)
        else:
            for subscriber in subscribers:
                subscriber.offer(_copy_frame(frame), at)


class CompatVideoTrack(MediaStreamTrack):
    """Decoded frames for aiortc's encoders (adaptive bitrate, any browser)."""
    kind = 'video'

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.origin = time.monotonic()
        self.latest = None
        self.event = asyncio.Event()
        self.last_pts = -1
        self.sent = 0
        self.replaced = 0
        hub.compat_decoder().attach(self)

    def offer(self, frame, at=None):
        """`at`: the picture's time (paced AirPlay video); None: now."""
        if self.latest is not None:
            self.replaced += 1  # encoder slower than source: newest frame wins
        self.latest = (frame, time.monotonic() if at is None else at)
        self.event.set()

    def request_keyframe(self):
        return False  # aiortc's encoder honours PLI itself

    def stuck(self):
        return False

    def on_pli(self):
        return False

    async def recv(self):
        while self.latest is None:
            if self.readyState != 'live':
                raise MediaStreamError
            self.event.clear()
            await self.event.wait()
        frame, produced = self.latest
        self.latest = None
        pts = max(self.last_pts + 1, round((produced - self.origin) * VIDEO_CLOCK))
        self.last_pts = pts
        frame.pts = pts
        frame.time_base = VIDEO_TIME_BASE
        self.sent += 1
        return frame

    def stop(self):
        super().stop()
        self.event.set()
        self.hub.compat_decoder().detach(self)


@dataclass
class AudioFrameItem:
    samples: object      # int16 ndarray (1, AUDIO_FRAME_SAMPLES*channels)
    discontinuity: bool
    position: float = 0.0    # seconds into the stream of its first sample


class ArrivalJitter:
    """A source's pace, and how unevenly its sound (or video, VideoPacer) arrives.

    A chunk's offset is its arrival time minus its position in the stream. The pace is the
    median offset of the chunks that came at their own pace (not back to back) in the last
    AUDIO_USUAL_SECONDS. It does not follow the sound a phone sends ahead when it resumes (its
    first ~0.2 s at once, then the rest in real time) nor the burst that catches up after a
    stall, and it follows within a moment when the pace itself moves (the phone paused its
    sound briefly, a packet was lost, its clock drifts).

    The jitter is the worst lateness against the pace over the last AUDIO_JITTER_WINDOW_SECONDS,
    in seconds: after a stall the sound comes late by the stall. A lateness counts at once, and
    is kept if the pace is still where it was AUDIO_CONFIRM_SECONDS later; sound that only came
    late because the pace moved (a brief pause) is forgotten then.
    """

    def __init__(self, rate, usual_seconds=AUDIO_USUAL_SECONDS, paced_gap=AUDIO_PACED_GAP_SECONDS,
                 max_seconds=AUDIO_JITTER_MAX_SECONDS):
        self.rate = rate
        self.usual_seconds = usual_seconds
        self.paced_gap = paced_gap
        self.max_seconds = max_seconds
        self.samples = 0             # since the stream (re)started
        self.last = None             # arrival of the previous chunk
        self.paced = deque()         # (arrival, offset) of chunks that came at their own pace
        self.usual = None
        self.usual_at = -1e9
        self.candidate = None        # (arrival, lateness, pace then): the worst lateness not yet confirmed
        self.peaks = deque()         # confirmed (arrival, lateness), lateness decreasing

    def restart(self):
        """A new stream: its position starts at zero. The network stays the same."""
        self.samples, self.last, self.usual, self.candidate = 0, None, None, None
        self.paced.clear()

    def reset(self):
        """Another input (another network path): forget the stalls seen so far too."""
        self.restart()
        self.peaks.clear()

    def note(self, arrival, count, discontinuity=False):
        """Note a chunk of `count` samples; returns its position in the stream (seconds)."""
        if discontinuity:
            self.restart()
        position = self.samples / self.rate
        self.samples += count
        self.observe(arrival, position)
        return position

    def observe(self, arrival, position):
        """Note what arrived at `arrival` for `position` seconds into the stream."""
        offset = arrival - position
        if self.last is None or arrival - self.last >= self.paced_gap:
            self.paced.append((arrival, offset))
            while arrival - self.paced[0][0] > self.usual_seconds:
                self.paced.popleft()
            if len(self.paced) < 16 or arrival - self.usual_at >= 0.05:
                values = sorted(o for _, o in self.paced)
                self.usual, self.usual_at = values[(len(values) - 1) // 2], arrival
        self.last = arrival
        while self.peaks and arrival - self.peaks[0][0] > AUDIO_JITTER_WINDOW_SECONDS:
            self.peaks.popleft()
        lateness = offset - self.usual
        if lateness > 0 and (self.candidate is None or lateness > self.candidate[1]):
            self.candidate = (arrival, lateness, self.usual)
        if self.candidate is not None and arrival - self.candidate[0] >= AUDIO_CONFIRM_SECONDS:
            at, late, pace = self.candidate
            self.candidate = None
            if abs(self.usual - pace) < late / 2:      # the pace stayed: it was a stall
                while self.peaks and self.peaks[-1][1] <= late:
                    self.peaks.pop()
                self.peaks.append((at, late))

    def pace(self):
        """The stream's usual offset (arrival time minus position); None before its first chunk."""
        return self.usual

    def seconds(self):
        worst = max(self.peaks[0][1] if self.peaks else 0.0, self.candidate[1] if self.candidate else 0.0)
        return min(self.max_seconds, worst)


class VideoPacer:
    """When each frame of the phone's video goes to the viewers: on the phone's own schedule.

    MirrorDesk's UxPlay (mirrordesk.3) gives every forwarded frame the phone's frame time as its
    RTP timestamp. A frame is due its stream's usual arrival offset (ArrivalJitter) plus its
    position plus a delay (MediaHub.video_delay): frames Wi-Fi held back and then delivered all
    at once go out as evenly as the phone showed them. A receiver that sends no frame times
    (another UxPlay build: every frame carries the same timestamp) is not paced; its frames go
    out the moment they arrive, as before.
    """

    def __init__(self):
        self.jitter = ArrivalJitter(VIDEO_CLOCK, VIDEO_USUAL_SECONDS, VIDEO_PACED_GAP_SECONDS,
                                    VIDEO_JITTER_MAX_SECONDS)
        self.late_frames = 0         # frames that arrived after their time (a stall longer than the delay)
        self.stalls = 0              # a gap, then frames back to back: Wi-Fi held the video back
        self.timed = None            # None: not known yet; False: the frames carry no times
        self.last_ts = None
        self.last_arrival = None
        self.ticks = 0               # timestamp ticks since the stream's first frame
        self.same = 0                # frames in a row with the previous frame's timestamp
        self.gap = 0.0               # the gap before the latest frame

    def restart(self):
        """A new stream (the phone reconnected, another input): new positions, same network."""
        self.jitter.restart()
        self.timed, self.last_ts, self.last_arrival, self.ticks, self.same = None, None, None, 0, 0

    def reset(self):
        """Another input: also forget the stalls seen so far."""
        self.restart()
        self.jitter.peaks.clear()

    def need(self):
        """The delay that covers the worst stall of the last minute, or 0 without frame times."""
        if not self.timed:
            return 0.0
        return min(VIDEO_JITTER_MAX_SECONDS, self.jitter.seconds()) + VIDEO_JITTER_MARGIN_SECONDS

    def note(self, timestamp, arrival):
        """Note a frame; returns its time offset (the stream's pace plus its position) or None
        while its time is not known. A frame is due that plus the delay."""
        if self.last_arrival is not None:
            gap = arrival - self.last_arrival
            if self.gap >= VIDEO_STALL_GAP_SECONDS and gap < VIDEO_PACED_GAP_SECONDS:
                self.stalls += 1
            self.gap = gap
        self.last_arrival = arrival
        if self.last_ts is None:
            self.last_ts = timestamp
            self.jitter.observe(arrival, 0.0)
            return None
        delta = (timestamp - self.last_ts) & 0xFFFFFFFF
        self.last_ts = timestamp
        if delta == 0:
            self.same += 1
            if self.same >= VIDEO_UNTIMED_FRAMES:
                self.timed = False
            return self.jitter.pace() + self.ticks / VIDEO_CLOCK if self.timed else None
        self.same = 0
        if delta >= 0x80000000:
            self.restart()                       # back in time: the phone started a new stream
            return self.note(timestamp, arrival)
        self.ticks += delta
        position = self.ticks / VIDEO_CLOCK
        pace = self.jitter.pace()
        if pace is not None and abs(arrival - position - pace) > VIDEO_TIMELINE_JUMP_SECONDS:
            self.restart()                       # times that no longer match the arrivals
            return self.note(timestamp, arrival)
        self.timed = True
        self.jitter.observe(arrival, position)
        return self.jitter.pace() + position


class LiveAudioTrack(MediaStreamTrack):
    """Hub audio -> exact 20 ms s16 frames on a steady output clock.

    Frames leave every 20 ms and each one's timestamp is the clock's own time, so
    the browser always sees evenly timed packets and keeps a small buffer, whatever
    the source does. Each frame is due a backlog after its usual arrival time (the
    source's pace for its position, ArrivalJitter):
    * the backlog covers the source's block size (the iPhone's USB audio comes as 85 ms
      blocks) and how unevenly its sound arrives: a Wi-Fi stall drains it, the sound
      arriving late refills it, nothing is lost;
    * sound a phone sends ahead (its first ~0.2 s at once when it resumes after a pause
      or seek) is early, not stale: it plays from its first moment;
    * sound past its time never builds a delay. UxPlay used to hold ~2.8 s of sound
      after a pause or seek and release it at once: more than AUDIO_SKIP_SECONDS late
      is skipped, a little leaves one quiet frame at a time;
    * a new stream (sound back after a gap, another input) starts at once: what is
      left of the old one is dropped;
    * a tick the source is late for carries no packet. The browser conceals that one
      packet, and its delay does not grow, because the timestamps keep real time;
    * silence longer than AUDIO_IDLE_SECONDS stops the clock; it restarts at real
      time when sound returns, as a phone pausing its audio (DTX) would.
    The earlier design kept timestamps contiguous and jumped them forward after
    gaps, many times in a burst; the browser filled every jump with invented audio
    and played everything after it that much later (measured on a tester's iPhone:
    1.5-1.8 s of delay after each pause or seek).
    """
    kind = 'audio'

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.rate = hub.audio_rate
        self.channels = hub.audio_channels
        self.layout = 'stereo' if self.channels == 2 else 'mono'
        self.frame_samples = self.rate // 50
        self.period = self.frame_samples / self.rate
        self.max_frames = max(2, round(AUDIO_MAX_QUEUE_SECONDS / self.period))
        self.origin = time.monotonic()
        self.frames = deque()
        self.pending = None          # (samples (1, k*ch) with k < frame_samples, discontinuity, position)
        self.event = asyncio.Event()
        self.clock = None            # time of tick 0 while sending; None while the source is quiet
        self.preroll_until = None    # while the clock is stopped: longest wait for the first frame's time
        self.tick = 0
        self.base_pts = 0
        self.next_pts = None
        self.last_input = None
        self.last_trim = -1e9
        self.dropped_samples = 0
        self.trimmed_samples = 0
        self.missed_ticks = 0
        self.held_ticks = 0          # ticks left empty on purpose (near silence, or right after a miss)
        self.underrun = False        # the last tick found nothing to send
        self.rebased = 0             # clock restarts after the source was quiet
        self.blocks = deque()        # (arrival, duration) of recent input blocks
        self.block_seconds = 0.0
        # The hub measures its input once for every viewer, so a new one starts with the right
        # backlog; a track fed directly (tests) measures its own.
        self.jitter = getattr(hub, 'audio_jitter', None)
        self.own_jitter = self.jitter is None
        if self.own_jitter:
            self.jitter = ArrivalJitter(self.rate)
        hub.audio_subscribers.add(self)

    @property
    def queued(self):
        """Samples per channel waiting to be sent (whole frames only)."""
        return len(self.frames) * self.frame_samples

    def target_frames(self):
        """The backlog in frames: the source's block size plus its arrival jitter, or at least
        some slack (two for UxPlay's small packets on a calm network, six for the iPhone's USB
        blocks, up to ten for Wi-Fi that stalls), and at least the picture's delay (MediaHub's
        VideoPacer) so the two stay together."""
        spread = max(AUDIO_SLACK_SECONDS, self.jitter.seconds() + AUDIO_JITTER_MARGIN_SECONDS)
        need = min(self.block_seconds, AUDIO_PACE_MAX_SECONDS) + spread
        video = getattr(self.hub, 'video_need', None)
        if video is not None:
            need = max(need, video())
        return max(round(AUDIO_TARGET_MIN_SECONDS / self.period), math.ceil(need / self.period - 1e-9))

    def _due(self, item):
        """When `item` should leave by the source's pace; None while the pace is unknown."""
        pace = self.jitter.pace()
        return None if pace is None else pace + item.position + self.target_frames() * self.period

    def _note_block(self, arrival, duration):
        self.blocks.append((arrival, duration))
        while self.blocks and arrival - self.blocks[0][0] > AUDIO_PACE_WINDOW_SECONDS:
            self.blocks.popleft()
        self.block_seconds = min(AUDIO_PACE_MAX_SECONDS, max(d for _, d in self.blocks))

    def push(self, chunk):
        if chunk.count <= 0:
            return
        # The hub marks a new stream; a track fed directly sees the same long gap itself.
        fresh = chunk.discontinuity or (self.last_input is not None
                                        and chunk.arrival - self.last_input > AUDIO_GAP_SECONDS)
        position = chunk.position
        if self.own_jitter:
            position = self.jitter.note(chunk.arrival, chunk.count, fresh)
        self._note_block(chunk.arrival, chunk.count / self.rate)
        self.last_input = chunk.arrival
        if fresh:
            # A new stream: what is left of the old one is stale. The new sound starts on a
            # frame of its own, and the clock starts again when that frame is due (it would
            # otherwise play the new sound the moment it arrives, with no backlog at all).
            self.pending = None
            self._drop(len(self.frames))
            self.clock = None
        if self.pending is None:
            samples, discontinuity, start = chunk.samples, fresh, position or 0.0
        else:
            samples = np.concatenate((self.pending[0], chunk.samples), axis=1)
            discontinuity, start = self.pending[1], self.pending[2]
            self.pending = None
        width = self.frame_samples * self.channels
        offset = 0
        while samples.shape[1] - offset >= width:
            self.frames.append(AudioFrameItem(samples[:, offset:offset + width], discontinuity,
                                              start + offset / self.channels / self.rate))
            offset += width
            discontinuity = False
        if offset < samples.shape[1]:
            self.pending = (samples[:, offset:], discontinuity, start + offset / self.channels / self.rate)
        self._drop(len(self.frames) - self.max_frames)
        self.event.set()

    def flush(self):
        """The phone paused or skipped ahead: the sound still queued is stale. It goes now, and
        the clock starts again with the next sound, which is a new stream."""
        flushed = len(self.frames) * self.frame_samples
        self.frames.clear()
        self.pending = None
        self.clock = None
        self.hub.audio_flushed_samples = getattr(self.hub, 'audio_flushed_samples', 0) + flushed

    def _blend(self, item, before):
        """Let `item` start from `before`, the sound that was taken out in front of it, over
        2 ms: the joint does not click."""
        n = min(AUDIO_BLEND_SAMPLES, self.frame_samples)
        ramp = np.repeat(np.arange(1, n + 1, dtype=np.float32) / (n + 1), self.channels)
        width = n * self.channels
        head = before[0, :width] * (1 - ramp) + item.samples[0, :width] * ramp
        item.samples = np.concatenate((np.round(head).astype(np.int16).reshape(1, -1), item.samples[:, width:]), axis=1)

    def _drop(self, count):
        """Skip the oldest `count` frames: stale audio, the newest wins."""
        count = min(count, len(self.frames))
        if count <= 0:
            return
        first = self.frames[0].samples
        for _ in range(count):
            self.frames.popleft()
        self.dropped_samples += count * self.frame_samples
        self.hub.audio_dropped_samples = getattr(self.hub, 'audio_dropped_samples', 0) + count * self.frame_samples
        if self.frames:
            self.frames[0].discontinuity = True
            self._blend(self.frames[0], first)

    @staticmethod
    def _loudness(item):
        return float(np.sqrt(np.mean(np.square(item.samples, dtype=np.float32))))

    def _trim(self, now, late):
        """The clock is `late` seconds behind the source's pace. Very late (a burst of stale
        sound), or late at the start of a stream: skip to what is due now. A little (the
        backlog shrinking once Wi-Fi calms down, a phone clock a little fast): take out one
        frame at a time, the quietest of the oldest few, its joint blended."""
        overdue = min(int(late / self.period), len(self.frames) - 1)   # the newest sound always stays
        if overdue * self.period > AUDIO_SKIP_SECONDS or self.frames[0].discontinuity:
            self._drop(overdue)
            return
        if now - self.last_trim < AUDIO_TRIM_INTERVAL_SECONDS or len(self.frames) < 2:
            return
        self.last_trim = now
        count = min(len(self.frames) - 1, AUDIO_TRIM_CANDIDATES)
        loudness = [self._loudness(self.frames[i]) for i in range(count)]
        index = loudness.index(min(loudness))
        removed = self.frames[index].samples
        del self.frames[index]
        self._blend(self.frames[index], removed)
        self.trimmed_samples += self.frame_samples
        self.hub.audio_trimmed_samples = getattr(self.hub, 'audio_trimmed_samples', 0) + self.frame_samples

    async def _wait_audio(self, timeout=None):
        """Wait for more audio; False when the timeout passed first."""
        self.event.clear()
        try:
            if timeout is None:
                await self.event.wait()
            else:
                await asyncio.wait_for(self.event.wait(), timeout)
        except TimeoutError:
            return False
        if self.readyState != 'live':
            raise MediaStreamError
        return True

    def _start_clock(self, now):
        self.preroll_until = None
        self.underrun = False
        base = round((now - self.origin) * self.rate)
        if self.next_pts is not None:
            base = max(base, self.next_pts)
            self.rebased += 1
            self.hub.audio_rebases = getattr(self.hub, 'audio_rebases', 0) + 1
        self.clock, self.tick, self.base_pts = now, 0, base

    def step(self, now):
        """The release clock at `now`: ('frame', frame) to send, ('sleep', seconds) until the
        next tick, or ('wait', seconds or None) for more audio. recv() runs it on the real
        clock; tests replay a recorded arrival pattern through it."""
        while True:
            if self.clock is None:
                if not self.frames:
                    self.preroll_until = None
                    return 'wait', None
                # A joining viewer, or sound coming back: start when the first frame is due.
                if self.preroll_until is None:
                    self.preroll_until = now + AUDIO_PREROLL_MAX_SECONDS
                due = self._due(self.frames[0])
                start = self.preroll_until if due is None else min(due, self.preroll_until)
                if now < start - 0.001:
                    return 'wait', start - now
                self._start_clock(now)
            tick_at = self.clock + self.tick * self.period
            if now < tick_at - 0.001:
                return 'sleep', tick_at - now
            late = int((now - tick_at) / self.period)
            if late > AUDIO_CATCHUP_TICKS:
                # This sender fell far behind (the event loop stalled): skip what those
                # ticks would have carried instead of sending it late, in a burst.
                self._drop(late)
                self.tick += late
                continue
            if self.frames:
                due = self._due(self.frames[0])
                behind = 0.0 if due is None else tick_at - due
                if behind > AUDIO_EXCESS_FRAMES * self.period:
                    self._trim(now, behind)
                elif behind < -self.period and (self.underrun or self._loudness(self.frames[0]) < AUDIO_QUIET_RMS):
                    # This clock runs ahead of the source (sound was lost, or the backlog has
                    # grown): wait out a tick where nobody hears it - in near silence, or right
                    # after the source ran dry, while the browser fills that gap anyway.
                    self.tick += 1
                    self.held_ticks += 1
                    self.hub.audio_held_samples = getattr(self.hub, 'audio_held_samples', 0) + self.frame_samples
                    continue
            if not self.frames:
                if self.last_input is None or now - self.last_input >= AUDIO_IDLE_SECONDS:
                    self.clock = None                       # the source went quiet: wait for it
                    continue
                # Nothing for this tick: no packet (the browser conceals it), real time goes on.
                self.tick += 1
                self.missed_ticks += 1
                self.underrun = True
                self.hub.audio_missed_samples = getattr(self.hub, 'audio_missed_samples', 0) + self.frame_samples
                continue
            self.underrun = False
            item = self.frames.popleft()
            pts = self.base_pts + self.tick * self.frame_samples
            self.tick += 1
            self.next_pts = pts + self.frame_samples
            frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(item.samples), format='s16', layout=self.layout)
            frame.sample_rate = self.rate
            frame.pts = pts
            frame.time_base = Fraction(1, self.rate)
            return 'frame', frame

    async def recv(self):
        while True:
            if self.readyState != 'live':
                raise MediaStreamError
            action, value = self.step(time.monotonic())
            if action == 'frame':
                return value
            if action == 'sleep':
                await asyncio.sleep(value)
            else:
                await self._wait_audio(value)

    def stop(self):
        super().stop()
        self.event.set()
        self.hub.audio_subscribers.discard(self)


AIRPLAY_AUDIO_RATE = 44100    # UxPlay's -artp: L16 44.1 kHz stereo
DIRECT_INPUTS = ('usb', 'android')
SOURCE_KEYFRAME_INTERVAL = 1.5   # seconds between keyframe requests to the source


class MediaHub:
    def __init__(self, video_port=5004, audio_port=5006, host='127.0.0.1',
                 audio_rate=AUDIO_RATE, audio_channels=AUDIO_CHANNELS, compat_max_height=1080):
        self.video_port = video_port
        self.audio_port = audio_port
        self.host = host
        self.audio_rate = audio_rate
        self.audio_channels = audio_channels
        self.airplay_audio = AudioConverter(audio_rate, audio_channels)
        self.audio_jitter = ArrivalJitter(audio_rate)
        self.audio_pending_discontinuity = False
        self.audio_dropped_samples = 0
        self.audio_trimmed_samples = 0
        self.audio_held_samples = 0
        self.audio_flushed_samples = 0
        self.audio_flushes = 0
        self.audio_missed_samples = 0
        self.audio_input_samples = 0     # sound the source delivered: ~real time unless it bursts or drifts
        self.audio_rebases = 0
        self.compat_max_height = compat_max_height
        self.loop = None
        self.transports = []
        self.video_subscribers = set()
        self.audio_subscribers = set()
        self._compat = None
        self.depacketizer = H264Depacketizer()
        self.sps = self.pps = None
        self.sps_info = None
        self.gop = []
        self.gop_bytes = 0
        self.gop_valid = False
        self.gop_overflows = 0
        self.newest_unit = None
        self.unit_seq = 0
        self.video_pacer = VideoPacer()
        self.video_pending = deque()       # (due, unit): AirPlay frames waiting for their time
        self.video_timer = None
        self.video_delay_now = 0.0         # the delay in use: rises at once, falls slowly
        self.video_delay_at = None
        self.video_bridged = 0             # pictures re-encoded for passthrough viewers (PassthroughVideoTrack)
        self.warm = False
        self.sessions_ended = 0
        self.started_at = time.monotonic()
        self.video_packets = self.video_bytes = self.video_units = self.keyframes = 0
        self.invalid_packets = 0
        self.last_video = self.last_keyframe = None
        self.keyframe_intervals = deque(maxlen=16)
        self.video_rate = RateWindow()
        self.video_byte_rate = RateWindow()
        self.audio_packets = 0
        self.last_audio = None
        self.audio_ssrc = None
        self.audio_next_ts = None
        self.audio_discontinuities = 0
        self.bind_error = None
        # 'airplay' (UxPlay's loopback RTP), 'usb' (usbsource) or 'android'
        # (androidsource): the last two push units directly. Only one input
        # feeds viewers; the others are counted and dropped.
        self.input = 'airplay'
        self.ignored_packets = 0
        self.quiet_until = 0.0              # AirPlay packets before this belong to an ended session
        self.keyframe_requester = None       # set by an input that can produce keyframes on demand
        self.last_source_keyframe_request = -1e9

    # -- lifecycle -------------------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        for port, handler in ((self.video_port, self._on_video), (self.audio_port, self._on_audio)):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
            except OSError:
                pass
            try:
                sock.bind((self.host, port))
            except OSError as exc:
                sock.close()
                self.bind_error = f'UDP port {port} is in use'
                await self.stop()
                raise RuntimeError(f'{self.bind_error}; close the other MirrorDesk session.') from exc
            transport, _ = await self.loop.create_datagram_endpoint(lambda h=handler: _Datagram(h), sock=sock)
            self.transports.append(transport)

    async def stop(self):
        self._drop_pending_video()
        for transport in self.transports:
            transport.close()
        self.transports.clear()
        for sub in list(self.video_subscribers):
            sub.close()
        if self._compat is not None:
            self._compat.stop()

    # -- subscriptions ----------------------------------------------------
    def subscribe_video(self):
        sub = VideoSubscription(self)
        self.video_subscribers.add(sub)
        return sub

    def unsubscribe_video(self, sub):
        sub.close()
        self.video_subscribers.discard(sub)

    def compat_decoder(self):
        if self._compat is None:
            self._compat = CompatDecoder(self, self.compat_max_height)
        return self._compat

    # -- what a new viewer can get ------------------------------------------
    def replayable(self):
        """The cached GOP is short enough to replay to a passthrough viewer."""
        return bool(self.gop_valid and self.gop and self.gop_bytes <= REPLAY_MAX_BYTES
                    and len(self.gop) <= REPLAY_MAX_UNITS)

    def can_start_passthrough(self):
        """A new passthrough viewer gets a picture now: from the cache, from a keyframe the
        source makes on demand (Android), or no stream yet (it starts with a keyframe)."""
        return self.newest_unit is None or self.keyframe_requester is not None or self.replayable()

    def _update_warm(self):
        """Keep the shared decoder following a GOP that grew too long to replay (sources
        that cannot make keyframes on demand), until a keyframe starts a short one."""
        if self.keyframe_requester is not None or self.newest_unit is None or self.replayable():
            want = False
        else:
            want = self.warm or self.gop_valid
        self._set_warm(want)

    def _set_warm(self, warm):
        if warm != self.warm:
            self.warm = warm
            if warm or self._compat is not None:
                self.compat_decoder().set_warm(warm)

    # -- input selection and sessions --------------------------------------
    def _reset_stream(self):
        """Forget the current stream's picture and sound state; viewers wait for the next."""
        self._drop_pending_video()
        self.video_pacer.restart()
        self.depacketizer = H264Depacketizer()
        self.sps = self.pps = None
        self.sps_info = None
        self.gop = []
        self.gop_bytes = 0
        self.gop_valid = False
        self.newest_unit = None
        self.last_video = self.last_audio = None
        self.audio_ssrc = self.audio_next_ts = None
        self.airplay_audio.reset()
        self.audio_jitter.restart()
        self.audio_pending_discontinuity = True
        self._set_warm(False)
        for sub in list(self.video_subscribers):
            sub.resync(force=True)
        if self._compat is not None:
            self._compat.reset()

    def set_input(self, name):
        """Switch between AirPlay, USB and Android. The streams have different
        parameter sets, so the cached picture state is dropped and viewers
        wait for the new input's first keyframe."""
        if name == self.input:
            return
        self.input = name
        self._reset_stream()
        self.audio_jitter.reset()
        self.video_pacer.reset()

    def end_session(self, source=None):
        """The phone stopped mirroring or was disconnected: drop its picture and sound at
        once, so pages say nothing is mirroring instead of showing the last frame frozen
        for up to a minute (the 'paused' state of a still screen). True if one ended."""
        if source is not None and source != self.input:
            return False
        if self.last_video is None and self.last_audio is None and self.newest_unit is None:
            return False
        self._reset_stream()
        self.sessions_ended += 1
        self.quiet_until = time.monotonic() + SESSION_END_QUIET_SECONDS
        return True

    def claim(self, name):
        """A direct input (USB iPhone, Android) asks to feed viewers. AirPlay is the
        fallback and always yields; between direct inputs the first one keeps the
        hub until it releases it, so two cables never fight frame by frame."""
        if self.input == name:
            return True
        if self.input not in DIRECT_INPUTS:
            self.set_input(name)
            return True
        return False

    def release(self, name):
        if self.input == name:
            self.keyframe_requester = None
            self.set_input('airplay')

    def request_source_keyframe(self):
        """Ask the active input for a new keyframe (Android can: scrcpy resets its
        encoder; AirPlay/USB cannot). Rate-limited; True if a request was sent."""
        requester = self.keyframe_requester
        now = time.monotonic()
        if requester is None or now - self.last_source_keyframe_request < SOURCE_KEYFRAME_INTERVAL:
            return False
        self.last_source_keyframe_request = now
        try:
            return bool(requester())
        except Exception:  # noqa: BLE001 - a broken source must not break viewers
            log.debug('keyframe request failed', exc_info=True)
            return False

    def push_unit(self, nals, arrival, timestamp=0, source='usb'):
        """Direct ingest of one access unit (USB/Android input; event-loop thread only)."""
        if self.input != source:
            return
        unit = AccessUnit(timestamp, list(nals), arrival)
        size = unit.size
        self.video_packets += 1
        self.video_bytes += size
        self.last_video = arrival
        self.video_byte_rate.add(arrival, size)
        self._on_unit(unit)

    def push_audio(self, samples, arrival, discontinuity=False, source='usb'):
        """Direct ingest of interleaved s16 audio at self.audio_rate, shaped (1, n*channels)."""
        if self.input != source:
            return
        self._deliver_audio(samples, arrival, discontinuity)

    def audio_flush(self):
        """AirPlay FLUSH (UxPlay reports it): the phone paused, seeked or started another video.
        What viewers still have queued of the old sound stops at once, and the next sound starts
        a new stream with a full backlog (it often comes after a short pause, and the phone sends
        its first ~0.2 s at once)."""
        if self.input != 'airplay':
            return
        self.audio_flushes += 1
        self.audio_pending_discontinuity = True
        self.airplay_audio.reset()
        for track in list(self.audio_subscribers):
            track.flush()

    def _deliver_audio(self, samples, arrival, discontinuity):
        count = samples.shape[1] // self.audio_channels
        if count <= 0:
            return
        if self.audio_pending_discontinuity or discontinuity or (
                self.last_audio is not None and arrival - self.last_audio > AUDIO_GAP_SECONDS):
            discontinuity = True
            self.audio_discontinuities += 1
        self.audio_pending_discontinuity = False
        self.audio_packets += 1
        self.audio_input_samples += count
        position = self.audio_jitter.note(arrival, count, discontinuity)
        self.last_audio = arrival
        for track in list(self.audio_subscribers):
            # One chunk object per track: each may flag its own drop discontinuity.
            track.push(AudioChunk(samples, count, arrival, discontinuity, position))

    # -- ingest -------------------------------------------------------------
    def _on_video(self, data, addr):
        now = time.monotonic()
        if self.input != 'airplay' or now < self.quiet_until:
            self.ignored_packets += 1
            return
        packet = parse_rtp(data)
        if packet is None:
            self.invalid_packets += 1
            return
        self.video_packets += 1
        self.video_bytes += len(data)
        self.last_video = now
        self.video_byte_rate.add(now, len(data))
        for unit in self.depacketizer.push(packet, now):
            self._on_unit(unit)

    def _on_unit(self, unit):
        sps, pps = parameter_sets(unit.nals)
        if sps is not None:
            info = parse_sps(sps)
            if info is not None:
                self.sps_info = info
            self.sps = sps
        if pps is not None:
            self.pps = pps
        if not any(nal_type(n) not in (NAL_SPS, NAL_PPS) for n in unit.nals):
            return  # parameter sets only: remembered, merged into the next IDR
        self.unit_seq += 1
        unit.seq = self.unit_seq
        if unit.keyframe:
            prefix = []
            if not unit.has(NAL_SPS) and self.sps is not None:
                prefix.append(self.sps)
            if not unit.has(NAL_PPS) and self.pps is not None:
                prefix.append(self.pps)
            unit.nals[:0] = prefix
            if self.last_keyframe is not None:
                self.keyframe_intervals.append(unit.arrival - self.last_keyframe)
            self.last_keyframe = unit.arrival
            self.keyframes += 1
        self.video_units += 1
        self.video_rate.add(unit.arrival)
        due = self._video_due(unit)
        if due is None and not self.video_pending:
            self._release(unit, unit.arrival)
            return
        if self.video_pending:   # frames keep their order
            due = max(unit.arrival if due is None else due, self.video_pending[-1][0])
        self.video_pending.append((due, unit))
        if self.video_timer is None:
            self._release_due()

    # -- video pacing (VideoPacer) --------------------------------------------
    def video_need(self):
        """How long AirPlay video waits after its usual arrival time to cover the stalls of the
        last minute (0 without the phone's frame times)."""
        return self.video_pacer.need() if self.input == 'airplay' else 0.0

    def _audio_backlog(self):
        return max((t.target_frames() * t.period for t in list(self.audio_subscribers)
                    if isinstance(t, LiveAudioTrack)), default=0.0)

    def video_delay(self, now):
        """The delay frames get: what the video needs and at least the sound's backlog (so picture
        and sound stay together). It rises at once and falls slowly: frames never come in a burst."""
        target = max(self.video_need(), self._audio_backlog())
        if self.video_delay_at is None or target >= self.video_delay_now:
            self.video_delay_now = target
        else:
            fall = VIDEO_DELAY_FALL_PER_SECOND * (now - self.video_delay_at)
            self.video_delay_now = max(target, self.video_delay_now - fall)
        self.video_delay_at = now
        return self.video_delay_now

    def _video_due(self, unit):
        """When this AirPlay frame goes to viewers, or None: at once (other inputs, a receiver
        without frame times, or no event loop to wait on)."""
        if self.input != 'airplay' or self.loop is None:
            return None
        offset = self.video_pacer.note(unit.rtp_timestamp, unit.arrival)
        if offset is None:
            return None
        due = offset + self.video_delay(unit.arrival)
        if due < unit.arrival - 0.002:
            self.video_pacer.late_frames += 1    # held back longer than the delay covers
            return unit.arrival
        return due

    def _release_due(self):
        """Hand every frame whose time has come to the viewers, then wait for the next one."""
        self.video_timer = None
        now = time.monotonic()
        while self.video_pending and self.video_pending[0][0] <= now + 0.001:
            due, unit = self.video_pending.popleft()
            self._release(unit, due)
        if self.video_pending and self.loop is not None and not self.loop.is_closed():
            self.video_timer = self.loop.call_at(self.video_pending[0][0], self._release_due)

    def _drop_pending_video(self):
        if self.video_timer is not None:
            self.video_timer.cancel()
            self.video_timer = None
        self.video_pending.clear()

    def _release(self, unit, at):
        """The frame goes to the viewers now; `at` is its time (paced, or its arrival)."""
        unit.release_at = at
        if unit.keyframe:
            self.gop = [unit]
            self.gop_bytes = unit.size
            self.gop_valid = True
        elif self.gop_valid:
            if len(self.gop) >= GOP_MAX_UNITS or self.gop_bytes + unit.size > GOP_MAX_BYTES:
                self.gop = []
                self.gop_bytes = 0
                self.gop_valid = False
                self.gop_overflows += 1
            else:
                self.gop.append(unit)
                self.gop_bytes += unit.size
        self.newest_unit = unit
        self._update_warm()
        for sub in list(self.video_subscribers):
            sub.push(unit)
        if self._compat is not None:
            self._compat.push(unit)

    def _on_audio(self, data, addr):
        now = time.monotonic()
        if self.input != 'airplay' or now < self.quiet_until:
            self.ignored_packets += 1
            return
        packet = parse_rtp(data)
        if packet is None:
            self.invalid_packets += 1
            return
        samples = l16_to_s16(packet.payload, AUDIO_CHANNELS)
        if samples is None:
            return
        count = samples.shape[1] // AUDIO_CHANNELS
        discontinuity = (packet.ssrc != self.audio_ssrc or self.audio_next_ts is None
                         or ((packet.timestamp - self.audio_next_ts) & 0xFFFFFFFF) != 0
                         or (self.last_audio is not None and now - self.last_audio > AUDIO_GAP_SECONDS))
        self.audio_ssrc = packet.ssrc
        self.audio_next_ts = (packet.timestamp + count) & 0xFFFFFFFF
        if discontinuity:
            self.airplay_audio.reset()   # no filter history across a gap
        converted = self.airplay_audio.convert(samples, AIRPLAY_AUDIO_RATE, AUDIO_CHANNELS)
        if converted is None:
            # The resampler can hold back the first few samples: carry the flag forward.
            self.audio_pending_discontinuity = self.audio_pending_discontinuity or discontinuity
            self.last_audio = now
            return
        self._deliver_audio(converted, now, discontinuity)

    # -- status -------------------------------------------------------------
    def source_state(self, now=None):
        """streaming: video in the last 3 s. paused: video within a minute - the
        phone sends nothing while its screen is unchanged, which must not look
        like a disconnect. audio_only: AirPlay audio without mirroring."""
        now = time.monotonic() if now is None else now
        video_age = None if self.last_video is None else now - self.last_video
        if video_age is not None and video_age < SOURCE_IDLE_SECONDS:
            return 'streaming'
        if video_age is not None and video_age < SOURCE_PAUSED_SECONDS:
            return 'paused'
        if self.last_audio is not None and now - self.last_audio < SOURCE_IDLE_SECONDS:
            return 'audio_only'
        return 'idle'

    def status(self):
        now = time.monotonic()
        info = self.sps_info
        intervals = list(self.keyframe_intervals)
        compat = self._compat
        return {
            'state': self.source_state(now),
            'input': self.input,
            'ignored_packets': self.ignored_packets,
            'width': info.width if info else None,
            'height': info.height if info else None,
            'profile': info.profile_name if info else None,
            'profile_level_id': info.profile_level_id if info else None,
            'fps': round(self.video_rate.rate(now), 1),
            'bitrate_kbps': round(self.video_byte_rate.bytes_rate(now) * 8 / 1000),
            'video_packets': self.video_packets,
            'video_units': self.video_units,
            'keyframes': self.keyframes,
            'keyframe_interval_s': round(sum(intervals) / len(intervals), 2) if intervals else None,
            'last_keyframe_age_s': round(now - self.last_keyframe, 1) if self.last_keyframe else None,
            'last_video_age_s': round(now - self.last_video, 1) if self.last_video else None,
            'last_audio_age_s': round(now - self.last_audio, 1) if self.last_audio else None,
            'gop_cached_units': len(self.gop) if self.gop_valid else 0,
            'gop_overflows': self.gop_overflows,
            'replayable': self.replayable(),
            'warm': self.warm,
            'video_timed': bool(self.video_pacer.timed),
            'video_jitter_ms': round(self.video_pacer.jitter.seconds() * 1000) if self.video_pacer.timed else 0,
            'video_delay_ms': round(self.video_delay_now * 1000) if self.video_pacer.timed else 0,
            'video_late_frames': self.video_pacer.late_frames,
            'video_stalls': self.video_pacer.stalls,
            'video_bridged': self.video_bridged,
            'lost_packets': self.depacketizer.lost_packets,
            'dropped_nals': self.depacketizer.dropped_nals,
            'stream_resets': self.depacketizer.resets,
            'invalid_packets': self.invalid_packets,
            'audio_packets': self.audio_packets,
            'audio_discontinuities': self.audio_discontinuities,
            'audio_rate': self.audio_rate,
            'audio_dropped_ms': round(self.audio_dropped_samples * 1000 / self.audio_rate),
            'audio_trimmed_ms': round(self.audio_trimmed_samples * 1000 / self.audio_rate),
            'audio_held_ms': round(self.audio_held_samples * 1000 / self.audio_rate),
            'audio_flushes': self.audio_flushes,
            'audio_flushed_ms': round(self.audio_flushed_samples * 1000 / self.audio_rate),
            'audio_missed_ms': round(self.audio_missed_samples * 1000 / self.audio_rate),
            'audio_input_ms': round(self.audio_input_samples * 1000 / self.audio_rate),
            'audio_rebases': self.audio_rebases,
            'audio_jitter_ms': round(self.audio_jitter.seconds() * 1000),
            'audio_backlog_ms': max((round(t.target_frames() * t.period * 1000) for t in list(self.audio_subscribers)
                                     if isinstance(t, LiveAudioTrack)), default=0),
            'sessions_ended': self.sessions_ended,
            'compat_decoded': compat.decoded if compat else 0,
            'compat_errors': compat.errors if compat else 0,
            'compat_queue': compat.input.qsize() if compat else 0,
            'compat_dropped': compat.dropped if compat else 0,
        }
