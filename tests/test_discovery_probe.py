"""Pure discovery-readiness logic. Linux; Windows adapter data are FAKE fixtures
modelled on the user's reported adapters (not a readback from their PC)."""
import pytest

import discovery_probe as dp


def adapter(alias, description, type_='Ethernet', up=True, gateways=1, ipv4=1, address='10.0.0.2', index=1):
    return {'index': index, 'alias': alias, 'description': description, 'type': type_,
            'up': up, 'gateways': gateways, 'ipv4': ipv4, 'address': address}


ETH = adapter('Ethernet', 'Realtek Gaming GbE Family Controller', address='192.168.1.20', index=11)
WIFI = adapter('Wi-Fi', 'Intel(R) Wi-Fi 6 AX200 160MHz', 'Wireless80211', address='192.168.1.30', index=12)
TS_GW = adapter('Tailscale', 'Tailscale Tunnel', 'Unknown', address='100.64.0.5', index=13)
TS_NOGW = dict(TS_GW, gateways=0)
HYPERV = adapter('vEthernet (WSL)', 'Hyper-V Virtual Ethernet Adapter', address='172.20.0.1', index=14)
LOOP = adapter('Loopback Pseudo-Interface 1', 'Software Loopback Interface 1', 'Loopback', address='127.0.0.1', index=1)
WAN = adapter('WAN Miniport', 'WAN Miniport (IP)', 'Ppp', up=False, gateways=0, ipv4=0, address=None, index=20)


@pytest.mark.parametrize('order, expected', [
    ([ETH, WIFI], ETH),
    ([WIFI, ETH], WIFI),
    ([TS_GW, WIFI, ETH], TS_GW),          # enumeration order wins, not route metric
    ([TS_NOGW, WIFI, ETH], WIFI),         # no gateway -> skipped
    ([HYPERV, ETH], ETH),
    ([LOOP, WAN, ETH], ETH),
    ([dict(ETH, ipv4=0, address=None), WIFI], WIFI),  # no IPv4 unicast -> continue
    ([dict(ETH, up=False), WIFI], WIFI),
    ([LOOP, WAN, TS_NOGW, HYPERV], None),
    ([], None),
])
def test_selection_replicates_mdnsd_order_and_filters(order, expected):
    assert dp.select_announce_adapter(order) == expected


@pytest.mark.parametrize('description, filtered', [
    ('Hyper-V Virtual Ethernet Adapter', True), ('Some Virtual NIC', True), ('WSL bridge', True),
    ('vEthernet (Default Switch)', True),
    ('hyper-v lower case', False), ('some virtual nic', False), ('wsl lower', False),
    ('VETHERNET upper', False), ('Microsoft Wi-Fi Direct Virtual Adapter', True),
])
def test_description_filter_is_case_sensitive_like_wcsstr(description, filtered):
    candidate = adapter('X', description)
    assert (dp.select_announce_adapter([candidate]) is None) is filtered


def test_selection_ignores_malformed_entries():
    assert dp.select_announce_adapter([None, 'x', {'up': True}, ETH]) == ETH
    assert dp.select_announce_adapter(None) is None


@pytest.mark.parametrize('item, kind', [
    (ETH, 'ethernet'), (WIFI, 'wifi'), (TS_GW, 'vpn_or_virtual'), (HYPERV, 'vpn_or_virtual'),
    (adapter('E', 'WireGuard Tunnel', 'Ethernet'), 'vpn_or_virtual'),
    (adapter('E', 'Wintun Userspace Tunnel'), 'vpn_or_virtual'),
    (adapter('E', 'ZeroTier Virtual Port'), 'vpn_or_virtual'),
    (adapter('E', 'TAP-Windows Adapter V9'), 'vpn_or_virtual'),
    (adapter('E', 'OpenVPN Data Channel Offload'), 'vpn_or_virtual'),
    (adapter('E', 'Cisco AnyConnect VPN Virtual Miniport'), 'vpn_or_virtual'),
    (adapter('E', 'Intel Ethernet I219-V', 'GigabitEthernet'), 'ethernet'),
    (adapter('P', 'PPP', 'Ppp'), 'vpn_or_virtual'),
    (adapter('X', 'Thing', 'Unknown'), 'other'), (None, 'other'),
])
def test_adapter_kind(item, kind):
    assert dp.adapter_kind(item) == kind


