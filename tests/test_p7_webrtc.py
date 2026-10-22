"""Prototype 7 WebRTC: codec choice, passthrough fidelity, compat fallback, lifecycle.

An aiortc peer stands in for the browser; real-browser coverage lives in
tests/e2e (Playwright). Also pins the aiortc internals webrtc.py relies on.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiortc import RTCPeerConnection, RTCConfiguration, RTCSessionDescription, rtcrtpsender
from aiortc.codecs import CODECS, get_capabilities
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synthetic_media import SyntheticPhone  # noqa: E402
from test_p7_media import free_udp_ports  # noqa: E402

from mirrordesk import webrtc
from mirrordesk.media import MediaHub
from mirrordesk.settings import Settings


def codec(mime, plid=None, pt=100):
    params = {'packetization-mode': '1', 'profile-level-id': plid} if plid else {}
    return RTCRtpCodecParameters(mimeType=mime, clockRate=90000, payloadType=pt, parameters=params)


CHROME_LIKE = [codec('video/VP8', pt=96), codec('video/H264', '42001f', 97), codec('video/H264', '42e01f', 98),
               codec('video/H264', '4d001f', 99), codec('video/H264', '64001f', 100)]
FIREFOX_LIKE = [codec('video/VP8', pt=120), codec('video/H264', '42e01f', 126), codec('video/H264', '42001f', 97)]
VP8_ONLY = [codec('video/VP8', pt=96)]
# Measured 2026-10-14: Chrome for Testing 156 headless shell (software decode) on Windows.
CHROME_SOFTWARE = [codec('video/VP8', pt=96), codec('video/H264', '42001f', 103), codec('video/H264', '42e01f', 107),
                   codec('video/H264', '4d001f', 109), codec('video/H264', 'f4001f', 111)]
MAIN_ONLY = [codec('video/H264', '42e01f', 98), codec('video/H264', '4d001f', 99)]


def test_pinned_aiortc_internals_still_exist():
    """webrtc.py patches these; a version bump must fail loudly here first."""
    assert hasattr(rtcrtpsender, 'RTP_HISTORY_SIZE') and hasattr(rtcrtpsender, 'get_encoder')
    pc = RTCPeerConnection()
    transceiver = pc.addTransceiver('video')
    assert hasattr(transceiver, '_codecs') and hasattr(transceiver.sender, '_stream_id')
    assert hasattr(transceiver.sender, '_send_keyframe')
    asyncio.run(pc.close())


def test_install_registers_high_and_main_profiles_once():
    webrtc.install()
    webrtc.install()
    ids = [c.parameters.get('profile-level-id') for c in CODECS['video'] if c.mimeType == 'video/H264']
    for plid in webrtc.EXTRA_H264_PROFILES:
        assert ids.count(plid) == 1
    types = [c.payloadType for c in CODECS['video']]
    assert len(types) == len(set(types)) and max(types) <= 127
    assert rtcrtpsender.RTP_HISTORY_SIZE == webrtc.RTP_HISTORY
    assert any(c.parameters.get('profile-level-id') == '64001f' for c in get_capabilities('video').codecs)


@pytest.mark.parametrize('offer,source,requested,mode,expected', [
    (CHROME_LIKE, 100, 'auto', 'passthrough', '64001f'),
    (CHROME_LIKE, None, 'auto', 'passthrough', '64001f'),
    (CHROME_LIKE, 77, 'auto', 'passthrough', '4d001f'),
    (CHROME_LIKE, 66, 'auto', 'passthrough', '42e01f'),
    (CHROME_LIKE, 100, 'compat', 'compat', '42e01f'),
    (FIREFOX_LIKE, 100, 'auto', 'compat', '42e01f'),
    (FIREFOX_LIKE, 66, 'auto', 'passthrough', '42e01f'),
    (FIREFOX_LIKE, 100, 'passthrough', 'passthrough', '42e01f'),
    (VP8_ONLY, 100, 'auto', 'compat', None),
    (CHROME_SOFTWARE, 100, 'auto', 'passthrough', 'f4001f'),
    (CHROME_SOFTWARE, 77, 'auto', 'passthrough', '4d001f'),
    (MAIN_ONLY, 100, 'auto', 'passthrough', '4d001f'),
])
def test_choose_video(offer, source, requested, mode, expected):
    chosen_mode, chosen = webrtc.choose_video(offer, requested, source)
    assert chosen_mode == mode
    if expected:
        assert chosen.parameters['profile-level-id'] == expected
    else:
        assert chosen.mimeType == 'video/VP8'


CHROME_156_VIDEO_OFFER = '\r\n'.join([
    'v=0', 'm=audio 9 UDP/TLS/RTP/SAVPF 111', 'a=rtpmap:111 opus/48000/2', 'a=fmtp:111 minptime=10;useinbandfec=1',
    'm=video 9 UDP/TLS/RTP/SAVPF 96 97 108 109 39 40',
    'a=rtpmap:96 VP8/90000', 'a=rtpmap:97 rtx/90000', 'a=fmtp:97 apt=96',
    'a=rtpmap:108 H264/90000', 'a=fmtp:108 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f',
    'a=rtpmap:109 rtx/90000', 'a=fmtp:109 apt=108',
    'a=rtpmap:39 H264/90000', 'a=fmtp:39 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=f4001f',
    'a=rtpmap:40 rtx/90000', 'a=fmtp:40 apt=39'])


def test_answer_reuses_browser_payload_types_in_35_63_range():
    """Regression (Playwright, Chrome 156): aiortc answered f4001f with its own PT 109,
    which Chrome had assigned to RTX -> packets arrived but no frame was assembled."""
    webrtc.install()
    payloads = webrtc.offer_video_payloads(CHROME_156_VIDEO_OFFER)
    assert payloads[39] == ('video/h264', {'level-asymmetry-allowed': '1', 'packetization-mode': '1',
                                           'profile-level-id': 'f4001f'})
    assert payloads[40][0] == 'video/rtx' and 111 not in payloads
    local = next(c for c in CODECS['video'] if c.parameters.get('profile-level-id') == 'f4001f')
    local_rtx = next(c for c in CODECS['video'] if c.mimeType == 'video/rtx' and c.parameters['apt'] == local.payloadType)
    chosen = webrtc._with_rtx([local, local_rtx], local, payloads)
    assert [c.payloadType for c in chosen] == [39, 40]
    assert chosen[1].parameters == {'apt': 39} and chosen[0].parameters['profile-level-id'] == 'f4001f'
    assert local.payloadType != 39, 'the shared aiortc codec table must not be mutated'
    in_range = next(c for c in CODECS['video'] if c.parameters.get('profile-level-id') == '42e01f')
    assert [c.payloadType for c in webrtc._with_rtx([in_range], in_range, payloads)] == [108, 109]


def test_vp8_last_resort_hint():
    assert webrtc.choose_video(CHROME_LIKE, 'auto', 100, prefer_vp8=True)[1].mimeType == 'video/VP8'
    assert webrtc.choose_video(FIREFOX_LIKE, 'compat', 100, prefer_vp8=True)[1].mimeType == 'video/VP8'
    only_h264 = [c for c in CHROME_LIKE if c.mimeType == 'video/H264']
    assert webrtc.choose_video(only_h264, 'auto', 100, prefer_vp8=True)[0] == 'passthrough', 'hint ignored without VP8'


def test_auto_starts_re_encoded_when_the_phone_stream_cannot_start_yet():
    """An iPhone GOP too long to replay: a new viewer starts re-encoded now instead of a black
    picture until the phone's next keyframe (minutes, while a video plays)."""
    assert webrtc.choose_video(CHROME_LIKE, 'auto', 100, passthrough_ready=False)[0] == 'compat'
    assert webrtc.choose_video(CHROME_LIKE, 'passthrough', 100, passthrough_ready=False)[0] == 'passthrough', \
        '"The phone\'s own video" chosen by hand is honoured'
    assert webrtc.choose_video(CHROME_LIKE, 'auto', 100)[0] == 'passthrough'


