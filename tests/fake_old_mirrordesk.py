"""Stand-in for a MirrorDesk from before the single-instance check (or the P6
backend): serves the old `{"ok": true}` health + a MirrorDesk page on the HTTP
port and holds the loopback RTP ports, until killed.

usage: python fake_old_mirrordesk.py HTTP_PORT VIDEO_PORT AUDIO_PORT
"""
import socket
import sys

from aiohttp import web

http_port, video_port, audio_port = (int(v) for v in sys.argv[1:4])
held = []
for port in (video_port, audio_port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', port))
    held.append(s)



async def health(request):
    return web.json_response({'ok': True})


async def index(request):
    return web.Response(text='<title>MirrorDesk</title>', content_type='text/html')

app = web.Application()
app.router.add_get('/api/health', health)
app.router.add_get('/', index)
web.run_app(app, host='0.0.0.0', port=http_port, print=None)