@pytest.mark.parametrize('raw, profile', [
    ('Public', 'public'), ('Private', 'private'), ('DomainAuthenticated', 'domain'),
    (0, 'public'), (1, 'private'), (2, 'domain'), (None, 'unknown'), ('Weird', 'unknown'), (True, 'unknown'),
])
def test_profile_category(raw, profile):
    assert dp.profile_category(raw) == profile


ON = {'Domain': {'enabled': True, 'default_inbound': 'Block'},
      'Private': {'enabled': True, 'default_inbound': 'Block'},
      'Public': {'enabled': True, 'default_inbound': 'Block'}}
USER_RULES = [{'action': 'Allow', 'profile': 'Private'}, {'action': 'Block', 'profile': 'Public'}]


@pytest.mark.parametrize('rules, profiles, profile, state', [
    (USER_RULES, ON, 'public', 'block'),
    (USER_RULES, ON, 'private', 'allow'),
    (USER_RULES, ON, 'domain', 'no_rule'),
    ([{'action': 'Allow', 'profile': 'Any'}, {'action': 'Block', 'profile': 'Public'}], ON, 'public', 'block'),
    ([{'action': 'Allow', 'profile': 'Any'}], ON, 'public', 'allow'),
    ([{'action': 'Allow', 'profile': 'Domain, Private'}], ON, 'private', 'allow'),
    ([{'action': 'Allow', 'profile': 'Domain, Private'}], ON, 'public', 'no_rule'),
    ([{'action': 'Block', 'profile': 'Domain, Private, Public'}, {'action': 'Allow', 'profile': 'Any'}], ON, 'private', 'block'),
    (USER_RULES, dict(ON, Public={'enabled': False, 'default_inbound': 'Block'}), 'public', 'off'),
    ([], dict(ON, Private={'enabled': True, 'default_inbound': 'Allow'}), 'private', 'allow'),
    ([], ON, 'private', 'no_rule'),
    (None, ON, 'private', 'unknown'),
    (USER_RULES, None, 'private', 'allow'),     # rules usable without profile section
    ([], None, 'private', 'unknown'),           # default inbound unknown
    (USER_RULES, ON, 'unknown', 'unknown'),
    ([{'action': 'Bogus', 'profile': 7}, 'x'], ON, 'private', 'no_rule'),
])
def test_firewall_state_block_wins(rules, profiles, profile, state):
    assert dp.firewall_state(rules, profiles, profile) == state


@pytest.mark.parametrize('kind, profile, firewall, verdict', [
    ('ethernet', 'public', 'block', 'public_network'),
    ('ethernet', 'public', 'no_rule', 'public_network'),
    ('ethernet', 'public', 'unknown', 'public_network'),
    ('ethernet', 'public', 'allow', 'ready'),
    ('ethernet', 'public', 'off', 'ready'),
    ('wifi', 'private', 'allow', 'ready'),
    ('wifi', 'private', 'block', 'firewall_blocked'),
    ('ethernet', 'domain', 'block', 'firewall_blocked'),
    ('wifi', 'private', 'unknown', 'ready'),
    ('vpn_or_virtual', 'private', 'allow', 'vpn_or_virtual_adapter'),
    ('wifi', 'unknown', 'allow', 'unknown'),
    (None, 'unknown', 'unknown', 'no_adapter'),
])
def test_verdict(kind, profile, firewall, verdict):
    assert dp.verdict(kind, profile, firewall) == verdict


def test_both_ethernet_and_wifi_up():
    assert dp.both_ethernet_and_wifi_up([ETH, WIFI, LOOP]) is True
    assert dp.both_ethernet_and_wifi_up([dict(ETH, up=False), WIFI]) is False
    assert dp.both_ethernet_and_wifi_up([TS_GW, WIFI]) is False
    assert dp.both_ethernet_and_wifi_up(None) is False
