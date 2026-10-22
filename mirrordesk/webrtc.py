"""WebRTC viewers: codec negotiation, passthrough/compat selection, lifecycle.

Relies on documented-but-internal hooks of the pinned aiortc 1.14.0 / aioice
0.10.x (codec table, transceiver._codecs, sender._stream_id, encoder factory,
host-address enumeration). tests/test_p7_webrtc.py pins each assumption.
"""
import asyncio
import dataclasses
import fractions
import ipaddress
import logging
import time
import uuid

import aioice.ice
import av
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc import codecs as aiortc_codecs
from aiortc import rtcrtpsender
from aiortc.codecs import h264 as aiortc_h264
from aiortc.codecs import vpx as aiortc_vpx
from aiortc.codecs.h264 import H264Encoder
from aiortc.codecs.opus import OpusEncoder
from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecParameters
from aiortc.sdp import H264Profile, parse_h264_profile_level_id

from .media import CompatVideoTrack, LiveAudioTrack, PassthroughVideoTrack

log = logging.getLogger('mirrordesk.webrtc')

# constrained high, high, main, high 4:4:4 predictive
EXTRA_H264_PROFILES = ('640c1f', '64001f', '4d001f', 'f4001f')
RTP_HISTORY = 4096          # 1080p keyframes span hundreds of packets; NACK needs them
# Video payload per packet: with RTP, its header extensions, SRTP and RTX about 1190 bytes, as
# browsers keep theirs (about 1200). aiortc's 1300 made 1360-byte packets that VPNs had to split:
# Tailscale and WireGuard carry at most 1280 bytes.
RTP_PAYLOAD_MAX = 1150
UNCONNECTED_LEASE = 20
PEER_CLOSE_TIMEOUT = 5
_installed = False
_addresses = ['127.0.0.1']

_P = H264Profile
# Browser profiles able to take the phone's stream as-is, best first. Per the
# H.264 spec a High 4:4:4 Predictive decoder also decodes High/Main streams
# (Chrome without hardware decode offers f4001f but not 64001f). Main is a
# last, pragmatic tier for High sources: every shipping browser decoder is
# High-capable, and the client watchdog falls back to compat if one is not.
PASSTHROUGH_PROFILES = {
    100: (_P.PROFILE_HIGH, _P.PROFILE_CONSTRAINED_HIGH, _P.PROFILE_PREDICTIVE_HIGH_444, _P.PROFILE_MAIN),
    77: (_P.PROFILE_MAIN, _P.PROFILE_HIGH, _P.PROFILE_CONSTRAINED_HIGH, _P.PROFILE_PREDICTIVE_HIGH_444),
    66: (_P.PROFILE_CONSTRAINED_BASELINE, _P.PROFILE_BASELINE, _P.PROFILE_MAIN, _P.PROFILE_CONSTRAINED_HIGH,
         _P.PROFILE_HIGH, _P.PROFILE_PREDICTIVE_HIGH_444),
}
COMPAT_PROFILES = (H264Profile.PROFILE_CONSTRAINED_BASELINE, H264Profile.PROFILE_BASELINE)


class TunedH264Encoder(H264Encoder):
    """aiortc's encoder with a realtime preset and a level fit for 1080p60."""

    def _encode_frame(self, frame, force_keyframe):
        if self.codec and (frame.width != self.codec.width or frame.height != self.codec.height
                           or abs(self.target_bitrate - self.codec.bit_rate) / self.codec.bit_rate > 0.1):
            self.buffer_data = b''
            self.buffer_pts = None
            self.codec = None
        frame.pict_type = av.video.frame.PictureType.I if force_keyframe else av.video.frame.PictureType.NONE
        if self.codec is None:
            self.codec = av.CodecContext.create('libx264', 'w')
            self.codec.width = frame.width
            self.codec.height = frame.height
            self.codec.bit_rate = self.target_bitrate
            self.codec.pix_fmt = 'yuv420p'
            self.codec.framerate = fractions.Fraction(60, 1)
            self.codec.time_base = fractions.Fraction(1, 60)
            self.codec.options = {'level': '42', 'preset': 'superfast', 'tune': 'zerolatency'}
            self.codec.profile = 'Baseline'
        data = b''.join(bytes(p) for p in self.codec.encode(frame))
        if data:
            yield from self._split_bitstream(data)


