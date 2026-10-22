"""No LAN traffic: defensive DNS and real legacy-unicast LOOPBACK fixtures."""
import io
import random
import socket
import struct
import threading
import time
import zipfile

import pytest
import discovery_probe as dp
import discovery_selftest as ds
from diagnostics import DiagnosticSession


def name(text):
    return b''.join(bytes([len(p.encode())]) + p.encode() for p in text.split('.')) + b'\0'


def rr(owner, type_, value, ttl=120):
    return name(owner) + struct.pack('!HHIH', type_, 1, ttl, len(value)) + value


def reply(address='127.0.0.1', instance='MirrorDesk Web', host='private-host.local'):
    instance += '.' + ds.AIRPLAY
    records = [rr(ds.AIRPLAY, 12, name(instance)),
               rr(instance, 33, struct.pack('!HHH', 0, 0, 7000) + name(host)),
               rr(host, 1, socket.inet_aton(address))]
    return struct.pack('!HHHHHH', 0, 0x8400, 0, 1, 0, 2) + b''.join(records)


def adapter(address='127.0.0.1', **extra):
    return dict(index=1, alias='Eth\x1b[2J\nPRIVATE_ALIAS', description='Realtek',
                type='Ethernet', up=True, gateways=1, ipv4=1, address=address,
                addresses=[address], **extra)


def discovery(*adapters, unicast='True'):
    return dp.interpret({'adapters': list(adapters), 'profiles': [{'index': 1, 'category': 'Private'}],
                         'firewall': [{'name': 'Private', 'enabled': 'True',
                                       'unicast_response': unicast}], 'rules': []})


def test_probe_retains_all_addresses_only_in_memory_and_fixed_readonly_setting():
    item = adapter()
    item['addresses'].append('127.0.0.2')
    result = discovery(item, unicast='False')
    assert result.adapters[0]['addresses'] == ['127.0.0.1', '127.0.0.2']
    assert result.adapters[0]['unicast_response'] is False
    assert '127.0.0.2' not in repr(result)
    assert 'AllowUnicastResponseToMulticast' in dp.FULL_SCRIPT
    assert 'addresses=' in dp.LIGHT_SCRIPT
    assert not any(key in dp.diagnostic_fields(result, 'full') for key in ('adapters', 'addresses'))


def test_parser_ptr_srv_a_and_compressed_names():
    parsed = ds.parse_response(reply())
    assert [r.type for r in parsed] == [12, 33, 1]
    assert parsed[0].value == 'mirrordesk web.' + ds.AIRPLAY
    assert parsed[2].value == '127.0.0.1'
    # Owner pointer back to the question; PTR value pointer also checked.
    q = name(ds.AIRPLAY) + struct.pack('!HH', 12, 1)
    wire = struct.pack('!HHHHHH', 123, 0x8400, 1, 1, 0, 0) + q
    wire += b'\xc0\x0c' + struct.pack('!HHIH', 12, 1, 120, 2) + b'\xc0\x0c'
    assert ds.parse_response(wire)[0].name == ds.AIRPLAY


@pytest.mark.parametrize('wire', [b'', b'\0' * 11, b'\0' * 10000,
    struct.pack('!HHHHHH', 0, 0x8400, 0, 5000, 0, 0),
    struct.pack('!HHHHHH', 0, 0x8400, 0, 1, 0, 0) + b'\xc0\x0c',
    struct.pack('!HHHHHH', 0, 0x8400, 0, 1, 0, 0) + b'\xc0\xff',
    struct.pack('!HHHHHH', 0, 0x8400, 0, 1, 0, 0) + b'\x40bad',
    reply()[:-1], reply().replace(b'\x84\x00', b'\0\0', 1)],
    ids=lambda wire: f'{len(wire)}-bytes')  # the raw bytes as an id overflow Windows' 32 KiB env var limit
def test_malformed_is_ignored(wire):
    assert ds.parse_response(wire) == ()


def test_fuzz_and_every_truncation_never_raises():
    rng = random.Random(6)
    wire = reply()
    for i in range(len(wire)):
        assert ds.parse_response(wire[:i]) == ()
    for _ in range(1000):
        assert isinstance(ds.parse_response(rng.randbytes(rng.randrange(0, 1200))), tuple)


def test_query_standard_ptr_and_no_qu_bit():
    wire = ds.ptr_query(ds.AIRPLAY)
    assert struct.unpack('!HHHHHH', wire[:12]) == (0, 0, 1, 0, 0, 0)
    assert wire[12:] == name(ds.AIRPLAY) + struct.pack('!HH', 12, 1)


@pytest.mark.parametrize('others, own, running, reason, expected', [
    (1, True, True, 'none', 'path_ok'), (0, True, True, 'none', 'no_lan_replies'),
    (0, False, False, 'none', 'no_lan_replies'), (1, False, True, 'none', 'receiver_silent'),
    (0, False, True, 'none', 'receiver_silent'), (0, False, True, 'socket_error', 'inconclusive'),
    (0, False, True, 'unicast_disabled', 'inconclusive')])
def test_classification(others, own, running, reason, expected):
    path = ds.AdapterPath(kind='ethernet', own_answer=own, other_responders=others, reason=reason)
    assert ds.classify((path,), running) == expected


def test_no_candidates_and_disabled_unicast_are_inconclusive():
    out = ds.run_selftest(dp.UNKNOWN, 'MirrorDesk Web', receiver_running=True)
    assert out.verdict == 'inconclusive' and not out.adapters
    out = ds.run_selftest(discovery(adapter(), unicast='False'), 'MirrorDesk Web', duration=.01)
    assert out.verdict == 'inconclusive' and out.adapters[0].reason == 'unicast_disabled'


