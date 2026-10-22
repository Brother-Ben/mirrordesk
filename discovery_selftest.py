"""Bounded stdlib mDNS path evidence; never bind 5353 or retain LAN identities.

Legacy-unicast PTR questions (RFC 6762 section 6.7) leave ephemeral per-adapter
ports. Only counts/categories leave this module. Silence is NOT proof of a
multicast filter: devices may be asleep and firewalls may suppress replies.
"""
from dataclasses import dataclass, field
import ipaddress
import select
import socket
import struct
import threading
import time

import discovery_probe as dp

AIRPLAY = '_airplay._tcp.local'
SERVICES = '_services._dns-sd._udp.local'
DESTINATION = ('224.0.0.251', 5353)
WINDOW_SECONDS = 3.2
BOUND_SECONDS = 4.0
MAX_PACKET = 9000
MAX_RECORDS = 128
MAX_ADAPTERS = 16
MAX_RESPONDERS = 256


@dataclass(frozen=True)
class Record:
    name: str
    type: int
    value: str


def _name(data, offset):
    labels, seen, end, length = [], set(), None, 0
    for _ in range(128):
        if offset >= len(data) or offset in seen:
            raise ValueError('bad name')
        seen.add(offset)
        size = data[offset]
        if size & 0xc0 == 0xc0:
            if offset + 1 >= len(data):
                raise ValueError('short pointer')
            if end is None:
                end = offset + 2
            offset = ((size & 0x3f) << 8) | data[offset + 1]
            continue
        if size & 0xc0:
            raise ValueError('bad label')
        offset += 1
        if size == 0:
            return '.'.join(labels).lower(), end if end is not None else offset
        length += size + 1
        if length > 254 or offset + size > len(data):
            raise ValueError('long/short name')
        labels.append(data[offset:offset + size].decode('utf-8', 'strict'))
        offset += size
    raise ValueError('name loop')


def parse_response(data):
    """PTR, SRV target and A records only; malformed/oversize -> (), never raises."""
    try:
        if not isinstance(data, bytes) or not 12 <= len(data) <= MAX_PACKET:
            return ()
        _, flags, questions, answers, authority, additional = struct.unpack('!6H', data[:12])
        if not flags & 0x8000 or flags & 0x780f or questions > 32:
            return ()
        count = answers + authority + additional
        if count > MAX_RECORDS:
            return ()
        offset, records = 12, []
        for _ in range(questions):
            _, offset = _name(data, offset)
            offset += 4
            if offset > len(data):
                return ()
        for _ in range(count):
            owner, offset = _name(data, offset)
            type_, class_, ttl, size = struct.unpack_from('!HHIH', data, offset)
            offset += 10
            end = offset + size
            if end > len(data):
                return ()
            value = None
            if type_ == 12:
                value, used = _name(data, offset)
                if used != end:
                    return ()
            elif type_ == 33:
                if size < 7:
                    return ()
                value, used = _name(data, offset + 6)
                if used != end:
                    return ()
            elif type_ == 1:
                if size != 4:
                    return ()
                value = socket.inet_ntoa(data[offset:end])
            if class_ & 0x7fff == 1 and ttl and value is not None:
                records.append(Record(owner, type_, value))
            offset = end
        return tuple(records) if offset == len(data) else ()
    except Exception:
        return ()


def ptr_query(service):
    labels = service.split('.')
    wire = b''.join(bytes([len(p.encode('utf-8'))]) + p.encode('utf-8') for p in labels) + b'\0'
    return struct.pack('!6H', 0, 0, 1, 0, 0, 0) + wire + struct.pack('!HH', 12, 1)


def _ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value)) if isinstance(value, str) else None
    except ValueError:
        return None


@dataclass(frozen=True)
class AdapterPath:
    kind: str = 'other'
    own_answer: bool = False
    own_a_matches: bool = False
    other_responders: int = 0
    other_airplay: int = 0
    reason: str = 'none'
    alias: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PathResult:
    verdict: str = 'inconclusive'
    adapters: tuple = ()
    receiver_running: bool = False
    reason: str = 'none'