def test_no_common_codec():
    assert webrtc.choose_video([], 'auto', 100) == (None, None)


def test_sanitize_offer_drops_mdns_and_tcp_candidates_only():
    sdp = '\r\n'.join(['v=0', 'a=candidate:1 1 udp 2122260223 192.168.1.20 53000 typ host',
                       'a=candidate:2 1 udp 2122260223 4b7c-uuid.local 53001 typ host',
                       'a=candidate:3 1 tcp 1518280447 192.168.1.20 9 typ host tcptype active',
                       'a=candidate:4 1 udp 2122260223 100.101.102.103 53002 typ host', 'a=end-of-candidates'])
    out = webrtc.sanitize_offer(sdp)
    assert '192.168.1.20 53000' in out and '100.101.102.103' in out
    assert '.local' not in out and ' tcp ' not in out
    assert 'end-of-candidates' not in out, 'keeps aioice waiting for peer-reflexive checks (default Firefox)'


async def test_default_firefox_offer_with_only_mdns_candidates_still_connects(live_hub):
    """Review finding: an offer whose candidates are all mDNS names plus
    end-of-candidates made aioice fail ICE before any check could arrive."""
    manager = webrtc.ViewerManager(live_hub, store())
    client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    client.addTransceiver('video', direction='recvonly')
    tracks = {}
    client.on('track', lambda track: tracks.__setitem__(track.kind, track))
    await client.setLocalDescription(await client.createOffer())
    disguised = []
    for line in client.localDescription.sdp.splitlines():
        if line.startswith('a=candidate:'):
            parts = line.split()
            parts[4] = 'f1e2d3c4-0000-4000-8000-000000000000.local'  # what Firefox/Chrome send by default
            line = ' '.join(parts)
        disguised.append(line)
    if not any(l.startswith('a=end-of-candidates') for l in disguised):
        disguised.append('a=end-of-candidates')
    answer = await manager.create('\r\n'.join(disguised) + '\r\n', 'auto', '127.0.0.1', True)
    await client.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type='answer'))
    try:
        frame = await first_video_frame(tracks, timeout=15)
        assert frame.width == 498
        viewer = next(iter(manager.viewers.values()))
        assert viewer.pc.connectionState == 'connected'
    finally:
        await client.close()
        await manager.close_all()


