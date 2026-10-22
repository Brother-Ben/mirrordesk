"""Prototype 7 media hub: loopback RTP ingest, GOP cache, subscriber resync, audio timeline."""
import asyncio
import socket
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synthetic_media import RtpWriter, encode_clip, l16_packet, packetize_h264  # noqa: E402

from mirrordesk import media
from mirrordesk.media import AudioChunk, LiveAudioTrack, MediaHub, PassthroughVideoTrack
from mirrordesk.rtp import AccessUnit


def free_udp_ports(count=2):
    socks, ports = [], []
    for _ in range(count):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('127.0.0.1', 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


async def send_clip(port, units, fps=60, start_index=0, writer=None):
    writer = writer or RtpWriter(96)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for i, unit in enumerate(units, start_index):
            for packet in packetize_h264(writer, unit, i * 90000 // fps):
                sock.sendto(packet, ('127.0.0.1', port))
            await asyncio.sleep(1 / fps)
    return writer


async def test_hub_ingests_real_rtp_and_reports_stream_facts():
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port)
    await hub.start()
    try:
        units = encode_clip(498, 1080, frames=40, fps=60, gop=20)
        await send_clip(video_port, units)
        await asyncio.sleep(0.1)
        status = hub.status()
        assert status['state'] == 'streaming'
        assert (status['width'], status['height'], status['profile']) == (498, 1080, 'high')
        assert status['video_units'] == 40 and status['keyframes'] == 2
        assert status['lost_packets'] == 0
        assert status['gop_cached_units'] == 20, 'cache holds exactly the current GOP'
        assert hub.gop[0].keyframe and hub.gop[0].nals[0][0] & 0x1F == 7, 'cached IDR starts with SPS'
    finally:
        await hub.stop()


async def test_second_bind_on_same_ports_fails_clearly():
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port)
    await hub.start()
    try:
        with pytest.raises(RuntimeError, match='in use'):
            await MediaHub(video_port, audio_port).start()
    finally:
        await hub.stop()


def unit(key, size=10, t=0.0):
    nals = [b'\x67sps', b'\x68pps', b'\x65' + b'k' * size] if key else [b'\x41' + b'p' * size]
    return AccessUnit(0, nals, arrival=t)


class FakeHub:
    def __init__(self):
        self.gop, self.gop_bytes, self.gop_valid = [], 0, False
        self.newest_unit = None
        self.subs = set()

    def add(self, u):
        if u.keyframe:
            self.gop, self.gop_bytes, self.gop_valid = [u], u.size, True
        elif self.gop_valid:
            self.gop.append(u)
            self.gop_bytes += u.size
        self.newest_unit = u
        for s in list(self.subs):
            s.push(u)

    def subscribe_video(self):
        s = media.VideoSubscription(self)
        self.subs.add(s)
        return s

    def unsubscribe_video(self, s):
        self.subs.discard(s)


async def test_late_joiner_gets_cached_gop_replayed_then_live():
    hub = FakeHub()
    hub.add(unit(True))
    hub.add(unit(False))
    hub.add(unit(False))
    track = PassthroughVideoTrack(hub)
    hub.add(unit(False, t=time.monotonic()))
    packets = [await track.recv() for _ in range(4)]
    assert bytes(packets[0]).startswith(b'\x00\x00\x00\x01\x67'), 'join starts at SPS/IDR'
    pts = [p.pts for p in packets]
    assert pts == sorted(pts) and len(set(pts)) == 4, 'strictly increasing timestamps'
    assert pts[1] - pts[0] == 1 and pts[2] - pts[1] == 1, 'replayed frames are compressed'
    track.stop()


async def test_joiner_without_cache_waits_for_next_keyframe():
    hub = FakeHub()
    track = PassthroughVideoTrack(hub)
    hub.add(unit(False))
    hub.add(unit(False))
    assert not track.sub.items and track.sub.waiting_keyframe
    hub.add(unit(True))
    packet = await asyncio.wait_for(track.recv(), 1)
    assert bytes(packet).startswith(b'\x00\x00\x00\x01\x67')
    track.stop()


async def test_slow_viewer_overflow_resyncs_from_cache_instead_of_growing():
    hub = FakeHub()
    track = PassthroughVideoTrack(hub)
    hub.add(unit(True))
    for _ in range(media.SUB_MAX_UNITS + 5):
        hub.add(unit(False))
    first, replay = track.sub.items[0]
    assert first.keyframe and replay, 'overflow restarts from the cached keyframe'
    assert track.sub.live <= 5, 'live backlog is bounded; the replay itself is byte-bounded'
    # Regression: a replay longer than the limit must not retrigger on every
    # new frame (which would keep a slow viewer from ever progressing).
    resyncs = track.sub.resyncs
    for _ in range(10):
        hub.add(unit(False))
    assert track.sub.resyncs == resyncs
    for _ in range(len(track.sub.items)):
        await track.recv()
    assert track.sub.live == 0
    track.stop()


async def test_oversized_gop_is_not_replayed_over_the_network(monkeypatch):
    monkeypatch.setattr(media, 'REPLAY_MAX_BYTES', 50)
    hub = FakeHub()
    hub.add(unit(True, size=100))
    track = PassthroughVideoTrack(hub)
    assert track.sub.waiting_keyframe and not track.sub.items
    track.stop()


async def test_keyframe_request_is_rate_limited():
    hub = FakeHub()
    hub.add(unit(True))
    track = PassthroughVideoTrack(hub)
    assert track.request_keyframe() is False, 'join already replayed the cache'
    track.sub.last_resync -= media.RESYNC_MIN_INTERVAL + 0.1
    assert track.request_keyframe() is True
    track.stop()


