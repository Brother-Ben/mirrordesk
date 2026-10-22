"""QuickTime USB capture protocol, checked against packets captured from real iPhones.

The fixtures in tests/fixtures/quicktime/ come from quicktime_video_hack (MIT).
Byte-for-byte equality with its recorded replies is the evidence that this port
answers the device exactly as the proven Go implementation does.
"""
from pathlib import Path
import struct

import pytest

from mirrordesk import quicktime as qt
from mirrordesk.h264 import NAL_IDR, NAL_PPS, NAL_SPS, nal_type

FIXTURES = Path(__file__).parent / 'fixtures' / 'quicktime'


def raw(name):
    return (FIXTURES / name).read_bytes()


def frame(name):
    """Fixture as a frame without its length field (one fixture was saved without it)."""
    data = raw(name)
    if struct.unpack_from('<I', data)[0] in (qt.SYNC, qt.ASYN, qt.PING):
        return data
    assert struct.unpack_from('<I', data)[0] == len(data)
    return data[4:]


def test_fourcc_magic_matches_wire_bytes():
    assert qt.SYNC == 0x73796E63 and struct.pack('<I', qt.SYNC) == b'cnys'
    assert qt.OG == 0x676F2120


@pytest.mark.parametrize('built, fixture', [
    (lambda: qt.need_packet(0x0000000102C16CA0), 'asyn-need'),
    (lambda: qt.hpd1_packet(), 'asyn-hpd1'),
    (lambda: qt.hpa1_packet(0x00000001145392F0), 'asyn-hpa1'),
    (lambda: qt.hpa0_packet(0x0000000102C5FC10), 'asyn-hpa0'),
    (lambda: qt.hpd0_packet(), 'asyn-hpd0'),
])
def test_outgoing_packets_match_captures(built, fixture):
    assert built() == raw(fixture)


def test_cwpa_handshake_reply():
    clock, subtype, correlation, rest = qt.sync_fields(frame('cwpa-request1'))
    assert (clock, subtype, correlation) == (qt.EMPTY_CLOCK, qt.CWPA, 0x113573DE0)
    assert struct.unpack_from('<Q', rest)[0] == 0x1135A74E0
    assert qt.clock_reply(correlation, 0x00007FA66CE20CB0) == raw('cwpa-reply1')


@pytest.mark.parametrize('fixture, subtype, correlation, reply_fixture, make_reply', [
    ('cvrp-request', qt.CVRP, 0x1135659D0, 'cvrp-reply', lambda c: qt.clock_reply(c, 0x00007FA66CD10250)),
    ('clok-request', qt.CLOK, 0x113584970, 'clok-reply', lambda c: qt.clock_reply(c, 0x00007FA67CC17980)),
    ('time-request1', qt.TIME, 0x113223D50, 'time-reply1',
     lambda c: qt.time_reply(c, qt.CMTime(0x0000BA62C442E1E1, 0x3B9ACA00))),
    ('skew-request', qt.SKEW, 0x102FDB960, 'skew-reply', lambda c: qt.skew_reply(c, 48000.0)),
    ('afmt-request', qt.AFMT, None, 'afmt-reply', qt.afmt_reply),
    ('og-request', qt.OG, 0x102D32F30, 'og-reply', qt.zero_reply),
    ('stop-request', qt.STOP, 0x102FD4910, 'stop-reply', qt.zero_reply),
])
def test_sync_replies_match_captures(fixture, subtype, correlation, reply_fixture, make_reply):
    _, kind, found, _ = qt.sync_fields(frame(fixture))
    assert kind == subtype
    if correlation is not None:
        assert found == correlation
    assert make_reply(found) == raw(reply_fixture)


def test_audio_format_from_afmt():
    _, _, _, rest = qt.sync_fields(frame('afmt-request'))
    fmt = qt.AudioFormat.parse(rest)
    assert fmt.sample_rate == 48000 and fmt.channels == 2 and fmt.bits_per_channel == 16
    assert fmt.format_flags == 0x4C and fmt.is_s16