def test_real_loopback_source_port_not_5353_and_own_a_linkage():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as responder:
        responder.bind(('127.0.0.1', 0))
        responder.settimeout(1)
        destination = responder.getsockname()
        queries = []
        def serve():
            for _ in range(6):
                wire, src = responder.recvfrom(4096)
                queries.append((wire, src))
                responder.sendto(reply(), src)
        worker = threading.Thread(target=serve)
        worker.start()
        result = ds.run_selftest(discovery(adapter()), 'MirrorDesk Web', receiver_running=True,
                                 destination=destination, duration=.18)
        worker.join(2)
        assert not worker.is_alive()
    path = result.adapters[0]
    assert len(queries) == 6 and all(src[1] != 5353 for _, src in queries)
    assert {wire for wire, _ in queries} == {ds.ptr_query(ds.AIRPLAY), ds.ptr_query(ds.SERVICES)}
    assert path.own_answer and path.own_a_matches
    assert path.other_responders == 0 and result.verdict == 'no_lan_replies'


def test_silent_loopback_bound_socket_errors_and_privacy(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
        sink.bind(('127.0.0.1', 0))
        started = time.monotonic()
        out = ds.run_selftest(discovery(adapter()), 'MirrorDesk Web', destination=sink.getsockname(), duration=.05)
        assert time.monotonic() - started < 1 and out.verdict == 'no_lan_replies'
    def boom(*a, **k):
        raise OSError('PRIVATE_IP_192.168.88.99')
    out = ds.run_selftest(discovery(adapter()), 'MirrorDesk Web', socket_factory=boom)
    assert out.verdict == 'inconclusive'
    session = DiagnosticSession.create(tmp_path, component='launcher')
    for fields in ds.diagnostic_records(out):
        session.record('discovery_path', **fields, name='PRIVATE_OTHER_NAME', address='192.168.88.99')
    with zipfile.ZipFile(io.BytesIO(session.export())) as bundle:
        for member in bundle.namelist():
            data = bundle.read(member)
            for sentinel in (b'PRIVATE_ALIAS', b'PRIVATE_OTHER_NAME', b'192.168.88.99', b'private-host.local'):
                assert sentinel not in data
    assert '"discovery_path"' in (session.directory/'launcher.jsonl').read_text()


def test_unknown_policy_silence_is_inconclusive_and_profiles_are_per_adapter():
    data = {'adapters': [adapter(), {**adapter('127.0.0.2'), 'index': 2}],
            'profiles': [{'index': 1, 'category': 'Private'}, {'index': 2, 'category': 'Public'}],
            'firewall': [{'name': 'Private', 'enabled': 'True', 'unicast_response': 'True'},
                         {'name': 'Public', 'enabled': 'True', 'unicast_response': 'False'}]}
    result = dp.interpret(data)
    assert [a['unicast_response'] for a in result.adapters] == [True, False]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
        sink.bind(('127.0.0.1', 0))
        for raw in (None, 'NotConfigured'):
            out = ds.run_selftest(discovery(adapter(), unicast=raw), 'MirrorDesk Web', True,
                                  destination=sink.getsockname(), duration=.01)
            assert out.verdict == 'inconclusive' and out.adapters[0].reason == 'unicast_unknown'
        light = dp.interpret(data, full=False)
        out = ds.run_selftest(light, 'MirrorDesk Web', False, destination=sink.getsockname(), duration=.01)
        assert out.verdict == 'inconclusive'
    data['firewall'][1]['enabled'] = 'False'
    assert dp.interpret(data).adapters[1]['unicast_response'] is True  # firewall disabled


def test_split_own_ptr_srv_a_datagrams_establish_address_match():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as responder:
        responder.bind(('127.0.0.1', 0))
        responder.settimeout(2)
        instance = 'MirrorDesk Web.' + ds.AIRPLAY
        records = [rr(ds.AIRPLAY, 12, name(instance)),
                   rr(instance, 33, struct.pack('!HHH', 0, 0, 7000) + name('private-host.local')),
                   rr('private-host.local', 1, socket.inet_aton('127.0.0.1'))]
        def serve():
            for _ in range(6):
                _, src = responder.recvfrom(4096)
                for record in records:
                    responder.sendto(struct.pack('!6H', 0, 0x8400, 0, 1, 0, 0) + record, src)
        worker = threading.Thread(target=serve)
        worker.start()
        out = ds.run_selftest(discovery(adapter()), 'MirrorDesk Web', True,
                              destination=responder.getsockname(), duration=.18)
        worker.join(2)
        assert not worker.is_alive()
    assert out.adapters[0].own_answer and out.adapters[0].own_a_matches


def test_responder_limit_still_updates_known_airplay(monkeypatch):
    monkeypatch.setattr(ds, 'MAX_RESPONDERS', 1)
    packets = [(struct.pack('!6H', 0, 0x8400, 0, 1, 0, 0) +
                rr(ds.SERVICES, 12, name('_http._tcp.local')), ('192.168.88.10', 5353)),
               (reply('192.168.88.10', 'PRIVATE_OTHER'), ('192.168.88.10', 5353))]
    class FakeSocket:
        def bind(self, endpoint): assert endpoint[1] == 0
        def getsockname(self): return ('127.0.0.1', 30000)
        def setsockopt(self, *a): pass
        def setblocking(self, *a): pass
        def sendto(self, *a): pass
        def recvfrom(self, *a): return packets.pop(0)
        def close(self): pass
    sock = FakeSocket()
    monkeypatch.setattr(ds.select, 'select', lambda *a: ([sock] if packets else [], [], []))
    out = ds.run_selftest(discovery(adapter()), 'MirrorDesk Web', destination=('127.0.0.1', 9),
                          duration=.01, socket_factory=lambda *a: sock)
    assert out.adapters[0].other_responders == out.adapters[0].other_airplay == 1
