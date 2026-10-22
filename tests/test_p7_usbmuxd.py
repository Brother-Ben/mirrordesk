"""usbmuxd client (Linux cable mirroring next to usbmuxd) against a stand-in usbmuxd.

The stand-in speaks usbmuxd's plist protocol over TCP (USBMUXD_SOCKET_ADDRESS, as
libimobiledevice allows) and, after Connect, the phone's lockdown framing.
"""
import plistlib
import socket
import struct
import threading

import pytest

from mirrordesk import lockdown, usbmuxd

HEADER = struct.Struct('<IIII')


def read_exact(conn, size):
    data = b''
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError
        data += chunk
    return data


class FakeUsbmuxd:
    def __init__(self, serial='00008030-001A35E90123402E', refuse=False):
        self.serial = serial
        self.refuse = refuse
        self.requests = []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _reply(self, conn, tag, payload):
        body = plistlib.dumps(payload)
        conn.sendall(HEADER.pack(HEADER.size + len(body), 1, 8, tag) + body)

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        with conn:
            try:
                while True:
                    length, version, message, tag = HEADER.unpack(read_exact(conn, HEADER.size))
                    request = plistlib.loads(read_exact(conn, length - HEADER.size))
                    self.requests.append(request)
                    assert (version, message) == (1, 8) and request['ProgName'] == 'MirrorDesk'
                    if request['MessageType'] == 'ListDevices':
                        self._reply(conn, tag, {'DeviceList': [{'DeviceID': 7, 'MessageType': 'Attached', 'Properties': {
                            'ConnectionType': 'USB', 'DeviceID': 7, 'SerialNumber': self.serial}}]})
                    elif request['MessageType'] == 'Connect':
                        ok = not self.refuse and request['DeviceID'] == 7 and request['PortNumber'] == socket.htons(62078)
                        self._reply(conn, tag, {'MessageType': 'Result', 'Number': 0 if ok else 3})
                        if ok:
                            self._lockdown(conn)
                        return
            except ConnectionError:
                return

    @staticmethod
    def _lockdown(conn):
        """The phone's lockdown service: length-prefixed property lists."""
        while True:
            try:
                length = struct.unpack('>I', read_exact(conn, 4))[0]
            except ConnectionError:
                return
            request = plistlib.loads(read_exact(conn, length))
            reply = plistlib.dumps({'Request': request['Request'], 'Type': 'com.apple.mobile.lockdown'})
            conn.sendall(struct.pack('>I', len(reply)) + reply)

    def close(self):
        self.sock.close()


@pytest.fixture
def fake(monkeypatch):
    server = FakeUsbmuxd()
    monkeypatch.setenv('USBMUXD_SOCKET_ADDRESS', f'127.0.0.1:{server.port}')
    yield server
    server.close()


def test_lockdown_request_through_usbmuxd(fake):
    # The USB serial has no dash; usbmuxd writes newer UDIDs with one.
    link = usbmuxd.DeviceLink('00008030001A35E90123402E')
    assert link.device_id == 7 and usbmuxd.available()
    conn = link.connect(lockdown.LOCKDOWN_PORT)
    try:
        reply = lockdown.Lockdown(conn).request({'Label': 'MirrorDesk', 'Request': 'QueryType'})
    finally:
        conn.close()
    assert reply == {'Request': 'QueryType', 'Type': 'com.apple.mobile.lockdown'}
    assert [r['MessageType'] for r in fake.requests] == ['ListDevices', 'Connect']


def test_unknown_phone_and_refused_connection_are_explained(fake, monkeypatch):
    with pytest.raises(usbmuxd.UsbmuxdError, match='does not see this iPhone'):
        usbmuxd.DeviceLink('SOMEOTHERPHONE')
    fake.refuse = True
    link = usbmuxd.DeviceLink('00008030-001A35E90123402E')
    with pytest.raises(usbmuxd.UsbmuxdError, match='refused the connection'):
        link.connect(lockdown.LOCKDOWN_PORT)


def test_usbmuxd_not_running(monkeypatch):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]       # nothing listens here once closed
    monkeypatch.setenv('USBMUXD_SOCKET_ADDRESS', f'127.0.0.1:{port}')
    with pytest.raises(usbmuxd.UsbmuxdError, match='not reachable'):
        usbmuxd.Client(timeout=1)
    monkeypatch.setenv('USBMUXD_SOCKET_ADDRESS', 'UNIX:/nonexistent/usbmuxd')
    assert usbmuxd.available() is False
