"""A minimal usbmuxd client: reach a service on an iPhone that usbmuxd owns (Linux).

On Linux, usbmuxd claims the phone's usbmux interface as soon as it is plugged in, so
MirrorDesk cannot speak usbmux to the phone itself (claiming the interface fails with
"resource busy"). For the trusted lockdown session that cable mirroring needs, it asks
usbmuxd for a connection to the phone's lockdown service instead, and claims only the
screen-capture interface, which usbmuxd leaves alone.

Protocol (libimobiledevice's usbmuxd, plist messages): a 16-byte little-endian header
(total length, version 1, message type 8 = plist, tag) and an XML property list. After
a successful Connect the socket is a plain byte stream to the phone's port.
USBMUXD_SOCKET_ADDRESS overrides the socket like libimobiledevice does ("UNIX:/path"
or "host:port"); the tests use it to run a stand-in usbmuxd over TCP.
"""
import os
import plistlib
import socket
import struct
import time

from . import lockdown

DEFAULT_PATH = '/var/run/usbmuxd'
VERSION_PLIST = 1
MESSAGE_PLIST = 8
HEADER = struct.Struct('<IIII')
RESULT_TEXT = {1: 'bad command', 2: 'the phone is not connected', 3: 'the phone refused the connection',
               6: 'bad version'}


class UsbmuxdError(RuntimeError):
    """A user-facing reason usbmuxd could not connect to the phone."""


def address():
    """('unix', path) or ('tcp', (host, port))."""
    configured = os.environ.get('USBMUXD_SOCKET_ADDRESS', '').strip()
    if configured:
        if configured.upper().startswith('UNIX:'):
            return 'unix', configured[5:]
        host, _, port = configured.rpartition(':')
        try:
            return 'tcp', (host or '127.0.0.1', int(port))
        except ValueError:
            pass
    return 'unix', DEFAULT_PATH


def available():
    """Whether a usbmuxd to ask is configured or running (its socket exists)."""
    kind, where = address()
    return kind == 'tcp' or os.path.exists(where)


def normalize_serial(serial):
    """USB serial numbers have no dash; usbmuxd writes newer UDIDs as 00008030-001A..."""
    return ''.join(c for c in (serial or '') if c.isalnum()).upper()


def _open(timeout):
    kind, where = address()
    if kind == 'unix':
        if not hasattr(socket, 'AF_UNIX'):
            raise UsbmuxdError('usbmuxd is not available on this computer.')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(where)
    except OSError as exc:
        sock.close()
        raise UsbmuxdError(f'usbmuxd is not reachable ({exc.strerror or exc}).') from exc
    return sock


def _recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        try:
            chunk = sock.recv(size - len(data))
        except socket.timeout as exc:
            raise UsbmuxdError('usbmuxd did not answer.') from exc
        except OSError as exc:
            raise UsbmuxdError(f'usbmuxd closed the connection ({exc.strerror or exc}).') from exc
        if not chunk:
            raise UsbmuxdError('usbmuxd closed the connection.')
        data += chunk
    return bytes(data)


class Client:
    """One usbmuxd connection: requests, then (after Connect) a stream to the phone."""

    def __init__(self, timeout=5.0):
        self.sock = _open(timeout)
        self.tag = 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def request(self, message_type, **fields):
        self.tag += 1
        body = plistlib.dumps({'MessageType': message_type, 'ClientVersionString': 'MirrorDesk',
                               'ProgName': 'MirrorDesk', 'kLibUSBMuxVersion': 3, **fields})
        try:
            self.sock.sendall(HEADER.pack(HEADER.size + len(body), VERSION_PLIST, MESSAGE_PLIST, self.tag) + body)
        except OSError as exc:
            raise UsbmuxdError(f'usbmuxd closed the connection ({exc.strerror or exc}).') from exc
        length, version, message, _ = HEADER.unpack(_recv_exact(self.sock, HEADER.size))
        if version != VERSION_PLIST or message != MESSAGE_PLIST or not HEADER.size <= length <= 1 << 20:
            raise UsbmuxdError('usbmuxd sent an unexpected reply.')
        try:
            reply = plistlib.loads(_recv_exact(self.sock, length - HEADER.size))
        except (ValueError, plistlib.InvalidFileException) as exc:
            raise UsbmuxdError('usbmuxd sent an unreadable reply.') from exc
        if not isinstance(reply, dict):
            raise UsbmuxdError('usbmuxd sent an unexpected reply.')
        return reply

    def devices(self):
        """[(device id, properties)] of the phones usbmuxd sees."""
        reply = self.request('ListDevices')
        found = []
        for entry in reply.get('DeviceList') or []:
            properties = entry.get('Properties') or {}
            device_id = entry.get('DeviceID', properties.get('DeviceID'))
            if isinstance(device_id, int):
                found.append((device_id, properties))
        return found

    def connect(self, device_id, port):
        """Turn this connection into a byte stream to `port` on the phone."""
        reply = self.request('Connect', DeviceID=device_id, PortNumber=socket.htons(port))
        number = reply.get('Number')
        if number != 0:
            raise UsbmuxdError(f'usbmuxd could not reach the phone ({RESULT_TEXT.get(number, number)}).')
        return self.sock


class StreamConnection:
    """lockdown.MuxConnection's interface (send / recv_exact / recv_some / close) on a socket."""

    def __init__(self, sock):
        self.sock = sock
        self.inbox = bytearray()
        self.closed = False

    def send(self, data):
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise lockdown.LockdownError('The phone closed the connection.') from exc

    def _fill(self, deadline):
        if self.closed:
            raise lockdown.LockdownError('The phone closed the connection.')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise lockdown.Timeout()
        self.sock.settimeout(remaining)
        try:
            data = self.sock.recv(65536)
        except socket.timeout as exc:
            raise lockdown.Timeout() from exc
        except OSError as exc:
            self.closed = True
            raise lockdown.LockdownError('The phone closed the connection.') from exc
        if not data:
            self.closed = True
            raise lockdown.LockdownError('The phone closed the connection.')
        self.inbox += data

    def recv_exact(self, size, timeout=10.0):
        deadline = time.monotonic() + timeout
        while len(self.inbox) < size:
            self._fill(deadline)
        out = bytes(self.inbox[:size])
        del self.inbox[:size]
        return out

    def recv_some(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not self.inbox:
            self._fill(deadline)
        data = bytes(self.inbox)
        self.inbox.clear()
        return data

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.sock.close()
            except OSError:
                pass


class DeviceLink:
    """What lockdown.trust() needs from a mux (connect(port) -> connection), through usbmuxd."""

    def __init__(self, serial, timeout=5.0):
        self.timeout = timeout
        wanted = normalize_serial(serial)
        client = Client(timeout)
        try:
            devices = client.devices()
        finally:
            client.close()
        self.device_id = next((device_id for device_id, properties in devices
                               if normalize_serial(properties.get('SerialNumber')) == wanted), None)
        if self.device_id is None:
            raise UsbmuxdError('usbmuxd does not see this iPhone yet. Unlock it, then unplug it and plug it in '
                               'again.')

    def connect(self, port, timeout=5.0):
        client = Client(timeout)
        try:
            return StreamConnection(client.connect(self.device_id, port))
        except Exception:
            client.close()
            raise