async def test_still_screen_keyframe_request_does_not_resend_the_picture():
    """Regression (ZimaOS over Tailscale): Chromium asks for a keyframe 3 s after its last
    frame while packets came within 5 s. On a still iPhone screen every such PLI replayed
    the whole GOP, which restarted that timer: 2.4 MB every ~4 s for as long as it stood still."""
    hub = FakeHub()
    hub.add(unit(True))
    hub.add(unit(False))
    track = PassthroughVideoTrack(hub)
    for _ in range(2):
        await track.recv()                     # the viewer has the whole picture now
    track.sub.last_resync -= media.RESYNC_MIN_INTERVAL + 0.1
    assert track.on_pli() is True, 'right after frames went out a PLI means damage: replay'
    for _ in range(2):
        await track.recv()
    track.sub.last_resync -= media.RESYNC_MIN_INTERVAL + 0.1
    track.last_sent_at -= media.STILL_PLI_SECONDS + 0.1
    assert track.on_pli() is False and not track.sub.items, 'still screen: nothing is resent'
    hub.add(unit(False))
    assert track.on_pli() is True, 'a frame the viewer has not had yet: replay as before'
    for _ in range(len(track.sub.items)):
        await track.recv()
    track.sub.last_resync -= media.RESYNC_MIN_INTERVAL + 0.1
    track.last_sent_at -= media.STILL_PLI_SECONDS + 0.1
    assert track.request_keyframe() is True, 'the client watchdog (no picture at all) still gets it'
    track.stop()


class AudioHub:
    audio_rate = media.AUDIO_RATE
    audio_channels = 2

    def __init__(self):
        self.audio_subscribers = set()


def chunk(arrival, count=352, discontinuity=False, value=0):
    return AudioChunk(np.full((1, count * 2), value, dtype=np.int16), count, arrival, discontinuity)


async def produce_in_real_time(track, seconds, block=384, value=lambda index: 0, start=None):
    """Push `block`-sample chunks as a real source would: whatever is due by the wall clock.
    (asyncio sleeps are ~16 ms coarse on Windows, so due chunks are sent together.)"""
    start = time.monotonic() if start is None else start
    sent = index = 0
    while sent < seconds * 48000:
        due = (time.monotonic() - start) * 48000
        while sent + block <= due and sent < seconds * 48000:
            track.push(chunk(time.monotonic(), count=block, value=value(index)))
            sent += block
            index += 1
        await asyncio.sleep(0.004)
    return index


async def drain(track, until, released, timeout=0.6):
    """recv() until `until` is done and the queue is empty: [(release time, frame)]."""
    while not until.done() or track.queued:
        try:
            frame = await asyncio.wait_for(track.recv(), timeout)
        except TimeoutError:
            break
        released.append((time.monotonic(), frame))
    return released


async def test_big_blocks_leave_on_a_steady_20ms_clock():
    """The iPhone's USB audio comes as 85 ms blocks; viewers must get one 20 ms frame every
    20 ms with evenly spaced timestamps, never a burst per block (the browser answered bursts
    with a bigger buffer)."""
    track = LiveAudioTrack(AudioHub())
    # Sound, not silence: in near silence the clock may leave a tick empty to grow its backlog.
    producer = asyncio.create_task(produce_in_real_time(track, 1.5, block=4096, value=lambda i: 1000))
    released = await drain(track, producer, [])
    await producer
    times = [t for t, _ in released]
    steps = {b.pts - a.pts for (_, a), (_, b) in zip(released, released[1:])}
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(released) >= 60
    assert steps == {960}, f'evenly spaced timestamps, no gaps or jumps ({sorted(steps)})'
    burst = max(sum(1 for g in gaps[i:i + 4] if g < 0.004) for i in range(len(gaps) - 4))
    assert burst <= 2, 'frames are never released in bursts'
    assert max(gaps) < 0.06
    # The start keeps the release backlog only (the second block overshoots it by ~2 frames).
    assert track.missed_ticks == 0 and track.dropped_samples <= 2 * 960
    track.stop()


async def test_pacing_survives_the_phone_switching_block_size():
    """Measured on an iPhone 11: with the screen on it sends 1024-sample (21 ms) blocks,
    then switches to 4096 (85 ms). The release clock must not hold already-late audio at
    the switch (browser dropout)."""
    track = LiveAudioTrack(AudioHub())
    released = []

    # (delay before the block, samples), as captured: the first big block follows the
    # last small one almost at once, the next after ~42 ms, then every 85 ms.
    schedule = [(1024 / 48000, 1024)] * 15 + [(0.001, 4096), (0.042, 4096)] + [(4096 / 48000, 4096)] * 3

    async def produce():   # on the wall clock, like the phone (Windows sleeps are ~16 ms coarse)
        start, due = time.monotonic(), 0.0
        for delay, size in schedule:
            due += delay
            while time.monotonic() - start < due:
                await asyncio.sleep(0.003)
            track.push(chunk(time.monotonic(), count=size, value=1000))   # sound, not silence

    producer = asyncio.create_task(produce())
    while not producer.done() or track.queued:
        try:
            await asyncio.wait_for(track.recv(), 0.5)
        except TimeoutError:
            break
        released.append(time.monotonic())
    await producer
    gaps = [b - a for a, b in zip(released, released[1:])]
    assert len(released) >= 30
    assert max(gaps) < 0.06, f'no long hold at the block-size switch (max gap {max(gaps) * 1000:.0f} ms)'
    track.stop()


