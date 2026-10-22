"""Read-only, fail-open phone-discovery readiness check (Windows).

Predicts which IPv4 adapter the user's UxPlay built-in mDNS responder announces
on and whether Windows profile/firewall state will block it. Changes nothing.

Model source: UxPlay d29dfabe lib/mdnsd/mdnsd.c mdns_get_default_ipv4()
(Windows branch, lines 348-391): FIRST GetAdaptersAddresses(AF_INET,
GAA_FLAG_INCLUDE_GATEWAYS) entry that is Up, not software loopback, has a
gateway, whose Description contains none of Hyper-V/Virtual/WSL/vEthernet
(case-sensitive wcsstr), and has an IPv4 unicast address. Static source
analysis; the installed binary's choice is not read back.
"""
import json
import re
import os
import subprocess
from dataclasses import dataclass, field, replace

# Case-sensitive, exactly as mdnsd.c lines 377-380.
MDNSD_EXCLUDED = ('Hyper-V', 'Virtual', 'WSL', 'vEthernet')
VPN_HINTS = ('tailscale', 'wireguard', 'wintun', 'zerotier', 'tap-windows', 'tap adapter',
             'openvpn', 'vpn', 'virtual', 'hyper-v', 'vethernet', 'wsl')
ETHERNET_TYPES = {'ethernet', 'gigabitethernet', 'fastethernetfx', 'fastethernett'}
WIFI_TYPES = {'wireless80211'}
VIRTUAL_TYPES = {'ppp', 'tunnel'}
VERDICTS = ('ready', 'public_network', 'firewall_blocked', 'vpn_or_virtual_adapter', 'no_adapter', 'unknown')
KINDS = ('ethernet', 'wifi', 'vpn_or_virtual', 'other')
PROFILES = ('private', 'public', 'domain', 'unknown')
FIREWALL = ('allow', 'block', 'no_rule', 'off', 'unknown')


def _text(value):
    return value if isinstance(value, str) else ''


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def select_announce_adapter(adapters):
    """Return the adapter mdnsd would pick, or None. Never raises."""
    if not isinstance(adapters, (list, tuple)):
        return None
    for item in adapters:
        if not isinstance(item, dict):
            continue
        if item.get('up') is not True or _text(item.get('type')).lower() == 'loopback':
            continue
        if _count(item.get('gateways')) < 1:
            continue
        description = _text(item.get('description'))
        if any(token in description for token in MDNSD_EXCLUDED):
            continue
        if _count(item.get('ipv4')) < 1:
            continue  # mdnsd: no FirstUnicastAddress -> keep looking
        return item
    return None


def adapter_kind(item):
    if not isinstance(item, dict):
        return 'other'
    description = _text(item.get('description')).lower()
    type_ = _text(item.get('type')).lower()
    if any(hint in description for hint in VPN_HINTS) or type_ in VIRTUAL_TYPES:
        return 'vpn_or_virtual'
    if type_ in WIFI_TYPES:
        return 'wifi'
    if type_ in ETHERNET_TYPES:
        return 'ethernet'
    return 'other'


def profile_category(raw):
    """NetConnectionProfile NetworkCategory: 0 Public, 1 Private, 2 DomainAuthenticated."""
    if type(raw) is int:
        return {0: 'public', 1: 'private', 2: 'domain'}.get(raw, 'unknown')
    return {'public': 'public', 'private': 'private', 'domainauthenticated': 'domain',
            'domain': 'domain'}.get(_text(raw).lower(), 'unknown')


def _rule_profiles(raw):
    text = _text(raw)
    if text.strip().lower() in ('any', 'all'):
        return {'domain', 'private', 'public'}
    return {part.strip().lower() for part in text.split(',') if part.strip()}


def firewall_state(rules, profiles, profile):
    """Firewall outcome for the program on `profile`. Any enabled Block wins."""
    if profile not in ('private', 'public', 'domain'):
        return 'unknown'
    settings = profiles.get(profile.capitalize()) if isinstance(profiles, dict) else None
    enabled = settings.get('enabled') if isinstance(settings, dict) else None
    if enabled is False or _text(enabled).lower() == 'false':  # NotConfigured is NOT off
        return 'off'
    if not isinstance(rules, (list, tuple)):
        return 'unknown'
    actions = {_text(rule.get('action')).lower() for rule in rules
               if isinstance(rule, dict) and profile in _rule_profiles(rule.get('profile'))}
    if 'block' in actions:
        return 'block'
    if 'allow' in actions:
        return 'allow'
    if isinstance(settings, dict):
        default = _text(settings.get('default_inbound')).lower()
        if default == 'allow':
            return 'allow'
        if default in ('block', 'notconfigured'):
            return 'no_rule'
    return 'unknown'


