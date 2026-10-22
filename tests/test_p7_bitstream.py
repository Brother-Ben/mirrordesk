"""Prototype 7: H.264 parsing and RTP depacketization (port-free)."""
import struct
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synthetic_media import RtpWriter, encode_clip, l16_packet, nal_units, packetize_h264  # noqa: E402

from mirrordesk.h264 import NAL_IDR, NAL_SPS, join_annexb, nal_type, parse_sps, split_annexb
from mirrordesk.rtp import H264Depacketizer, l16_to_s16, parse_rtp


def depacketize(packets):
    d = H264Depacketizer()
    out = []
    for p in packets:
        out += d.push(parse_rtp(p), time.monotonic())
    return d, out


@pytest.mark.parametrize('size', [(498, 1080), (1920, 888), (1280, 592), (334, 720)])
def test_sps_reports_real_stream_size_and_profile(size):
    units = encode_clip(*size, frames=2, fps=30)
    sps = next(n for n in split_annexb(units[0]) if nal_type(n) == NAL_SPS)
    info = parse_sps(sps)
    assert (info.width, info.height) == size, 'cropping must be applied (1080 is not a multiple of 16)'
    assert info.profile_idc == 100 and info.profile_name == 'high'
    assert info.profile_level_id.startswith('64')


def test_sps_baseline_profile_detected():
    units = encode_clip(640, 360, frames=2, preset='ultrafast', profile='baseline')
    info = parse_sps(next(n for n in split_annexb(units[0]) if nal_type(n) == NAL_SPS))
    assert info.profile_idc == 66


@pytest.mark.parametrize('garbage', [b'', b'\x67', b'\x67\x64\x00', b'\x67' + b'\xff' * 40, b'\x68\x00\x00'])
def test_malformed_sps_never_raises(garbage):
    assert parse_sps(garbage) is None


def test_annexb_split_join_roundtrip_with_3_and_4_byte_start_codes():
    nals = [b'\x67abc', b'\x68de', b'\x65' + bytes(range(1, 200))]
    data = b'\x00\x00\x00\x01' + nals[0] + b'\x00\x00\x01' + nals[1] + b'\x00\x00\x00\x01' + nals[2]
    assert split_annexb(data) == nals
    assert split_annexb(join_annexb(nals)) == nals


def test_depacketizer_reassembles_fu_a_and_stap_a_exactly():
    units = encode_clip(1920, 888, frames=12, fps=30)
    writer = RtpWriter(96)
    packets = []
    for i, unit in enumerate(units):
        packets += packetize_h264(writer, unit, i * 3000, stap_parameter_sets=(i == 0))
    assert any((p[12] & 0x1F) == 28 for p in packets), 'fixture must exercise FU-A'
    assert any((p[12] & 0x1F) == 24 for p in packets), 'fixture must exercise STAP-A'
    d, out = depacketize(packets)
    assert len(out) == len(units)
    for au, unit in zip(out, units):
        assert au.nals == nal_units(unit)
        assert not au.damaged
    assert out[0].keyframe and not out[1].keyframe
    assert d.lost_packets == d.dropped_nals == 0


def test_lost_fragment_drops_only_that_nal_and_is_counted():
    units = encode_clip(1920, 888, frames=3, fps=30)
    writer = RtpWriter(96)
    packets = packetize_h264(writer, units[0], 0) + packetize_h264(writer, units[1], 3000)
    fu = [i for i, p in enumerate(packets) if (p[12] & 0x1F) == 28 and not (p[13] & 0x80)]
    del packets[fu[0]]  # lose a middle/end fragment of the keyframe
    d, out = depacketize(packets)
    assert d.lost_packets == 1 and d.dropped_nals >= 1
    assert out[0].damaged
    assert len(out) == 2


def test_duplicates_ignored_and_ssrc_change_resets():
    units = encode_clip(640, 360, frames=4, fps=30)
    first = RtpWriter(96, ssrc=1)
    second = RtpWriter(96, ssrc=2, seq=50000)
    packets = packetize_h264(first, units[0], 0)
    packets = packets + packets[:2] + packetize_h264(second, units[1], 9000)
    d, out = depacketize(packets)
    assert d.resets == 1
    assert len(out) == 2


def test_sequence_restart_under_same_ssrc_resynchronises():
    units = encode_clip(640, 360, frames=4, fps=30)
    a = RtpWriter(96, ssrc=7, seq=40000)
    b = RtpWriter(96, ssrc=7, seq=10)
    d, out = depacketize(packetize_h264(a, units[0], 0) + packetize_h264(b, units[0], 3000))
    assert len(out) == 2 and d.resets == 1 and d.lost_packets == 0


def test_rtp_parser_handles_csrc_extension_padding_and_rejects_garbage():
    payload = b'\x65hello'
    header = struct.pack('!BBHII', 0x80 | 0x20 | 0x10 | 0x01, 96, 1, 2, 3) + b'\x00\x00\x00\x09'
    ext = struct.pack('!HH', 0xBEDE, 1) + b'\x00\x00\x00\x00'
    packet = header + ext + payload + b'\x00\x00\x03'
    parsed = parse_rtp(packet)
    assert parsed.payload == payload and parsed.payload_type == 96
    assert parse_rtp(b'\x00' * 20) is None and parse_rtp(b'\x80') is None


def test_l16_network_order_converted_to_native_interleaved():
    writer = RtpWriter(97)
    packet = parse_rtp(l16_packet(writer, 0, samples=4))
    samples = l16_to_s16(packet.payload, 2)
    assert samples.dtype == np.int16 and samples.shape == (1, 8)
    raw = np.frombuffer(packet.payload, dtype='>i2')
    assert list(samples[0]) == list(raw.astype(np.int16))
    assert l16_to_s16(b'\x01', 2) is None
