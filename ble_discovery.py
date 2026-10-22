"""Off-by-default ownership/policy wrapper for the unmodified UxPlay BLE beacon.

No Bluetooth settings are read or changed. Dependencies are located only on
explicit opt-in. Raw beacon output (contains local addresses) stays console-only.
"""
from dataclasses import dataclass, field, replace
import importlib.util
import ipaddress
from pathlib import Path
import re

MODULES = ('psutil', 'winrt.windows.foundation.collections',
           'winrt.windows.devices.bluetooth.advertisement', 'winrt.windows.storage.streams')
CAPABILITY_ENV = 'MIRRORDESK_BLE_CAPABLE'  # scoped child environment, not a global setting


@dataclass(frozen=True)
class BlePlan:
    status: str = 'off'
    requested: bool = False
    ipv4: str | None = field(default=None, repr=False)
    path: Path | None = field(default=None, repr=False)
    script: Path | None = field(default=None, repr=False)

    @property
    def enabled(self):
        return self.status == 'ready'


def help_has_ble(text):
    return bool(re.search(r'^\s*-ble(?=\s|\[|$)', text, re.MULTILINE))


def prepare(requested, capable, discovery, root, find_spec=None):
    """No spawning or stale-file removal here; --check remains read-only probes."""
    if not requested:
        return BlePlan()
    if not capable:
        return BlePlan('capability_missing', True)
    try:
        address = str(ipaddress.IPv4Address(discovery.address))
    except (ValueError, TypeError, AttributeError):
        return BlePlan('no_ipv4', True)
    try:
        spec = find_spec or importlib.util.find_spec
        if any(spec(name) is None for name in MODULES):
            return BlePlan('deps_missing', True)
    except Exception:
        return BlePlan('deps_missing', True)
    root = Path(root).absolute()
    home = root/'.runtime/home'
    path = home/'.uxplay.ble'
    script = root/'third_party/uxplay_beacon/uxplay-beacon.py'
    plan = BlePlan('ready', True, address, path, script)
    try:
        if home.is_symlink() or home.parent.is_symlink():
            return replace(plan, status='unsafe_path')
        config = home/'.uxplay.beacon'
        if config.exists() or config.is_symlink():
            return replace(plan, status='config_present')
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return replace(plan, status='unsafe_path')
        if not script.is_file() or not (script.parent/'uxplay_beacon_module_winrt.py').is_file():
            return replace(plan, status='beacon_missing')
    except OSError:
        return replace(plan, status='file_error')
    return plan


def activate(plan):
    """Just before receiver start: remove ONLY the owned stale regular BLE file."""
    if not plan.enabled:
        return plan
    try:
        home = plan.path.parent
        if home.is_symlink() or home.parent.is_symlink():
            return replace(plan, status='unsafe_path')
        config = home/'.uxplay.beacon'
        if config.exists() or config.is_symlink():
            return replace(plan, status='config_present')
        if plan.path.is_symlink() or (plan.path.exists() and not plan.path.is_file()):
            return replace(plan, status='unsafe_path')
        if plan.path.exists():
            plan.path.unlink()
    except OSError:
        return replace(plan, status='file_error')
    return plan


def report(plan, diagnostics):
    if diagnostics:
        diagnostics.record('ble_preflight', ble_requested=plan.requested, ble_enabled=plan.enabled,
                           ble_status=plan.status)
    if plan.enabled:
        print('Bluetooth discovery: PC Bluetooth must be on. The first-pairing PIN is still required; '
              'pairing and media use the network. While running, the PC broadcasts its local IP + '
              'AirPlay port to nearby Bluetooth devices. No Bluetooth setting was changed.', flush=True)
    elif plan.requested:
        why = {'capability_missing': 'the existing receiver lacks -ble support',
               'deps_missing': 'Bluetooth Python support is missing (run Setup again)',
               'no_ipv4': 'the selected discovery adapter has no known IPv4',
               'config_present': 'a scoped beacon config exists (move it aside manually)',
               'unsafe_path': 'the scoped beacon path is not a safe owned regular file',
               'file_error': 'the scoped beacon file could not be prepared',
               'beacon_missing': 'the upstream beacon files are missing'}.get(plan.status, 'the check failed')
        print(f'Bluetooth discovery unavailable: {why}; continuing with network discovery only.', flush=True)


def command(plan, python):
    return [python, str(plan.script), '--path', str(plan.path), '--ipv4', plan.ipv4]


def output_category(line):
    text = line.lower()
    if 'importerror' in text or 'failed to import' in text or 'modulenotfounderror' in text:
        return 'import_error'
    if 'aborted' in text or 'failed to start beacon' in text or 'failed to start publisher' in text:
        return 'aborted_no_bluetooth'
    if ('advertisement (' in text and 'registered' in text) or 'status change to: started' in text:
        return 'started'
    return None


def capture_output(stream, diagnostics):
    seen = set()
    try:
        while True:
            chunk = stream.readline(1024)
            if not chunk:
                break
            text = chunk.decode('utf-8', 'replace')
            category = output_category(text)
            if category and category not in seen:
                seen.add(category)
                if diagnostics:
                    diagnostics.record('ble_signal', ble_status=category)
            print(text, end='', flush=True)  # console only, never record text
    finally:
        stream.close()
