"""Windows setup steps (winsetup.py): firewall rules and the icon. Nothing is applied for real."""
import sys
from pathlib import Path

from mirrordesk import winsetup


def test_firewall_rules_name_the_real_interpreter_and_bundled_programs(tmp_path, monkeypatch):
    (tmp_path / 'uxplay' / 'bin').mkdir(parents=True)
    (tmp_path / 'uxplay' / 'bin' / 'uxplay.exe').write_bytes(b'')
    from mirrordesk import androidtools
    adb = tmp_path / 'tools' / 'platform-tools' / 'adb.exe'
    adb.parent.mkdir(parents=True)
    adb.write_bytes(b'')
    monkeypatch.setattr(androidtools, 'bundled_adb', lambda tools_dir=None: adb)
    rules = winsetup.program_rules(tmp_path)
    paths = [path for _, path in rules]
    base = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    assert paths[0] == base, 'a venv python.exe only launches the base interpreter, which opens the sockets'
    assert tmp_path / 'uxplay' / 'bin' / 'uxplay.exe' in paths and adb in paths
    assert all(name.startswith('MirrorDesk - ') for name, _ in rules)


def test_firewall_commands_replace_old_rules_and_allow_private_networks_only():
    rules = [('MirrorDesk - uxplay.exe', Path(r'C:\Program Files\MD\uxplay.exe'))]
    commands = winsetup.firewall_commands(rules)
    assert commands[0] == 'netsh advfirewall firewall delete rule name="MirrorDesk - uxplay.exe" >nul 2>nul'
    assert commands[1] == ('netsh advfirewall firewall add rule name="MirrorDesk - uxplay.exe" dir=in action=allow '
                           'program="C:\\Program Files\\MD\\uxplay.exe" enable=yes profile=private,domain')
    assert winsetup.firewall_commands(rules, add=False) == commands[:1]


def test_all_rules_go_through_one_elevated_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(winsetup, 'rule_exists', lambda name: True)
    ok = winsetup.apply_firewall([('MirrorDesk - a', Path('a.exe')), ('MirrorDesk - b', Path('b.exe'))], tmp_path,
                                 run_elevated=lambda command, log: calls.append(command) or 0)
    assert ok and len(calls) == 1, 'one Windows permission prompt for every rule'
    assert calls[0].count('add rule') == 2


def test_shortcut_icon_is_a_real_ico(tmp_path):
    icon = winsetup.make_icon(tmp_path / 'mirrordesk.ico')
    data = icon.read_bytes()
    assert data[:4] == b'\x00\x00\x01\x00' and int.from_bytes(data[4:6], 'little') >= 4
