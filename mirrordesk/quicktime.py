"""iPhone/iPad screen and sound over a USB cable: the QuickTime capture protocol.

The phone exposes a hidden USB configuration (the one macOS QuickTime uses to
record an iPhone screen) that streams the screen as H.264 plus uncompressed
audio. This module is a Python port of the protocol as reverse-engineered by
quicktime_video_hack: Copyright (c) 2019 danielpaulus, MIT License (full text in
licenses/quicktime_video_hack-LICENSE.txt; see THIRD_PARTY_NOTICES.md). It only
parses and builds packets and runs the session handshake; the USB access lives
in usbsource.py, so everything here is tested against captured packets.

Every packet on the wire is: u32 little-endian total length (including the
length itself), u32 type ('ping', 'sync', 'asyn' or 'rply'), then a payload.
Magic values are FourCCs read as little-endian u32, so 'sync' appears on the
wire as the bytes "cnys". Functions below take a frame WITHOUT its length.
"""
from dataclasses import dataclass, field
import logging
import struct
import time

log = logging.getLogger('mirrordesk.quicktime')


def fourcc(text):
    return int.from_bytes(text.encode('ascii'), 'big')


# Packet types and subtypes
PING, SYNC, ASYN, RPLY = (fourcc(x) for x in ('ping', 'sync', 'asyn', 'rply'))
CWPA, AFMT, CVRP, CLOK, TIME, SKEW, OG, STOP = (fourcc(x) for x in (
    'cwpa', 'afmt', 'cvrp', 'clok', 'time', 'skew', 'go! ', 'stop'))
FEED, EAT, SPRP, TJMP, SRAT, TBAS, RELS = (fourcc(x) for x in (
    'feed', 'eat!', 'sprp', 'tjmp', 'srat', 'tbas', 'rels'))
HPD1, HPA1, NEED, HPD0, HPA0 = (fourcc(x) for x in ('hpd1', 'hpa1', 'need', 'hpd0', 'hpa0'))

# Serialised object markers
KEYV, STRK, IDXK = fourcc('keyv'), fourcc('strk'), fourcc('idxk')
BULV, DICT, DATV, STRV, NMBV = (fourcc(x) for x in ('bulv', 'dict', 'datv', 'strv', 'nmbv'))
FDSC, MDIA, VDIM, CODC, EXTN, ASBD = (fourcc(x) for x in ('fdsc', 'mdia', 'vdim', 'codc', 'extn', 'asbd'))
SBUF, OPTS, STIA, SDAT, SATT, SARY, SSIZ, NSMP = (fourcc(x) for x in (
    'sbuf', 'opts', 'stia', 'sdat', 'satt', 'sary', 'ssiz', 'nsmp'))
MEDIA_VIDEO, MEDIA_SOUND = fourcc('vide'), fourcc('soun')
CODEC_AVC1 = fourcc('avc1')
FORMAT_LPCM = fourcc('lpcm')

EMPTY_CLOCK = 1
NANOSECONDS = 1_000_000_000
SKEW_MIN_SECONDS = 1.0          # device audio history needed before estimating the clock ratio
SKEW_REANCHOR_SECONDS = 0.05    # an earlier-arriving sample by this much replaces the reference
# Our clock moved this much further than the phone's audio clock between two samples: a pause,
# seek or new video on the phone (its audio clock does not run on through it), not drift.
SKEW_JUMP_SECONDS = 0.25
# Real clocks differ by well under 100 ppm. A larger answer is a measurement error, and the phone
# obeys it: told after a pause that our clock ran ~40% fast, a tester's iPhone sent its sound
# 1.2-1.4x faster than real time for the next 15 s, and the surplus had to be dropped (choppy sound).
SKEW_MAX_PPM = 1000
CMTIME = struct.Struct('<QIIQ')            # value, timescale, flags, epoch
ASBD_STRUCT = struct.Struct('<dIIIIIIII')  # AudioStreamBasicDescription (40 bytes)
TIME_ROUNDED = 1

# Extension keys inside a video format description (CMFormatDescription).
EXT_SAMPLE_DESCRIPTION_ATOMS = 49
EXT_AVCC = 105


class ProtocolError(ValueError):
    pass