async def test_audio_frames_are_exactly_20ms_whatever_the_block_size():
    """iPhone USB audio arrives as 4096-sample (85 ms) blocks; viewers must still get 20 ms frames."""
    track = LiveAudioTrack(AudioHub())
    track.push(chunk(time.monotonic(), count=4096, discontinuity=True, value=5000))
    frames = [await track.recv() for _ in range(4)]
    assert [f.samples for f in frames] == [960] * 4
    assert [f.pts - frames[0].pts for f in frames] == [0, 960, 1920, 2880], 'contiguous timestamps'
    assert all(f.sample_rate == 48000 and f.format.name == 's16' and f.layout.name == 'stereo' for f in frames)
    assert track.queued == 0 and track.pending[0].shape[1] == (4096 - 3840) * 2, 'remainder waits for the next block'
    track.push(chunk(time.monotonic(), count=4096, value=6000))
    fifth = (await track.recv()).to_ndarray().reshape(-1)
    assert (fifth[:256 * 2] == 5000).all() and (fifth[256 * 2:] == 6000).all(), \
        'the remainder joins the next block seamlessly'
    track.stop()


async def test_audio_viewer_starts_without_a_backlog():
    """Audio that piled up before the viewer's sender starts pulling is stale: it arrived at its
    own pace long ago. The clock starts with the sound that is due now."""
    track = LiveAudioTrack(AudioHub())
    now = time.monotonic()
    for k in range(38):                       # ~300 ms of UxPlay-sized packets, the last just now
        track.push(chunk(now - 0.3 + k * 0.008, count=384))
    first = await track.recv()
    assert track.target_frames() == 2
    assert first.samples == 960 and track.queued == 960, 'only the newest 40 ms is kept'
    assert track.dropped_samples == 13 * 960
    track.stop()


async def test_burst_of_late_audio_is_skipped_not_queued():
    """Regression (a tester's iPhone, 0.10.0): after a pause or seek UxPlay held the sound back
    ~1.5 s and released it all at once. The old track turned that into dozens of small timestamp
    jumps, the browser filled each with invented sound and ended up 1.5-1.8 s behind. That sound
    comes late for its own pace: it is skipped, timestamps stay evenly spaced and the next sound
    is the newest."""
    track = LiveAudioTrack(AudioHub())

    async def source():
        await produce_in_real_time(track, 0.4, value=lambda i: 1)
        await asyncio.sleep(1.5)                                  # held back...
        for _ in range(int(1.5 * 48000 / 384)):                   # ...then all of it at once
            track.push(chunk(time.monotonic(), count=384, value=2))
        track.push(chunk(time.monotonic(), count=384, value=3))   # the newest sound of the burst
        await produce_in_real_time(track, 0.4, value=lambda i: 4)

    producer = asyncio.create_task(source())
    released = await drain(track, producer, [], timeout=2.5)
    await producer
    values = [int(frame.to_ndarray().reshape(-1)[0]) for _, frame in released]
    assert values.count(4) >= 15, 'the sound after the burst plays'
    assert values.count(2) <= media.AUDIO_EXCESS_FRAMES + track.target_frames(), \
        'at most the release backlog of the stale burst is played'
    assert track.dropped_samples >= 1.2 * 48000, 'the stale second and more was skipped'
    resumed = [(t, f) for (t, f), v in zip(released, values) if v != 1]
    steps = {b.pts - a.pts for (_, a), (_, b) in zip(resumed, resumed[1:])}
    assert max(steps) <= 3 * 960, f'no timestamp jumps beyond an occasional late tick ({sorted(steps)})'
    duration = resumed[-1][0] - resumed[0][0]
    assert len(resumed) <= duration / 0.02 + 3, 'never faster than real time: no burst to the browser'
    track.stop()


async def test_late_source_leaves_a_timestamp_gap_not_a_delay():
    """A tick the source is late for carries no packet; the next one keeps real time, so the
    browser conceals one packet instead of playing everything after it later."""
    track = LiveAudioTrack(AudioHub())
    track.push(chunk(time.monotonic(), count=1920))
    first, second = await track.recv(), await track.recv()
    assert second.pts == first.pts + 960
    waiting = asyncio.create_task(track.recv())   # aiortc's sender always has one waiting
    await asyncio.sleep(0.07)                     # ~3 ticks without audio (still under the idle limit)
    track.push(chunk(time.monotonic(), count=960))
    third = await asyncio.wait_for(waiting, 1)
    gap = third.pts - second.pts
    assert gap >= 3 * 960 and gap % 960 == 0, gap
    assert track.missed_ticks >= 2 and track.rebased == 0
    track.stop()


async def test_audio_resumes_at_real_time_after_silence(monkeypatch):
    """After the source went quiet (a paused video) the clock stops; when sound returns it
    restarts at real time, like a phone that pauses its audio (no delay carried over)."""
    monkeypatch.setattr(media, 'AUDIO_IDLE_SECONDS', 0.1)
    track = LiveAudioTrack(AudioHub())
    track.push(chunk(time.monotonic(), count=1920))
    first, second = await track.recv(), await track.recv()
    waiting = asyncio.create_task(track.recv())
    await asyncio.sleep(0.6)
    assert not waiting.done() and track.clock is None, 'quiet source: nothing is sent'
    track.push(chunk(time.monotonic(), count=1920))
    third = await asyncio.wait_for(waiting, 1)
    assert third.pts - second.pts >= 0.5 * 48000, 'the timestamp jumped by the silence'
    assert track.rebased == 1
    track.stop()


async def test_audio_discontinuity_never_splits_a_frame():
    """A new stream never finishes the old one's frame: what is left of the old sound goes."""
    track = LiveAudioTrack(AudioHub())
    track.push(chunk(time.monotonic(), count=2420, discontinuity=True, value=7))   # 2 frames + 500 samples
    track.push(chunk(time.monotonic(), count=960, discontinuity=True, value=9))
    fresh = await track.recv()
    assert (fresh.to_ndarray() == 9).all() and track.queued == 0 and track.pending is None
    assert track.dropped_samples == 2 * 960
    track.stop()


