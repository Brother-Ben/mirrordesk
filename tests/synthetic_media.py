"""Synthetic phone media for tests: real libx264 H.264 + L16, sent as RTP.

Mimics UxPlay's -vrtp (rtph264pay: single NAL / FU-A, marker on the last
packet of a picture) and -artp (rtpL16pay, 44.1 kHz stereo big-endian).
This is generated media, never iPhone evidence.
"""
from fractions import Fraction
import math
import random
import socket
import struct
import threading
import time

import av
import numpy as np

MTU_PAYLOAD = 1200
_CLIPS = {}


def _pattern(width, height, index):
    y = np.zeros((height, width), dtype=np.uint8)
    xs = (np.arange(width, dtype=np.uint16) * 2 + index * 6) % 256
    y[:, :] = xs.astype(np.uint8)
    size = max(16, min(width, height) // 6)
    left = (index * 9) % max(1, width - size)
    top = (index * 5) % max(1, height - size)
    y[top:top + size, left:left + size] = 235
    noise = np.random.default_rng(index).integers(0, 24, (height // 8, width // 8), dtype=np.uint8)
    y[:height // 8 * 8, :width // 8 * 8] += np.kron(noise, np.ones((8, 8), dtype=np.uint8))
    u = np.full((height // 2, width // 2), 128 + (index % 40), dtype=np.uint8)
    v = np.full((height // 2, width // 2), 128 - (index % 40), dtype=np.uint8)
    return np.concatenate([y.reshape(-1), u.reshape(-1), v.reshape(-1)]).reshape(height * 3 // 2, width)


def encode_clip(width, height, frames=60, fps=30, gop=None, preset='superfast', profile='high'):
    """Return a list of Annex-B access units (bytes). Cached per parameters."""
    key = (width, height, frames, fps, gop, preset, profile)
    if key in _CLIPS:
        return _CLIPS[key]
    codec = av.CodecContext.create('libx264', 'w')
    codec.width, codec.height = width, height
    codec.pix_fmt = 'yuv420p'
    codec.time_base = Fraction(1, fps)
    codec.framerate = Fraction(fps, 1)
    codec.gop_size = gop or frames
    codec.max_b_frames = 0
    codec.profile = profile
    codec.options = {'preset': preset, 'tune': 'zerolatency',
                     'x264-params': f'repeat-headers=1:scenecut=0:keyint={gop or frames}:min-keyint={gop or frames}'}
    units = []
    for index in range(frames):
        frame = av.VideoFrame.from_ndarray(_pattern(width, height, index), format='yuv420p')
        frame.pts = index
        for packet in codec.encode(frame):
            units.append(bytes(packet))
    for packet in codec.encode(None):
        units.append(bytes(packet))
    _CLIPS[key] = units
    return units


def nal_units(annexb):
    out, i = [], annexb.find(b'\x00\x00\x01')
    while i != -1:
        start = i + 3
        nxt = annexb.find(b'\x00\x00\x01', start)
        end = len(annexb) if nxt == -1 else (nxt - 1 if annexb[nxt - 1] == 0 else nxt)
        if end > start:
            out.append(annexb[start:end])
        i = nxt
    return out


class RtpWriter:
    def __init__(self, payload_type, ssrc=None, seq=None):
        self.pt = payload_type
        self.ssrc = ssrc if ssrc is not None else random.getrandbits(32)
        self.seq = seq if seq is not None else random.getrandbits(16)

    def packet(self, payload, timestamp, marker=False):
        header = struct.pack('!BBHII', 0x80, (0x80 if marker else 0) | self.pt, self.seq & 0xFFFF,
                             timestamp & 0xFFFFFFFF, self.ssrc)
        self.seq = (self.seq + 1) & 0xFFFF
        return header + payload


def packetize_h264(writer, annexb, timestamp, stap_parameter_sets=False, mtu=MTU_PAYLOAD):
    nals = nal_units(annexb)
    payloads = []
    if stap_parameter_sets:
        params = [n for n in nals if n[0] & 0x1F in (7, 8)]
        if len(params) >= 2:
            stap = bytes([24 | (params[0][0] & 0x60)]) + b''.join(struct.pack('!H', len(n)) + n for n in params)
            payloads.append(stap)
            nals = [n for n in nals if n not in params]
    for nal in nals:
        if len(nal) <= mtu:
            payloads.append(nal)
            continue
        header, body = nal[0], nal[1:]
        indicator = (header & 0xE0) | 28
        kind = header & 0x1F
        chunks = [body[i:i + mtu - 2] for i in range(0, len(body), mtu - 2)]
        for index, chunk in enumerate(chunks):
            fu = kind | (0x80 if index == 0 else 0) | (0x40 if index == len(chunks) - 1 else 0)
            payloads.append(bytes([indicator, fu]) + chunk)
    return [writer.packet(p, timestamp, marker=(i == len(payloads) - 1)) for i, p in enumerate(payloads)]


def l16_packet(writer, timestamp, samples=352, rate=44100, frequency=440.0, phase=0):
    t = (np.arange(samples) + phase) / rate
    tone = (np.sin(2 * math.pi * frequency * t) * 8000).astype('>i2')
    stereo = np.repeat(tone, 2)
    return writer.packet(stereo.tobytes(), timestamp)


def live_units(width, height, fps, profile='high', preset='superfast'):
    """An endless stream with ONE keyframe, encoded as it goes: an iPhone mirroring a video
    sends a new keyframe only when the picture changes a lot (minutes apart)."""
    codec = av.CodecContext.create('libx264', 'w')
    codec.width, codec.height = width, height
    codec.pix_fmt = 'yuv420p'
    codec.time_base = Fraction(1, fps)
    codec.framerate = Fraction(fps, 1)
    codec.max_b_frames = 0
    codec.profile = profile
    codec.options = {'preset': preset, 'tune': 'zerolatency',
                     'x264-params': 'repeat-headers=1:scenecut=0:keyint=infinite'}
    index = 0
    while True:
        frame = av.VideoFrame.from_ndarray(_pattern(width, height, index), format='yuv420p')
        frame.pts = index
        index += 1
        for packet in codec.encode(frame):
            yield bytes(packet)


class SyntheticPhone:
    """Streams a looping clip (and optional audio) to UDP ports in real time."""

    def __init__(self, video_port, audio_port=None, width=498, height=1080, fps=30, frames=60, gop=None,
                 profile='high', preset='superfast', host='127.0.0.1', audio=True, audio_samples=352,
                 still_after=0, endless_gop=False, audio_burst=None, audio_seek=None, video_burst=None):
        self.address = host
        self.video_port, self.audio_port = video_port, audio_port
        self.fps = fps
        # > 0: video stops after this many seconds, like a still iPhone screen (sound goes on).
        self.still_after = still_after
        # 352 = UxPlay-sized packets; 4096 imitates the iPhone's 85 ms USB audio blocks.
        self.audio_samples = audio_samples
        # (every, pause) seconds: the sound stops for `pause`, then all of it arrives at once, as UxPlay
        # releases audio it held back waiting for a missing packet (after a pause or seek on the phone);
        # (every, 0.1) is a Wi-Fi stall.
        self.audio_burst = audio_burst
        # (every, gap) seconds: the phone seeks. MirrorDesk's UxPlay reports the FLUSH on stdout, no sound
        # comes for `gap`, then the phone sends the new sound's first 0.17 s at once and the rest in real time.
        self.audio_seek = audio_seek
        # (every, stall) seconds: Wi-Fi holds the video back for `stall`, then delivers it at once (a user's
        # iPhone: every viewer froze every 15-20 s). Frames keep the phone's own timestamps, as MirrorDesk's
        # UxPlay sends them.
        self.video_burst = video_burst
        self.live = live_units(width, height, fps, profile, preset) if endless_gop else None
        self.units = encode_clip(width, height, frames, fps, gop or frames, preset, profile)
        self.audio = audio and audio_port is not None
        self.stop_event = threading.Event()
        self.threads = []
        self.frames_sent = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def switch_clip(self, width, height, frames=None, profile='high'):
        """Simulate rotation: new SPS/PPS + IDR at a new size."""
        self.units = encode_clip(width, height, frames or len(self.units), self.fps, None, 'superfast', profile)

    def _video(self):
        writer = RtpWriter(96)
        start = time.monotonic()
        index = 0
        while not self.stop_event.is_set():
            if self.still_after and time.monotonic() - start >= self.still_after:
                self.stop_event.wait()
                return
            units = self.units
            unit = next(self.live) if self.live is not None else units[index % len(units)]
            timestamp = int(index * 90000 / self.fps)
            for packet in packetize_h264(writer, unit, timestamp):
                self.sock.sendto(packet, (self.address, self.video_port))
            self.frames_sent += 1
            index += 1
            due = index / self.fps
            if self.video_burst:
                every, stall = self.video_burst
                if due % every >= every - stall:      # held back: sent at the end of the stall
                    due = (due // every + 1) * every
            delay = start + due - time.monotonic()
            if delay > 0:
                self.stop_event.wait(delay)

    def _audio(self):
        writer = RtpWriter(97)
        start = time.monotonic()
        sent = ahead = 0
        last_seek = start
        while not self.stop_event.is_set():
            if self.audio_seek and time.monotonic() - last_seek >= self.audio_seek[0]:
                print('MirrorDesk: audio flush', flush=True)
                self.stop_event.wait(self.audio_seek[1])
                # The new sound keeps time from now, and its first 0.17 s goes at once: the phone may
                # send that far ahead of when it is to be heard.
                last_seek = time.monotonic()
                start = last_seek - sent / 44100
                ahead = sent + int(0.17 * 44100)
                continue
            if self.audio_burst:
                every, pause = self.audio_burst
                into = (time.monotonic() - start) % every
                if into >= every - pause:          # held back: nothing now, all of it at the end
                    self.stop_event.wait(every - into)
                    continue
            self.sock.sendto(l16_packet(writer, sent, samples=self.audio_samples, phase=sent),
                             (self.address, self.audio_port))
            sent += self.audio_samples
            if sent < ahead:
                continue                           # the new sound's first 0.17 s: at once
            delay = start + sent / 44100 - time.monotonic()
            if delay > 0:
                self.stop_event.wait(delay)

    def start(self):
        self.threads = [threading.Thread(target=self._video, daemon=True)]
        if self.audio:
            self.threads.append(threading.Thread(target=self._audio, daemon=True))
        for thread in self.threads:
            thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.sock.close()