class MusicOpusEncoder(OpusEncoder):
    """Mirrored audio is mostly music/video sound, not speech.

    LiveAudioTrack already delivers exact 20 ms s16 stereo 48 kHz frames, so they go
    straight to libopus and each packet's RTP timestamp is its own frame's pts.
    Through aiortc's resampler (and libopus' look-ahead) the first packet after a
    timestamp jump kept the old timeline, so the browser saw that packet arrive late
    by the whole jump, measured: frames with pts 102880, 106720, 107680 went out as
    2880, 3840, 7680."""

    def __init__(self):
        super().__init__()
        self.codec.bit_rate = 128000
        self.codec.options = {'application': 'audio'}
        self.first_frame_pts = None

    def encode(self, frame, force_keyframe=False):
        if not (isinstance(frame, av.AudioFrame) and frame.samples == 960 and frame.sample_rate == 48000
                and frame.format.name == 's16' and frame.layout.name == 'stereo' and frame.pts is not None
                and frame.time_base == fractions.Fraction(1, 48000)):
            return super().encode(frame, force_keyframe)
        if self.first_frame_pts is None:
            self.first_frame_pts = frame.pts
        timestamp = frame.pts - self.first_frame_pts
        packets = self.codec.encode(frame)
        if not packets:
            return [], None
        return [bytes(p) for p in packets], timestamp


_original_get_encoder = aiortc_codecs.get_encoder


def _get_encoder(codec):
    mime = codec.mimeType.lower()
    if mime == 'video/h264':
        return TunedH264Encoder()
    if mime == 'audio/opus':
        return MusicOpusEncoder()
    return _original_get_encoder(codec)


def _host_addresses(use_ipv4=True, use_ipv6=False):
    return list(_addresses)


def install():
    """Idempotently register High/Main H.264 and tune aiortc for LAN video."""
    global _installed
    aioice.ice.get_host_addresses = _host_addresses  # re-applied: legacy backend import overrides it
    if _installed:
        return
    video = aiortc_codecs.CODECS['video']
    present = {c.parameters.get('profile-level-id') for c in video if c.mimeType.lower() == 'video/h264'}
    payload_type = max(c.payloadType for c in video) + 1
    for profile_level_id in EXTRA_H264_PROFILES:
        if profile_level_id in present:
            continue
        video.append(RTCRtpCodecParameters(
            mimeType='video/H264', clockRate=90000, payloadType=payload_type,
            rtcpFeedback=[RTCRtcpFeedback(type='nack'), RTCRtcpFeedback(type='nack', parameter='pli'),
                          RTCRtcpFeedback(type='goog-remb')],
            parameters={'level-asymmetry-allowed': '1', 'packetization-mode': '1',
                        'profile-level-id': profile_level_id}))
        video.append(RTCRtpCodecParameters(mimeType='video/rtx', clockRate=90000,
                                           payloadType=payload_type + 1, parameters={'apt': payload_type}))
        payload_type += 2
    aiortc_h264.DEFAULT_BITRATE = 4_000_000
    aiortc_h264.MAX_BITRATE = 12_000_000
    aiortc_vpx.DEFAULT_BITRATE = 2_000_000
    aiortc_vpx.MAX_BITRATE = 8_000_000
    aiortc_h264.PACKET_MAX = RTP_PAYLOAD_MAX
    aiortc_vpx.PACKET_MAX = RTP_PAYLOAD_MAX
    rtcrtpsender.RTP_HISTORY_SIZE = RTP_HISTORY
    rtcrtpsender.get_encoder = _get_encoder
    _installed = True


def set_candidate_addresses(addresses):
    _addresses[:] = list(addresses) or ['127.0.0.1']


def sanitize_offer(sdp):
    """Drop browser mDNS (.local) and TCP candidates: never resolve or dial them.

    Browsers hide LAN addresses behind mDNS names; the server instead learns
    the browser's address from its connectivity checks (peer-reflexive). The
    end-of-candidates marker is dropped too: with every candidate stripped
    (default Firefox) aioice would otherwise fail ICE at once, before the
    browser has even received the answer and sent its first check.
    """
    lines = []
    for line in sdp.splitlines():
        if line.startswith('a=end-of-candidates'):
            continue
        if line.startswith('a=candidate:'):
            parts = line.split()
            try:
                ipaddress.ip_address(parts[4])
            except (ValueError, IndexError):
                continue
            if len(parts) < 3 or parts[2].lower() != 'udp':
                continue
        lines.append(line)
    return '\r\n'.join(lines) + '\r\n'


