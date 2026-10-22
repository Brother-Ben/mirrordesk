"""Pairing with an iPhone over USB ("Trust This Computer"), without Apple's software.

The phone only streams its screen to a computer it trusts. Normally Apple's
usbmux service (iTunes / Apple Devices) pairs it; with the libusb driver that
service cannot see the phone, so MirrorDesk does the same steps itself:

1. usbmux: the phone's multiplexing protocol on its USB interface (subclass
   0xFE): a version exchange, then TCP-like connections to services on the phone.
2. lockdown (service port 62078): length-prefixed property-list requests.
   ``Pair`` shows "Trust This Computer?" on the phone; the result is a pair
   record (certificates and IDs) saved under the data directory.
3. ``StartSession`` + TLS with the pair record, as macOS and Linux do every time
   a trusted phone is connected.

Pairing request, certificate layout and session flow follow go-ios
(Copyright (c) 2019 danielpaulus, MIT License, see THIRD_PARTY_NOTICES.md).
"""
import datetime
import logging
import os
from pathlib import Path
import plistlib
import ssl
import struct
import tempfile
import time
import uuid
import warnings

log = logging.getLogger('mirrordesk.lockdown')

LOCKDOWN_PORT = 62078
LABEL = 'MirrorDesk'

# usbmux packet types
MUX_VERSION, MUX_CONTROL, MUX_SETUP, MUX_TCP = 0, 1, 2, 6
MUX_MAGIC = 0xFEEDFACE
TCP_FIN, TCP_SYN, TCP_RST, TCP_PUSH, TCP_ACK = 0x01, 0x02, 0x04, 0x08, 0x10
WINDOW = 0x0200          # advertised receive window, in units of 256 bytes (128 KiB)
MAX_PAYLOAD = 16 * 1024


class LockdownError(RuntimeError):
    """A user-facing reason pairing or the session failed."""


class PairingPending(LockdownError):
    """The phone shows the Trust dialog (or is locked) and is waiting for the user."""


class Timeout(Exception):
    pass