async def test_audio_queue_is_bounded_newest_wins():
    track = LiveAudioTrack(AudioHub())
    now = time.monotonic()
    for i in range(200):
        track.push(chunk(now + i * 0.008))
    assert track.queued <= 48000 * media.AUDIO_MAX_QUEUE_SECONDS
    assert track.dropped_samples > 0
    assert track.frames[0].discontinuity, 'dropping audio is marked as a discontinuity'
    track.stop()


AAC_ELD_FRAME = 480 / 44100   # one AirPlay audio packet from the phone, as UxPlay forwards it


def airplay_arrivals(start, end, first_value=1000, stalls=()):
    """UxPlay's sound as it reaches MirrorDesk over Wi-Fi: every 10.9 ms one AAC-ELD frame, as two
    rtpL16pay packets (347 + 133 samples at 44.1 kHz) resampled to 48 kHz. During each (time,
    length) stall nothing arrives, then everything due at once. Returns [(arrival, count, value)];
    the value counts up per packet from first_value."""
    out, t, frac, value = [], start, 0.0, first_value
    while t < end:
        arrival = t
        for at, length in stalls:
            if at <= t < at + length:
                arrival = at + length + (t - at) * 0.002   # all of it, back to back
        for size in (347, 133):
            frac += size * 48000 / 44100
            count = int(frac)
            frac -= count
            out.append((arrival, count, value))
            value += 1
            arrival += 0.00001
        t += AAC_ELD_FRAME
    return out


def replay(track, events, until):
    """Run a track's release clock in virtual time. events: sorted [(time, callable)] (pushes,
    flushes). Returns ([(release time, frame)], [times of ticks that carried no packet])."""
    track.origin = 0.0
    now, index, released, missed = 0.0, 0, [], []
    while now < until:
        while index < len(events) and events[index][0] <= now:
            events[index][1]()
            index += 1
        before = track.missed_ticks
        action, value = track.step(now)
        missed += [now] * (track.missed_ticks - before)
        if action == 'frame':
            released.append((now, value))
            continue
        upcoming = events[index][0] if index < len(events) else until
        if action == 'sleep':
            now += value
        else:
            now = upcoming if value is None else min(upcoming, now + value)
    return released, missed


def pushes(track, arrivals, discontinuity_at=None):
    return [(a, lambda a=a, c=c, v=v: track.push(chunk(a, count=c, value=v, discontinuity=a == discontinuity_at)))
            for a, c, v in arrivals]


def first_values(released):
    return [int(frame.to_ndarray().reshape(-1)[0]) for _, frame in released]


def test_wifi_stalls_no_longer_cut_the_sound():
    """Measured on a user's ZimaOS box (0.11.1, iPhone over Wi-Fi): now and then no sound came
    for ~0.1 s, then all of it at once. The fixed 40 ms backlog ran dry (40-60 ms missing), then
    the late sound was skipped (60 ms): a dropout every 5-15 s. Now the backlog grows to the
    worst stall: after the first one nothing is missing or skipped, and the delay stays bounded."""
    track = LiveAudioTrack(AudioHub())
    stalls = [(2.0 + 3.0 * k, (0.07, 0.09, 0.1, 0.08)[k % 4]) for k in range(12)]
    released, missed = replay(track, pushes(track, airplay_arrivals(0.0, 39.0, stalls=stalls)), 39.0)
    after_learning = [t for t in missed if t > stalls[2][0]]
    print(f'missed ticks: {len(missed)} (after the first two stalls: {len(after_learning)}), '
          f'dropped {track.dropped_samples}, trimmed {track.trimmed_samples}, target {track.target_frames()}')
    assert after_learning == [], 'a stall the backlog has seen before leaves no hole'
    assert len(missed) <= 4, 'the first stalls cost a tick or two each, not 100 ms'
    assert track.dropped_samples == 0, 'no sound that came late is skipped'
    values = first_values(released)
    assert values == sorted(values) and len(set(values)) == len(values), 'every frame once, in order'
    assert 4 <= track.target_frames() <= 8, 'the backlog covers a 0.1 s stall, not more'
    track.stop()


def test_sound_the_phone_sends_ahead_plays_from_its_first_moment():
    """When sound resumes after a pause or seek the phone sends its first ~0.2 s at once, then the
    rest in real time. 0.11.1 took that for stale sound: it kept 40 ms of it, then ran dry until
    the phone caught up (a user's diagnostics: 120-300 ms skipped and as much missing after every
    seek). It is the new video's first moment: it plays from its start, with the usual backlog."""
    track = LiveAudioTrack(AudioHub())
    ahead = airplay_arrivals(1.0, 1.17, first_value=1)
    ahead = [(1.0 + k * 0.0002, c, v) for k, (_, c, v) in enumerate(ahead)]       # all at once
    steady = airplay_arrivals(1.17, 4.0, first_value=ahead[-1][2] + 1)
    released, missed = replay(track, pushes(track, ahead + steady, discontinuity_at=1.0), 4.0)
    assert missed == [] and track.dropped_samples == 0 and track.trimmed_samples == 0
    assert first_values(released)[0] == 1, 'the new sound starts with its first moment'
    start = released[0][0]
    assert 1.0 + track.target_frames() * 0.02 - 0.002 <= start <= 1.0 + track.target_frames() * 0.02 + 0.002
    assert track.target_frames() == 2, 'a calm network keeps the delay small'
    track.stop()


