"""Windows: install (or remove) the libusb-win32 driver for a plugged-in iPhone.

Cable mirroring needs the iPhone's composite USB device on the libusb-win32
driver (Apple's driver hides it from libusb). install() runs wdi-simple
(libwdi, built by tools/build_wdi_simple.py with libusb-win32 1.2.6.0 embedded)
elevated: Windows asks for permission once, then the driver package is created,
self-signed and installed for exactly that device, as Zadig does. restore()
removes those packages again so Apple's driver (iTunes / Apple Devices) returns.

Both run elevated helpers with ShellExecuteEx("runas") and wait for them; their
output goes to a log file in the data folder because an elevated process cannot
hand its console back. Read-only detection (pnputil) needs no rights.
"""
import ctypes
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = (ROOT / 'tools' / 'usb-driver' / 'wdi-simple.exe', ROOT / 'build' / 'usb-driver' / 'wdi-simple.exe')
APPLE_VID = 0x05AC
DEVICE_NAME = 'iPhone (MirrorDesk cable mirroring)'
INF_NAME = 'mirrordesk_iphone.inf'
TIMEOUT_SECONDS = 300
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class DriverError(RuntimeError):
    """A user-facing reason the driver could not be changed."""


def tool():
    override = os.environ.get('MIRRORDESK_WDI_SIMPLE')
    if override:
        return Path(override) if Path(override).is_file() else None
    return next((p for p in CANDIDATES if p.is_file()), None)


def _on_windows():
    return os.name == 'nt'


def available():
    return _on_windows() and tool() is not None


def _pnputil(*args):
    result = subprocess.run(['pnputil', *args], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
                            encoding='utf-8', errors='replace')
    return result.stdout


def connected_iphones(text=None):
    """[(description, product id, uses libusb)] for Apple phones/tablets plugged in now
    (composite parent devices only; interfaces are listed separately by Windows)."""
    if os.name != 'nt' and text is None:
        return []
    text = text if text is not None else _pnputil('/enum-devices', '/connected')
    found = []
    for block in re.split(r'\r?\n\s*\r?\n', text):
        fields = dict(re.findall(r'^\s*([^:\r\n]+?):\s+(.*?)\s*$', block, re.M))
        instance = fields.get('Instance ID', '')
        match = re.match(r'USB\\VID_05AC&PID_(12[9A][0-9A-F])\\', instance, re.I)
        if not match or '&MI_' in instance.upper():
            continue
        driver = ' '.join([fields.get('Class Name', ''), fields.get('Driver Name', ''),
                           fields.get('Manufacturer Name', '')]).lower()
        found.append((fields.get('Device Description') or 'Apple device', int(match.group(1), 16), 'libusb' in driver))
    return found


def libusb_packages(text=None):
    """Published names (oemNN.inf) of libusb-win32 driver packages made for Apple devices."""
    text = text if text is not None else _pnputil('/enum-drivers')
    names = []
    for block in re.split(r'\r?\n\s*\r?\n', text):
        fields = dict(re.findall(r'^\s*([^:\r\n]+?):\s+(.*?)\s*$', block, re.M))
        if fields.get('Provider Name', '').lower() != 'libusb-win32':
            continue
        published = fields.get('Published Name', '')
        signer = fields.get('Signer Name', '')
        inf = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'INF' / published
        try:
            content = inf.read_text(encoding='utf-8', errors='replace') if inf.is_file() else ''
        except OSError:
            content = ''
        if re.fullmatch(r'oem\d+\.inf', published, re.I) and ('VID_05AC' in signer.upper() or 'VID_05AC' in content.upper()):
            names.append(published)
    return names


def _run_elevated(command_line, log_path):
    """Run `cmd /c <command_line> > log` elevated and hidden; return its exit code.
    Raises DriverError if the user declines the Windows permission prompt."""
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HANDLE),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                    ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                    ('dwHotKey', wintypes.DWORD), ('hIconOrMonitor', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]

    SEE_MASK_NOCLOSEPROCESS, SW_HIDE, ERROR_CANCELLED = 0x40, 0, 1223
    cmd = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'cmd.exe')
    # ( ... ) so the log captures every command of a chain, not just the last one.
    info = ShellExecuteInfo(cbSize=ctypes.sizeof(ShellExecuteInfo), fMask=SEE_MASK_NOCLOSEPROCESS, lpVerb='runas',
                            lpFile=cmd, lpParameters=f'/d /s /c "( {command_line} ) > "{log_path}" 2>&1"',
                            nShow=SW_HIDE)
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            raise DriverError('Windows permission was not given, so nothing was changed.')
        raise DriverError(f'Could not start the driver installer (Windows error {error}).')
    try:
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        if kernel32.WaitForSingleObject(info.hProcess, TIMEOUT_SECONDS * 1000) != 0:
            raise DriverError('The driver installer did not finish in time.')
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)


def _log_dir(data_dir):
    directory = Path(data_dir) / 'logs'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def install(data_dir, run_elevated=_run_elevated, phones=connected_iphones):
    """Install libusb-win32 for the plugged-in iPhone. Returns a user-facing summary."""
    if not _on_windows():
        raise DriverError('The cable driver is only needed on Windows.')
    executable = tool()
    if executable is None:
        raise DriverError('The driver installer is missing from this MirrorDesk folder. Reinstall MirrorDesk.')
    plugged = phones()
    if not plugged:
        raise DriverError('No iPhone or iPad found. Plug it in with a cable, unlock it, then try again.')
    description, pid, libusb = plugged[0]
    if libusb:
        return f'{description} already uses the cable driver.'
    log = _log_dir(data_dir) / 'usb-driver-install.log'
    command = (f'"{executable}" --vid 0x{APPLE_VID:04X} --pid 0x{pid:04X} --type 1 --name "{DEVICE_NAME}" '
               f'--inf {INF_NAME} --dest "{Path(tempfile.gettempdir()) / "mirrordesk-usb-driver"}" '
               '--timeout 120000 --log 1')
    code = run_elevated(command, log)
    text = log.read_text(encoding='utf-8', errors='replace') if log.is_file() else ''
    if code != 0 or 'Success' not in text:
        detail = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), f'exit code {code}')
        raise DriverError(f'The driver could not be installed ({detail[:200]}). Details: {log}')
    return (f'Cable driver installed for {description}. Unplug the phone and plug it in again; '
            'iTunes and Apple Devices will not see this phone until you restore Apple\'s driver.')


def restore(data_dir, run_elevated=_run_elevated):
    """Remove MirrorDesk/Zadig libusb packages for Apple devices; Apple's driver takes over again."""
    if not _on_windows():
        raise DriverError('Only needed on Windows.')
    packages = libusb_packages()
    if not packages:
        return 'No libusb driver is installed for an Apple device. Nothing to restore.'
    log = _log_dir(data_dir) / 'usb-driver-restore.log'
    command = ' & '.join([f'pnputil /delete-driver {name} /uninstall /force' for name in packages]
                         + ['pnputil /scan-devices'])
    code = run_elevated(command, log)
    remaining = libusb_packages()
    if remaining:
        raise DriverError(f'Could not remove {", ".join(remaining)} (exit code {code}). Details: {log}')
    return 'Apple\'s driver is back. Unplug the phone and plug it in again. (Cable mirroring needs the cable driver.)'


if __name__ == '__main__':
    import json
    print(json.dumps({'tool': str(tool()), 'iphones': connected_iphones(), 'libusb_packages': libusb_packages()}, indent=2))
