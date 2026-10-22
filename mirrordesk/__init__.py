"""MirrorDesk browser-first server (Prototype 7).

One process owns the AirPlay receiver (UxPlay), ingests its loopback RTP
output and serves every control and viewer surface over HTTP/WebRTC, so the
whole product can be used from any browser and run headless in a container.
"""
import hashlib
import os
from pathlib import Path

__version__ = '0.13.0'
# Which copy of MirrorDesk this is (a hash of its folder, not the path): a second
# launch must not mistake a copy in another folder for itself.
INSTALL_ID = hashlib.sha256(os.path.normcase(str(Path(__file__).resolve().parents[1])).encode('utf-8')).hexdigest()[:12]