async def negotiate(manager, preferences=None, mode='auto'):
    client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    video_t = client.addTransceiver('video', direction='recvonly')
    client.addTransceiver('audio', direction='recvonly')
    if preferences:
        caps = [c for c in get_capabilities('video').codecs if preferences(c)]
        video_t.setCodecPreferences(caps)
    tracks = {}
    client.on('track', lambda track: tracks.__setitem__(track.kind, track))
    await client.setLocalDescription(await client.createOffer())
    answer = await manager.create(client.localDescription.sdp, mode, '127.0.0.1', True)
    await client.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type='answer'))
    return client, answer, tracks


@pytest.fixture
async def live_hub():
    video_port, audio_port = free_udp_ports()
    hub = MediaHub(video_port, audio_port, compat_max_height=720)
    await hub.start()
    phone = SyntheticPhone(video_port, audio_port, width=498, height=1080, fps=30, frames=30).start()
    for _ in range(100):
        if hub.gop_valid:
            break
        await asyncio.sleep(0.05)
    yield hub
    phone.stop()
    await hub.stop()


def store():
    return SimpleNamespace(settings=Settings(pin='1234', access_code='ABCDEF'))


async def first_video_frame(tracks, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if 'video' in tracks:
            break
        await asyncio.sleep(0.05)
    return await asyncio.wait_for(tracks['video'].recv(), timeout)


async def test_passthrough_delivers_source_resolution_and_separate_streams(live_hub):
    manager = webrtc.ViewerManager(live_hub, store())
    client, answer, tracks = await negotiate(manager)
    try:
        assert answer['mode'] == 'passthrough' and '64' in answer['codec']
        sdp = answer['sdp']
        msids = [line.split()[0] for line in sdp.splitlines() if line.startswith('a=msid:')]
        assert len(msids) == 2 and msids[0] != msids[1], 'audio/video in separate streams (no lip-sync coupling)'
        frame = await first_video_frame(tracks)
        assert (frame.width, frame.height) == (498, 1080), 'passthrough keeps the phone resolution'
        audio = await asyncio.wait_for(tracks['audio'].recv(), 10)
        assert audio.sample_rate == 48000
        viewer = next(iter(manager.viewers.values()))
        assert viewer.video.sent > 0
    finally:
        await client.close()
        await manager.close_all()
    assert not manager.viewers and not live_hub.video_subscribers and not live_hub.audio_subscribers


async def test_baseline_only_browser_gets_compat_reencode(live_hub):
    manager = webrtc.ViewerManager(live_hub, store())
    client, answer, tracks = await negotiate(
        manager, lambda c: c.mimeType != 'video/H264' or c.parameters.get('profile-level-id', '').startswith('42'))
    try:
        assert answer['mode'] == 'compat'
        frame = await first_video_frame(tracks, timeout=15)
        assert frame.height == 720 and frame.width == 332, 'compat honours the max-height cap'
    finally:
        await client.close()
        await manager.close_all()
    for _ in range(40):
        if live_hub._compat.thread is None:
            break
        await asyncio.sleep(0.05)
    assert live_hub._compat.thread is None


async def test_vp8_only_browser_gets_compat_vp8(live_hub):
    manager = webrtc.ViewerManager(live_hub, store())
    client, answer, tracks = await negotiate(manager, lambda c: c.mimeType in ('video/VP8', 'video/rtx'))
    try:
        assert answer['mode'] == 'compat' and answer['codec'].startswith('VP8')
        frame = await first_video_frame(tracks, timeout=15)
        assert frame.height == 720
    finally:
        await client.close()
        await manager.close_all()


async def test_viewer_limit_enforced(live_hub):
    s = store()
    s.settings.max_viewers = 1
    manager = webrtc.ViewerManager(live_hub, s)
    client, _, _ = await negotiate(manager)
    try:
        with pytest.raises(webrtc.ViewerLimit):
            await negotiate(manager)
    finally:
        await client.close()
        await manager.close_all()


async def test_stale_viewer_is_evicted_when_limit_reached(live_hub):
    s = store()
    s.settings.max_viewers = 1
    manager = webrtc.ViewerManager(live_hub, s)
    gone = RTCPeerConnection()
    gone.addTransceiver('video', direction='recvonly')
    await gone.setLocalDescription(await gone.createOffer())
    await manager.create(gone.localDescription.sdp, 'auto', '127.0.0.1', True)
    await gone.close()  # tab closed before connecting; its slot must not block others
    next(iter(manager.viewers.values())).created -= 60
    client, answer, _ = await negotiate(manager)
    try:
        assert answer['mode'] == 'passthrough' and len(manager.viewers) == 1
    finally:
        await client.close()
        await manager.close_all()


async def test_unconnected_viewer_lease_expires(live_hub, monkeypatch):
    monkeypatch.setattr(webrtc, 'UNCONNECTED_LEASE', 0.3)
    manager = webrtc.ViewerManager(live_hub, store())
    client = RTCPeerConnection()
    client.addTransceiver('video', direction='recvonly')
    await client.setLocalDescription(await client.createOffer())
    await manager.create(client.localDescription.sdp, 'auto', '127.0.0.1', True)
    await client.close()  # never answers -> never connects
    await asyncio.sleep(0.8)
    assert not manager.viewers
