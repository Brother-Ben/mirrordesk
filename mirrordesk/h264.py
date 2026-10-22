"""Minimal H.264 bitstream helpers: NAL typing and SPS parsing.

Only what the passthrough path needs to make decisions: is this access unit a
keyframe, which profile/level does the phone send, and at what resolution.
Malformed input never raises out of the public helpers.
"""
from dataclasses import dataclass

NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9

START_CODE = b'\x00\x00\x00\x01'

PROFILE_NAMES = {66: 'baseline', 77: 'main', 88: 'extended', 100: 'high', 110: 'high10',
                 122: 'high422', 244: 'high444'}
# Profiles whose SPS carries chroma_format_idc and scaling-matrix fields.
_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
_KNOWN_PROFILES = _HIGH_PROFILES | {66, 77, 88}


def nal_type(nal):
    return nal[0] & 0x1F if nal else 0


def split_annexb(data):
    """Split an Annex-B byte string into NAL units (without start codes)."""
    nals = []
    i = data.find(b'\x00\x00\x01')
    while i != -1:
        start = i + 3
        nxt = data.find(b'\x00\x00\x01', start)
        if nxt == -1:
            nal = data[start:]
        else:
            end = nxt - 1 if data[nxt - 1] == 0 else nxt
            nal = data[start:end]
        if nal:
            nals.append(nal)
        i = nxt
    return nals


def join_annexb(nals):
    return b''.join(START_CODE + nal for nal in nals)


def _unescape(rbsp):
    """Remove emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class _Bits:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def bit(self):
        if self.pos >= len(self.data) * 8:
            raise ValueError('SPS truncated')
        value = (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return value

    def bits(self, count):
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self):
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError('Exp-Golomb overflow')
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self):
        value = self.ue()
        return (value + 1) // 2 if value & 1 else -(value // 2)


@dataclass(frozen=True)
class SpsInfo:
    profile_idc: int
    constraints: int
    level_idc: int
    width: int
    height: int

    @property
    def profile_level_id(self):
        return f'{self.profile_idc:02x}{self.constraints:02x}{self.level_idc:02x}'

    @property
    def profile_name(self):
        return PROFILE_NAMES.get(self.profile_idc, f'profile{self.profile_idc}')


def _skip_scaling_list(bits, size):
    last = nxt = 8
    for _ in range(size):
        if nxt:
            nxt = (last + bits.se() + 256) % 256
        last = nxt or last


def parse_sps(nal):
    """Return SpsInfo for an SPS NAL (with its 1-byte header), or None."""
    try:
        if nal_type(nal) != NAL_SPS or len(nal) < 4:
            return None
        bits = _Bits(_unescape(nal[1:]))
        profile_idc = bits.bits(8)
        constraints = bits.bits(8)
        level_idc = bits.bits(8)
        if profile_idc not in _KNOWN_PROFILES or not 9 <= level_idc <= 62 or constraints & 0x03:
            return None
        if bits.ue() > 31:  # seq_parameter_set_id
            return None
        chroma_format_idc = 1
        if profile_idc in _HIGH_PROFILES:
            chroma_format_idc = bits.ue()
            if chroma_format_idc > 3:
                return None
            if chroma_format_idc == 3:
                bits.bit()  # separate_colour_plane_flag
            if bits.ue() > 6 or bits.ue() > 6:  # bit_depth_luma/chroma_minus8
                return None
            bits.bit()  # qpprime_y_zero_transform_bypass_flag
            if bits.bit():  # seq_scaling_matrix_present_flag
                for index in range(8 if chroma_format_idc != 3 else 12):
                    if bits.bit():
                        _skip_scaling_list(bits, 16 if index < 6 else 64)
        if bits.ue() > 12:  # log2_max_frame_num_minus4
            return None
        poc_type = bits.ue()
        if poc_type == 0:
            if bits.ue() > 12:
                return None
        elif poc_type == 1:
            bits.bit()
            bits.se()
            bits.se()
            cycle = bits.ue()
            if cycle > 255:
                return None
            for _ in range(cycle):
                bits.se()
        elif poc_type != 2:
            return None
        if bits.ue() > 16:  # max_num_ref_frames
            return None
        bits.bit()  # gaps_in_frame_num_value_allowed_flag
        width_mbs = bits.ue() + 1
        height_map_units = bits.ue() + 1
        frame_mbs_only = bits.bit()
        if not frame_mbs_only:
            bits.bit()  # mb_adaptive_frame_field_flag
        bits.bit()  # direct_8x8_inference_flag
        width = width_mbs * 16
        height = height_map_units * 16 * (2 - frame_mbs_only)
        if bits.bit():  # frame_cropping_flag
            left, right, top, bottom = bits.ue(), bits.ue(), bits.ue(), bits.ue()
            crop_x = 1 if chroma_format_idc in (0, 3) else 2
            crop_y = (1 if chroma_format_idc in (0, 3) or chroma_format_idc == 2 else 2) * (2 - frame_mbs_only)
            width -= (left + right) * crop_x
            height -= (top + bottom) * crop_y
        if not (0 < width <= 16384 and 0 < height <= 16384):
            return None
        return SpsInfo(profile_idc, constraints, level_idc, width, height)
    except (ValueError, IndexError):
        return None
