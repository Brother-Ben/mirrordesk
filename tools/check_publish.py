"""Check what git would publish for this computer's private data (developer tool).

    set MIRRORDESK_PRIVATE_EMAIL=...   (the developer's e-mail address, also a needle)
    .venv\\Scripts\\python -X utf8 tools\\check_publish.py

Run before every push. Scans every file git tracks or would add (git ls-files after `git add -A`)
for the same private text as the tester packages' privacy gate (make_tester_package.private_needles:
user and computer name, codes, PIN, the iPhone's ID, this PC's addresses, the e-mail address and
.runtime/private-needles.txt), in UTF-8 and UTF-16, and for things that look like keys or tokens.
Prints each hit with the private text hidden and exits 1; also lists every private or Tailscale IP
address and MAC address in the files for a look by eye (most are made-up examples).
"""
import ipaddress
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import make_tester_package as packager  # noqa: E402

SECRETS = {
    'Tailscale auth key': re.compile(rb'tskey-[A-Za-z0-9-]{8,}'),
    'API key': re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}'),
    'GitHub token': re.compile(rb'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'),
    'private key': re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    'AWS key id': re.compile(rb'\bAKIA[0-9A-Z]{16}\b'),
    'Slack token': re.compile(rb'\bxox[baprs]-[A-Za-z0-9-]{10,}'),
    'tailnet name': re.compile(rb'[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.ts\.net'),
    'secret in a setting': re.compile(rb'(?i)(?:AUTHKEY|API_KEY|SECRET|PASSWORD|TOKEN)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{16,}'),
}
# Made-up examples in the tests.
ALLOWED = {b'desk.tail1234.ts.net', b'mirrordesk.tailnet-abc.ts.net'}
IP = re.compile(rb'(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])')
MAC = re.compile(rb'(?i)(?<![0-9a-f:])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f:])')
TAILSCALE = ipaddress.ip_network('100.64.0.0/10')


def git():
    found = shutil.which('git') or r'C:\msys64\usr\bin\git.exe'
    return found


def published_files():
    out = subprocess.run([git(), '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True).stdout
    return [ROOT / name for name in out.decode('utf-8').split('\0') if name]


def hidden(data, index, length):
    """Line number, and the line with the private text replaced."""
    start = data.rfind(b'\n', 0, index) + 1
    end = data.find(b'\n', index)
    end = len(data) if end < 0 else end
    line = data[start:index] + b'<hidden>' + data[index + length:end]
    return data.count(b'\n', 0, index) + 1, line.strip()[:160].decode('utf-8', 'replace')


def check(paths):
    needles = [(label, form) for text, label in packager.private_needles().items()
               for form in {text.encode('utf-8').lower(), text.encode('utf-16-le').lower()}]
    problems, addresses, macs = [], {}, {}
    for path in paths:
        name = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        lowered = data.lower()
        for label, form in needles:
            index = lowered.find(form)
            while index >= 0:
                problems.append((name, *hidden(data, index, len(form)), label))
                index = lowered.find(form, index + 1)
        for label, pattern in SECRETS.items():
            for match in pattern.finditer(data):
                if match.group(0) not in ALLOWED:
                    problems.append((name, *hidden(data, match.start(), len(match.group(0))), label))
        for match in IP.finditer(data):
            try:
                address = ipaddress.ip_address(match.group(1).decode())
            except ValueError:
                continue
            if (address.is_private or address in TAILSCALE) and not (
                    address.is_loopback or address.is_unspecified or address.is_link_local):
                addresses.setdefault(address, set()).add(name)
        for match in MAC.finditer(data):
            macs.setdefault(match.group(0).decode(), set()).add(name)
    return problems, addresses, macs


def main():
    paths = published_files()
    problems, addresses, macs = check(paths)
    print(f'{len(paths)} files checked')
    for name, line, context, label in problems:
        print(f'  {name}:{line}: {label}: {context}')
    print('Private and Tailscale addresses in the files (examples are fine):')
    for address in sorted(addresses):
        print(f'  {str(address):16} {", ".join(sorted(addresses[address]))[:150]}')
    print('MAC addresses in the files:', ', '.join(sorted(macs)) or 'none')
    print('OK: nothing private found.' if not problems else f'{len(problems)} problems: fix them before pushing.')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
