"""MirrorDesk's TURN relay on its web port (mirrordesk/turn.py, __main__.SharedPortSite).

Viewers whose direct (UDP) connection fails get the video through the connection the page itself
came over (seen on a user's ZimaOS box: friends opened the page through Tailscale and never got a
picture). Driven by aioice's own TURN client and by hand-made browser-style messages.
"""
import asyncio
import struct

import aiohttp
from aiohttp import web
from aioice import stun
from aioice.turn import create_turn_endpoint
import pytest

from mirrordesk.__main__ import SharedPortSite
from mirrordesk.turn import REALM, USERNAME, TurnRelay

REQUESTED_UDP = 17 << 24


class Inbox(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.queue.put_nowait((data, addr))


async def ice_socket():
    """Stands in for one of a viewer's ICE sockets (aiortc's, in MirrorDesk)."""
    transport, inbox = await asyncio.get_running_loop().create_datagram_endpoint(Inbox, local_addr=('127.0.0.1', 0))
    return transport, inbox, transport.get_extra_info('sockname')


async def start_relay(peers):
    relay = TurnRelay(lambda: peers, lambda: '127.0.0.1')
    server = await asyncio.get_running_loop().create_server(relay.protocol, '127.0.0.1', 0)
    return relay, server, server.sockets[0].getsockname()[1]


async def test_a_browser_reaches_a_viewers_ice_socket_through_the_relay():
    ice, inbox, peer = await ice_socket()
    relay, server, port = await start_relay({peer})
    try:
        turn, client = await create_turn_endpoint(Inbox, ('127.0.0.1', port), USERNAME, relay.password, transport='tcp')
        relayed = turn.get_extra_info('sockname')
        assert relayed[0] == '127.0.0.1' and relay.allocations == 1
        turn.sendto(b'check from the browser', peer)                 # a channel is bound first
        data, addr = await asyncio.wait_for(inbox.queue.get(), 2)
        assert data == b'check from the browser' and addr[1] == relayed[1]
        ice.sendto(b'answer from MirrorDesk', addr)
        data, addr = await asyncio.wait_for(client.queue.get(), 2)
        assert data == b'answer from MirrorDesk' and addr == peer
        assert relay.relayed_bytes == len(b'check from the browser') + len(b'answer from MirrorDesk')
        turn.close()
    finally:
        relay.close()
        server.close()
        ice.close()


async def test_wrong_credentials_get_no_allocation():
    ice, _, peer = await ice_socket()
    relay, server, port = await start_relay({peer})
    try:
        with pytest.raises(stun.TransactionFailed, match='401'):
            await create_turn_endpoint(Inbox, ('127.0.0.1', port), USERNAME, 'not the password', transport='tcp')
        assert relay.allocations == 0
    finally:
        relay.close()
        server.close()
        ice.close()


async def test_the_relay_forwards_only_to_viewers_ice_sockets():
    """Never a way to other programs on this computer (such as MirrorDesk's own RTP ports)."""
    ice, inbox, peer = await ice_socket()
    other, other_inbox, elsewhere = await ice_socket()
    relay, server, port = await start_relay({peer})
    try:
        turn, _ = await create_turn_endpoint(Inbox, ('127.0.0.1', port), USERNAME, relay.password, transport='tcp')
        turn.sendto(b'not for you', elsewhere)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(other_inbox.queue.get(), 0.5)
        assert relay.allowed(peer) and not relay.allowed(elsewhere) and not relay.allowed(('10.0.0.5', peer[1]))
        assert relay.allowed_address('127.0.0.1') and not relay.allowed_address('10.0.0.5')
        turn.close()
    finally:
        relay.close()
        server.close()
        ice.close()
        other.close()


async def read_message(reader):
    header = await asyncio.wait_for(reader.readexactly(4), 2)
    if header[0] >= 0x40:                                  # ChannelData
        length = struct.unpack('!H', header[2:])[0]
        return 'channel', await reader.readexactly(length + (-length % 4))
    rest = await reader.readexactly(16 + struct.unpack('!H', header[2:])[0])
    return 'stun', stun.parse_message(header + rest)


async def test_browser_style_permission_and_send_indications():
    """Firefox's way: CreatePermission, then Send indications; the answer comes back as a Data
    indication (no channel)."""
    ice, inbox, peer = await ice_socket()
    relay, server, port = await start_relay({peer})
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
        writer.write(bytes(stun.Message(stun.Method.ALLOCATE, stun.Class.REQUEST,
                                        attributes={'REQUESTED-TRANSPORT': REQUESTED_UDP})))
        _, challenge = await read_message(reader)
        assert challenge.attributes['ERROR-CODE'][0] == 401 and challenge.attributes['REALM'] == REALM
        signed = {'USERNAME': USERNAME, 'REALM': REALM, 'NONCE': challenge.attributes['NONCE']}

        def request(method, **attributes):
            message = stun.Message(method, stun.Class.REQUEST, attributes={**attributes, **signed})
            message.add_message_integrity(relay.key)
            writer.write(bytes(message))

        request(stun.Method.ALLOCATE, **{'REQUESTED-TRANSPORT': REQUESTED_UDP})
        _, allocated = await read_message(reader)
        assert allocated.message_class == stun.Class.RESPONSE
        relayed = allocated.attributes['XOR-RELAYED-ADDRESS']
        request(stun.Method.CREATE_PERMISSION, **{'XOR-PEER-ADDRESS': ('10.0.0.5', 9)})
        _, refused = await read_message(reader)
        assert refused.attributes['ERROR-CODE'][0] == 403, 'not an address of this computer'
        request(stun.Method.CREATE_PERMISSION, **{'XOR-PEER-ADDRESS': peer})
        _, permitted = await read_message(reader)
        assert permitted.message_class == stun.Class.RESPONSE
        writer.write(bytes(stun.Message(stun.Method.SEND, stun.Class.INDICATION,
                                        attributes={'XOR-PEER-ADDRESS': peer, 'DATA': b'hello'})))
        data, addr = await asyncio.wait_for(inbox.queue.get(), 2)
        assert data == b'hello' and addr[1] == relayed[1]
        ice.sendto(b'world', addr)
        kind, indication = await read_message(reader)
        assert kind == 'stun' and indication.message_method == stun.Method.DATA
        assert indication.attributes['DATA'] == b'world' and indication.attributes['XOR-PEER-ADDRESS'] == peer
    finally:
        writer.close()
        relay.close()
        server.close()
        ice.close()


async def test_web_page_and_relay_share_one_port():
    """A viewer that can open the page can reach the relay: same port, told apart by the first byte."""
    ice, inbox, peer = await ice_socket()
    relay = TurnRelay(lambda: {peer}, lambda: '127.0.0.1')
    app = web.Application()

    async def page(request):
        return web.Response(text='MirrorDesk page')
    app.router.add_get('/', page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = SharedPortSite(runner, '127.0.0.1', 0, relay.protocol)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'http://127.0.0.1:{port}/') as response:
                assert response.status == 200 and await response.text() == 'MirrorDesk page'
        turn, client = await create_turn_endpoint(Inbox, ('127.0.0.1', port), USERNAME, relay.password, transport='tcp')
        turn.sendto(b'through the web port', peer)
        data, _ = await asyncio.wait_for(inbox.queue.get(), 2)
        assert data == b'through the web port'
        turn.close()
    finally:
        relay.close()
        await runner.cleanup()
        ice.close()