def classify(paths, receiver_running):
    usable = [p for p in paths if p.reason == 'none']
    if not usable:
        return 'inconclusive'
    if receiver_running and not any(p.own_answer for p in paths):
        # Incomplete measurements cannot establish receiver silence.
        return 'inconclusive' if len(usable) != len(paths) else 'receiver_silent'
    if any(p.other_responders for p in usable):
        return 'path_ok'
    return 'inconclusive' if len(usable) != len(paths) else 'no_lan_replies'


def _addresses(adapter):
    values = adapter.get('addresses')
    if not isinstance(values, (list, tuple)):
        values = [adapter.get('address')]
    return {_ipv4(v) for v in values if _ipv4(v)}


def _measure(discovery, receiver_name, receiver_running, destination, duration, socket_factory):
    own_ips = set().union(*(_addresses(a) for a in discovery.adapters if a.get('up') is True))
    candidates = [a for a in discovery.adapters if dp.select_announce_adapter([a]) is not None]
    if not candidates or len(candidates) > MAX_ADAPTERS:
        return PathResult(receiver_running=receiver_running, reason='no_candidates' if not candidates else 'limit')
    states, sockets = [], {}
    own_name = (receiver_name + '.' + AIRPLAY).lower()
    started = time.monotonic()
    try:
        for adapter in candidates:
            state = dict(kind=dp.adapter_kind(adapter), alias=adapter.get('alias'), reason='none',
                         address=_ipv4(adapter.get('address')), own=set(), others=set(), airplay=set(),
                         policy=adapter.get('unicast_response'), got_reply=False)
            states.append(state)
            if adapter.get('unicast_response') is False:
                state['reason'] = 'unicast_disabled'
                continue
            if not state['address']:
                state['reason'] = 'no_ipv4'
                continue
            sock = None
            try:
                sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
                # Ephemeral only. Never claim/share the receiver's 5353 socket.
                sock.bind((state['address'], 0))
                if sock.getsockname()[1] == 5353:
                    raise OSError('unsafe ephemeral port')
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(state['address']))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
                sock.setblocking(False)
                sockets[sock] = state
            except Exception:
                state['reason'] = 'socket_error'
                if sock is not None:
                    sock.close()
        next_send, sent = started, 0
        deadline = started + duration
        while sockets and time.monotonic() < deadline:
            now = time.monotonic()
            if sent < 3 and now >= next_send:
                for sock, state in sockets.items():
                    try:
                        for service in (AIRPLAY, SERVICES):
                            sock.sendto(ptr_query(service), destination)
                    except OSError:
                        state['reason'] = 'socket_error'
                sent += 1
                next_send = started + sent * duration / 3
            wait = min(.05, max(0, deadline - time.monotonic()))
            ready, _, _ = select.select(list(sockets), [], [], wait)
            for sock in ready:
                state = sockets[sock]
                try:
                    data, source = sock.recvfrom(MAX_PACKET + 1)
                except BlockingIOError:
                    continue
                except OSError:
                    state['reason'] = 'socket_error'
                    continue
                records = parse_response(data)
                relevant = any(r.type == 12 and r.name in (AIRPLAY, SERVICES) for r in records)
                if source[0] in own_ips:
                    # SRV/A may be split into separate datagrams, even before PTR.
                    state['got_reply'] |= relevant
                    state['own'].update(records)
                    if len(state['own']) > MAX_RECORDS * 2:
                        state['reason'] = 'limit'
                        state['own'].clear()
                elif relevant:
                    state['got_reply'] = True
                    if source[0] not in state['others'] and len(state['others']) >= MAX_RESPONDERS:
                        state['reason'] = 'limit'
                        continue
                    state['others'].add(source[0])
                    if any(r.type == 12 and r.name == AIRPLAY for r in records):
                        state['airplay'].add(source[0])
        paths = []
        for state in states:
            if state['reason'] == 'none' and state['policy'] is not True and not state['got_reply']:
                state['reason'] = 'unicast_unknown'
            records = state['own']
            own = any(r.type == 12 and r.name == AIRPLAY and r.value == own_name for r in records)
            hosts = {r.value for r in records if r.type == 33 and r.name == own_name}
            matches = own and any(r.type == 1 and r.name in hosts and r.value == state['address'] for r in records)
            paths.append(AdapterPath(kind=state['kind'], alias=state['alias'], reason=state['reason'],
                                     own_answer=own, own_a_matches=matches,
                                     other_responders=len(state['others']), other_airplay=len(state['airplay'])))
        return PathResult(classify(paths, receiver_running), tuple(paths), receiver_running)
    finally:
        for sock in sockets:
            sock.close()


