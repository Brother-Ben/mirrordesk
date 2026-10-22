"""RTP parsing for UxPlay's loopback forwarding (-vrtp H.264, -artp L16).

Replaces the per-viewer PyAV SDP demuxer: packets are parsed directly from a
UDP socket that lives as long as the server, so media no longer depends on a
browser offer arriving first and silent periods never time out a reader.
"""
from dataclasses import dataclass, field
import struct

import numpy as np

from .h264 import NAL_IDR, NAL_PPS, NAL_SPS, nal_type

RTP_HEADER = struct.Struct('!BBHII')
MAX_AU_BYTES = 8 * 1024 * 1024   # A single access unit larger than this is malformed.
MAX_AU_NALS = 512


@dataclass
class RtpPacket:
    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes


def parse_rtp(data):
    """Parse one RTP packet; returns None for anything that is not RTP v2."""
    if len(data) < 12:
        return None
    first, second, sequence, timestamp, ssrc = RTP_HEADER.unpack_from(data)
    if first >> 6 != 2:
        return None
    offset = 12 + 4 * (first & 0x0F)
    if first & 0x10:  # header extension
        if len(data) < offset + 4:
            return None
        length = struct.unpack_from('!H', data, offset + 2)[0]
        offset += 4 + 4 * length
    end = len(data)
    if first & 0x20:  # padding
        if end == 0 or data[-1] > end - offset:
            return None
        end -= data[-1]
    if offset > end:
        return None
    return RtpPacket(bool(second & 0x80), second & 0x7F, sequence, timestamp, ssrc, bytes(data[offset:end]))


def seq_delta(new, old):
    """Signed 16-bit sequence distance."""
    return ((new - old + 0x8000) & 0xFFFF) - 0x8000


@dataclass
class AccessUnit:
    """One coded picture worth of NAL units (no start codes)."""
    rtp_timestamp: int
    nals: list = field(default_factory=list)
    arrival: float = 0.0
    damaged: bool = False
    seq: int = 0          # the hub's running count: tells the shared decoder what it has already decoded
    release_at: float = None   # when the hub handed it to viewers: its arrival, or its paced time (MediaHub)

    @property
    def keyframe(self):
        return any(nal_type(n) == NAL_IDR for n in self.nals)

    @property
    def size(self):
        return sum(len(n) for n in self.nals)

    def has(self, kind):
        return any(nal_type(n) == kind for n in self.nals)


class H264Depacketizer:
    """RFC 6184 packetization-mode 1 (single NAL, STAP-A, FU-A) reassembly.

    Access units are delimited by the RTP marker bit or a timestamp change.
    Lost FU-A fragments drop only the affected NAL and mark the access unit
    damaged; counters make every loss visible instead of silently hidden.
    """

    def __init__(self):
        self.reset()
        self.lost_packets = 0
        self.unsupported = 0
        self.dropped_nals = 0
        self.resets = 0

    def reset(self):
        self.last_seq = None
        self.ssrc = None
        self.current = None
        self.fragment = None

    def push(self, packet, arrival):
        """Feed one RtpPacket; returns a list of completed AccessUnits."""
        done = []
        if self.ssrc is not None and packet.ssrc != self.ssrc:
            # A new sender session (phone reconnect / receiver restart).
            done.extend(self._flush())
            self.reset()
            self.resets += 1
        self.ssrc = packet.ssrc
        if self.last_seq is not None:
            delta = seq_delta(packet.sequence, self.last_seq)
            if delta < -512 or delta > 4096:
                # Sequence restart under the same SSRC (sender relaunch): resync
                # rather than ignoring everything or reporting phantom loss.
                done.extend(self._flush())
                self.reset()
                self.ssrc = packet.ssrc
                self.resets += 1
                delta = 1
            elif delta <= 0:
                return done  # duplicate or late reorder: ignore
            if delta > 1:
                self.lost_packets += delta - 1
                if self.fragment is not None:
                    self.fragment = None
                    self.dropped_nals += 1
                if self.current is not None:
                    self.current.damaged = True
        self.last_seq = packet.sequence
        if self.current is not None and packet.timestamp != self.current.rtp_timestamp:
            done.extend(self._flush())
        if self.current is None:
            self.current = AccessUnit(packet.timestamp, arrival=arrival)
        self._payload(packet.payload)
        if packet.marker:
            done.extend(self._flush())
        return done

    def _add(self, nal):
        if not nal:
            return
        current = self.current
        if len(current.nals) >= MAX_AU_NALS or current.size + len(nal) > MAX_AU_BYTES:
            current.damaged = True
            self.dropped_nals += 1
            return
        current.nals.append(nal)

    def _payload(self, payload):
        if not payload:
            return
        kind = payload[0] & 0x1F
        if 1 <= kind <= 23:
            self._add(payload)
        elif kind == 24:  # STAP-A
            offset = 1
            while offset + 2 <= len(payload):
                size = struct.unpack_from('!H', payload, offset)[0]
                offset += 2
                if size == 0 or offset + size > len(payload):
                    self.current.damaged = True
                    break
                self._add(payload[offset:offset + size])
                offset += size
        elif kind == 28:  # FU-A
            if len(payload) < 2:
                return
            header = payload[1]
            start, end = header & 0x80, header & 0x40
            if start:
                if self.fragment is not None:
                    self.dropped_nals += 1
                self.fragment = bytearray([(payload[0] & 0xE0) | (header & 0x1F)])
                self.fragment += payload[2:]
            elif self.fragment is None:
                self.dropped_nals += 1  # middle/end without a start
                if self.current is not None:
                    self.current.damaged = True
                return
            else:
                self.fragment += payload[2:]
                if len(self.fragment) > MAX_AU_BYTES:
                    self.fragment = None
                    self.dropped_nals += 1
                    self.current.damaged = True
                    return
            if end and self.fragment is not None:
                self._add(bytes(self.fragment))
                self.fragment = None
        else:
            self.unsupported += 1

    def _flush(self):
        current, self.current = self.current, None
        if self.fragment is not None:
            self.fragment = None
            self.dropped_nals += 1
            if current is not None:
                current.damaged = True
        if current is None or not current.nals:
            return []
        return [current]


def l16_to_s16(payload, channels=2):
    """Network-order 16-bit PCM -> native int16 array shaped (1, samples*channels)."""
    usable = len(payload) - (len(payload) % (2 * channels))
    if usable <= 0:
        return None
    samples = np.frombuffer(payload[:usable], dtype='>i2').astype(np.int16)
    return samples.reshape(1, -1)


def parameter_sets(nals):
    """Return (sps, pps) NALs present in a list, either may be None."""
    sps = next((n for n in nals if nal_type(n) == NAL_SPS), None)
    pps = next((n for n in nals if nal_type(n) == NAL_PPS), None)
    return sps, pps
