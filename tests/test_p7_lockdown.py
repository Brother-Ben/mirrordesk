"""Pairing over USB ("Trust This Computer") against a simulated phone.

The simulated phone speaks usbmux (version exchange, TCP-like connections),
lockdown property lists and, when the session asks for it, TLS - the same flow
that paired and streamed with a real iPhone 11 on 2026-10-14. Its usbmux version
reply is the one that phone sent.
"""
import plistlib
import ssl
import struct

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from mirrordesk import lockdown

REAL_VERSION_REPLY = bytes.fromhex('00000000000000140000000200000000f1020500')
DEVICE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
DEVICE_PUBLIC_PEM = DEVICE_KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.PKCS1)


def tls_server_context(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([])
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(7).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    path = tmp_path / 'phone.pem'
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(path))
    context.verify_mode = ssl.CERT_REQUIRED      # like the phone: the computer must present its certificate
    return context


class FakePhone:
    """The phone side of usbmux + lockdown."""

    def __init__(self, trust_after=0, deny=False, ssl_context=None):
        self.trust_after = trust_after
        self.deny = deny
        self.ssl_context = ssl_context
        self.out = bytearray()
        self.inbox = bytearray()
        self.version = 0
        self.port = None
        self.seq = 0
        self.known_hosts = set()
        self.requests = []
        self.pair_records = []
        self.tls = None
        self.client_certificate = None

    # ---- the computer reads from / writes to these
    def read(self, size, timeout_ms):
        if not self.out:
            raise lockdown.Timeout()
        chunk = bytes(self.out[:size])
        del self.out[:size]
        return chunk

    def write(self, data):
        protocol, length = struct.unpack_from('>II', data)
        header = 16 if self.version >= 2 else 8
        payload = data[header:length]
        if protocol == lockdown.MUX_VERSION:
            self.out += REAL_VERSION_REPLY
            self.version = 2
        elif protocol == lockdown.MUX_TCP:
            self._tcp(payload)

    # ---- TCP-like connection
    def _send_tcp(self, flags, data=b''):
        tcp = struct.pack('>HHIIBBHHH', lockdown.LOCKDOWN_PORT, self.port, self.seq, 0, 0x50, flags, 0x200, 0, 0)
        body = tcp + data
        self.out += struct.pack('>IIIHH', lockdown.MUX_TCP, 16 + len(body), lockdown.MUX_MAGIC, 0, 0) + body
        self.seq += len(data)

    def _tcp(self, payload):
        sport, dport, seq, ack, offset, flags = struct.unpack_from('>HHIIBB', payload)
        data = payload[(offset >> 4) * 4:]
        if flags & lockdown.TCP_SYN:
            assert dport == lockdown.LOCKDOWN_PORT
            self.port = sport
            self._send_tcp(lockdown.TCP_SYN | lockdown.TCP_ACK)
            self.seq = 1
            return
        if flags & lockdown.TCP_RST:
            self.tls = None
            self.inbox.clear()
            return
        if not data:
            return
        if self.tls is not None:
            self.tls_in.write(data)
            if not self.handshaken:
                try:
                    self.tls.do_handshake()
                    self.handshaken = True
                    der = self.tls.getpeercert(binary_form=True)
                    self.client_certificate = x509.load_der_x509_certificate(der) if der else None
                except ssl.SSLWantReadError:
                    pass
                self._flush_tls()
                if not self.handshaken:
                    return
            try:
                plain = self.tls.read(65536)
            except ssl.SSLWantReadError:
                return
            self.inbox += plain
        else:
            self.inbox += data
        while len(self.inbox) >= 4:
            size = struct.unpack_from('>I', self.inbox)[0]
            if len(self.inbox) < 4 + size:
                break
            request = plistlib.loads(bytes(self.inbox[4:4 + size]))
            del self.inbox[:4 + size]
            self._reply(self._answer(request))

    def _flush_tls(self):
        pending = self.tls_out.read()
        if pending:
            self._send_tcp(lockdown.TCP_ACK, pending)

    def _reply(self, payload):
        body = plistlib.dumps(payload)
        data = struct.pack('>I', len(body)) + body
        if self.tls is not None and self.handshaken:
            self.tls.write(data)
            self._flush_tls()
        else:
            self._send_tcp(lockdown.TCP_ACK, data)
        if payload.get('EnableSessionSSL'):
            # The phone accepts the computer's certificate from the pair record (and no other).
            self.ssl_context.load_verify_locations(cadata=self.pair_records[-1]['HostCertificate'].decode())
            self.ssl_context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
            self.tls_in, self.tls_out = ssl.MemoryBIO(), ssl.MemoryBIO()
            self.tls = self.ssl_context.wrap_bio(self.tls_in, self.tls_out, server_side=True)
            self.handshaken = False

    # ---- lockdown
    def _answer(self, request):
        kind = request.get('Request')
        self.requests.append(kind)
        assert request.get('Label') == lockdown.LABEL
        if kind == 'QueryType':
            return {'Request': kind, 'Type': 'com.apple.mobile.lockdown'}
        if kind == 'GetValue' and request.get('Key') == 'DevicePublicKey':
            return {'Request': kind, 'Key': 'DevicePublicKey', 'Value': DEVICE_PUBLIC_PEM}
        if kind == 'GetValue' and request.get('Key') == 'DeviceName':
            return {'Request': kind, 'Key': 'DeviceName', 'Value': 'Test iPhone'}
        if kind == 'Pair':
            self.pair_records.append(request['PairRecord'])
            if self.deny:
                return {'Request': kind, 'Error': 'UserDeniedPairing'}
            if self.trust_after > 0:
                self.trust_after -= 1
                return {'Request': kind, 'Error': 'PairingDialogResponsePending'}
            self.known_hosts.add(request['PairRecord']['HostID'])
            return {'Request': kind, 'EscrowBag': b'escrow'}
        if kind == 'StartSession':
            if request['HostID'] not in self.known_hosts:
                return {'Request': kind, 'Error': 'InvalidHostID'}
            return {'Request': kind, 'SessionID': 'session-1', 'EnableSessionSSL': self.ssl_context is not None}
        if kind == 'StopSession':
            return {'Request': kind}
        return {'Request': kind, 'Error': 'UnknownRequest'}