def _profile(codec):
    try:
        return parse_h264_profile_level_id(str(codec.parameters.get('profile-level-id', '42e01f')))[0]
    except ValueError:
        return None


def _is_h264(codec):
    return codec.mimeType.lower() == 'video/h264' and str(codec.parameters.get('packetization-mode', '0')) == '1'


def choose_video(codecs, requested='auto', source_profile_idc=None, prefer_vp8=False, passthrough_ready=True):
    """Pick (mode, codec) from the negotiated common codecs (offer order).

    passthrough: the browser offered an H.264 profile able to decode the
    phone's stream as-is. compat: decode + re-encode to constrained baseline
    H.264 or VP8, which every WebRTC browser must support. prefer_vp8 is the
    client's last-resort fallback when even baseline H.264 never decoded
    (e.g. Firefox on Linux without the OpenH264 plugin). passthrough_ready=False
    (the phone's stream cannot start for a new viewer until its next keyframe,
    MediaHub.can_start_passthrough) makes 'auto' start re-encoded.
    """
    vp8 = next((c for c in codecs if c.mimeType.lower() == 'video/vp8'), None)
    if prefer_vp8 and vp8 is not None:
        return 'compat', vp8
    if requested == 'auto' and not passthrough_ready:
        requested = 'compat'
    primary = [c for c in codecs if c.mimeType.lower() not in ('video/rtx',)]
    h264 = [c for c in primary if _is_h264(c)]
    acceptable = PASSTHROUGH_PROFILES.get(source_profile_idc or 100, ())
    passthrough = None
    for wanted in acceptable:
        passthrough = next((c for c in h264 if _profile(c) == wanted), None)
        if passthrough:
            break
    if requested == 'passthrough' and passthrough is None and h264:
        passthrough = h264[0]  # forced: the browser may still decode it
    compat = None
    for wanted in COMPAT_PROFILES:
        compat = next((c for c in h264 if _profile(c) == wanted), None)
        if compat:
            break
    compat = compat or next((c for c in primary if c.mimeType.lower() == 'video/vp8'), None) or (h264[0] if h264 else None)
    if requested in ('auto', 'passthrough') and passthrough is not None:
        return 'passthrough', passthrough
    if compat is not None:
        return 'compat', compat
    return None, None


def offer_video_payloads(sdp):
    """Map the offer's video payload types: pt -> (mime, {fmtp params})."""
    payloads = {}
    in_video = False
    for line in sdp.splitlines():
        if line.startswith('m='):
            in_video = line.startswith('m=video')
            continue
        if not in_video:
            continue
        if line.startswith('a=rtpmap:'):
            pt, _, rest = line[len('a=rtpmap:'):].partition(' ')
            if pt.isdigit():
                payloads[int(pt)] = ('video/' + rest.split('/')[0].lower(), payloads.get(int(pt), (None, {}))[1])
        elif line.startswith('a=fmtp:'):
            pt, _, rest = line[len('a=fmtp:'):].partition(' ')
            if pt.isdigit():
                params = dict(item.split('=', 1) for item in rest.split(';') if '=' in item)
                mime = payloads.get(int(pt), (None, {}))[0]
                payloads[int(pt)] = (mime, {k.strip(): v.strip() for k, v in params.items()})
    return payloads


def _remote_payload_type(payloads, codec):
    mime = codec.mimeType.lower()
    for pt, (remote_mime, params) in payloads.items():
        if remote_mime != mime:
            continue
        if mime != 'video/h264':
            return pt
        if str(params.get('packetization-mode', '0')) != '1':
            continue
        try:
            remote_profile = parse_h264_profile_level_id(params.get('profile-level-id', '42e01f'))[0]
        except ValueError:
            continue
        if remote_profile == _profile(codec):
            return pt
    return None