# --------------------------------------------------------------- primitives
@dataclass(frozen=True)
class Number:
    """NSNumber as serialised by the device: type 3 = u32, 4 = u64, 6 = float64."""
    kind: int
    value: object

    def to_bytes(self):
        if self.kind == 3:
            return struct.pack('<BI', 3, self.value)
        if self.kind == 4:
            return struct.pack('<BQ', 4, self.value)
        if self.kind == 6:
            return struct.pack('<Bd', 6, self.value)
        raise ProtocolError(f'cannot serialise number type {self.kind}')

    @classmethod
    def parse(cls, data):
        if not data:
            raise ProtocolError('empty number')
        kind = data[0]
        if kind in (3, 5) and len(data) == 5:
            return cls(kind, struct.unpack_from('<I', data, 1)[0])
        if kind == 4 and len(data) == 9:
            return cls(kind, struct.unpack_from('<Q', data, 1)[0])
        if kind == 6 and len(data) == 9:
            return cls(kind, struct.unpack_from('<d', data, 1)[0])
        raise ProtocolError(f'unknown number type {kind} ({len(data)} bytes)')


@dataclass(frozen=True)
class CMTime:
    value: int
    scale: int
    flags: int = TIME_ROUNDED
    epoch: int = 0

    def to_bytes(self):
        return CMTIME.pack(self.value, self.scale, self.flags, self.epoch)

    @classmethod
    def parse(cls, data, offset=0):
        if len(data) < offset + CMTIME.size:
            raise ProtocolError('truncated CMTime')
        return cls(*CMTIME.unpack_from(data, offset))

    @property
    def seconds(self):
        return self.value / self.scale if self.scale else 0.0


@dataclass(frozen=True)
class AudioFormat:
    """AudioStreamBasicDescription (CoreAudio)."""
    sample_rate: float = 48000.0
    format_id: int = FORMAT_LPCM
    format_flags: int = 12
    bytes_per_packet: int = 4
    frames_per_packet: int = 1
    bytes_per_frame: int = 4
    channels: int = 2
    bits_per_channel: int = 16
    reserved: int = 0

    @classmethod
    def parse(cls, data):
        if len(data) < ASBD_STRUCT.size:
            raise ProtocolError('truncated audio format')
        return cls(*ASBD_STRUCT.unpack_from(data))

    def to_bytes(self):
        return ASBD_STRUCT.pack(self.sample_rate, self.format_id, self.format_flags, self.bytes_per_packet,
                                self.frames_per_packet, self.bytes_per_frame, self.channels,
                                self.bits_per_channel, self.reserved)

    @property
    def is_s16(self):
        # Flag 0x2 = big endian; the phone sends little-endian signed 16-bit PCM.
        return self.format_id == FORMAT_LPCM and self.bits_per_channel == 16 and not self.format_flags & 0x2


def atom(magic, payload=b''):
    return struct.pack('<II', 8 + len(payload), magic) + payload


def read_atom(data, offset=0):
    """Return (magic, payload, next_offset) for the atom at offset."""
    if len(data) < offset + 8:
        raise ProtocolError('truncated atom header')
    length, magic = struct.unpack_from('<II', data, offset)
    if length < 8 or offset + length > len(data):
        raise ProtocolError(f'bad atom length {length} at {offset} (have {len(data) - offset})')
    return magic, data[offset + 8:offset + length], offset + length


def iter_atoms(data):
    offset = 0
    while offset < len(data):
        magic, payload, offset = read_atom(data, offset)
        yield magic, payload


# --------------------------------------------------------------- dictionaries
def serialize_value(value):
    if isinstance(value, bool):
        return atom(BULV, b'\x01' if value else b'\x00')
    if isinstance(value, Number):
        return atom(NMBV, value.to_bytes())
    if isinstance(value, str):
        return atom(STRV, value.encode('utf-8'))
    if isinstance(value, (bytes, bytearray)):
        return atom(DATV, bytes(value))
    if isinstance(value, dict):
        return serialize_dict(value)
    raise ProtocolError(f'cannot serialise {type(value).__name__}')