def test_flush_stops_the_old_sound_at_once_and_the_new_sound_gets_its_backlog():
    """The phone's FLUSH (pause, seek, next video; UxPlay reports it): the old sound still queued
    stops at once, and the new sound, often only a moment later, starts with a full backlog."""
    hub = MediaHub()
    track = LiveAudioTrack(hub)

    def deliver(a, c, v):
        return a, lambda: hub._deliver_audio(np.full((1, c * 2), v, dtype=np.int16), a, False)
    old = [deliver(a, c, 2) for a, c, _ in airplay_arrivals(0.0, 1.0)]
    new = [deliver(a, c, 9) for a, c, _ in airplay_arrivals(1.08, 3.0)]
    released, missed = replay(track, old + [(1.0, hub.audio_flush)] + new, 3.0)
    after_flush = [first_values([r])[0] for r in released if r[0] >= 1.0]
    assert after_flush and set(after_flush) == {9}, 'nothing of the old sound after the flush'
    first_new = next(t for t, _ in released if t >= 1.0)
    assert first_new >= 1.08 + track.target_frames() * 0.02 - 0.002, 'the new sound starts with its backlog'
    assert [t for t in missed if t > first_new] == [], 'and runs without holes'
    status = hub.status()
    assert status['audio_flushes'] == 1 and status['audio_flushed_ms'] > 0 and status['audio_dropped_ms'] == 0
    track.stop()


def test_a_brief_pause_is_not_mistaken_for_a_stall():
    """Sound that comes later only because the phone paused it briefly moves the pace; it is not
    network jitter and must not keep the delay up for a minute."""
    track = LiveAudioTrack(AudioHub())
    first = airplay_arrivals(0.0, 2.0)
    later = airplay_arrivals(2.15, 4.0, first_value=first[-1][2] + 1)            # 150 ms without sound
    replay(track, pushes(track, first + later), 4.0)
    assert track.jitter.seconds() < 0.02 and track.target_frames() == 2
    track.stop()


def test_surplus_leaves_one_quiet_frame_at_a_time_without_a_click():
    track = LiveAudioTrack(AudioHub())
    for value in (1000, 2000, 3, 4000, 5000):
        track.push(chunk(0.0, count=960, value=value))
    track._trim(1.0, 0.06)
    values = [item.samples[0, -1] for item in track.frames]
    assert values == [1000, 2000, 4000, 5000] and track.trimmed_samples == 960, 'the quietest frame went'
    joint = track.frames[2].samples[0, ::2]
    assert joint[0] < 200 and joint[media.AUDIO_BLEND_SAMPLES] == 4000, 'it fades from the removed sound'
    track._trim(1.2, 0.06)
    assert len(track.frames) == 4, 'one at a time'
    track.stop()


def test_a_viewer_joining_later_starts_with_the_backlog_the_stalls_need():
    hub = MediaHub()
    first = LiveAudioTrack(hub)
    for a, c, v in airplay_arrivals(0.0, 3.0, stalls=[(1.0, 0.1)]):
        hub._deliver_audio(np.full((1, c * 2), v, dtype=np.int16), a, False)
    late = LiveAudioTrack(hub)
    assert first.target_frames() >= 5 and late.target_frames() >= 5, 'the hub measured the stall for both'
    assert hub.status()['audio_jitter_ms'] >= 90
    first.stop()
    late.stop()


async def test_usb_sized_blocks_give_one_opus_packet_per_frame():
    """Regression: aiortc stamps every packet from one encode() call with the same RTP
    timestamp; 85 ms blocks made Chromium discard 3 of 4 packets (76% concealed)."""
    from mirrordesk.webrtc import MusicOpusEncoder
    encoder = MusicOpusEncoder()
    track = LiveAudioTrack(AudioHub())
    timestamps = []
    for block in range(6):
        track.push(chunk(time.monotonic(), count=4096, discontinuity=block == 0, value=1000))
        while track.queued:
            payloads, timestamp = encoder.encode(await track.recv())
            assert len(payloads) <= 1, 'one Opus packet per encode call'
            if payloads:
                timestamps.append(timestamp)
    steps = {b - a for a, b in zip(timestamps, timestamps[1:])}
    assert len(timestamps) >= 20 and steps == {960}, 'every packet has its own, evenly spaced timestamp'
    track.stop()


def test_opus_packet_timestamp_follows_its_own_frame():
    """Through aiortc's resampler and libopus' look-ahead the first packet after a timestamp
    jump kept the old timeline: the browser saw it arrive late by the whole jump."""
    from fractions import Fraction
    import av
    from mirrordesk.webrtc import MusicOpusEncoder
    encoder = MusicOpusEncoder()

    def frame(pts):
        f = av.AudioFrame.from_ndarray(np.zeros((1, 1920), dtype=np.int16), format='s16', layout='stereo')
        f.sample_rate, f.pts, f.time_base = 48000, pts, Fraction(1, 48000)
        return f
    stamps = [encoder.encode(frame(pts))[1] for pts in (100000, 100960, 101920, 106720, 107680)]
    assert stamps == [0, 960, 1920, 6720, 7680]


async def test_airplay_l16_is_resampled_once_to_48k():
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port)
    await hub.start()
    track = LiveAudioTrack(hub)
    try:
        writer = RtpWriter(97)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for i in range(126):   # 1 s of UxPlay-sized 44.1 kHz packets
                sock.sendto(l16_packet(writer, i * 352, phase=i * 352), ('127.0.0.1', audio_port))
                await asyncio.sleep(0.002)
        await asyncio.sleep(0.2)
        assert hub.status()['audio_rate'] == 48000
        assert hub.audio_packets >= 120, 'the resampler may hold back only the very first packet'
        # Faster than real time: the bounded queue kept the newest 0.5 s.
        assert track.queued == 48000 * media.AUDIO_MAX_QUEUE_SECONDS and track.dropped_samples > 0.4 * 48000
        frame = await track.recv()
        assert frame.sample_rate == 48000 and frame.samples == 960
    finally:
        track.stop()
        await hub.stop()