def test_cvrp_carries_the_video_format():
    session = qt.Session(monotonic_ns=lambda: 0)
    out, events = session.feed(frame('cvrp-request'))
    assert out[0] == qt.need_packet(0x113538DA0)  # NEED uses the device's video clock
    fmt = session.video_format
    assert (fmt.width, fmt.height, fmt.codec) == (1126, 2436, qt.CODEC_AVC1)
    assert [nal_type(n) for n in fmt.sps + fmt.pps] == [NAL_SPS, NAL_PPS]
    assert fmt.sps[0].hex() == '27640033ac5680470133e69e6e04040404'
    assert fmt.pps[0].hex() == '28ee3cb0'


def test_feed_with_format_becomes_a_decodable_keyframe():
    session = qt.Session(monotonic_ns=lambda: 0)
    out, events = session.feed(frame('asyn-feed'))
    assert out == []  # no NEED before the CVRP handshake
    [sample] = events
    assert [nal_type(n) for n in sample.nals] == [NAL_SPS, NAL_PPS, 6, NAL_IDR]
    assert len(sample.nals[3]) == 90712
    assert (sample.width, sample.height) == (1126, 2436)
    assert sample.presentation.scale == 1_000_000_000


def test_keyframe_decodes_with_ffmpeg():
    import av
    from mirrordesk.h264 import join_annexb
    session = qt.Session(monotonic_ns=lambda: 0)
    [sample] = session.feed(frame('asyn-feed'))[1]
    codec = av.CodecContext.create('h264', 'r')
    frames = codec.decode(av.Packet(join_annexb(sample.nals))) + codec.decode(None)
    assert frames and (frames[0].width, frames[0].height) == (1126, 2436)


def test_feed_without_format_uses_the_known_parameter_sets():
    session = qt.Session(monotonic_ns=lambda: 0)
    session.feed(frame('cvrp-request'))
    out, events = session.feed(frame('asyn-feed-nofdsc'))
    assert out == [session.need]  # every FEED is answered with NEED
    [sample] = events
    assert NAL_SPS not in [nal_type(n) for n in sample.nals]
    assert sum(len(n) for n in sample.nals) > 50_000


def test_audio_sample():
    session = qt.Session(monotonic_ns=lambda: 0)
    out, events = session.feed(frame('asyn-eat'))
    [sample] = events
    assert sample.format.sample_rate == 48000 and sample.format.channels == 2
    assert len(sample.pcm) % 4 == 0 and len(sample.pcm) > 0
    assert sample.presentation is not None


def test_handshake_sequence():
    clock = [0]
    session = qt.Session(monotonic_ns=lambda: clock[0])
    assert session.feed(qt.ping_packet()[4:])[0] == [qt.ping_packet()]
    out, _ = session.feed(frame('cwpa-request1'))
    assert out[:2] == [qt.hpd1_packet(), qt.hpd1_packet()]
    assert out[2] == qt.clock_reply(0x113573DE0, 0x1135A74E0 + 1000)
    assert out[3] == qt.hpa1_packet(0x1135A74E0)
    session.feed(frame('clok-request'))
    clock[0] = 1_500_000_000
    out, _ = session.feed(frame('time-request1'))
    assert qt.CMTime.parse(out[0], 20) == qt.CMTime(1_500_000_000, 1_000_000_000)
    assert session.stop_packets() == [qt.hpa0_packet(0x1135A74E0), qt.hpd0_packet()]


def test_skew_tracks_the_device_audio_clock():
    session = qt.Session(monotonic_ns=lambda: 0)
    assert session.skew() == 48000.0  # no audio yet: report aligned clocks
    session.audio_first = (0, qt.CMTime(1000, 48000))
    # The device clock advanced one second while ours advanced 1.001 s.
    session.audio_last = (1_001_000_000, qt.CMTime(1000 + 48000, 48000))
    assert session.skew() == pytest.approx(48048.0)


