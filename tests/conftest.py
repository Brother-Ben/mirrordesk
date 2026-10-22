"""Test safety: refuse real multicast even if a launcher fake regresses, and
never let a test server grab a real phone on USB or through a real adb."""
import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def no_real_usb(monkeypatch):
    monkeypatch.setenv('MIRRORDESK_USB', '0')  # inherited by e2e server subprocesses too
    # Android: servers built by tests start without the adb watcher; tests that need
    # it pass their own AndroidSource (or MIRRORDESK_ADB_CMD with tests/fake_adb.py).
    monkeypatch.setenv('MIRRORDESK_ANDROID', '0')
    monkeypatch.delenv('MIRRORDESK_ADB', raising=False)


@pytest.fixture(autouse=True)
def forbid_multicast_send(monkeypatch):
    original = socket.socket
    attempts = []
    class GuardedSocket(original):
        def sendto(self, data, *args):
            endpoint = args[-1]
            try:
                multicast = ipaddress.ip_address(endpoint[0]).is_multicast
            except ValueError:
                multicast = False
            if multicast:
                attempts.append(endpoint)
                raise AssertionError('Tests must inject loopback; real multicast is forbidden')
            return super().sendto(data, *args)
    monkeypatch.setattr(socket, 'socket', GuardedSocket)
    yield
    assert not attempts, 'A test attempted real multicast (blocked before sending)'
