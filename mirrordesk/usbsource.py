"""USB cable input: an iPhone or iPad plugged into this computer mirrors its
screen and sound, with no Wi-Fi, network or internet involved.

The phone is switched into its QuickTime capture mode (quicktime.py) over
libusb. On Windows the phone must use the libusb-win32 driver (it installs
libusb0.dll): with Apple's own driver Windows hides the device from libusb.
On Linux libusb-1.0 is used. There usbmuxd normally owns the phone's usbmux
interface: MirrorDesk then shares the phone ("shared" mode), running its trusted
session through usbmuxd (usbmuxd.py) and claiming only the capture interface.
That needs a usbmuxd that leaves the phone in capture mode
(USBMUXD_DEFAULT_DEVICE_MODE=2, which the Linux installer sets). macOS keeps the
phone for itself (usbmuxd, QuickTime), so cable mirroring is not offered there.

Threading: one capture thread per connected phone does all USB I/O and
protocol replies; media is handed to the event loop with call_soon_threadsafe,
where the MediaHub (not thread-safe) ingests it.
"""
import array
import asyncio
import contextlib
from dataclasses import dataclass
import errno
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

import numpy as np

from . import lockdown
from . import quicktime as qt
from . import usbmuxd
from .media import AudioConverter

log = logging.getLogger('mirrordesk.usb')

APPLE_VENDOR = 0x05AC
PRODUCTS = range(0x1290, 0x12B0)          # iPhone, iPad and iPod touch
VENDOR_CLASS, MUX_SUBCLASS, QT_SUBCLASS = 0xFF, 0xFE, 0x2A
MODE_NORMAL, MODE_CAPTURE = 0, 2          # vendor request 0x52 index ("Valeria" = screen capture)
POLL_SECONDS = 2.0
RETRY_SECONDS = 5.0
DRIVER_CHECK_SECONDS = 15.0
REATTACH_SECONDS = 20.0
REATTACH_POLL = 0.7
MODE_DELAY = 0.5                          # between the mode request and the port reset
SETTLE_SECONDS = 1.0                      # after claiming the capture interface
READ_SIZE = 1024 * 1024
READ_TIMEOUT_MS = 1000
WRITE_TIMEOUT_MS = 2000
SILENCE_SECONDS = 15.0                   # no answer on the capture channel this long: give up / start over
STOP_SECONDS = 3.0
HELD_RESET_SECONDS = 10.0                # handing a disconnected phone back resets its port: not an unplug
DEFAULT_DATA = Path(os.environ.get('MIRRORDESK_DATA') or Path(__file__).resolve().parents[1] / '.runtime')

# States shown in the browser.
STATES = ('off', 'unavailable', 'waiting', 'driver', 'connecting', 'pairing', 'streaming', 'held', 'error')
WAITING = 'Plug the iPhone into this computer with a cable.'
HELD = 'Disconnected. Unplug the iPhone and plug it in again, or click Reconnect, to mirror it again.'


def _on_windows():
    return os.name == 'nt'


class CaptureError(RuntimeError):
    """A user-facing reason the capture ended."""


class NoAnswer(CaptureError):
    """The capture channel stayed silent (it can be stuck from an earlier session)."""


MAC_UNSUPPORTED = ('iPhone by cable is not available on a Mac: macOS keeps the phone for itself. Use wireless '
                   'mirroring (AirPlay), or Android by cable.')


def load_backend():
    """Return (backend, problem). problem is a user-facing sentence when backend is None."""
    if sys.platform == 'darwin':
        return None, MAC_UNSUPPORTED
    try:
        import usb.backend.libusb0
        import usb.backend.libusb1
    except ImportError:
        return None, 'USB support is not installed (run the MirrorDesk setup again).'
    if os.name == 'nt':
        # libusb0.dll comes with the libusb-win32 driver; libusb-1.0 cannot open
        # devices bound to that driver, so it is only a fallback.
        for module in (usb.backend.libusb0, usb.backend.libusb1):
            backend = module.get_backend()
            if backend is not None:
                return backend, None
        return None, ('The libusb driver is not installed on this PC. Install it for the iPhone '
                      '(see "USB cable" in the guide), then reconnect the phone.')
    backend = usb.backend.libusb1.get_backend()
    if backend is None:
        return None, 'libusb-1.0 is not installed on this computer (Debian/Ubuntu: sudo apt install libusb-1.0-0).'
    return backend, None


def _string(device, index):
    import usb.util
    try:
        return usb.util.get_string(device, index) if index else None
    except Exception:  # noqa: BLE001 - backends raise varied errors for unreadable strings
        return None