async def test_compat_decoder_produces_frames_at_capped_height():
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port, compat_max_height=720)
    await hub.start()
    try:
        track = media.CompatVideoTrack(hub)
        await send_clip(video_port, encode_clip(1920, 888, frames=20, fps=60, gop=20))
        frame = await asyncio.wait_for(track.recv(), 5)
        assert frame.height == 720 and frame.width == 1556
        assert frame.format.name == 'yuv420p'
        track.stop()
        for _ in range(40):
            if hub._compat.thread is None:
                break
            await asyncio.sleep(0.05)
        assert hub._compat.thread is None, 'decoder thread stops with its last viewer'
    finally:
        await hub.stop()


async def test_compat_viewers_get_independent_frames_and_late_joiner_sees_picture():
    """Review findings: one shared av.VideoFrame let encoders overwrite each
    other's pts; a compat viewer joining a static screen saw nothing."""
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port, compat_max_height=720)
    await hub.start()
    try:
        first = media.CompatVideoTrack(hub)
        await send_clip(video_port, encode_clip(498, 1080, frames=10, fps=60, gop=10))
        frame_a = await asyncio.wait_for(first.recv(), 5)
        late = media.CompatVideoTrack(hub)   # source now static: no new packets
        frame_b = await asyncio.wait_for(late.recv(), 2)
        assert frame_b is not frame_a and frame_b.height == 720
        await send_clip(video_port, encode_clip(498, 1080, frames=10, fps=60, gop=10))
        a, b = await asyncio.wait_for(first.recv(), 5), await asyncio.wait_for(late.recv(), 5)
        assert a is not b, 'each viewer encodes its own frame object'
        first.stop()
        late.stop()
    finally:
        await hub.stop()


async def test_compat_long_gop_priming_keeps_the_keyframe(monkeypatch):
    """Review finding: priming a >240-unit GOP through the bounded queue dropped the IDR."""
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port, compat_max_height=360)
    await hub.start()
    try:
        await send_clip(video_port, encode_clip(320, 240, frames=300, fps=300, gop=300), fps=300)
        assert hub.gop_valid and len(hub.gop) == 300
        track = media.CompatVideoTrack(hub)
        frame = await asyncio.wait_for(track.recv(), 10)
        assert frame.width == 320 and hub._compat.errors == 0
        track.stop()
    finally:
        await hub.stop()


def test_pli_with_unreplayable_cache_does_not_freeze_a_healthy_viewer(monkeypatch):
    monkeypatch.setattr(media, 'REPLAY_MAX_BYTES', 50)
    hub = FakeHub()
    hub.add(unit(True, size=100))
    sub = hub.subscribe_video()
    sub.waiting_keyframe = False
    hub.add(unit(False))
    sub.last_resync -= 10
    assert sub.resync() is False
    assert len(sub.items) == 1 and not sub.waiting_keyframe, 'live queue kept, still decoding'


def test_viewer_waiting_for_a_keyframe_nothing_will_send_is_not_stuck(monkeypatch):
    """A viewer waiting for the iPhone's next keyframe is shown the picture re-encoded meanwhile
    (the bridge), so its page has no reason to reconnect; with no stream yet it just waits."""
    monkeypatch.setattr(media, 'REPLAY_MAX_UNITS', 2)
    hub = FakeHub()
    for key in (True, False, False):
        hub.add(unit(key))
    track = PassthroughVideoTrack(hub)
    assert track.sub.waiting_keyframe and track._can_bridge() and not track.stuck()
    hub.keyframe_requester = lambda: True    # Android makes keyframes on demand: no bridge, no wait
    assert not track._can_bridge() and not track.stuck()
    track.stop()


async def test_phone_stopping_ends_the_session_at_once():
    """Stop Mirroring on the iPhone: pages must show that nothing is mirroring now, not the last
    frame frozen for up to a minute (the 'paused' state a still screen is allowed)."""
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port)
    await hub.start()
    try:
        await send_clip(video_port, encode_clip(320, 240, frames=10, fps=60, gop=10))
        await asyncio.sleep(0.05)
        sub = hub.subscribe_video()
        assert hub.source_state() == 'streaming' and hub.gop_valid
        assert hub.end_session('usb') is False, 'not the input that feeds viewers: nothing ends'
        assert hub.end_session('airplay') is True
        assert hub.source_state() == 'idle' and not hub.gop_valid and hub.newest_unit is None
        assert sub.waiting_keyframe, 'viewers wait for the next phone'
        assert hub.end_session('airplay') is False and hub.sessions_ended == 1
    finally:
        await hub.stop()


async def test_packets_still_in_flight_do_not_revive_an_ended_session(monkeypatch):
    """The stop line from UxPlay can be handled before the last packets of the stream it ended:
    they set the hub 'streaming' again, then 'paused', and pages showed the frozen last frame for
    a minute (seen intermittently in Firefox's e2e run). They are ignored for a moment."""
    monkeypatch.setattr(media, 'SESSION_END_QUIET_SECONDS', 0.3)
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port)
    await hub.start()
    try:
        clip = encode_clip(320, 240, frames=10, fps=60, gop=10)
        await send_clip(video_port, clip)
        await asyncio.sleep(0.05)
        assert hub.end_session('airplay') is True
        await send_clip(video_port, clip[-3:])          # late packets of the ended stream
        await asyncio.sleep(0.05)
        assert hub.source_state() == 'idle' and hub.newest_unit is None
        await asyncio.sleep(0.3)
        await send_clip(video_port, clip)                # the next phone, later
        await asyncio.sleep(0.05)
        assert hub.source_state() == 'streaming'
    finally:
        await hub.stop()


def test_switching_input_forgets_the_old_picture():
    """A USB phone unplugged (release) must not leave 'paused' with a frozen frame for a minute."""
    hub = MediaHub(0, 0)
    hub.last_video = hub.last_audio = time.monotonic()
    assert hub.source_state() == 'streaming'
    assert hub.claim('usb') and hub.source_state() == 'idle'
    hub.last_video = time.monotonic()
    hub.release('usb')
    assert hub.input == 'airplay' and hub.source_state() == 'idle'