def connect(phone):
    mux = lockdown.MuxDevice(phone.read, phone.write)
    mux.handshake()
    return mux


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(lockdown.time, 'sleep', lambda seconds: None)


def test_usbmux_version_exchange_and_setup():
    phone = FakePhone()
    writes = []
    mux = lockdown.MuxDevice(phone.read, lambda data: (writes.append(bytes(data)), phone.write(data)))
    mux.handshake()
    assert mux.version == 2
    assert writes[0] == struct.pack('>IIIII', 0, 20, 2, 0, 0)
    assert writes[1] == struct.pack('>IIIHH', lockdown.MUX_SETUP, 17, lockdown.MUX_MAGIC, 0, 0xFFFF) + b'\x07'


def test_first_connection_pairs_once_the_user_taps_trust(tmp_path):
    phone = FakePhone(trust_after=2)
    store = lockdown.PairStore(tmp_path)
    waits = []
    assert lockdown.trust(connect(phone), store, 'SERIAL1', on_wait=waits.append) == 'Test iPhone'
    assert len(waits) == 2 and all('Tap Trust' in w for w in waits)
    assert phone.requests == ['QueryType', 'GetValue', 'Pair', 'Pair', 'Pair', 'StartSession', 'GetValue',
                              'StopSession']
    record = store.load('SERIAL1')
    assert record['EscrowBag'] == b'escrow' and record['HostID'] in phone.known_hosts
    assert record['SystemBUID'] == store.system_buid()
    # The certificate we gave the phone carries the phone's own public key.
    device_cert = x509.load_pem_x509_certificate(phone.pair_records[0]['DeviceCertificate'])
    assert device_cert.public_key().public_numbers() == DEVICE_KEY.public_key().public_numbers()
    assert 'HostPrivateKey' not in phone.pair_records[0] and 'RootPrivateKey' not in phone.pair_records[0]


def test_a_trusted_computer_does_not_ask_again(tmp_path):
    phone = FakePhone()
    store = lockdown.PairStore(tmp_path)
    lockdown.trust(connect(phone), store, 'SERIAL1')
    phone.requests.clear()
    lockdown.trust(connect(phone), store, 'SERIAL1')
    assert 'Pair' not in phone.requests and phone.requests[:2] == ['QueryType', 'StartSession']


def test_a_phone_that_forgot_this_computer_pairs_again(tmp_path):
    phone = FakePhone()
    store = lockdown.PairStore(tmp_path)
    lockdown.trust(connect(phone), store, 'SERIAL1')
    old = store.load('SERIAL1')['HostID']
    phone.known_hosts.clear()  # e.g. "Reset Location & Privacy" on the phone
    lockdown.trust(connect(phone), store, 'SERIAL1')
    assert store.load('SERIAL1')['HostID'] != old
    assert phone.requests.count('Pair') == 2


def test_dont_trust_is_reported(tmp_path):
    with pytest.raises(lockdown.LockdownError, match="Don't Trust"):
        lockdown.trust(connect(FakePhone(deny=True)), lockdown.PairStore(tmp_path), 'SERIAL1')


def test_secure_session_presents_the_host_certificate(tmp_path):
    phone = FakePhone(ssl_context=tls_server_context(tmp_path))
    store = lockdown.PairStore(tmp_path)
    assert lockdown.trust(connect(phone), store, 'SERIAL1') == 'Test iPhone'
    assert phone.requests[-2:] == ['GetValue', 'StopSession'], 'name and stop went over TLS'
    host = x509.load_pem_x509_certificate(store.load('SERIAL1')['HostCertificate'])
    assert phone.client_certificate is not None and phone.client_certificate == host
    assert not list(tmp_path.glob('usb-pairing/.tls-*')), 'the temporary key file is removed'


def test_pairing_certificates_chain_to_the_root():
    certs = lockdown.make_pair_certificates(DEVICE_PUBLIC_PEM)
    root = x509.load_pem_x509_certificate(certs['RootCertificate'])
    for name in ('HostCertificate', 'DeviceCertificate'):
        cert = x509.load_pem_x509_certificate(certs[name])
        root.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(),
                                 cert.signature_hash_algorithm)
        assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
        assert cert.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature
    assert root.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    host_key = serialization.load_pem_private_key(certs['HostPrivateKey'], None)
    assert host_key.public_key().public_numbers() == \
        x509.load_pem_x509_certificate(certs['HostCertificate']).public_key().public_numbers()


def test_pair_store_names_and_round_trip(tmp_path):
    store = lockdown.PairStore(tmp_path)
    assert store.load('00008030-000F1E2D3C4B5A69') is None
    store.save('00008030-000F1E2D3C4B5A69', {'HostID': 'X'})
    assert store.load('00008030-000F1E2D3C4B5A69') == {'HostID': 'X'}
    assert (tmp_path / 'usb-pairing' / '00008030000F1E2D3C4B5A69.plist').exists()
    buid = store.system_buid()
    assert buid == store.system_buid() and len(buid) == 36
    store.forget('00008030-000F1E2D3C4B5A69')
    assert store.load('00008030-000F1E2D3C4B5A69') is None
