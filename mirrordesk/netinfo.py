"""Interface inventory for viewer URLs and WebRTC host candidates.

Classifies adapters as Ethernet, Wi-Fi, Tailscale, USB tether (iPhone
Personal Hotspot over USB / RNDIS), VPN or virtual. Names and addresses stay in
memory and in authenticated API responses only; diagnostics never store them.
"""
from dataclasses import dataclass, asdict
import ipaddress
import json
import re
import shutil
import socket
import subprocess

import ifaddr

TAILSCALE_V4 = ipaddress.ip_network('100.64.0.0/10')
IPHONE_HOTSPOT = ipaddress.ip_network('172.20.10.0/28')   # iOS Personal Hotspot subnet
LINK_LOCAL = ipaddress.ip_network('169.254.0.0/16')

_VIRTUAL_HINTS = ('docker', 'br-', 'veth', 'vethernet', 'hyper-v', 'wsl', 'vmware', 'virtualbox',
                  'vboxnet', 'virbr', 'lxc', 'cni', 'flannel', 'podman', 'loopback pseudo')
_VPN_HINTS = ('wireguard', 'zerotier', 'wintun', 'tap-windows', 'tap adapter', 'openvpn', 'nordlynx')
# ZeroTier names its Linux interfaces zt + 8 letters (ZimaOS's remote access uses it, as "zt4atdzzjl").
_ZEROTIER_NAME = re.compile(r'^zt[0-9a-z]{6,12}$')
_VPN_NAMES = ('wg0', 'wg1', 'tun0', 'tun1', 'utun0', 'utun1', 'ppp0')
# 'usb' also matches USB Ethernet dongles; the UI labels this kind "USB network".
_USB_HINTS = ('apple mobile device', 'iphone', 'rndis', 'usb', 'remote ndis')
_WIFI_HINTS = ('wi-fi', 'wifi', 'wireless', 'wlan', '802.11')
_ETHERNET_HINTS = ('ethernet', 'eth', 'enp', 'eno', 'ens', 'enx', 'gbe', 'realtek', 'killer e', 'lan', 'en0', 'en1')
KINDS = ('loopback', 'ethernet', 'wifi', 'usb_tether', 'tailscale', 'vpn', 'virtual', 'other')
# Kinds that browsers on other devices can plausibly reach.
VIEWER_KINDS = ('ethernet', 'wifi', 'usb_tether', 'tailscale', 'vpn', 'other')


@dataclass
class Interface:
    name: str
    label: str
    kind: str
    ipv4: list

    def as_dict(self):
        return asdict(self)


def classify(name, label, addresses):
    text = f'{name} {label}'.lower()
    nets = []
    for address in addresses:
        try:
            nets.append(ipaddress.ip_address(address))
        except ValueError:
            pass
    if any(ip.is_loopback for ip in nets) or text.strip().startswith('lo'):
        return 'loopback'
    if 'tailscale' in text or any(ip in TAILSCALE_V4 for ip in nets):
        return 'tailscale'
    if any(ip in IPHONE_HOTSPOT for ip in nets) or any(h in text for h in _USB_HINTS):
        return 'usb_tether'
    if any(h in text for h in _VIRTUAL_HINTS):
        return 'virtual'
    words = text.replace('_', ' ').split()
    if (any(h in text for h in _VPN_HINTS) or any(w in _VPN_NAMES for w in words)
            or any(_ZEROTIER_NAME.match(w) for w in words)):
        return 'vpn'
    if any(h in text for h in _WIFI_HINTS) or (words and words[0].startswith(('wlp', 'wlx'))):
        return 'wifi'
    if any(h in text for h in _ETHERNET_HINTS):
        return 'ethernet'
    return 'other'


def list_interfaces(adapters=None):
    """Return Interface entries with usable IPv4 addresses (no link-local)."""
    result = []
    for adapter in (adapters if adapters is not None else ifaddr.get_adapters()):
        ipv4 = []
        for ip in adapter.ips:
            if isinstance(ip.ip, str):
                try:
                    parsed = ipaddress.ip_address(ip.ip)
                except ValueError:
                    continue
                if parsed in LINK_LOCAL or parsed.is_unspecified:
                    continue
                ipv4.append(ip.ip)
        if not ipv4:
            continue
        name = str(adapter.name)
        label = str(getattr(adapter, 'nice_name', '') or name)
        result.append(Interface(name, label, classify(name, label, ipv4), ipv4))
    return result


def candidate_addresses(interfaces, include_loopback=True, include_virtual=False):
    """IPv4 addresses for aioice host candidates, loopback first."""
    addresses = ['127.0.0.1'] if include_loopback else []
    for iface in interfaces:
        if iface.kind == 'loopback':
            continue
        if iface.kind == 'virtual' and not include_virtual:
            continue
        for address in iface.ipv4:
            if address not in addresses:
                addresses.append(address)
    return addresses


def tailscale_status(timeout=3.0):
    """Best-effort read-only `tailscale status --json`; None when unavailable."""
    exe = shutil.which('tailscale') or shutil.which('tailscale.exe')
    if exe is None:
        for candidate in (r'C:\Program Files\Tailscale\tailscale.exe',):
            try:
                with open(candidate, 'rb'):
                    exe = candidate
                    break
            except OSError:
                pass
    if exe is None:
        return None
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        completed = subprocess.run([exe, 'status', '--json', '--self=true', '--peers=false'],
                                   capture_output=True, timeout=timeout, creationflags=flags)
        if completed.returncode != 0:
            return {'available': True, 'running': False}
        data = json.loads(completed.stdout.decode('utf-8', 'replace'))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    me = data.get('Self') or {}
    dns_name = str(me.get('DNSName') or '').rstrip('.')
    addresses = [a for a in (me.get('TailscaleIPs') or []) if ':' not in str(a)]
    return {'available': True, 'running': data.get('BackendState') == 'Running',
            'dns_name': dns_name or None, 'ipv4': addresses[:2]}


def viewer_urls(interfaces, port, scheme='http', tailscale=None):
    urls = []
    seen = set()
    for iface in interfaces:
        if iface.kind not in VIEWER_KINDS:
            continue
        for address in iface.ipv4:
            url = f'{scheme}://{address}:{port}/'
            if url not in seen:
                seen.add(url)
                urls.append({'url': url, 'kind': iface.kind, 'label': iface.label})
    if tailscale and tailscale.get('running') and tailscale.get('dns_name'):
        url = f'{scheme}://{tailscale["dns_name"]}:{port}/'
        if url not in seen:
            urls.append({'url': url, 'kind': 'tailscale', 'label': 'Tailscale MagicDNS'})
    host = socket.gethostname()
    if host:
        urls.append({'url': f'{scheme}://{host.lower()}.local:{port}/', 'kind': 'mdns', 'label': 'Local hostname'})
    return urls