def run_selftest(discovery, receiver_name, receiver_running=False, *, destination=DESTINATION,
                 duration=WINDOW_SECONDS, socket_factory=None, bound=BOUND_SECONDS):
    """Fail-open daemon work joined <=4s total for ALL adapters, even a hung socket.

    Launcher delays the post-start call by 3s, so its total scheduled bound is
    <=7s. No thread join is part of child cleanup. Tests inject loopback only.
    """
    box = {}
    def work():
        try:
            box['result'] = _measure(discovery, receiver_name, receiver_running, destination,
                                     max(.001, min(float(duration), WINDOW_SECONDS)), socket_factory or socket.socket)
        except Exception:
            pass
    worker = threading.Thread(target=work, name='mirrordesk-path-sockets', daemon=True)
    worker.start()
    worker.join(max(0, min(float(bound), BOUND_SECONDS)))
    return box.get('result', PathResult(receiver_running=receiver_running,
                                       reason='timeout' if worker.is_alive() else 'socket_error'))


def diagnostic_records(result):
    records = [{'path_scope': 'summary', 'path_verdict': result.verdict, 'path_reason': result.reason,
                'path_receiver_running': result.receiver_running, 'path_adapters': len(result.adapters)}]
    for i, path in enumerate(result.adapters):
        records.append({'path_scope': 'adapter', 'path_verdict': classify((path,), False),
                        'path_kind': path.kind, 'path_index': i, 'path_reason': path.reason,
                        'path_own_answer': path.own_answer, 'path_own_a_matches': path.own_a_matches,
                        'path_other_responders': path.other_responders, 'path_other_airplay': path.other_airplay,
                        'path_receiver_running': result.receiver_running})
    return records


def summary_lines(result):
    lines = []
    for path in result.adapters:
        alias = dp._console_name(path.alias)
        own = ('yes' if path.own_answer else 'no') if result.receiver_running else 'not tested (receiver not started)'
        lines.append(f'Network discovery path: {alias}: other devices {path.other_responders}, '
                     f'AirPlay devices {path.other_airplay}; receiver answer {own}; '
                     f'adapter address matches {"yes" if path.own_a_matches else "no"}.')
    meaning = {
        'path_ok': 'Other devices answered over this connection. Discovery traffic reached the LAN; this does not prove the iPhone sees the receiver.',
        'no_lan_replies': 'No other devices answered. The connection or a powered box in between may not pass discovery traffic; sleeping devices can also explain this. Try Bluetooth discovery, plugging directly into the router, or Wi-Fi.',
        'receiver_silent': 'The running receiver did not answer on any tested connection. Restart MirrorDesk Web; try Bluetooth discovery or Wi-Fi. LAN reply counts above are separate evidence.',
        'inconclusive': 'Could not establish the discovery path (settings, missing adapters, errors or time limit). Nothing was changed; normal discovery continues. Try Bluetooth discovery, a direct router cable, or Wi-Fi.'}
    lines.append('Network discovery path: ' + meaning.get(result.verdict, meaning['inconclusive']))
    if any(p.reason == 'unicast_disabled' for p in result.adapters):
        lines.append('Network discovery path: Windows disables unicast responses to multicast on a tested profile; this test is inconclusive there. No firewall setting was changed.')
    return lines
