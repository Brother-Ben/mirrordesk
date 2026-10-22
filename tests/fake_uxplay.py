"""Stand-in for the UxPlay executable in automated tests (never shipped).

Accepts the arguments MirrorDesk passes, prints UxPlay-like log lines and,
after a short delay, streams synthetic H.264/L16 RTP to the -vrtp/-artp ports
at the height requested with -s (portrait iPhone aspect by default).

Environment knobs: FAKE_UXPLAY_ORIENTATION=portrait|landscape,
FAKE_UXPLAY_CONNECT_DELAY (s), FAKE_UXPLAY_EXIT_AFTER (s, simulate a crash),
FAKE_UXPLAY_NO_MEDIA=1, FAKE_UXPLAY_PROFILE=high|main|baseline,
FAKE_UXPLAY_AUDIO_SAMPLES (samples per audio packet; 4096 = iPhone USB-sized blocks),
FAKE_UXPLAY_STILL_AFTER (s; video stops like a still iPhone screen: keep it under the
2 s clip so the phone's single keyframe is never repeated),
FAKE_UXPLAY_ENDLESS_GOP=1 (one keyframe, then P-frames only, encoded live: an iPhone
mirroring a video), FAKE_UXPLAY_AUDIO_BURST="every,pause" (s; the sound stops for `pause`,
then all of it arrives at once, like UxPlay after a pause or seek on the phone),
FAKE_UXPLAY_STOP_AFTER (s after connecting: the phone taps Stop Mirroring),
FAKE_UXPLAY_AUDIO_SEEK="every,gap" (s; the phone seeks: "MirrorDesk: audio flush" on stdout, no sound
for `gap`, then the new sound's first 0.17 s at once and the rest in real time),
FAKE_UXPLAY_SIZE=WxH (exact stream size, e.g. 1179x2556 like an iPhone's USB stream; default:
bounded by -s), FAKE_UXPLAY_GOP (frames between keyframes; default: one per clip loop),
FAKE_UXPLAY_VIDEO_BURST="every,stall" (s; Wi-Fi holds the video back for `stall`, then delivers it
at once; frames keep their own timestamps, as MirrorDesk's UxPlay change sends them).
"""
import os
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = """UxPlay 1.74: An open-source AirPlay mirroring server.
Options:
-n name   Specify network name of the AirPlay server (UTF-8/ascii)
-nh       Do not add "@hostname" at the end of AirPlay server name
-h265     Support h265 (4K) video (with h265 versions of h264 plugins)
-pin[xxxx]Use a 4-digit pin code to control client access (default: no)
-s wxh[@r]Request to client for video display resolution [refresh_rate]
-vrtp pl  Use rtph26[4,5]pay to send decoded video elsewhere: "pl"
-artp pl  Use rtpL16pay to send decoded audio elsewhere: "pl"
-key [fn] Store private key in $HOME/.uxplay.pem (or in file "fn")
-reg [fn] Keep a register in $HOME/.uxplay.register to verify returning
-nohold   Drop current connection when new client connects.
-fps n    Set maximum allowed streaming framerate, default 30
"""


def option(args, name):
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return None


def main(args):
    if '-h' in args:
        print(HELP, flush=True)
        return 0
    if '-pin' not in args:
        print('fake uxplay: refusing to run without -pin', flush=True)
        return 2
    name = option(args, '-n') or 'UxPlay'
    size = option(args, '-s') or '1920x1080@60'
    match = re.match(r'(\d+)x(\d+)(?:@(\d+))?', size)
    req_w, req_h = int(match.group(1)), int(match.group(2))
    fps = int(option(args, '-fps') or match.group(3) or 30)
    video_port = int(re.search(r'port=(\d+)', option(args, '-vrtp') or 'port=5004').group(1))
    artp = option(args, '-artp')
    audio_port = int(re.search(r'port=(\d+)', artp).group(1)) if artp else None
    print(f'UxPlay 1.74: Initialized server socket(s); name "{name}"', flush=True)
    print('Using the built-in mDNS responder (fake)', flush=True)
    running = [True]
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, lambda *_: running.__setitem__(0, False))
    connect_delay = float(os.environ.get('FAKE_UXPLAY_CONNECT_DELAY', '1.0'))
    exit_after = float(os.environ.get('FAKE_UXPLAY_EXIT_AFTER', '0') or 0)
    stop_after = float(os.environ.get('FAKE_UXPLAY_STOP_AFTER', '0') or 0)
    burst = os.environ.get('FAKE_UXPLAY_AUDIO_BURST')
    seek = os.environ.get('FAKE_UXPLAY_AUDIO_SEEK')
    video_burst = os.environ.get('FAKE_UXPLAY_VIDEO_BURST')
    started = time.monotonic()
    phone = None
    connected = stopped = False
    while running[0]:
        elapsed = time.monotonic() - started
        if exit_after and elapsed >= exit_after:
            print('*** ERROR fake crash', flush=True)
            return 3
        if connected and not stopped and stop_after and elapsed >= connect_delay + stop_after:
            if phone is not None:
                phone.stop()       # Stop Mirroring on the phone: UxPlay's mirror thread ends
            phone, stopped = None, True
            print('raop_rtp_mirror->running is no longer true', flush=True)
            print('Connection closed on socket 31', flush=True)
        if not connected and elapsed >= connect_delay:
            connected = True
            print('connection request from Test iPhone (iPhone15,2) with deviceID = 00:11:22:33:44:55', flush=True)
            if os.environ.get('FAKE_UXPLAY_NO_MEDIA') != '1':
                from synthetic_media import SyntheticPhone
                # iPhone 15 screen 1179x2556: the requested HEIGHT bounds the stream.
                if os.environ.get('FAKE_UXPLAY_ORIENTATION', 'portrait') == 'portrait':
                    height = min(req_h, 1080)
                    width = int(height * 1179 / 2556) // 2 * 2
                else:
                    width = min(req_w, 1920)
                    height = int(width * 1179 / 2556) // 2 * 2
                if os.environ.get('FAKE_UXPLAY_SIZE'):
                    width, height = (int(v) for v in os.environ['FAKE_UXPLAY_SIZE'].split('x'))
                phone = SyntheticPhone(video_port, audio_port, width=width, height=height, fps=min(fps, 60),
                                       frames=min(fps, 60) * 2, gop=int(os.environ.get('FAKE_UXPLAY_GOP') or 0) or None,
                                       profile=os.environ.get('FAKE_UXPLAY_PROFILE', 'high'),
                                       audio=audio_port is not None,
                                       audio_samples=int(os.environ.get('FAKE_UXPLAY_AUDIO_SAMPLES', '352')),
                                       still_after=float(os.environ.get('FAKE_UXPLAY_STILL_AFTER', '0')),
                                       endless_gop=os.environ.get('FAKE_UXPLAY_ENDLESS_GOP') == '1',
                                       audio_burst=tuple(float(v) for v in burst.split(',')) if burst else None,
                                       audio_seek=tuple(float(v) for v in seek.split(',')) if seek else None,
                                       video_burst=tuple(float(v) for v in video_burst.split(',')) if video_burst else None
                                       ).start()
                print(f'fake stream {width}x{height}@{min(fps, 60)}', flush=True)
        time.sleep(0.1)
    if phone is not None:
        phone.stop()
    print('Stopping RAOP Server...', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