def serialize_dict(entries):
    """String-key dictionary; `entries` keeps insertion order (the device's order matters for tests only)."""
    body = b''.join(atom(KEYV, atom(STRK, key.encode('utf-8')) + serialize_value(value))
                    for key, value in entries.items())
    return atom(DICT, body)


def parse_value(magic, payload):
    if magic == STRV:
        return payload.decode('utf-8', 'replace')
    if magic == DATV:
        return bytes(payload)
    if magic == BULV:
        return bool(payload[:1] == b'\x01')
    if magic == NMBV:
        return Number.parse(payload)
    if magic == DICT:
        return parse_dict(payload)
    if magic == FDSC:
        return FormatDescription.parse(payload)
    raise ProtocolError(f'unknown value type {magic:#010x}')


def parse_dict(payload):
    """Dictionary body (after its header) -> dict keyed by str or int."""
    result = {}
    for magic, entry in iter_atoms(payload):
        if magic != KEYV:
            raise ProtocolError(f'expected key/value pair, got {magic:#010x}')
        key_magic, key, offset = read_atom(entry)
        if key_magic == STRK:
            key = key.decode('utf-8', 'replace')
        elif key_magic == IDXK:
            if len(key) < 2:
                raise ProtocolError('short index key')
            key = struct.unpack_from('<H', key)[0]
        else:
            raise ProtocolError(f'unknown key type {key_magic:#010x}')
        value_magic, value, _ = read_atom(entry, offset)
        result[key] = parse_value(value_magic, value)
    return result


# --------------------------------------------------------------- media objects
def parse_avcc(data):
    """AVCDecoderConfigurationRecord -> (sps list, pps list)."""
    if len(data) < 7 or data[0] != 1:
        raise ProtocolError('not an avcC record')
    offset = 5
    sps, pps = [], []
    for target, mask in ((sps, 0x1F), (pps, 0xFF)):
        if offset >= len(data):
            raise ProtocolError('truncated avcC record')
        count = data[offset] & mask
        offset += 1
        for _ in range(count):
            if offset + 2 > len(data):
                raise ProtocolError('truncated avcC record')
            size = struct.unpack_from('>H', data, offset)[0]
            offset += 2
            if offset + size > len(data):
                raise ProtocolError('truncated avcC parameter set')
            target.append(bytes(data[offset:offset + size]))
            offset += size
    return sps, pps


def split_avcc_sample(data, length_size=4):
    """Length-prefixed H.264 sample -> list of NAL units (no start codes)."""
    nals = []
    offset = 0
    while offset + length_size <= len(data):
        size = int.from_bytes(data[offset:offset + length_size], 'big')
        offset += length_size
        if size == 0 or offset + size > len(data):
            raise ProtocolError('bad NAL length in sample')
        nals.append(bytes(data[offset:offset + size]))
        offset += size
    if offset != len(data):
        raise ProtocolError('trailing bytes in sample')
    return nals


@dataclass
class FormatDescription:
    media_type: int = 0
    width: int = 0
    height: int = 0
    codec: int = 0
    sps: list = field(default_factory=list)
    pps: list = field(default_factory=list)
    nal_length_size: int = 4
    audio: AudioFormat = None

    @classmethod
    def parse(cls, payload):
        fmt = cls()
        for magic, value in iter_atoms(payload):
            if magic == MDIA and len(value) >= 4:
                fmt.media_type = struct.unpack_from('<I', value)[0]
            elif magic == VDIM and len(value) >= 8:
                fmt.width, fmt.height = struct.unpack_from('<II', value)
            elif magic == CODC and len(value) >= 4:
                fmt.codec = struct.unpack_from('<I', value)[0]
            elif magic == EXTN:
                extensions = parse_dict(value)
                atoms = extensions.get(EXT_SAMPLE_DESCRIPTION_ATOMS)
                avcc = atoms.get(EXT_AVCC) if isinstance(atoms, dict) else None
                if isinstance(avcc, bytes):
                    fmt.sps, fmt.pps = parse_avcc(avcc)
                    fmt.nal_length_size = (avcc[4] & 0x03) + 1
            elif magic == ASBD:
                fmt.audio = AudioFormat.parse(value)
        return fmt


