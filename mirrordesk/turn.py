"""TURN over TCP on MirrorDesk's web port: a way in for viewers whose direct connection fails.

A browser that can open MirrorDesk's page can reach that TCP port, but not always the UDP ports
WebRTC uses: a firewall, a VPN, or a route that carries the page but not the video (seen on a
user's ZimaOS box: friends opened the page through its Tailscale address and never got a
picture). Pages therefore list MirrorDesk itself as a TURN server (turn:<the page's host>:<the
page's port>?transport=tcp). A browser uses it only when its direct checks fail; its media then
travels inside that TCP connection, and MirrorDesk hands it to its own WebRTC sockets.

An RFC 8656 subset over TCP: Allocate (UDP), Refresh, CreatePermission, ChannelBind, Send and
Data indications, ChannelData, and Binding; long-term credentials, given to signed-in pages
only. The relay forwards only to the local ICE sockets of MirrorDesk's own viewers, never to
another program on this computer or into the network.
"""
import asyncio
import hashlib
import logging
import secrets
import struct
import time

from aioice import stun

log = logging.getLogger('mirrordesk.turn')

# aioice's attribute table lacks DATA (its own TURN client only uses channels).
if 'DATA' not in stun.ATTRIBUTES_BY_NAME:
    _DATA = (0x0013, 'DATA', stun.pack_bytes, stun.unpack_bytes)
    stun.ATTRIBUTES_BY_TYPE[_DATA[0]] = _DATA
    stun.ATTRIBUTES_BY_NAME[_DATA[1]] = _DATA

USERNAME = 'mirrordesk'
REALM = 'mirrordesk'
LIFETIME = 600                  # seconds an allocation lives without a Refresh
PERMISSION_SECONDS = 300
FIRST_DATA_SECONDS = 30         # a connection that sends nothing is closed
MAX_BUFFERED = 1024 * 1024      # a browser that cannot keep up loses media, not memory
PEERS_REFRESH_SECONDS = 0.2
UDP = 17


class TurnRelay:
    """The relay's credentials, and which addresses it may forward to.

    peers(): (address, port) of every local ICE socket of the current viewers.
    relay_host(): the address of this computer that relayed sockets are announced at (one
    MirrorDesk's own ICE sockets can reach)."""

    def __init__(self, peers, relay_host):
        self.peers = peers
        self.relay_host = relay_host
        self.password = secrets.token_urlsafe(18)
        self.key = hashlib.md5(f'{USERNAME}:{REALM}:{self.password}'.encode()).digest()
        self.allocations = 0          # since the start, for diagnostics
        self.relayed_bytes = 0
        self.connections = set()
        self._known = frozenset()
        self._known_at = -1e9

    def credentials(self):
        return {'username': USERNAME, 'credential': self.password}

    def protocol(self):
        return TurnConnection(self)

    def _refresh(self, force=False):
        now = time.monotonic()
        if force or now - self._known_at >= PEERS_REFRESH_SECONDS:
            try:
                self._known = frozenset(tuple(p) for p in self.peers())
            except Exception:   # noqa: BLE001 - a viewer closing meanwhile must not break the relay
                log.debug('could not list ICE sockets', exc_info=True)
            self._known_at = now
        return self._known

    def allowed(self, addr):
        """May data go to this (address, port)? Only to a viewer's own ICE socket."""
        addr = (addr[0], addr[1])
        return addr in self._known or addr in self._refresh()

    def allowed_address(self, address):
        return any(a == address for a, _ in self._known) or any(a == address for a, _ in self._refresh())

    def close(self):
        for connection in list(self.connections):
            connection.close()


class _Relayed(asyncio.DatagramProtocol):
    def __init__(self, connection):
        self.connection = connection

    def datagram_received(self, data, addr):
        self.connection.from_peer(data, addr)

    def error_received(self, exc):   # e.g. Windows' ICMP port-unreachable echoes
        pass