def verdict(kind, profile, firewall):
    if kind is None:
        return 'no_adapter'
    if kind == 'vpn_or_virtual':
        return 'vpn_or_virtual_adapter'
    if firewall == 'block':
        return 'public_network' if profile == 'public' else 'firewall_blocked'
    if profile == 'public' and firewall not in ('allow', 'off'):
        return 'public_network'
    if profile in ('private', 'domain', 'public'):
        return 'ready'
    return 'unknown'


def both_ethernet_and_wifi_up(adapters):
    if not isinstance(adapters, (list, tuple)):
        return False
    kinds = {adapter_kind(a) for a in adapters if isinstance(a, dict) and a.get('up') is True}
    return {'ethernet', 'wifi'} <= kinds


# ---------------------------------------------------------------- runner ----

FULL_TIMEOUT_SECONDS = 8
LIGHT_TIMEOUT_SECONDS = 6
PROGRAM_ENV = 'MIRRORDESK_DISCOVERY_PROGRAM'

# Fixed, read-only. No runtime value is interpolated; the program path arrives
# only via $env:MIRRORDESK_DISCOVERY_PROGRAM. Each section fails independently
# to $null. NetworkInterface enumeration wraps GetAdaptersAddresses and keeps
# its order (same source as mdnsd's choice).
_ADAPTERS = r"""
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$ErrorActionPreference='Stop'
$r=[ordered]@{adapters=$null;profiles=$null;rules=$null;firewall=$null}
try{$r.adapters=@([System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()|ForEach-Object{
$p=$_.GetIPProperties();$i=$null;try{$i=$p.GetIPv4Properties().Index}catch{}
$g=@($p.GatewayAddresses|Where-Object{$_.Address.AddressFamily -eq 'InterNetwork'})
$u=@($p.UnicastAddresses|Where-Object{$_.Address.AddressFamily -eq 'InterNetwork'})
[ordered]@{index=$i;alias=[string]$_.Name;description=[string]$_.Description;type=[string]$_.NetworkInterfaceType;
up=($_.OperationalStatus -eq 'Up');gateways=$g.Count;ipv4=$u.Count;addresses=@($u|ForEach-Object{[string]$_.Address});
address=$(if($u.Count){[string]$u[0].Address}else{$null})}})}catch{}
try{$r.profiles=@(Get-NetConnectionProfile|ForEach-Object{[ordered]@{index=[int]$_.InterfaceIndex;category=[string]$_.NetworkCategory}})}catch{}
"""
_FIREWALL = r"""
try{$prog=$env:MIRRORDESK_DISCOVERY_PROGRAM;if($prog){$r.rules=@(Get-NetFirewallApplicationFilter -PolicyStore ActiveStore|
Where-Object{$_.Program -ieq $prog}|Get-NetFirewallRule|Where-Object{$_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound'}|
ForEach-Object{[ordered]@{action=[string]$_.Action;profile=[string]$_.Profile}})}}catch{$r.rules=$null}
try{$r.firewall=@(Get-NetFirewallProfile -PolicyStore ActiveStore|ForEach-Object{[ordered]@{name=[string]$_.Name;
enabled=[string]$_.Enabled;default_inbound=[string]$_.DefaultInboundAction;
unicast_response=[string]$_.AllowUnicastResponseToMulticast}})}catch{}
"""
_EMIT = "\n$r|ConvertTo-Json -Depth 4 -Compress\n"
FULL_SCRIPT = _ADAPTERS + _FIREWALL + _EMIT
LIGHT_SCRIPT = _ADAPTERS + _EMIT