def find_phones(backend):
    import usb.core
    try:
        return [d for d in usb.core.find(find_all=True, idVendor=APPLE_VENDOR, backend=backend)
                if d.idProduct in PRODUCTS]
    except usb.core.USBError:
        return []


@dataclass
class Interface:
    number: int
    alt: int
    cls: int
    subclass: int
    endpoints: list          # (address, attributes, max packet size)

    def bulk(self):
        """(IN address, OUT address, IN max packet size) when this has one bulk pair."""
        found = {'in': None, 'out': None}
        packet = 512
        for address, attributes, size in self.endpoints:
            if attributes & 0x03 == 0x02:
                found['in' if address & 0x80 else 'out'] = address
                if address & 0x80:
                    packet = (size & 0x7FF) or 512
        return found['in'], found['out'], packet


def parse_configuration(data):
    """Raw configuration descriptor -> (configuration value, [Interface])."""
    if len(data) < 9 or data[1] != 2:
        raise ValueError('not a configuration descriptor')
    interfaces, position, current = [], 0, None
    while position + 2 <= len(data):
        length, kind = data[position], data[position + 1]
        if length < 2:
            break
        if kind == 4 and length >= 9:
            current = Interface(data[position + 2], data[position + 3], data[position + 5], data[position + 6], [])
            interfaces.append(current)
        elif kind == 5 and length >= 7 and current is not None:
            current.endpoints.append((data[position + 2], data[position + 3],
                                      data[position + 4] | data[position + 5] << 8))
        position += length
    return data[5], interfaces


class Handle:
    """One opened phone, driven with direct libusb calls.

    libusb-win32 keeps descriptors per process, and they can be out of date after
    the phone changes USB mode, so configurations are read from the phone itself
    and pyusb's descriptor-based helpers are not used.
    """

    def __init__(self, device):
        # pyusb exposes no public way to open a device without its descriptor cache.
        self.backend = device._ctx.backend
        self.handle = self.backend.open_device(device._ctx.dev)

    def control(self, request_type, request, value, index, data=b'', timeout=WRITE_TIMEOUT_MS):
        if isinstance(data, int):
            buffer = array.array('B', bytes(data))
            count = self.backend.ctrl_transfer(self.handle, request_type, request, value, index, buffer, timeout)
            return bytes(buffer[:count])
        return self.backend.ctrl_transfer(self.handle, request_type, request, value, index,
                                          array.array('B', data), timeout)

    def configurations(self):
        """{configuration value: [Interface]} straight from the phone (GET_DESCRIPTOR)."""
        count = self.control(0x80, 6, 0x0100, 0, 18)[17]
        result = {}
        for index in range(count):
            head = self.control(0x80, 6, 0x0200 | index, 0, 9)
            total = head[2] | head[3] << 8
            value, interfaces = parse_configuration(self.control(0x80, 6, 0x0200 | index, 0, total))
            result[value] = interfaces
        return result

    def _detach(self, numbers):
        """Linux: a kernel driver on the phone (ipheth for its hotspot, its sound or camera
        functions) blocks SET_CONFIGURATION and claiming. Detached for this session only;
        the phone's reset at the end gives it back."""
        if not sys.platform.startswith('linux'):
            return
        for number in numbers:
            try:
                if self.backend.is_kernel_driver_active(self.handle, number):
                    self.backend.detach_kernel_driver(self.handle, number)
            except Exception:  # noqa: BLE001 - no such interface in the current configuration
                pass

    def active_configuration(self):
        try:
            return self.backend.get_configuration(self.handle)
        except Exception:  # noqa: BLE001 - backends without the call, or a detaching phone
            return None

    def set_configuration(self, value):
        if sys.platform.startswith('linux') and self.active_configuration() == value:
            # Already active. Setting it again is refused ("busy") while usbmuxd holds an
            # interface, even though nothing would change.
            return
        self._detach(range(16))
        with _linux_busy():
            self.backend.set_configuration(self.handle, value)

    def claim(self, number):
        self._detach([number])
        with _linux_busy():
            self.backend.claim_interface(self.handle, number)

    def release(self, number):
        self.backend.release_interface(self.handle, number)

    def read(self, endpoint, size, timeout):
        buffer = array.array('B', bytes(size))
        count = self.backend.bulk_read(self.handle, endpoint, 0, buffer, timeout)
        return bytes(buffer[:count])

    def write(self, endpoint, data, timeout=WRITE_TIMEOUT_MS):
        return self.backend.bulk_write(self.handle, endpoint, 0, array.array('B', data), timeout)

    def reset(self):
        self.backend.reset_device(self.handle)

    def close(self):
        try:
            self.backend.close_device(self.handle)
        except Exception:  # noqa: BLE001 - already gone after a reset or unplug
            pass