async def test_long_gop_keeps_the_shared_decoder_warm_so_late_viewers_get_a_picture(monkeypatch):
    """Regression (a tester's iPhone, 0.10.0): the phone sent a keyframe every 45-70 s and the
    picture cache gave up a minute in, so anyone who joined later saw nothing, in either mode,
    until the phone happened to send another keyframe. Once the GOP is too long to replay,
    the shared decoder follows it without viewers and a joining viewer starts re-encoded."""
    monkeypatch.setattr(media, 'REPLAY_MAX_UNITS', 30)
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port, compat_max_height=240)
    await hub.start()
    try:
        units = encode_clip(320, 240, frames=120, fps=120, gop=120)   # one keyframe, then P-frames
        writer = await send_clip(video_port, units[:20], fps=240)
        await asyncio.sleep(0.05)
        assert hub.replayable() and not hub.warm and hub.can_start_passthrough()
        await send_clip(video_port, units[20:], fps=240, start_index=20, writer=writer)
        await asyncio.sleep(0.05)
        assert not hub.replayable() and hub.warm and not hub.can_start_passthrough()
        for _ in range(100):
            if hub._compat.decoded >= 115:
                break
            await asyncio.sleep(0.05)
        assert hub._compat.thread is not None and hub._compat.decoded >= 115, 'decoding with nobody watching'
        assert hub._compat.errors == 0
        track = media.CompatVideoTrack(hub)       # someone joins long after the keyframe
        frame = await asyncio.wait_for(track.recv(), 2)
        assert frame.height == 240 and frame.width == 320
        track.stop()
        await send_clip(video_port, units[:1], fps=240, start_index=120, writer=writer)   # the next keyframe
        await asyncio.sleep(0.05)
        assert hub.replayable() and not hub.warm
        for _ in range(40):
            if hub._compat.thread is None:
                break
            await asyncio.sleep(0.05)
        assert hub._compat.thread is None, 'the decoder stops when nothing needs it'
    finally:
        await hub.stop()


async def test_warm_decoder_resumes_from_the_cache_after_falling_behind(monkeypatch):
    """A full queue (slow computer) costs no restart from the keyframe: the decoder continues
    from the cached GOP where it stands."""
    monkeypatch.setattr(media, 'REPLAY_MAX_UNITS', 5)
    monkeypatch.setattr(media, 'COMPAT_QUEUE_UNITS', 4)
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port, compat_max_height=240)
    await hub.start()
    try:
        units = encode_clip(320, 240, frames=90, fps=90, gop=90)
        await send_clip(video_port, units, fps=900)      # ten times real time: the queue overflows
        for _ in range(100):
            if hub._compat.last_seq == hub.unit_seq:
                break
            await asyncio.sleep(0.05)
        compat = hub._compat
        assert compat.last_seq == hub.unit_seq and compat.errors == 0
        assert compat.resyncs == 1, 'started from the keyframe once, then resumed from the cache'
    finally:
        await hub.stop()


