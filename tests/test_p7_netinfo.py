"""Prototype 7: interface classification (Ethernet, Wi-Fi, Tailscale, USB tether, virtual)."""
from types import SimpleNamespace

import pytest

from mirrordesk import netinfo
from mirrordesk.receiver import build_args, help_capabilities, line_buffered
from mirrordesk.settings import Settings


def adapter(name, nice, *ips):
    return SimpleNamespace(name=name, nice_name=nice, ips=[SimpleNamespace(ip=ip) for ip in ips])


@pytest.mark.parametrize('name,nice,ip,kind', [
    ('{A}', 'Realtek PCIe GbE Family Controller', '192.168.1.20', 'ethernet'),
    ('eth0', 'eth0', '10.1.2.3', 'ethernet'),
    ('enp3s0', 'enp3s0', '10.0.0.5', 'ethernet'),
    ('{B}', 'Intel(R) Wi-Fi 6 AX201 160MHz', '192.168.1.21', 'wifi'),
    ('wlan0', 'wlan0', '192.168.1.22', 'wifi'),
    ('{C}', 'Tailscale Tunnel', '100.101.102.103', 'tailscale'),
    ('tailscale0', 'tailscale0', '100.64.1.2', 'tailscale'),
    ('{D}', 'Apple Mobile Device Ethernet', '172.20.10.2', 'usb_tether'),
    ('usb0', 'usb0', '192.168.42.10', 'usb_tether'),
    ('{E}', 'Remote NDIS based Internet Sharing Device', '192.168.42.129', 'usb_tether'),
    ('{F}', 'Hyper-V Virtual Ethernet Adapter', '172.25.80.1', 'virtual'),
    ('docker0', 'docker0', '172.17.0.1', 'virtual'),
    ('{G}', 'vEthernet (WSL)', '172.30.0.1', 'virtual'),
    ('wg0', 'wg0', '10.8.0.2', 'vpn'),
    ('lo', 'lo', '127.0.0.1', 'loopback'),
])
def test_classify(name, nice, ip, kind):
    assert netinfo.classify(name, nice, [ip]) == kind


def test_list_and_candidates_skip_link_local_and_virtual():
    adapters = [adapter('lo', 'lo', '127.0.0.1'), adapter('eth0', 'eth0', '192.168.1.5', ('fe80::1', 0, 2)),
                adapter('docker0', 'docker0', '172.17.0.1'), adapter('x', 'Ethernet 2', '169.254.3.4'),
                adapter('tailscale0', 'tailscale0', '100.80.1.1'), adapter('{D}', 'Apple Mobile Device Ethernet', '172.20.10.3')]
    interfaces = netinfo.list_interfaces(adapters)
    assert [i.kind for i in interfaces] == ['loopback', 'ethernet', 'virtual', 'tailscale', 'usb_tether']
    assert netinfo.candidate_addresses(interfaces) == ['127.0.0.1', '192.168.1.5', '100.80.1.1', '172.20.10.3']
    urls = netinfo.viewer_urls(interfaces, 8765, tailscale={'running': True, 'dns_name': 'desk.tail1234.ts.net'})
    kinds = [u['kind'] for u in urls]
    assert 'virtual' not in kinds and 'loopback' not in kinds
    assert {'ethernet', 'tailscale', 'usb_tether', 'mdns'} <= set(kinds)
    assert any(u['url'] == 'http://desk.tail1234.ts.net:8765/' for u in urls)


def test_build_args_maps_settings_to_uxplay_options():
    s = Settings(pin='4821', resolution='1080p', fps=60, receiver_name='Desk', airplay_ports='7100')
    args = build_args(s, 5004, 5006, key_path='/data/uxplay-key.pem', register_path='/data/uxplay-register.txt')
    assert args[:3] == ['-n', 'Desk', '-nh']
    assert args[args.index('-s') + 1] == '1920x1080@60' and args[args.index('-fps') + 1] == '60'
    assert args[args.index('-pin') + 1] == '4821', '-pin takes the PIN as a separate argument (verified on UxPlay 1.74)'
    assert 'port=5004 sync=false' in args[args.index('-vrtp') + 1]
    assert 'port=5006' in args[args.index('-artp') + 1]
    assert args[args.index('-p') + 1] == '7100,7101,7102' and '-key' in args
    assert args[args.index('-reg') + 1] == '/data/uxplay-register.txt', 'only phones paired this session skip the code'
    assert '-nohold' in args, 'a phone coming back after its connection dropped is not refused'
    quiet = build_args(Settings(pin='1', audio_enabled=False, av_sync='synced'), 1, 2)
    assert '-artp' not in quiet and quiet[quiet.index('-as') + 1] == '0' and '-reg' not in quiet
    assert 'sync=true' in quiet[quiet.index('-vrtp') + 1]


def test_line_buffered_uses_stdbuf_on_linux_only():
    found = {'stdbuf': '/usr/bin/stdbuf'}.get
    assert line_buffered(['/usr/local/bin/uxplay'], 'posix', found) == [
        '/usr/bin/stdbuf', '-oL', '-eL', '/usr/local/bin/uxplay']
    assert line_buffered(['/usr/local/bin/uxplay'], 'posix', lambda name: None) == ['/usr/local/bin/uxplay']
    assert line_buffered([r'C:\uxplay.exe'], 'nt', found) == [r'C:\uxplay.exe']


def test_help_capabilities_from_real_uxplay_174_help():
    text = ('UxPlay 1.74: An open-source AirPlay mirroring server.\n-h265     Support h265 (4K) video\n'
            '-pin[xxxx]Use a 4-digit pin code\n-vrtp pl  Use rtph26[4,5]pay\n-artp pl  Use rtpL16pay\n'
            '-key [fn] Store private key\n-reg [fn] Keep a register in $HOME/.uxplay.register to verify returning\n')
    caps = help_capabilities(text)
    assert caps == {'version': '1.74', 'vrtp': True, 'artp': True, 'pin': True, 'h265': True, 'key': True, 'reg': True}
    old = help_capabilities('UxPlay version 1.71.1\n-pin[xxxx] pin\n')
    assert old['version'] == '1.71.1' and not old['vrtp']