@dataclass
class SampleBuffer:
    """CMSampleBuffer: timestamps, optional format description and the media bytes."""
    media_type: int
    presentation: CMTime = None
    timing: list = field(default_factory=list)   # (duration, pts, dts) CMTime triples
    data: bytes = None
    sample_count: int = 0
    sample_sizes: list = field(default_factory=list)
    format: FormatDescription = None

    @classmethod
    def parse(cls, sbuf_atom, media_type):
        magic, payload, _ = read_atom(sbuf_atom)
        if magic != SBUF:
            raise ProtocolError(f'expected sample buffer, got {magic:#010x}')
        buf = cls(media_type)
        for magic, value in iter_atoms(payload):
            if magic == OPTS:
                buf.presentation = CMTime.parse(value)
            elif magic == STIA:
                step = 3 * CMTIME.size
                buf.timing = [tuple(CMTime.parse(value, i + j * CMTIME.size) for j in range(3))
                              for i in range(0, len(value) - step + 1, step)]
            elif magic == SDAT:
                buf.data = bytes(value)
            elif magic == NSMP and len(value) >= 4:
                buf.sample_count = struct.unpack_from('<I', value)[0]
            elif magic == SSIZ:
                buf.sample_sizes = [struct.unpack_from('<I', value, i)[0] for i in range(0, len(value) - 3, 4)]
            elif magic == FDSC:
                buf.format = FormatDescription.parse(value)
            # satt / sary (sample attachments) carry nothing MirrorDesk needs.
        return buf


# --------------------------------------------------------------- packets
def header(frame):
    """(type, clock_ref, subtype) of a frame without its length field."""
    if len(frame) < 16:
        raise ProtocolError('short packet')
    kind, clock, subtype = struct.unpack_from('<IQI', frame)
    return kind, clock, subtype


def with_length(body):
    return struct.pack('<I', 4 + len(body)) + body


def ping_packet():
    return struct.pack('<IIQ', 16, PING, 0x0000000100000000)


def asyn_packet(subtype, clock=EMPTY_CLOCK, payload=b''):
    return with_length(struct.pack('<IQI', ASYN, clock, subtype) + payload)


def need_packet(clock):
    return asyn_packet(NEED, clock)


def hpd0_packet():
    return asyn_packet(HPD0)


def hpa0_packet(device_audio_clock):
    return asyn_packet(HPA0, device_audio_clock)


def hpd1_packet():
    """Tells the device what this receiver can show (values as sent by macOS QuickTime)."""
    info = {'Valeria': True, 'HEVCDecoderSupports444': True,
            'DisplaySize': {'Width': Number(6, 1920.0), 'Height': Number(6, 1200.0)}}
    return asyn_packet(HPD1, EMPTY_CLOCK, serialize_dict(info))


def hpa1_packet(device_audio_clock):
    info = {'BufferAheadInterval': Number(6, 0.07300000000000001), 'deviceUID': 'Valeria',
            'ScreenLatency': Number(6, 0.04), 'formats': AudioFormat().to_bytes() + struct.pack(
                '<dd', 48000.0, 48000.0), 'EDIDAC3Support': Number(3, 0), 'deviceName': 'Valeria'}
    return asyn_packet(HPA1, device_audio_clock, serialize_dict(info))


def reply(correlation, payload=b''):
    return with_length(struct.pack('<IQ', RPLY, correlation) + payload)


def clock_reply(correlation, clock):
    return reply(correlation, struct.pack('<IQ', 0, clock))


def afmt_reply(correlation):
    return reply(correlation, struct.pack('<I', 0) + serialize_dict({'Error': Number(3, 0)}))


def zero_reply(correlation):
    return reply(correlation, bytes(8))


def time_reply(correlation, cmtime):
    return reply(correlation, struct.pack('<I', 0) + cmtime.to_bytes())


def skew_reply(correlation, skew):
    return reply(correlation, struct.pack('<Id', 0, skew))


def sync_fields(frame):
    """(clock_ref, subtype, correlation, rest) of a SYNC frame."""
    kind, clock, subtype = header(frame)
    if kind != SYNC or len(frame) < 24:
        raise ProtocolError('not a sync packet')
    correlation = struct.unpack_from('<Q', frame, 16)[0]
    return clock, subtype, correlation, frame[24:]