class TurnConnection(asyncio.Protocol):
    """One browser's TCP connection: at most one allocation (its relayed UDP socket)."""

    def __init__(self, relay):
        self.relay = relay
        self.transport = None
        self.buffer = bytearray()
        self.client = None             # the browser's address, as seen here
        self.nonce = secrets.token_hex(12).encode()
        self.udp = None
        self.relayed = None            # (address, port) announced for the relayed socket
        self.allocating = False
        self.task = None               # the Allocate in progress (opening the relayed socket)
        self.expiry = None
        self.permissions = {}          # address -> expiry (monotonic)
        self.channels = {}             # channel number -> (address, port)
        self.numbers = {}              # (address, port) -> channel number
        self.idle = None

    # -- connection ------------------------------------------------------------
    def connection_made(self, transport):
        self.transport = transport
        peer = transport.get_extra_info('peername')
        self.client = (peer[0], peer[1]) if peer else ('0.0.0.0', 0)
        self.relay.connections.add(self)
        self.idle = asyncio.get_running_loop().call_later(FIRST_DATA_SECONDS, self.close)

    def connection_lost(self, exc):
        self.relay.connections.discard(self)
        self._release()
        if self.idle is not None:
            self.idle.cancel()

    def close(self):
        if self.transport is not None:
            self.transport.close()

    def _release(self):
        if self.udp is not None:
            self.udp.close()
            self.udp = None
        self.permissions.clear()
        self.channels.clear()
        self.numbers.clear()

    def data_received(self, data):
        if self.idle is not None:
            self.idle.cancel()
            self.idle = None
        self.buffer += data
        while len(self.buffer) >= 4:
            first = self.buffer[0]
            if first < 0x40:                           # a STUN message
                if len(self.buffer) < stun.HEADER_LENGTH:
                    return
                total = stun.HEADER_LENGTH + struct.unpack_from('!H', self.buffer, 2)[0]
                if len(self.buffer) < total:
                    return
                frame = bytes(self.buffer[:total])
                del self.buffer[:total]
                self._message(frame)
            elif first < 0x80:                         # ChannelData, padded to 4 bytes over TCP
                number, length = struct.unpack_from('!HH', self.buffer, 0)
                total = 4 + length + (-length % 4)
                if len(self.buffer) < total:
                    return
                payload = bytes(self.buffer[4:4 + length])
                del self.buffer[:total]
                peer = self.channels.get(number)
                if peer is not None:
                    self._to_peer(payload, peer)
            else:
                self.close()                           # not TURN
                return

    # -- messages from the browser ----------------------------------------------
    def _write(self, data):
        if self.transport is not None and not self.transport.is_closing():
            self.transport.write(data)

    def _reply(self, request, attributes=None, error=None, sign=True):
        message = stun.Message(request.message_method, stun.Class.ERROR if error else stun.Class.RESPONSE,
                               transaction_id=request.transaction_id, attributes=dict(attributes or {}))
        if error:
            message.attributes['ERROR-CODE'] = error
        if sign:
            message.add_message_integrity(self.relay.key)
        if log.isEnabledFor(logging.DEBUG):
            log.debug('-> %s %s %s', request.message_method.name, message.message_class.name, error or '')
        self._write(bytes(message))

    def _challenge(self, request, code=(401, 'Unauthorized')):
        self._reply(request, {'REALM': REALM, 'NONCE': self.nonce}, error=code, sign=False)

    def _authenticated(self, request, frame):
        """Long-term credentials (RFC 8489 9.2); a wrong or missing one gets the challenge."""
        attributes = request.attributes
        if 'MESSAGE-INTEGRITY' not in attributes or attributes.get('USERNAME') != USERNAME \
                or attributes.get('REALM') != REALM:
            self._challenge(request)
            return False
        if attributes.get('NONCE') != self.nonce:
            self._challenge(request, (438, 'Stale Nonce'))
            return False
        try:
            stun.parse_message(frame, integrity_key=self.relay.key)
        except ValueError:
            self._challenge(request)
            return False
        return True

    def _message(self, frame):
        try:
            message = stun.parse_message(frame)
        except ValueError:
            return                                     # unknown method, or not STUN after all
        method, kind = message.message_method, message.message_class
        if log.isEnabledFor(logging.DEBUG) and method != stun.Method.SEND:
            log.debug('%s %s %s', method.name, kind.name, sorted(message.attributes))
        if kind == stun.Class.INDICATION:
            if method == stun.Method.SEND:
                peer = message.attributes.get('XOR-PEER-ADDRESS')
                data = message.attributes.get('DATA')
                if peer is not None and data is not None and self._permitted(peer[0]):
                    self._to_peer(data, peer)
            return
        if kind != stun.Class.REQUEST:
            return
        if method == stun.Method.BINDING:
            self._reply(message, {'XOR-MAPPED-ADDRESS': self.client}, sign=False)
            return
        if not self._authenticated(message, frame):
            return
        if method == stun.Method.ALLOCATE:
            self.task = asyncio.ensure_future(self._allocate(message))
        elif method == stun.Method.REFRESH:
            self._refresh(message)
        elif method == stun.Method.CREATE_PERMISSION:
            self._create_permission(message)
        elif method == stun.Method.CHANNEL_BIND:
            self._channel_bind(message)
        else:
            self._reply(message, error=(400, 'Bad Request'))

    async def _allocate(self, request):
        if self.udp is not None or self.allocating:
            self._reply(request, error=(437, 'Allocation Mismatch'))
            return
        if request.attributes.get('REQUESTED-TRANSPORT', 0) >> 24 != UDP:
            self._reply(request, error=(442, 'Unsupported Transport Protocol'))
            return
        self.allocating = True
        try:
            loop = asyncio.get_running_loop()
            self.udp, _ = await loop.create_datagram_endpoint(lambda: _Relayed(self), local_addr=('0.0.0.0', 0))
        except OSError:
            self._reply(request, error=(508, 'Insufficient Capacity'))
            return
        finally:
            self.allocating = False
        if self.transport is None or self.transport.is_closing():
            self._release()
            return
        port = self.udp.get_extra_info('sockname')[1]
        self.relayed = (self.relay.relay_host(), port)
        self.expiry = time.monotonic() + LIFETIME
        self.relay.allocations += 1
        self._reply(request, {'XOR-RELAYED-ADDRESS': self.relayed, 'XOR-MAPPED-ADDRESS': self.client,
                              'LIFETIME': LIFETIME})

    def _refresh(self, request):
        if self.udp is None:
            self._reply(request, error=(437, 'Allocation Mismatch'))
            return
        lifetime = min(request.attributes.get('LIFETIME', LIFETIME), 3600)
        if lifetime == 0:
            self._release()
        else:
            self.expiry = time.monotonic() + lifetime
        self._reply(request, {'LIFETIME': lifetime})

    def _create_permission(self, request):
        peer = request.attributes.get('XOR-PEER-ADDRESS')
        if self.udp is None:
            self._reply(request, error=(437, 'Allocation Mismatch'))
        elif peer is None:
            self._reply(request, error=(400, 'Bad Request'))
        elif not self.relay.allowed_address(peer[0]):
            self._reply(request, error=(403, 'Forbidden'))
        else:
            self.permissions[peer[0]] = time.monotonic() + PERMISSION_SECONDS
            self._reply(request)

    def _channel_bind(self, request):
        number = request.attributes.get('CHANNEL-NUMBER')
        peer = request.attributes.get('XOR-PEER-ADDRESS')
        if self.udp is None:
            self._reply(request, error=(437, 'Allocation Mismatch'))
            return
        if number is None or peer is None or not 0x4000 <= number <= 0x7FFE \
                or self.channels.get(number, peer) != peer or self.numbers.get(peer, number) != number:
            self._reply(request, error=(400, 'Bad Request'))
            return
        if not self.relay.allowed(peer):
            self._reply(request, error=(403, 'Forbidden'))
            return
        self.channels[number] = peer
        self.numbers[peer] = number
        self.permissions[peer[0]] = time.monotonic() + PERMISSION_SECONDS
        self._reply(request)

    # -- relaying -------------------------------------------------------------------
    def _permitted(self, address):
        expiry = self.permissions.get(address)
        return expiry is not None and expiry > time.monotonic()

    def _alive(self):
        if self.udp is None:
            return False
        if self.expiry is not None and time.monotonic() > self.expiry:
            self._release()
            return False
        return True

    def _to_peer(self, data, peer):
        """Browser -> MirrorDesk's ICE socket."""
        if self._alive() and self.relay.allowed(peer):
            self.udp.sendto(data, (peer[0], peer[1]))
            self.relay.relayed_bytes += len(data)

    def from_peer(self, data, addr):
        """MirrorDesk's ICE socket -> the browser (a channel when one is bound)."""
        if not self._alive() or not self._permitted(addr[0]) or self.transport is None:
            return
        if self.transport.get_write_buffer_size() > MAX_BUFFERED:
            return                                     # real-time media: late is as good as lost
        peer = (addr[0], addr[1])
        number = self.numbers.get(peer)
        if number is not None:
            self._write(struct.pack('!HH', number, len(data)) + data + bytes(-len(data) % 4))
        else:
            message = stun.Message(stun.Method.DATA, stun.Class.INDICATION,
                                   attributes={'XOR-PEER-ADDRESS': peer, 'DATA': data})
            self._write(bytes(message))
        self.relay.relayed_bytes += len(data)