def _with_rtx(codecs, chosen, payloads=None):
    """Chosen codec (+ its RTX) using the BROWSER's payload type numbers.

    Chrome assigns newer codecs to the 35-63 range, which aiortc 1.14 does not
    treat as dynamic: it would answer with its own local number, which in
    Chrome's offer can mean a different codec (seen: our f4001f went out as
    PT 109, Chrome's RTX for 42e01f, so no frame was ever assembled).
    """
    original_pt = chosen.payloadType
    rtx = next((c for c in codecs if c.mimeType.lower() == 'video/rtx'
                and c.parameters.get('apt') == original_pt), None)
    if payloads:
        remote_pt = _remote_payload_type(payloads, chosen)
        if remote_pt is not None and remote_pt != original_pt:
            chosen = dataclasses.replace(chosen, payloadType=remote_pt)
            rtx = None
            for pt, (mime, params) in payloads.items():
                if mime == 'video/rtx' and str(params.get('apt')) == str(remote_pt):
                    rtx = RTCRtpCodecParameters(mimeType='video/rtx', clockRate=90000, payloadType=pt,
                                                parameters={'apt': remote_pt})
                    break
    return [chosen] + ([rtx] if rtx else [])


class ViewerLimit(Exception):
    pass


class Viewer:
    def __init__(self, viewer_id, pc, client_ip, local, role='owner', kind=None):
        self.id = viewer_id
        self.pc = pc
        self.client_ip = client_ip
        self.local = local
        self.role = role
        self.kind = dict(kind or {})   # diagnostics categories: browser, device, path (server.py)
        self.hidden = None             # the page was in the background (its last stats report)
        self.created = time.monotonic()
        self.connected_at = None
        self.mode = None
        self.codec = None
        self.video = None
        self.audio = None
        self.lease = None

    def summary(self):
        video = self.video
        return {'id': self.id[:8], 'mode': self.mode, 'codec': self.codec, 'local': self.local, 'role': self.role,
                'state': self.pc.connectionState, 'age_s': round(time.monotonic() - self.created),
                'frames_sent': getattr(video, 'sent', 0)}