class FrameReader:
    """Reassembles length-prefixed frames from arbitrary USB read chunks."""
    MAX_FRAME = 64 * 1024 * 1024

    def __init__(self):
        self.buffer = bytearray()

    def missing(self):
        """Bytes still needed to complete the frame being received (its length field first)."""
        if len(self.buffer) < 4:
            return 4 - len(self.buffer)
        return max(1, struct.unpack_from('<I', self.buffer)[0] - len(self.buffer))

    def feed(self, chunk):
        self.buffer += chunk
        frames = []
        while len(self.buffer) >= 4:
            length = struct.unpack_from('<I', self.buffer)[0]
            if length < 8 or length > self.MAX_FRAME:
                self.buffer.clear()
                raise ProtocolError(f'bad frame length {length}')
            if len(self.buffer) < length:
                break
            frames.append(bytes(self.buffer[4:length]))
            del self.buffer[:length]
        return frames


# --------------------------------------------------------------- session
@dataclass
class VideoSample:
    nals: list
    presentation: CMTime
    width: int
    height: int


@dataclass
class AudioSample:
    pcm: bytes
    format: AudioFormat
    presentation: CMTime


class Session:
    """One capture session's handshake and stream decoding (no I/O).

    feed(frame) returns (packets_to_send, media_events). The clock ref
    offsets mirror what quicktime_video_hack sends; the device only needs
    them to be distinct and stable.
    """

    def __init__(self, monotonic_ns=time.monotonic_ns, audio=True):
        self.now = monotonic_ns
        self.audio = audio
        self.device_audio_clock = None
        self.local_audio_start = None
        self.need = None
        self.clock_start = None
        self.video_format = None
        self.audio_format = AudioFormat()
        self.audio_first = None       # (local ns, device CMTime) of the first audio sample
        self.audio_last = None
        self.last_skew = None         # the last answer to the phone (diagnostics)
        self.released = 0
        self.handshake_done = False
        self.counts = {'video': 0, 'audio': 0, 'unknown': 0, 'errors': 0}

    # The skew answer keeps the device's audio clock in step with ours.
    def skew(self):
        if not self.audio_first or not self.audio_last:
            return float(self.audio_format.sample_rate)
        local = self.audio_last[0] - self.audio_first[0]
        device = self.audio_last[1].value - self.audio_first[1].value
        scale = self.audio_last[1].scale or 48000
        # Too little history (or none since a re-anchor) is all arrival jitter: report aligned clocks.
        if local <= 0 or device < SKEW_MIN_SECONDS * scale:
            return float(scale)
        ratio = (local * scale / NANOSECONDS) / device
        limit = SKEW_MAX_PPM / 1e6
        return scale * min(max(ratio, 1 - limit), 1 + limit)

    def _audio_mark(self, presentation):
        """Remember (our time, device time) of an audio sample for the skew answer.

        The reference must be a sample that arrived in real time. A capture left
        running by an earlier session first delivers minutes of buffered audio in
        one burst; anchoring there made the answer wildly wrong (measured: the
        phone then sent odd 128 ms blocks followed by 172 ms gaps = dropouts).
        A sample that arrives with less delay than the reference (our clock minus
        the device clock went down) proves the reference was late: re-anchor.
        The phone's audio clock stops while it pauses or seeks: our clock moving far
        ahead of it from one sample to the next starts the measurement over too.
        """
        mark = (self.now(), presentation)
        first, last = self.audio_first, self.audio_last
        if (first is None or first[1].scale != presentation.scale
                or self._offset(mark) < self._offset(first) - SKEW_REANCHOR_SECONDS
                or (last is not None and self._offset(mark) > self._offset(last) + SKEW_JUMP_SECONDS)):
            self.audio_first = mark
        self.audio_last = mark

    @staticmethod
    def _offset(mark):
        return mark[0] / NANOSECONDS - mark[1].seconds

    def feed(self, frame):
        out, events = [], []
        kind = struct.unpack_from('<I', frame)[0] if len(frame) >= 4 else None
        if kind == PING:
            out.append(ping_packet())
        elif kind == SYNC:
            self._sync(frame, out)
        elif kind == ASYN:
            self._asyn(frame, out, events)
        else:
            self.counts['unknown'] += 1
            log.debug('Ignoring unknown packet type %r', frame[:4])
        return out, events

    def _sync(self, frame, out):
        clock, subtype, correlation, rest = sync_fields(frame)
        if subtype == CWPA:
            self.device_audio_clock = struct.unpack_from('<Q', rest)[0]
            self.local_audio_start = self.now()
            out.extend([hpd1_packet(), hpd1_packet()])
            out.append(clock_reply(correlation, self.device_audio_clock + 1000))
            out.append(hpa1_packet(self.device_audio_clock))
        elif subtype == AFMT:
            try:
                self.audio_format = AudioFormat.parse(rest)
            except ProtocolError:
                self.counts['errors'] += 1
            out.append(afmt_reply(correlation))
        elif subtype == CVRP:
            device_video_clock = struct.unpack_from('<Q', rest)[0]
            try:
                magic, payload, _ = read_atom(rest, 8)
                info = parse_dict(payload) if magic == DICT else {}
                fmt = info.get('FormatDescription')
                if isinstance(fmt, FormatDescription):
                    self.video_format = fmt
            except ProtocolError:
                self.counts['errors'] += 1
            self.need = need_packet(device_video_clock)
            out.append(self.need)
            out.append(clock_reply(correlation, device_video_clock + 0x1000AF))
        elif subtype == CLOK:
            self.clock_start = self.now()
            out.append(clock_reply(correlation, clock + 0x10000))
        elif subtype == TIME:
            start = self.clock_start if self.clock_start is not None else self.now()
            out.append(time_reply(correlation, CMTime(self.now() - start, NANOSECONDS)))
            self.handshake_done = True
        elif subtype == SKEW:
            self.last_skew = self.skew()
            out.append(skew_reply(correlation, self.last_skew))
        elif subtype in (OG, STOP):
            out.append(zero_reply(correlation))
        else:
            # Unanswered sync requests stall the device; an empty reply is the
            # most neutral answer to a request this port does not know.
            self.counts['unknown'] += 1
            log.debug('Unknown sync request %#010x', subtype)
            out.append(zero_reply(correlation))

    def _asyn(self, frame, out, events):
        _, clock, subtype = header(frame)
        if subtype == FEED:
            if self.need is not None:
                out.append(self.need)
            try:
                buf = SampleBuffer.parse(frame[16:], MEDIA_VIDEO)
            except ProtocolError:
                self.counts['errors'] += 1
                return
            if buf.format is not None and buf.format.sps:
                self.video_format = buf.format
            if not buf.data:
                return
            fmt = self.video_format
            try:
                nals = split_avcc_sample(buf.data, fmt.nal_length_size if fmt else 4)
            except ProtocolError:
                self.counts['errors'] += 1
                return
            if buf.format is not None and fmt is not None:
                nals = fmt.sps + fmt.pps + nals  # new format: parameter sets travel with the picture
            self.counts['video'] += 1
            events.append(VideoSample(nals, buf.presentation, fmt.width if fmt else 0, fmt.height if fmt else 0))
        elif subtype == EAT:
            try:
                buf = SampleBuffer.parse(frame[16:], MEDIA_SOUND)
            except ProtocolError:
                self.counts['errors'] += 1
                return
            if buf.format is not None and buf.format.audio is not None:
                self.audio_format = buf.format.audio
            if buf.presentation is not None and buf.presentation.scale:
                self._audio_mark(buf.presentation)
            if buf.data and self.audio:
                self.counts['audio'] += 1
                events.append(AudioSample(buf.data, self.audio_format, buf.presentation))
        elif subtype == RELS:
            self.released += 1
        elif subtype in (SPRP, TJMP, SRAT, TBAS):
            pass  # clock/property notifications: nothing to answer
        else:
            self.counts['unknown'] += 1
            log.debug('Unknown asyn packet %#010x', subtype)

    def stop_packets(self):
        """Ask the device to stop audio and video (it answers with RELS)."""
        packets = []
        if self.device_audio_clock is not None:
            packets.append(hpa0_packet(self.device_audio_clock))
        packets.append(hpd0_packet())
        return packets