def test_skew_ignores_a_backlog_burst_from_an_earlier_session():
    """A capture left running delivers old audio in one burst; the reference must move
    to the first real-time sample, or the answer is wildly wrong (phone then stutters)."""
    clock = [0]
    session = qt.Session(monotonic_ns=lambda: clock[0])
    block = 4096
    device = 0
    clock[0] = 100 * qt.NANOSECONDS
    for _ in range(1000):                      # ~87 s of buffered audio, all arriving "now"
        session._audio_mark(qt.CMTime(device, 48000))
        device += block
        clock[0] += 50_000                       # 0.05 ms apart
    for _ in range(60):                          # then real time: one block per 85.33 ms
        clock[0] += block * qt.NANOSECONDS // 48000
        session._audio_mark(qt.CMTime(device, 48000))
        device += block
    assert session.skew() == pytest.approx(48000.0, rel=1e-3)
    # Without the re-anchor this would have been ~48000 * 5 s / 92 s.


def _real_time(session, clock, device, seconds, block=4096):
    for _ in range(int(seconds * 48000 / block)):
        clock[0] += block * qt.NANOSECONDS // 48000
        session._audio_mark(qt.CMTime(device, 48000))
        device += block
    return device


def test_skew_starts_over_after_the_phone_pauses():
    """The phone's audio clock stops while a video is paused or seeking. Measured across the pause,
    our clock seemed ~40% faster; the iPhone obeyed and sent its sound 1.2-1.4x faster than real time
    for 15 s (a tester's 0.11.0 diagnostics), which MirrorDesk had to drop."""
    clock = [0]
    session = qt.Session(monotonic_ns=lambda: clock[0])
    device = _real_time(session, clock, 0, 25)
    clock[0] += 10 * qt.NANOSECONDS               # paused for 10 s: no sound, the phone's clock stands still
    _real_time(session, clock, device, 5)
    assert session.skew() == pytest.approx(48000.0, rel=1e-3)


def test_skew_answer_never_leaves_real_clock_drift():
    session = qt.Session(monotonic_ns=lambda: 0)
    session.audio_first = (0, qt.CMTime(0, 48000))
    session.audio_last = (13 * qt.NANOSECONDS, qt.CMTime(10 * 48000, 48000))   # "30% fast"
    assert session.skew() == pytest.approx(48000 * (1 + qt.SKEW_MAX_PPM / 1e6))
    session.audio_last = (7 * qt.NANOSECONDS, qt.CMTime(10 * 48000, 48000))    # "30% slow"
    assert session.skew() == pytest.approx(48000 * (1 - qt.SKEW_MAX_PPM / 1e6))


def test_skew_reports_aligned_clocks_until_there_is_history():
    clock = [0]
    session = qt.Session(monotonic_ns=lambda: clock[0])
    session._audio_mark(qt.CMTime(0, 48000))
    clock[0] = 200_000_000                       # jittery early arrival
    session._audio_mark(qt.CMTime(4096, 48000))
    assert session.skew() == 48000.0


def test_frame_reader_reassembles_split_chunks():
    packets = [qt.ping_packet(), raw('asyn-feed'), qt.need_packet(5)]
    stream = b''.join(packets)
    reader = qt.FrameReader()
    frames = []
    for i in range(0, len(stream), 777):
        frames += reader.feed(stream[i:i + 777])
    assert frames == [p[4:] for p in packets]


def test_frame_reader_rejects_garbage():
    with pytest.raises(qt.ProtocolError):
        qt.FrameReader().feed(b'\x02\x00\x00\x00xxxx')


def test_malformed_packets_never_raise_from_feed():
    session = qt.Session(monotonic_ns=lambda: 0)
    feed = bytearray(frame('asyn-feed'))
    struct.pack_into('<I', feed, 16, 0xFFFFFF)  # corrupt the sample buffer length
    assert session.feed(bytes(feed)) == ([], [])
    assert session.counts['errors'] == 1
    assert session.feed(b'junkjunkjunkjunk')[1] == []