# -- video pacing (VideoPacer, MirrorDesk's UxPlay change mirrordesk.3) ---------------------------
async def send_with_stalls(port, units, fps, every, stall, timestamps=True):
    """Frames at `fps`, except that Wi-Fi holds back the last `stall` seconds of every `every`
    and then delivers them all at once (measured on a user's iPhone: every viewer froze every
    15-20 s). RTP timestamps are the phone's frame times, as MirrorDesk's UxPlay sends them, or
    all the same (`timestamps=False`: another UxPlay build)."""
    writer = RtpWriter(96)
    start = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for index, data in enumerate(units):
            shown = index / fps
            held = shown % every >= every - stall
            send_at = start + ((shown // every) + 1) * every if held else start + shown
            delay = send_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            for packet in packetize_h264(writer, data, index * 90000 // fps if timestamps else 0):
                sock.sendto(packet, ('127.0.0.1', port))


def test_pacer_evens_out_a_stall_once_it_has_seen_one():
    pacer = media.VideoPacer()
    fps, released, arrivals = 60, [], []
    for index in range(4 * fps):
        shown = index / fps
        arrival = 100.0 + (int(shown) + 1 if shown % 1.0 >= 0.85 else shown)   # 150 ms held every second
        offset = pacer.note(index * 1500, arrival)
        arrivals.append(arrival)
        released.append(None if offset is None else offset + pacer.need())
    assert pacer.timed and pacer.stalls == 4
    assert 0.15 <= pacer.need() <= 0.15 + media.VIDEO_JITTER_MARGIN_SECONDS + 0.001
    arrived_gaps = [b - a for a, b in zip(arrivals[fps:], arrivals[fps + 1:])]
    assert max(arrived_gaps) > 0.14, 'the input stalls'
    later = released[2 * fps:]
    gaps = [b - a for a, b in zip(later, later[1:])]
    assert max(gaps) < 1.5 / fps and min(gaps) > 0.5 / fps, 'out evenly once the stall is known'
    assert all(r >= a for r, a in zip(later, arrivals[2 * fps:])), 'no frame is due before it arrived'


def test_pacer_leaves_a_receiver_without_frame_times_alone():
    """Another UxPlay build sends every frame with the same timestamp: frames go out on arrival."""
    pacer = media.VideoPacer()
    offsets = [pacer.note(12345, 10.0 + index / 60) for index in range(30)]
    assert offsets == [None] * 30 and pacer.timed is False and pacer.need() == 0.0


def test_pacer_starts_a_new_timeline_when_the_phone_does():
    pacer = media.VideoPacer()
    for index in range(30):
        pacer.note(900000 + index * 1500, 10.0 + index / 60)
    assert pacer.timed and pacer.ticks == 29 * 1500
    assert pacer.note(0, 10.6) is None, 'back in time: the phone started a new stream'
    assert pacer.ticks == 0 and pacer.timed is None
    offset = pacer.note(1500, 10.6 + 1 / 60)
    assert offset is not None and abs(offset - (10.6 + 1 / 60)) < 0.005, 'due on arrival plus the delay'


async def test_hub_sends_stalled_video_on_the_phones_own_schedule():
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port)
    await hub.start()
    try:
        fps = 60
        units = encode_clip(160, 120, frames=4 * fps, fps=fps, gop=4 * fps)
        await send_with_stalls(video_port, units, fps, every=1.0, stall=0.15)
        await asyncio.sleep(0.4)
        status = hub.status()
        assert status['video_timed'] and status['video_stalls'] >= 3
        assert status['video_delay_ms'] >= 150 and status['video_late_frames'] <= fps // 4
        times = [u.release_at for u in hub.gop]
        arrivals = [u.arrival for u in hub.gop]
        assert len(times) == 4 * fps
        arrived = [b - a for a, b in zip(arrivals[fps:], arrivals[fps + 1:])]
        assert max(arrived) > 0.12, 'the frames came in bursts'
        later = times[2 * fps:]
        gaps = [b - a for a, b in zip(later, later[1:])]
        assert max(gaps) < 0.03, f'evenly paced once the stall is known: {max(gaps) * 1000:.0f} ms'
        assert all(t >= a - 0.002 for t, a in zip(times, arrivals))
    finally:
        await hub.stop()


async def test_hub_sends_frames_without_frame_times_as_they_arrive():
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port)
    await hub.start()
    try:
        units = encode_clip(160, 120, frames=30, fps=60, gop=30)
        await send_with_stalls(video_port, units, 60, every=1.0, stall=0.15, timestamps=False)
        await asyncio.sleep(0.05)
        assert not hub.status()['video_timed'] and not hub.video_pending
        assert len(hub.gop) == 30 and all(u.release_at == u.arrival for u in hub.gop)
    finally:
        await hub.stop()


def test_sound_waits_as_long_as_the_picture():
    """Picture and sound stay together: the sound's backlog is at least the video's delay."""
    hub = MediaHub(0, 0)
    for index in range(4 * 60):
        shown = index / 60
        hub.video_pacer.note(index * 1500, 100.0 + (int(shown) + 1 if shown % 1.0 >= 0.8 else shown))
    assert hub.video_need() > 0.2
    track = LiveAudioTrack(hub)
    assert track.target_frames() * track.period >= hub.video_need() - 1e-9
    assert hub.video_delay(200.0) == pytest.approx(track.target_frames() * track.period)
    track.stop()
    hub.input = 'usb'
    assert hub.video_need() == 0.0, 'cable inputs are not paced'


async def test_original_viewer_sees_the_picture_re_encoded_until_the_phones_next_keyframe(monkeypatch):
    """Regression (a user's ZimaOS box, 0.12.0): switching to Original mid-stream showed nothing
    until the iPhone's next keyframe, 331 s apart on that phone. The viewer now gets the current
    picture re-encoded at once, and the phone's own video from its next keyframe on."""
    from mirrordesk.h264 import parse_sps, split_annexb
    monkeypatch.setattr(media, 'REPLAY_MAX_UNITS', 10)
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port, compat_max_height=240)
    await hub.start()
    track = None
    try:
        units = encode_clip(320, 240, frames=60, fps=60, gop=60)    # one keyframe, then P-frames
        writer = await send_clip(video_port, units[:40], fps=120)
        for _ in range(100):
            if hub.warm and not hub.replayable() and hub._compat.decoded >= 35:
                break
            await asyncio.sleep(0.05)
        assert hub.warm and not hub.replayable()

        def profile(packet):
            sps = next((n for n in split_annexb(bytes(packet)) if n[0] & 0x1F == 7), None)
            return parse_sps(sps).profile_idc if sps else None

        track = PassthroughVideoTrack(hub)
        assert not track.stuck()
        first = await asyncio.wait_for(track.recv(), 5)
        assert profile(first) == 66 and track.bridge is not None, 'the picture at once, re-encoded'
        await send_clip(video_port, units[40:50], fps=120, start_index=40, writer=writer)
        assert profile(await asyncio.wait_for(track.recv(), 5)) in (None, 66)
        await send_clip(video_port, units[:1], fps=120, start_index=60, writer=writer)   # the phone's next keyframe
        for _ in range(30):
            packet = await asyncio.wait_for(track.recv(), 5)
            if profile(packet) == 100:
                break
        assert profile(packet) == 100 and track.bridge is None, "the phone's own video from its keyframe on"
        assert track.bridges == 1 and track.bridged >= 2
    finally:
        if track is not None:
            track.stop()
        await hub.stop()


async def test_damaged_original_viewer_is_bridged_when_the_cache_is_too_long(monkeypatch):
    """A PLI (lost packets) once the GOP is too long to replay: re-encoded until the next keyframe,
    instead of a picture that stays broken."""
    monkeypatch.setattr(media, 'REPLAY_MAX_UNITS', 3)
    hub = FakeHub()
    hub.add(unit(True))
    track = PassthroughVideoTrack(hub)    # joins: the cache (one keyframe) is replayed
    track.started = True                  # as if aiortc were pulling already
    for _ in range(5):
        hub.add(unit(False))
    for _ in range(len(track.sub.items)):
        await track.recv()
    track.sub.last_resync -= media.RESYNC_MIN_INTERVAL + 0.1
    assert track.on_pli() is True and track.sub.waiting_keyframe and track._can_bridge()
    track.stop()