@dataclass(frozen=True)
class DiscoveryResult:
    ok: bool = False
    verdict: str = 'unknown'
    kind: str = 'other'
    profile: str = 'unknown'
    firewall: str = 'unknown'
    both_up: bool = False
    adapters_up: int = 0
    candidates: int = 0
    # Console-only (never recorded in diagnostics):
    alias: str | None = None
    address: str | None = None
    # Path self-test inputs, memory only; excluded from repr/equality/diagnostics.
    adapters: tuple = field(default=(), repr=False, compare=False)

    def key(self):
        """Comparison identity for the change watcher."""
        return (self.alias, self.address, self.verdict)


UNKNOWN = DiscoveryResult()


def _list(value):
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else None


def interpret(data, full=True):
    if not isinstance(data, dict):
        return UNKNOWN
    adapters = _list(data.get('adapters'))
    if adapters is None:
        return UNKNOWN
    adapters = [a for a in adapters if isinstance(a, dict)]
    chosen = select_announce_adapter(adapters)
    up = [a for a in adapters if a.get('up') is True and _text(a.get('type')).lower() != 'loopback']
    # Retain read-only path inputs without changing selection or readiness verdicts.
    retained = []
    for adapter in adapters:
        item = dict(adapter)
        profile = next((profile_category(p.get('category')) for p in _list(data.get('profiles')) or []
                        if isinstance(p, dict) and p.get('index') == item.get('index')), 'unknown')
        settings = next((p for p in _list(data.get('firewall')) or [] if isinstance(p, dict)
                         and _text(p.get('name')).lower() == profile), {}) if full else {}
        raw = settings.get('unicast_response')
        item['unicast_response'] = (False if raw is False or _text(raw).lower() == 'false' else
                                    True if raw is True or _text(raw).lower() == 'true' else None)
        if settings.get('enabled') is False or _text(settings.get('enabled')).lower() == 'false':
            item['unicast_response'] = True  # disabled firewall cannot suppress replies
        retained.append(item)
    base = dict(ok=True, adapters=tuple(retained), both_up=both_ethernet_and_wifi_up(adapters), adapters_up=len(up),
                candidates=sum(1 for a in up if select_announce_adapter([a]) is not None))
    if chosen is None:
        return DiscoveryResult(verdict='no_adapter', **base)
    profile = 'unknown'
    profiles = _list(data.get('profiles'))
    if profiles is not None:
        match = [p for p in profiles if isinstance(p, dict) and type(p.get('index')) is int
                 and p.get('index') == chosen.get('index')]
        profile = profile_category(match[0].get('category')) if match else 'unknown'
    firewall = 'unknown'
    if full:
        profile_settings = {}
        for entry in _list(data.get('firewall')) or []:
            if isinstance(entry, dict) and _text(entry.get('name')) in ('Domain', 'Private', 'Public'):
                profile_settings[entry['name']] = entry
        firewall = firewall_state(_list(data.get('rules')), profile_settings or None, profile)
    kind = adapter_kind(chosen)
    address = chosen.get('address') if isinstance(chosen.get('address'), str) else None
    return DiscoveryResult(verdict=verdict(kind, profile, firewall), kind=kind, profile=profile,
                           firewall=firewall, alias=_text(chosen.get('alias')) or None, address=address, **base)


def run_probe(program=None, full=True, run=None, environ=None):
    """Run the fixed read-only probe. Never raises; failures -> UNKNOWN."""
    if run is None:
        if os.name != 'nt':
            return UNKNOWN
        run = subprocess.run
    try:
        env = dict(os.environ if environ is None else environ)
        env.pop(PROGRAM_ENV, None)
        if full and program:
            env[PROGRAM_ENV] = str(program)
        script = FULL_SCRIPT if full else LIGHT_SCRIPT
        completed = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                        env=env, capture_output=True, stdin=subprocess.DEVNULL,
                        timeout=FULL_TIMEOUT_SECONDS if full else LIGHT_TIMEOUT_SECONDS,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if completed is None or completed.returncode != 0:
            return UNKNOWN
        return interpret(json.loads(completed.stdout.decode('utf-8-sig', errors='replace')), full=full)
    except Exception:
        return UNKNOWN


def diagnostic_fields(result, mode):
    """Categorical, allowlisted fields only. Never alias/description/address/path."""
    return {'discovery_mode': mode, 'discovery_ok': result.ok, 'discovery_verdict': result.verdict,
            'discovery_kind': result.kind, 'discovery_profile': result.profile,
            'discovery_firewall': result.firewall, 'discovery_both_up': result.both_up,
            'discovery_adapters_up': result.adapters_up, 'discovery_candidates': result.candidates}