# ------------------------------------------------------------------ usbmux
class MuxDevice:
    """usbmux over one pair of bulk endpoints.

    read(size, timeout_ms) returns bytes or raises Timeout; write(bytes) sends.
    Only one TCP connection is open at a time, which is all lockdown needs.
    """

    def __init__(self, read, write, packet=512):
        self._read = read
        self._write = write
        self.packet = packet
        self.version = 0
        self.tx_seq = 0
        self.rx_seq = 0xFFFF
        self.buffer = bytearray()
        self.next_port = 0x7000

    # framing
    def _header(self, protocol, length):
        if self.version >= 2:
            header = struct.pack('>IIIHH', protocol, length + 16, MUX_MAGIC, self.tx_seq, self.rx_seq)
            self.tx_seq = (self.tx_seq + 1) & 0xFFFF
            return header
        return struct.pack('>II', protocol, length + 8)

    def send(self, protocol, payload=b''):
        self._write(self._header(protocol, len(payload)) + payload)

    def receive(self, timeout=5.0):
        """One packet from the phone: (protocol, payload)."""
        deadline = time.monotonic() + timeout
        while True:
            if len(self.buffer) >= 8:
                protocol, length = struct.unpack_from('>II', self.buffer)
                if length < 8 or length > 1 << 20:
                    self.buffer.clear()
                    raise LockdownError(f'Bad usbmux packet (length {length}).')
                if len(self.buffer) >= length:
                    packet = bytes(self.buffer[:length])
                    del self.buffer[:length]
                    header = 16 if self.version >= 2 and protocol != MUX_VERSION else 8
                    if header == 16 and len(packet) >= 16:
                        self.rx_seq = struct.unpack_from('>H', packet, 12)[0]
                    return protocol, packet[header:]
                need = length - len(self.buffer)
            else:
                need = 8 - len(self.buffer)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Timeout()
            size = max(self.packet, -(-need // self.packet) * self.packet)
            try:
                self.buffer += self._read(min(size, 1 << 20), int(min(remaining, 1.0) * 1000) or 1)
            except Timeout:
                continue

    def handshake(self):
        self.send(MUX_VERSION, struct.pack('>III', 2, 0, 0))
        while True:
            protocol, payload = self.receive(timeout=5.0)
            if protocol == MUX_VERSION:
                break
        major = struct.unpack_from('>I', payload)[0] if len(payload) >= 4 else 0
        if major < 1:
            raise LockdownError('The phone did not answer the usbmux version exchange.')
        self.version = major
        if self.version >= 2:
            self.send(MUX_SETUP, b'\x07')
        log.debug('usbmux version %d', major)

    def connect(self, port, timeout=5.0):
        self.next_port = 0x7000 + ((self.next_port + 1) & 0x0FFF)
        conn = MuxConnection(self, self.next_port, port)
        conn.open(timeout)
        return conn


class MuxConnection:
    """A TCP-like stream to a service on the phone (sequence numbers, acks, no retransmits:
    USB is reliable)."""

    def __init__(self, mux, sport, dport):
        self.mux = mux
        self.sport, self.dport = sport, dport
        self.tx_seq = 0
        self.tx_ack = 0
        self.inbox = bytearray()
        self.closed = False

    def _tcp(self, flags, data=b''):
        header = struct.pack('>HHIIBBHHH', self.sport, self.dport, self.tx_seq, self.tx_ack,
                             0x50, flags, WINDOW, 0, 0)
        self.mux.send(MUX_TCP, header + data)

    def open(self, timeout):
        self._tcp(TCP_SYN)
        deadline = time.monotonic() + timeout
        while True:
            flags, data = self._packet(deadline)
            if flags & TCP_RST:
                raise LockdownError(f'The phone refused a connection to service port {self.dport}.')
            if flags & TCP_SYN and flags & TCP_ACK:
                self.tx_seq += 1
                self.tx_ack += 1
                self._tcp(TCP_ACK)
                return

    def _packet(self, deadline):
        """Next TCP packet for this connection: (flags, payload). Other traffic is ignored."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Timeout()
            protocol, payload = self.mux.receive(remaining)
            if protocol != MUX_TCP or len(payload) < 20:
                continue
            sport, dport, seq, ack, offset, flags = struct.unpack_from('>HHIIBB', payload)
            if (sport, dport) != (self.dport, self.sport):
                continue
            return flags, payload[(offset >> 4) * 4:]

    def send(self, data):
        for start in range(0, len(data), MAX_PAYLOAD):
            chunk = data[start:start + MAX_PAYLOAD]
            self._tcp(TCP_ACK, chunk)
            self.tx_seq = (self.tx_seq + len(chunk)) & 0xFFFFFFFF

    def _fill(self, deadline):
        """Receive one packet's payload into the inbox (acknowledged at once)."""
        if self.closed:
            raise LockdownError('The phone closed the connection.')
        flags, data = self._packet(deadline)
        if data:
            self.inbox += data
            self.tx_ack = (self.tx_ack + len(data)) & 0xFFFFFFFF
            self._tcp(TCP_ACK)
        if flags & (TCP_RST | TCP_FIN):
            self.closed = True

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
            try:
                self._tcp(TCP_RST)
            except Exception:  # noqa: BLE001 - the phone may be gone
                pass
            self.closed = True


# ------------------------------------------------------------------ lockdown
class Lockdown:
    """Property-list requests to the phone's lockdown service, optionally over TLS."""

    def __init__(self, conn):
        self.conn = conn
        self.tls = None
        self.incoming = None
        self.outgoing = None

    # transport (plain or TLS)
    def _send_raw(self, data):
        if self.tls is None:
            self.conn.send(data)
            return
        self.tls.write(data)
        self._flush()

    def _flush(self):
        pending = self.outgoing.read()
        if pending:
            self.conn.send(pending)

    def _recv_raw(self, size, timeout=10.0):
        if self.tls is None:
            return self.conn.recv_exact(size, timeout)
        out = bytearray()
        deadline = time.monotonic() + timeout
        while len(out) < size:
            try:
                out += self.tls.read(size - len(out))
            except ssl.SSLWantReadError:
                self.incoming.write(self.conn.recv_some(max(0.1, deadline - time.monotonic())))
        return bytes(out)

    def request(self, payload, timeout=10.0):
        body = plistlib.dumps(payload)
        self._send_raw(struct.pack('>I', len(body)) + body)
        length = struct.unpack('>I', self._recv_raw(4, timeout))[0]
        if length > 1 << 22:
            raise LockdownError('Oversized reply from the phone.')
        return plistlib.loads(self._recv_raw(length, timeout))

    def start_tls(self, certificate_pem, key_pem, directory):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE   # the phone's certificate is self-issued
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)  # older iOS versions only speak TLS 1.0
            try:
                context.minimum_version = ssl.TLSVersion.TLSv1
            except (ValueError, ssl.SSLError):
                pass
        context.set_ciphers('ALL:@SECLEVEL=0')  # older phones use SHA-1 and small parameters
        # ssl can only load a client certificate from files; they live for one handshake.
        fd, path = tempfile.mkstemp(dir=directory, prefix='.tls-', suffix='.pem')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(certificate_pem + b'\n' + key_pem)
            context.load_cert_chain(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.tls = context.wrap_bio(self.incoming, self.outgoing, server_side=False)
        deadline = time.monotonic() + 10
        while True:
            try:
                self.tls.do_handshake()
                self._flush()
                return
            except ssl.SSLWantReadError:
                self._flush()
                if time.monotonic() > deadline:
                    raise LockdownError('The secure session with the phone did not start.')
                self.incoming.write(self.conn.recv_some(max(0.1, deadline - time.monotonic())))


def _base(request, **extra):
    return {'Label': LABEL, 'ProtocolVersion': '2', 'Request': request, **extra}


# ------------------------------------------------------------------ certificates
def make_pair_certificates(device_public_key_pem):
    """Root CA, host certificate and the phone's certificate, as PEM bytes (go-ios layout)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
    later = now + datetime.timedelta(days=3650)
    empty = x509.Name([])

    def build(public_key, signing_key, ca):
        builder = (x509.CertificateBuilder().subject_name(empty).issuer_name(empty)
                   .public_key(public_key).serial_number(1).not_valid_before(now).not_valid_after(later)
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False))
        if not ca:
            builder = builder.add_extension(x509.KeyUsage(
                digital_signature=True, key_encipherment=True, content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False,
                decipher_only=False), critical=True)
        try:
            return builder.sign(signing_key, hashes.SHA1())
        except Exception:  # noqa: BLE001 - a build of cryptography without SHA-1 signing
            return builder.sign(signing_key, hashes.SHA256())

    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    device_key = serialization.load_pem_public_key(device_public_key_pem)
    root = build(root_key.public_key(), root_key, True)
    host = build(host_key.public_key(), root_key, False)
    device = build(device_key, root_key, False)

    def pem(cert):
        return cert.public_bytes(serialization.Encoding.PEM)

    def key_pem(key):
        return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption())

    return {'RootCertificate': pem(root), 'HostCertificate': pem(host), 'DeviceCertificate': pem(device),
            'RootPrivateKey': key_pem(root_key), 'HostPrivateKey': key_pem(host_key)}


# ------------------------------------------------------------------ pair records
class PairStore:
    """Pair records in <data>/usb-pairing/: one plist per phone plus this computer's ID.
    They contain private keys: never share them (the diagnostics export never includes them)."""

    def __init__(self, data_dir):
        self.directory = Path(data_dir) / 'usb-pairing'

    def _path(self, serial):
        safe = ''.join(c for c in serial if c.isalnum())[:64] or 'device'
        return self.directory / f'{safe}.plist'

    def system_buid(self):
        path = self.directory / 'system-buid.txt'
        try:
            value = path.read_text(encoding='ascii').strip()
            if value:
                return value
        except OSError:
            pass
        value = str(uuid.uuid4()).upper()
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='ascii')
        return value

    def load(self, serial):
        try:
            return plistlib.loads(self._path(serial).read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None

    def save(self, serial, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(serial)
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.pair-', suffix='.tmp')
        with os.fdopen(fd, 'wb') as stream:
            stream.write(plistlib.dumps(record))
        os.replace(temporary, path)

    def forget(self, serial):
        try:
            self._path(serial).unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ the whole flow
PAIRING_WAIT_SECONDS = 120


def trust(mux, store, serial, on_wait=None, wait=PAIRING_WAIT_SECONDS):
    """Make sure the phone trusts this computer and run one secure lockdown session,
    as macOS and Linux do on every connect. on_wait(message) is called while the
    user has to act on the phone. Returns the phone's name when it reports one."""
    conn = mux.connect(LOCKDOWN_PORT)
    try:
        lockdown = Lockdown(conn)
        kind = lockdown.request(_base('QueryType')).get('Type')
        if kind != 'com.apple.mobile.lockdown':
            raise LockdownError(f'Unexpected service on the phone ({kind}).')
        record = store.load(serial)
        if record is None:
            record = _pair(lockdown, store, serial, on_wait, wait)
        reply = lockdown.request(_base('StartSession', HostID=record['HostID'], SystemBUID=record['SystemBUID']))
        if reply.get('Error') in ('InvalidHostID', 'InvalidPairRecord'):
            # The phone forgot this computer (e.g. "Reset Location & Privacy"): pair again.
            store.forget(serial)
            record = _pair(lockdown, store, serial, on_wait, wait)
            reply = lockdown.request(_base('StartSession', HostID=record['HostID'],
                                           SystemBUID=record['SystemBUID']))
        if reply.get('Error'):
            raise LockdownError(f'The phone refused the session ({reply["Error"]}).')
        if reply.get('EnableSessionSSL'):
            lockdown.start_tls(record['HostCertificate'], record['HostPrivateKey'], store.directory)
        name = None
        try:
            name = lockdown.request(_base('GetValue', Key='DeviceName')).get('Value')
        except (LockdownError, Timeout, ssl.SSLError):
            pass
        try:
            lockdown.request(_base('StopSession', SessionID=reply.get('SessionID', '')), timeout=3)
        except (LockdownError, Timeout, ssl.SSLError):
            pass
        return name
    finally:
        conn.close()


def _pair(lockdown, store, serial, on_wait, wait):
    public_key = lockdown.request(_base('GetValue', Key='DevicePublicKey')).get('Value')
    if not isinstance(public_key, bytes):
        raise LockdownError('The phone did not share its public key. Unlock it and try again.')
    certificates = make_pair_certificates(public_key)
    record = {'HostID': str(uuid.uuid4()).upper(), 'SystemBUID': store.system_buid(), **certificates}
    pair_record = {key: record[key] for key in ('DeviceCertificate', 'HostCertificate', 'RootCertificate',
                                                  'SystemBUID', 'HostID')}
    deadline = time.monotonic() + wait
    while True:
        reply = lockdown.request(_base('Pair', PairRecord=pair_record,
                                       PairingOptions={'ExtendedPairingErrors': True}), timeout=15)
        error = reply.get('Error')
        if not error:
            if isinstance(reply.get('EscrowBag'), bytes):
                record['EscrowBag'] = reply['EscrowBag']
            store.save(serial, record)
            log.info('Paired with the phone over USB')
            return record
        if error == 'UserDeniedPairing':
            raise LockdownError('"Don\'t Trust" was chosen on the phone. Unplug it, plug it in again and tap Trust.')
        if error not in ('PairingDialogResponsePending', 'PasswordProtected'):
            raise LockdownError(f'Pairing failed ({error}).')
        if time.monotonic() > deadline:
            raise PairingPending('Waiting too long for "Trust" on the phone. Reconnect to try again.')
        if on_wait:
            on_wait('Unlock the iPhone and tap Trust, then enter its passcode.' if error == 'PasswordProtected'
                    else 'Tap Trust on the iPhone, then enter its passcode.')
        time.sleep(1.0)