LINUX_BUSY = ('Another program on this computer is using the iPhone, and usbmuxd (which MirrorDesk can share the '
              'phone with) is not running. Close that program, then unplug the phone and plug it in again.')
USBMUXD_MODE = ('usbmuxd keeps switching the iPhone back to its normal USB mode, so cable mirroring cannot start. '
                'Run the MirrorDesk installer again and let it set up usbmuxd for cable mirroring, then unplug the '
                'phone and plug it in again.')


@contextlib.contextmanager
def _linux_busy():
    """Linux: "resource busy" means another program (usbmuxd) holds the phone: say so."""
    try:
        yield
    except Exception as exc:
        if sys.platform.startswith('linux') and (getattr(exc, 'errno', None) == errno.EBUSY
                                                 or getattr(exc, 'backend_error_code', None) == -6):
            raise CaptureError(LINUX_BUSY) from exc
        raise


def open_handle(device):
    return Handle(device)


def _shares_with_usbmuxd():
    """Linux with usbmuxd running: it owns the phone's usbmux interface, and MirrorDesk shares the phone."""
    return sys.platform.startswith('linux') and usbmuxd.available()


def find_interface(interfaces, subclass):
    return next((i for i in interfaces if (i.cls, i.subclass) == (VENDOR_CLASS, subclass) and len(i.endpoints) == 2),
                None)


def capture_configuration(configs):
    """The configuration value that offers screen capture, or None (normal mode)."""
    return next((value for value, interfaces in sorted(configs.items())
                 if find_interface(interfaces, QT_SUBCLASS) is not None), None)


def mux_configuration(configs):
    """The highest normal configuration with the usbmux interface (what usbmuxd selects)."""
    return next((value for value, interfaces in sorted(configs.items(), reverse=True)
                 if find_interface(interfaces, MUX_SUBCLASS) is not None
                 and find_interface(interfaces, QT_SUBCLASS) is None), None)


def establish_trust(handle, interface, store, serial, on_wait):
    """usbmux on `interface`, then a trusted lockdown session (pairs first if needed).
    Returns the phone's name. Replaced by a stub in tests."""
    ep_in, ep_out, packet = interface.bulk()

    def read(size, timeout_ms):
        try:
            return handle.read(ep_in, size, timeout_ms)
        except Exception as exc:
            if _is_timeout(exc):
                raise lockdown.Timeout()
            raise
    mux = lockdown.MuxDevice(read, lambda data: handle.write(ep_out, data), packet)
    mux.handshake()
    return lockdown.trust(mux, store, serial, on_wait=on_wait)