def change_fields(previous, current):
    fields = diagnostic_fields(current, 'light')
    fields.pop('discovery_firewall')  # light probe never reads firewall
    fields.update(discovery_previous_verdict=previous.verdict,
                  discovery_adapter_changed=previous.alias != current.alias,
                  discovery_address_changed=previous.address != current.address)
    return fields


# ------------------------------------------------------- console guidance ---
UNKNOWN_LINE = 'Could not check phone discovery; continuing.'
CHANGE_LINE = ('Network changed while MirrorDesk Web is running. The receiver still announces on the '
               'previous connection; press Ctrl+C and start MirrorDesk Web again.')
_KIND_LABEL = {'ethernet': 'Ethernet', 'wifi': 'Wi-Fi', 'vpn_or_virtual': 'VPN/virtual', 'other': 'other'}
_PROFILE_LABEL = {'private': 'Private', 'public': 'Public', 'domain': 'Domain'}


def _console_name(value):
    """Local console only: printable, single line, bounded."""
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b.?", " ", _text(value))
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:60]
    return text or 'an unnamed adapter'


def summary_lines(result):
    """Plain-language guidance for the launcher console. Changes nothing."""
    if not result.ok or result.verdict == 'unknown':
        return [UNKNOWN_LINE]
    if result.verdict == 'no_adapter':
        return ['Phone discovery: No connected network adapter with a gateway was found, so the iPhone '
                'cannot find MirrorDesk Web. Connect Ethernet or Wi-Fi to your home network, then '
                'restart MirrorDesk Web. Nothing was changed.']
    name = _console_name(result.alias)
    kind = _KIND_LABEL.get(result.kind, 'other')
    lines = []
    if result.verdict == 'vpn_or_virtual_adapter':
        lines.append(f'Phone discovery: MirrorDesk Web will be announced only on {name}, which looks like a '
                     'VPN or virtual adapter, not your home network, so the iPhone will probably not see it. '
                     'Temporarily disconnect the VPN (for example Tailscale) or connect Ethernet/Wi-Fi, then '
                     'restart MirrorDesk Web. Nothing was changed.')
    elif result.verdict == 'public_network':
        page = kind if result.kind in ('ethernet', 'wifi') else 'Ethernet or Wi-Fi'
        where = f'{name} ({kind})' if result.kind in ('ethernet', 'wifi') else name
        lines.append(f'Phone discovery: MirrorDesk Web will be announced only on {where}. Windows marks '
                     'that network Public and the receiver is blocked on Public networks, so the iPhone will '
                     f'not see it. On your trusted home network: Settings > Network & internet > {page} > '
                     'Network profile type > Private, then restart MirrorDesk Web. Nothing was changed.')
    elif result.verdict == 'firewall_blocked':
        profile = _PROFILE_LABEL.get(result.profile, 'current')
        lines.append(f'Phone discovery: MirrorDesk Web will be announced only on {name} ({kind}, {profile} '
                     'network), but a Windows firewall rule blocks the receiver there, so the iPhone will not '
                     'see it. Check Windows Security > Firewall & network protection > Allow an app through '
                     'firewall for uxplay.exe, then restart MirrorDesk Web. Nothing was changed.')
    else:
        profile = _PROFILE_LABEL.get(result.profile, 'unknown')
        lines.append(f'Phone discovery: MirrorDesk Web will be announced on {name} ({kind}, {profile} network). '
                     'The iPhone must be on the same home network. Not proof the phone will see it.')
    if result.both_up and result.verdict != 'no_adapter':
        lines.append(f'Ethernet and Wi-Fi are both connected; discovery uses only {name}. If the iPhone does '
                     'not see MirrorDesk Web, disconnect one of them and restart MirrorDesk Web.')
    return lines


def as_light(result):
    """What the LIGHT probe would report for the same adapters/profiles (no firewall)."""
    if not result.ok or result.verdict in ('unknown', 'no_adapter'):
        return result
    return replace(result, firewall='unknown', verdict=verdict(result.kind, result.profile, 'unknown'))