class ViewerManager:
    def __init__(self, hub, store, diagnostics=None, addresses=None):
        self.hub = hub
        self.store = store
        self.diagnostics = diagnostics
        self.addresses = addresses or (lambda local: ['127.0.0.1'])
        self.viewers = {}
        install()

    async def evict_stale(self, unconnected_after=15):
        """Close viewers that are gone but not yet detected (a closed tab is only
        noticed by ICE consent checks ~30 s later) so they don't hold slots."""
        now = time.monotonic()
        for viewer in list(self.viewers.values()):
            state = viewer.pc.connectionState
            if state in ('failed', 'closed', 'disconnected') or (
                    state != 'connected' and now - viewer.created > unconnected_after):
                await self.close(viewer.id)

    async def create(self, offer_sdp, requested_mode='auto', client_ip=None, local=True, prefer_vp8=False,
                     role='owner', kind=None):
        settings = self.store.settings
        if len(self.viewers) >= settings.max_viewers:
            await self.evict_stale()
        if len(self.viewers) >= settings.max_viewers:
            raise ViewerLimit(f'Viewer limit reached ({settings.max_viewers}). Close another viewer or raise the limit.')
        if requested_mode not in ('auto', 'passthrough', 'compat'):
            requested_mode = settings.video_mode
        if requested_mode == 'auto' and settings.video_mode != 'auto':
            requested_mode = settings.video_mode
        install()
        set_candidate_addresses(self.addresses(local))
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        viewer = Viewer(uuid.uuid4().hex, pc, client_ip, local, role, kind)
        self.viewers[viewer.id] = viewer
        try:
            payloads = offer_video_payloads(offer_sdp)
            async with asyncio.timeout(12):
                await pc.setRemoteDescription(RTCSessionDescription(sdp=sanitize_offer(offer_sdp), type='offer'))
                video_t = next((t for t in pc.getTransceivers() if t.kind == 'video'), None)
                audio_t = next((t for t in pc.getTransceivers() if t.kind == 'audio'), None)
                if video_t is None:
                    raise ValueError('Offer has no video section')
                info = self.hub.sps_info
                ready = getattr(self.hub, 'can_start_passthrough', lambda: True)()
                mode, codec = choose_video(video_t._codecs, requested_mode, info.profile_idc if info else None,
                                           prefer_vp8, passthrough_ready=ready)
                if codec is None:
                    raise ValueError('No common video codec (need H.264 or VP8)')
                viewer.mode = mode
                viewer.codec = codec.mimeType.split('/')[1] + (
                    f" {codec.parameters.get('profile-level-id')}" if _is_h264(codec) else '')
                viewer.video = PassthroughVideoTrack(self.hub) if mode == 'passthrough' else CompatVideoTrack(self.hub)
                video_sender = pc.addTrack(viewer.video)
                video_t._codecs = _with_rtx(video_t._codecs, codec, payloads)
                video_sender._stream_id = uuid.uuid4().hex
                original_keyframe = video_sender._send_keyframe
                track = viewer.video

                def on_keyframe_request(track=track, original=original_keyframe):
                    track.on_pli()
                    original()
                video_sender._send_keyframe = on_keyframe_request
                if audio_t is not None and settings.audio_enabled and audio_t._codecs:
                    viewer.audio = LiveAudioTrack(self.hub)
                    audio_sender = pc.addTrack(viewer.audio)
                    # Separate media stream: no browser lip-sync coupling, so a
                    # late audio clock can never hold video back.
                    audio_sender._stream_id = uuid.uuid4().hex
                await pc.setLocalDescription(await pc.createAnswer())
        except Exception:
            await self.close(viewer.id)
            raise

        @pc.on('connectionstatechange')
        async def changed():
            if pc.connectionState == 'connected' and viewer.connected_at is None:
                viewer.connected_at = time.monotonic()
            if pc.connectionState in ('failed', 'closed'):
                await self.close(viewer.id)

        async def expire():
            await asyncio.sleep(UNCONNECTED_LEASE)
            if viewer.id in self.viewers and pc.connectionState != 'connected':
                await self.close(viewer.id)
        viewer.lease = asyncio.create_task(expire())
        self._record('offer')
        return {'viewer_id': viewer.id, 'type': pc.localDescription.type, 'sdp': pc.localDescription.sdp,
                'mode': viewer.mode, 'codec': viewer.codec}

    def request_keyframe(self, viewer_id):
        """The browser's watchdog: (resync started, stuck). stuck: this passthrough viewer waits
        for a keyframe the phone may not send for minutes; the page reconnects re-encoded."""
        viewer = self.viewers.get(viewer_id)
        if viewer is None or viewer.video is None:
            return False, False
        return viewer.video.request_keyframe(), viewer.video.stuck()

    async def close(self, viewer_id):
        viewer = self.viewers.pop(viewer_id, None)
        if viewer is None:
            return False
        if viewer.lease is not None and viewer.lease is not asyncio.current_task():
            viewer.lease.cancel()
        for track in (viewer.video, viewer.audio):
            if track is not None:
                track.stop()
        if viewer.connected_at is None and viewer.mode is not None:
            # Never connected: the browser's checks never reached this server, or it gave up.
            fields = {'viewer_mode': viewer.mode, 'ice': viewer.pc.iceConnectionState, **viewer.kind}
            if viewer.hidden is not None:
                fields['page_hidden'] = viewer.hidden
            self._record('viewer_failed', **fields)
        try:
            # Bounded: a peer stuck mid-handshake must not hang DELETE or shutdown.
            await asyncio.wait_for(viewer.pc.close(), PEER_CLOSE_TIMEOUT)
        except Exception:  # closing a half-negotiated peer must never fail the API
            log.debug('peer close failed', exc_info=True)
        self._record('viewer_stop')
        return True

    async def close_all(self):
        for viewer_id in list(self.viewers):
            await self.close(viewer_id)

    def status(self):
        return [viewer.summary() for viewer in self.viewers.values()]

    def ice_sockets(self):
        """(address, port) of every local ICE socket of the current viewers: the only places the
        TURN relay (turn.py) forwards to."""
        found = set()
        for viewer in list(self.viewers.values()):
            for transceiver in viewer.pc.getTransceivers():
                try:
                    candidates = transceiver.sender.transport.transport._connection.local_candidates
                except AttributeError:
                    continue
                found.update((candidate.host, candidate.port) for candidate in candidates)
        return found

    def _record(self, event, **fields):
        if self.diagnostics is not None:
            try:
                self.diagnostics.record(event, **fields)
            except ValueError:
                pass