def read_size(reader, packet):
    """Ask for exactly what the current frame still needs, rounded up to whole USB packets.

    A read then always completes as soon as the phone has sent that frame. A
    bigger read could still be waiting for more when it times out, and the
    libusb-win32 driver discards what a timed-out read had already received.
    """
    need = -(-reader.missing() // packet) * packet
    return min(max(need, packet), READ_SIZE)


def _is_timeout(exc):
    import usb.core
    if isinstance(exc, usb.core.USBTimeoutError):
        return True
    # libusb-win32 returns -116 (the Linux ETIMEDOUT) on Windows, where Python's
    # errno.ETIMEDOUT is different, so pyusb raises a plain USBError for timeouts.
    return isinstance(exc, usb.core.USBError) and (
        getattr(exc, 'backend_error_code', None) in (-116, 116) or 'timeout' in str(exc).lower())


def windows_apple_drivers():
    """Connected iPhones/iPads on Windows and their driver: [(description, uses_libusb)].

    Read-only (pnputil). Explains the common case where the phone is plugged in
    but invisible to libusb because Apple's driver owns it.
    """
    if not _on_windows():
        return []
    try:
        result = subprocess.run(['pnputil', '/enum-devices', '/connected'], capture_output=True, text=True,
                                timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for block in re.split(r'\r?\n\s*\r?\n', result.stdout):
        fields = dict(re.findall(r'^\s*([^:\r\n]+?):\s+(.*?)\s*$', block, re.M))
        instance = fields.get('Instance ID', '')
        match = re.match(r'USB\\VID_05AC&PID_(12[9A][0-9A-F])\\', instance, re.I)
        if not match or '&MI_' in instance.upper():
            continue
        description = fields.get('Device Description') or 'Apple device'
        driver_text = ' '.join([fields.get('Class Name', ''), fields.get('Driver Name', ''),
                                fields.get('Manufacturer Name', '')]).lower()
        found.append((description, 'libusb' in driver_text))
    return found


class _AudioConverter:
    """Phone PCM (normally 48 kHz stereo, which passes through untouched) ->
    the hub's rate and layout, as s16 (1, n*channels) arrays."""

    def __init__(self, rate, channels):
        self.converter = AudioConverter(rate, channels)

    def convert(self, pcm, fmt):
        if not fmt.is_s16 or fmt.channels not in (1, 2):
            return None
        samples = np.frombuffer(pcm[:len(pcm) - len(pcm) % (2 * fmt.channels)], dtype='<i2')
        if not samples.size:
            return None
        return self.converter.convert(samples.astype(np.int16), int(fmt.sample_rate), fmt.channels)


class Capture(threading.Thread):
    """One phone, one capture session, start to finish.

    Sequence (worked out on a real iPhone 11 with the libusb-win32 driver):
    1. Normal mode: usbmux + a trusted lockdown session. The phone only streams
       its screen to a computer it trusts; this is where "Trust This Computer?"
       appears the first time.
    2. Ask for capture mode (vendor request 0x52 index 2) and reset the port:
       the phone only changes its USB configuration when it re-attaches, and on
       Windows it does not re-attach by itself.
    3. Capture configuration: usbmux + trusted session again (as Apple's
       software does on every connect), then claim the capture interface.
       No CLEAR_FEATURE / SET_INTERFACE: they can leave the phone's capture
       function stuck until the phone restarts.
    4. The phone normally sends PING first; current iOS versions can wait for
       the computer instead, so silence is answered with vendor request 0x40
       and a PING (as the working Windows port does).
    5. On stop: back to normal mode (0x52 index 0) and reset the port.
    """

    def __init__(self, backend, serial, sink, audio=True, store=None):
        super().__init__(name='mirrordesk-usb-capture', daemon=True)
        self.backend = backend
        self.serial = serial
        self.sink = sink
        self.audio = audio
        self.store = store or lockdown.PairStore(DEFAULT_DATA)
        self.stop_event = threading.Event()
        self.error = None
        self.unplugged = False
        self.name = None
        self.session = None
        self.shared = False      # Linux: usbmuxd holds the usbmux interface; MirrorDesk shares the phone

    def stop(self):
        self.stop_event.set()

    # -- device handling --------------------------------------------------
    def _find(self):
        for device in find_phones(self.backend):
            if _string(device, device.iSerialNumber) == self.serial:
                return device
        return None

    def _open(self):
        device = self._find()
        if device is None:
            raise CaptureError('The phone was disconnected.')
        handle = open_handle(device)
        return handle, handle.configurations()

    def _trusted_session(self, handle, configs, value):
        """Select a configuration, then usbmux + a trusted lockdown session (through usbmuxd
        when usbmuxd holds the usbmux interface: Linux)."""
        mux = find_interface(configs[value], MUX_SUBCLASS)
        if mux is None:
            raise CaptureError('The phone offered no data connection.')
        on_wait = lambda message: self.sink.capture_state('pairing', message)  # noqa: E731
        try:
            handle.set_configuration(value)
            handle.claim(mux.number)
        except CaptureError:
            if not _shares_with_usbmuxd():
                raise
            self.shared = True
        try:
            if self.shared:
                name = lockdown.trust(usbmuxd.DeviceLink(self.serial), self.store, self.serial, on_wait=on_wait)
            else:
                name = establish_trust(handle, mux, self.store, self.serial, on_wait=on_wait)
        except usbmuxd.UsbmuxdError as exc:
            raise CaptureError(str(exc))
        except lockdown.Timeout:
            raise CaptureError('The phone did not answer. Unlock it, then unplug and reconnect the cable.')
        except lockdown.LockdownError as exc:
            raise CaptureError(str(exc))
        if name and name != self.name:
            self.name = name
            self.sink.phone_name(name)
        return mux

    def _reattach(self, handle, mode):
        """Ask for a USB mode and reset the port so the phone re-attaches in it now."""
        try:
            handle.control(0x40, 0x52, 0, mode)
        except Exception as exc:  # noqa: BLE001
            log.debug('Mode request reported %s', exc)
        time.sleep(MODE_DELAY)
        try:
            handle.reset()
        except Exception:  # noqa: BLE001 - libusb-win32 reports an error: the phone detaches mid-call
            pass
        handle.close()
        deadline = time.monotonic() + REATTACH_SECONDS
        while time.monotonic() < deadline and not self.stop_event.is_set():
            time.sleep(REATTACH_POLL)
            device = self._find()
            if device is None:
                continue
            try:
                candidate = open_handle(device)
                configs = candidate.configurations()
            except Exception:  # noqa: BLE001 - still re-attaching
                continue
            if (capture_configuration(configs) is not None) == (mode == MODE_CAPTURE):
                return candidate, configs
            candidate.close()
        if self.stop_event.is_set():
            raise CaptureError('Stopped.')
        if self.shared and mode == MODE_CAPTURE:
            raise CaptureError(USBMUXD_MODE)
        raise CaptureError('The phone did not switch to cable mirroring. Unplug it, plug it in again and unlock it.')

    def _enter_capture(self, handle, configs):
        value = mux_configuration(configs)
        if value is None:
            raise CaptureError('The phone offered no data connection.')
        self._trusted_session(handle, configs, value)
        return self._reattach(handle, MODE_CAPTURE)

    def run(self):
        handle = None
        try:
            self.sink.capture_state('connecting', 'Connecting to the phone over USB…')
            handle, configs = self._open()
            leftover = capture_configuration(configs) is not None
            if not leftover:
                handle, configs = self._enter_capture(handle, configs)
            try:
                self._stream(handle, configs)
            except NoAnswer:
                if not leftover:
                    raise
                # Capture mode left over from an earlier session can be stuck: start over cleanly.
                log.info('Capture mode left over from an earlier session; restarting it')
                if self.shared:
                    # usbmuxd decides the mode (it would switch it straight back): re-attach in capture mode.
                    handle, configs = self._reattach(handle, MODE_CAPTURE)
                else:
                    handle, configs = self._reattach(handle, MODE_NORMAL)
                    handle, configs = self._enter_capture(handle, configs)
                self._stream(handle, configs)
        except CaptureError as exc:
            self.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - any USB failure ends this session only
            if self._find() is None:
                self.unplugged = True
                self.error = 'The phone was disconnected.'
            else:
                log.warning('USB capture failed: %s', exc, exc_info=log.isEnabledFor(logging.DEBUG))
                self.error = f'USB capture stopped ({type(exc).__name__}: {exc}).'
        finally:
            if handle is not None:
                self._release(handle)
            self.sink.capture_ended(self)

    def _nudge(self, handle, ep_out):
        for step in (lambda: handle.control(0x40, 0x40, 0x6400, 0x6400, b'', 1000),
                     lambda: handle.write(ep_out, qt.ping_packet(), 1000)):
            try:
                step()
            except Exception:  # noqa: BLE001 - refused until the phone's side is ready
                pass

    def _stream(self, handle, configs):
        value = capture_configuration(configs)
        self._trusted_session(handle, configs, value)
        capture = find_interface(configs[value], QT_SUBCLASS)
        ep_in, ep_out, packet = capture.bulk()
        if self.shared and handle.active_configuration() not in (None, value):
            raise CaptureError(USBMUXD_MODE)   # usbmuxd chose the normal configuration
        handle.claim(capture.number)
        time.sleep(SETTLE_SECONDS)
        session = qt.Session(audio=self.audio)
        self.session = session
        reader = qt.FrameReader()
        started = time.monotonic()
        heard = streaming = False
        while not self.stop_event.is_set():
            try:
                chunk = handle.read(ep_in, read_size(reader, packet), READ_TIMEOUT_MS)
            except Exception as exc:
                if not _is_timeout(exc):
                    raise
                # Only the start has a deadline: once mirroring, an unchanged screen
                # with no sound may legitimately send nothing for a long time.
                if not heard:
                    if time.monotonic() - started > SILENCE_SECONDS:
                        raise NoAnswer('The phone did not start sending its screen. Unplug it, plug it in '
                                       'again and unlock it. If that does not help, restart the phone.')
                    self._nudge(handle, ep_out)
                continue
            arrival = time.monotonic()
            try:
                frames = reader.feed(chunk)
            except qt.ProtocolError:
                if not heard:  # leftovers of an earlier session still queued on the phone
                    raise NoAnswer('The phone sent leftover data. Unplug it and plug it in again.')
                raise CaptureError('The data from the phone was garbled. Reconnecting…')
            heard = heard or bool(frames)
            for frame in frames:
                packets, events = session.feed(frame)
                for reply in packets:
                    handle.write(ep_out, reply)
                for event in events:
                    if not streaming:
                        streaming = True
                        self.sink.capture_state('streaming', 'Mirroring over the USB cable.')
                    if isinstance(event, qt.VideoSample):
                        self.sink.video(event, arrival)
                    else:
                        self.sink.audio(event, arrival)
        self._close_session(handle, session, reader, ep_in, ep_out, packet)

    def _close_session(self, handle, session, reader, ep_in, ep_out, packet):
        """Ask the phone to stop and wait briefly for it to release its clocks."""
        try:
            for message in session.stop_packets():
                handle.write(ep_out, message)
            deadline = time.monotonic() + STOP_SECONDS
            while session.released < 2 and time.monotonic() < deadline:
                try:
                    chunk = handle.read(ep_in, read_size(reader, packet), READ_TIMEOUT_MS)
                except Exception as exc:  # noqa: BLE001
                    if _is_timeout(exc):
                        continue
                    raise
                for frame in reader.feed(chunk):
                    for reply in session.feed(frame)[0]:
                        handle.write(ep_out, reply)
            handle.write(ep_out, qt.hpd0_packet())
        except Exception:  # noqa: BLE001 - the phone may already be gone
            pass

    def _release(self, handle):
        """Hand the phone back in its normal mode (best effort: it may be unplugged). Shared
        with usbmuxd, the mode is usbmuxd's to choose: only the capture interface goes back."""
        if not self.unplugged and not self.shared:
            for step in (lambda: handle.control(0x40, 0x52, 0, MODE_NORMAL), handle.reset):
                try:
                    step()
                except Exception:  # noqa: BLE001 - the reset "fails" as the phone detaches
                    pass
        handle.close()


class UsbSource:
    """Watches for a phone on USB and runs one Capture at a time."""

    def __init__(self, store, hub, diagnostics=None, backend_loader=load_backend, driver_check=windows_apple_drivers,
                 data_dir=None):
        self.store = store
        self.hub = hub
        self.diagnostics = diagnostics
        self.pairs = lockdown.PairStore(data_dir or DEFAULT_DATA)
        self.backend_loader = backend_loader
        self.driver_check = driver_check
        self.state = 'off'
        self.message = 'USB cable mirroring is off.'
        self.device = None          # {'name': ..., 'width': ..., 'height': ...}; never the serial
        self.capture = None
        self.loop = None
        self.task = None
        self.sessions = 0
        self.errors = 0
        self.video_samples = 0
        self.audio_samples = 0
        self.retry_at = 0.0
        self.driver_checked = 0.0
        self.converter = None
        self.audio_gap = True
        self.held = None            # serial disconnected from the page: not mirrored again until replugged
        self.held_at = 0.0
        self.driver_job = {'state': 'idle', 'message': ''}   # Windows cable-driver install / restore

    # -- lifecycle ----------------------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        if self.task is None or self.task.done():
            self.retry_at = 0.0
            self.task = asyncio.create_task(self._watch())

    async def stop(self):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_capture()
        self._set('off', 'USB cable mirroring is off.')

    async def disconnect_phone(self):
        """Stop mirroring the plugged-in iPhone from the page; it is not mirrored again until
        it is unplugged and plugged in again, or Reconnect is clicked."""
        capture = self.capture
        if capture is None:
            return False
        self.held, self.held_at = capture.serial, time.monotonic()
        await self._stop_capture()
        self.device = None
        self._set('held', HELD)
        return True

    async def restart(self):
        await self._stop_capture()
        self.retry_at = 0.0
        self.held = None
        task, self.task = self.task, None
        if task is not None:   # re-check libusb: a driver may just have been installed
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.start()

    async def driver_action(self, action, data_dir):
        """Windows: install the libusb driver for the plugged-in iPhone, or give it back
        to Apple's driver. Each asks for Windows permission once (usbdriver.py)."""
        from . import usbdriver
        if self.driver_job['state'] == 'running':
            return
        self.driver_job = {'state': 'running', 'message': 'Windows is asking for permission…'}
        await self._stop_capture()   # the phone must not be in use while its driver changes
        try:
            work = usbdriver.install if action == 'install' else usbdriver.restore
            self.driver_job = {'state': 'done', 'message': await asyncio.to_thread(work, data_dir)}
        except (usbdriver.DriverError, OSError) as exc:
            self.driver_job = {'state': 'failed', 'message': str(exc)}
        if self.store.settings.usb_enabled:
            await self.restart()

    async def _stop_capture(self):
        capture, self.capture = self.capture, None
        if capture is not None:
            capture.stop()
            await asyncio.to_thread(capture.join, STOP_SECONDS + 3)
        self.hub.release('usb')

    # -- watcher ----------------------------------------------------------------
    def _set(self, state, message):
        if state != self.state and self.diagnostics is not None:
            try:
                self.diagnostics.record('usb_state', usb_state=state, usb_sessions=self.sessions)
            except ValueError:
                pass
        self.state, self.message = state, message

    async def _watch(self):
        backend, problem = await asyncio.to_thread(self.backend_loader)
        while backend is None:
            if not _on_windows():
                self._set('unavailable', problem)
                return
            # A new Windows PC has no libusb yet: say when an iPhone on Apple's driver is
            # plugged in (the page then offers the one-click driver install), and pick up
            # the driver as soon as it is installed.
            phones = await asyncio.to_thread(self.driver_check)
            apple = [name for name, libusb in phones if not libusb]
            if apple:
                self._set('driver', f'{apple[0]} is plugged in. Cable mirroring needs the cable driver for it: '
                                    'click "Install cable driver" (Windows asks for permission once).')
            else:
                self._set('unavailable', 'Plug in the iPhone with a cable to set up cable mirroring.')
            await asyncio.sleep(DRIVER_CHECK_SECONDS / 3)
            backend, problem = await asyncio.to_thread(self.backend_loader)
        self._set('waiting', WAITING)
        while True:
            # After an error the message stays up until the retry time.
            if self.capture is None and time.monotonic() >= self.retry_at:
                phones = await asyncio.to_thread(self._serials, backend)
                if (self.held is not None and all(serial != self.held for serial, _ in phones)
                        and time.monotonic() - self.held_at > HELD_RESET_SECONDS):
                    self.held = None   # unplugged since it was disconnected: mirror it when it is back
                free = [phone for phone in phones if phone[0] != self.held]
                if phones and not free:
                    self._set('held', HELD)
                elif free:
                    serial, name = free[0]
                    self.device = {'name': name or 'iPhone', 'width': None, 'height': None}
                    self.sessions += 1
                    self.converter = _AudioConverter(self.hub.audio_rate, self.hub.audio_channels)
                    self.audio_gap = True
                    self.capture = Capture(backend, serial, self, audio=self.store.settings.audio_enabled,
                                           store=self.pairs)
                    self.capture.start()
                else:
                    await self._explain_missing()
            await asyncio.sleep(POLL_SECONDS)

    @staticmethod
    def _serials(backend):
        result = []
        for device in find_phones(backend):
            serial = _string(device, device.iSerialNumber)
            if serial:
                result.append((serial, _string(device, device.iProduct)))
        return result

    async def _explain_missing(self):
        """No phone visible to libusb: say whether one is plugged in with the wrong driver.
        The (Windows-only) driver check runs every 15 s; between checks the last answer stays."""
        self.device = None
        now = time.monotonic()
        if now - self.driver_checked < DRIVER_CHECK_SECONDS:
            if self.state != 'driver':
                self._set('waiting', WAITING)
            return
        self.driver_checked = now
        phones = await asyncio.to_thread(self.driver_check)
        apple_driver = [name for name, libusb in phones if not libusb]
        if apple_driver:
            self._set('driver', f'{apple_driver[0]} is plugged in, but Windows is using Apple\'s driver for it. '
                                'Click "Install cable driver" (Windows asks for permission once).')
        else:
            self._set('waiting', WAITING)

    # -- capture callbacks (capture thread) ---------------------------------------
    def capture_state(self, state, message):
        self.loop.call_soon_threadsafe(self._set, state, message)

    def phone_name(self, name):
        self.loop.call_soon_threadsafe(self._name, name)

    def _name(self, name):
        if self.device is not None:
            self.device['name'] = name

    def video(self, sample, arrival):
        self.loop.call_soon_threadsafe(self._video, sample, arrival)

    def audio(self, sample, arrival):
        samples = self.converter.convert(sample.pcm, sample.format) if self.converter else None
        if samples is not None:
            self.loop.call_soon_threadsafe(self._audio, samples, arrival)

    def capture_ended(self, capture):
        self.loop.call_soon_threadsafe(self._ended, capture)

    # -- event loop side --------------------------------------------------------------
    def _video(self, sample, arrival):
        if self.capture is None or not self.hub.claim('usb'):
            return   # another cable input (Android) is mirroring: it keeps the screen until it stops
        if self.device is not None and sample.width:
            self.device['width'], self.device['height'] = sample.width, sample.height
        self.video_samples += 1
        timestamp = round(sample.presentation.seconds * 90000) & 0xFFFFFFFF if sample.presentation else 0
        self.hub.push_unit(sample.nals, arrival, timestamp)

    def _audio(self, samples, arrival):
        if self.capture is None or not self.hub.claim('usb'):
            return
        self.audio_samples += 1
        self.hub.push_audio(samples, arrival, discontinuity=self.audio_gap)
        self.audio_gap = False

    def _ended(self, capture):
        if capture is not self.capture:
            return  # a capture we already stopped on purpose
        self.capture = None
        self.hub.release('usb')
        if capture.error and not capture.unplugged:
            self.errors += 1
            self.retry_at = time.monotonic() + RETRY_SECONDS
            self._set('error', capture.error)
        else:
            self.device = None
            self._set('waiting', WAITING)

    def skew_ppm(self):
        """How far from real time MirrorDesk last told the phone our clock runs (None: no answer yet)."""
        session = getattr(self.capture, 'session', None)
        skew = getattr(session, 'last_skew', None)
        rate = getattr(getattr(session, 'audio_format', None), 'sample_rate', None) or 48000
        return None if skew is None else round((skew / rate - 1) * 1e6)

    def status(self):
        from . import usbdriver
        return {'state': self.state, 'message': self.message, 'enabled': bool(self.store.settings.usb_enabled),
                'skew_ppm': self.skew_ppm(),
                'held': self.held is not None,
                'device': self.device, 'sessions': self.sessions, 'errors': self.errors,
                'video_samples': self.video_samples, 'audio_samples': self.audio_samples,
                'driver_tool': usbdriver.available(), 'driver_job': dict(self.driver_job)}


# ------------------------------------------------------------------ hardware check
class _CheckSink:
    """Counts what the phone sends; optionally saves the video as a raw .h264 file."""

    def __init__(self, save=None):
        self.done = threading.Event()
        self.file = open(save, 'wb') if save else None
        self.frames = self.keyframes = self.audio_bytes = 0
        self.size = None
        self.first = self.last = None
        self.audio_format = None

    def capture_state(self, state, message):
        print(f'  [{state}] {message}', flush=True)

    def phone_name(self, name):
        pass

    def video(self, sample, arrival):
        from .h264 import NAL_IDR, join_annexb, nal_type
        self.frames += 1
        self.keyframes += any(nal_type(n) == NAL_IDR for n in sample.nals)
        self.size = (sample.width, sample.height) if sample.width else self.size
        self.first = self.first or arrival
        self.last = arrival
        if self.file:
            self.file.write(join_annexb(sample.nals))

    def audio(self, sample, arrival):
        self.audio_bytes += len(sample.pcm)
        self.audio_format = sample.format

    def capture_ended(self, capture):
        self.done.set()


def check(seconds=15.0, save=None, data_dir=None):
    """`python -m mirrordesk.usbsource`: mirror over the cable for a few seconds and report.
    Quit MirrorDesk first: only one program can use the phone's capture mode."""
    backend, problem = load_backend()
    if backend is None:
        print(problem)
        return 2
    phones = UsbSource._serials(backend)
    if not phones:
        stuck = [name for name, libusb in windows_apple_drivers() if not libusb]
        print(f'{stuck[0]} is plugged in but uses Apple\'s driver; install the libusb driver (guide, section 3).'
              if stuck else 'No iPhone or iPad found on USB. Plug it in, unlock it, and try again.')
        return 1
    serial, name = phones[0]
    print(f'Found {name or "an Apple device"}. Unlock it and tap Trust if asked. '
          f'Capturing for {seconds:g} s once the picture arrives…')
    sink = _CheckSink(save)
    capture = Capture(backend, serial, sink, store=lockdown.PairStore(data_dir or DEFAULT_DATA))
    capture.start()
    # Connecting (and the first Trust prompt) takes a while; the capture time starts with the first frame.
    deadline = time.monotonic() + seconds + lockdown.PAIRING_WAIT_SECONDS + 30
    while not sink.done.wait(0.25) and time.monotonic() < deadline:
        if sink.first is not None and time.monotonic() - sink.first >= seconds:
            break
    capture.stop()
    capture.join(STOP_SECONDS + 5)
    if sink.file:
        sink.file.close()
    span = (sink.last - sink.first) if sink.frames > 1 else 0
    print(f'Video: {sink.frames} frames' + (f', {sink.size[0]}×{sink.size[1]}' if sink.size else '')
          + (f', about {sink.frames / span:.0f} fps' if span else '') + f', {sink.keyframes} keyframes')
    if sink.audio_format:
        rate, channels = sink.audio_format.sample_rate, sink.audio_format.channels
        print(f'Sound: {sink.audio_bytes / (rate * channels * 2):.1f} s of {rate:g} Hz, {channels} channels')
    else:
        print('Sound: none received')
    if save and sink.frames:
        print(f'Saved the video to {save} (play it with VLC or ffplay).')
    if capture.error:
        print(f'Ended with: {capture.error}')
    print('Result:', 'OK - cable mirroring works.' if sink.frames else 'FAILED - no video arrived.')
    return 0 if sink.frames else 1


if __name__ == '__main__':
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog='python -m mirrordesk.usbsource',
                                     description='Check USB cable mirroring with a plugged-in iPhone (quit MirrorDesk first).')
    parser.add_argument('--seconds', type=float, default=15.0)
    parser.add_argument('--save', metavar='FILE.h264', help='Also save the received video.')
    parser.add_argument('--debug', action='store_true', help='Print every USB step.')
    options = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if options.debug else logging.WARNING,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    sys.exit(check(options.seconds, options.save))
