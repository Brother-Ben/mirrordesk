# MirrorDesk Prototype 7 — browser-first guide

MirrorDesk is now one server (`python -m mirrordesk`) that you control **entirely from a
browser**: start/stop the AirPlay receiver, see the pairing PIN, change quality, watch the
phone screen, export diagnostics. Any browser on the same computer, your LAN, a Tailscale
tailnet or a USB-tethered device can watch. The same server runs headless in Docker on Linux.

A phone can reach MirrorDesk three ways: an iPhone/iPad **wirelessly** (AirPlay) or over a **USB
cable** (section 3, no Wi-Fi or internet involved), and an **Android** phone over USB or Wi-Fi
(section 4, with mouse/keyboard control from the browser).

```text
iPhone/iPad ──AirPlay (Wi-Fi/Ethernet)──► UxPlay (unchanged, supervised)
   │                                         └► loopback RTP: H.264 + L16 44.1 kHz ─┐
   └──USB cable (QuickTime capture mode)──► MirrorDesk USB input ──────────────────┤
Android ──USB debugging / Wireless debugging──► adb + scrcpy-server on the phone ───┤
                                                  H.264 + 48 kHz PCM                ▼
                                                                      MirrorDesk media hub
                                                (all sound → 48 kHz, exact 20 ms frames)
        ├─ passthrough: phone's H.264 forwarded as-is over WebRTC (full resolution, ~no CPU)
        └─ compatibility: decode once, re-encode (H.264 baseline / VP8) for other browsers
             ──► any number of browsers (default limit 4)
```

Only one input feeds viewers at a time: the first cable input (iPhone USB or Android) keeps the screen
until it stops; AirPlay video is ignored while a cable input is active.

**Sending MirrorDesk to someone to test:** section 11 builds the tester packages: Windows (one folder
with the AirPlay receiver, the iPhone cable driver installer and Android support, a one-step setup),
macOS (an installer that builds UxPlay on the Mac and creates MirrorDesk.app) and Linux (a zip with
the program and `install.sh`: desktop app, systemd service over SSH, or a Docker/Dockge stack). Native macOS
and Linux installs are described in section 12; `packaging/common/INSTALL-GUIDE.html` is the
tester-facing install guide for all of them.

## 1. Windows (existing UxPlay 1.74 UCRT64 build)

1. Once: `Setup-MirrorDesk.cmd` (installs the locked Python environment into this folder's `.venv`;
   re-run it after updating the project so new dependencies are installed).
2. Start: double-click **`Start-MirrorDesk.cmd`**. MirrorDesk runs **in the background** (no
   console window) and opens **its own window** (0.13.0): Edge (or Chrome) in app mode with a browser
   profile of its own (`.runtime\window`), held in a Windows job object, so Quit closes it — your own
   browser, tabs and profile are never touched. Without Edge or Chrome the default browser opens
   `http://127.0.0.1:8765/` as before. **Shortcuts:** `python -m mirrordesk.winsetup install --no-launch
   --no-firewall` (what the tester package's setup runs) puts *MirrorDesk* on the desktop and in the Start
   menu for this folder; they run `Start-MirrorDesk.cmd`'s command.
   A **MirrorDesk icon** appears next to the clock (it may be under the **^** arrow):
   left-click opens the window; right-click → *Open MirrorDesk* / *Start with Windows* / *Quit MirrorDesk*.
   - Starting it again while it runs opens another MirrorDesk window (one instance only; the second start
     asks the running one, with the token from `.runtime`).
   - If **another** copy still holds MirrorDesk's ports (MirrorDesk started from a different folder such
     as the Prototype 6 folder, a forgotten console window, a copy started before an update, or the
     Prototype 6 launcher), it asks *Close it and start MirrorDesk now?* —
     Yes closes that Python process and starts. It never closes non-Python programs; it names them
     instead. `--replace` answers Yes without asking.
   - First background run: Windows Firewall may ask about **pythonw.exe** (a different program file
     from the console's python.exe). **Allow on Private networks** lets other devices watch.
   - Want the console output? `Start-MirrorDesk-Console.cmd` (the previous behaviour; Ctrl+C stops).
     In the background the same output goes to `.runtime\logs\mirrordesk.log` (previous run:
     `mirrordesk.previous.log`; it contains the access code, like `settings.json`).
   - This computer only: add `--host 127.0.0.1` to either launcher.
   - Different UxPlay path: `set MIRRORDESK_UXPLAY=D:\path\uxplay.exe` first (or use the console launcher).
3. On the iPhone: Control Center → Screen Mirroring → **MirrorDesk Web** → enter the code shown
   in the browser. **Every session has its own code**: a session lasts while MirrorDesk runs, and a phone
   that entered its code may stop mirroring, be disconnected and come back without it. The session ends —
   MirrorDesk makes a new code and forgets every phone (a new receiver key `.runtime\uxplay-key.pem` and an
   empty register `.runtime\uxplay-register.txt`) — when MirrorDesk starts (after Quit or a restart), when
   you press **New code**, or after 4 hours with no phone connected (`SESSION_IDLE_SECONDS`; a server that
   never quits, like the ZimaOS box, still gets a new code the next day). 0.12.0 ended a session 30 s after
   the phone left and on **Disconnect**: a short break meant a new code. Or plug the iPhone in with a cable
   (section 3).
4. **Start automatically:** tick *Start with Windows* in the tray menu, or *Start MirrorDesk when I
   sign in to Windows* in the browser (Settings → *This computer*). It
   adds a per-user sign-in entry (`HKCU\…\Run`, no admin rights; also listed in Task Manager →
   Startup apps). Untick to remove it. The entry points at this project folder — re-tick after moving it.
5. **Stop:** tray icon → *Quit MirrorDesk*, or **Quit** at the bottom left of the page (shown in the
   browser on this computer only: from another device MirrorDesk could not be started again). It asks
   *Quit MirrorDesk?* first; *Don't ask me again* turns that off (Settings → General → *Ask before
   quitting* turns it back on). Quit shuts everything down: MirrorDesk's windows close, the receiver,
   USB and Android capture stop, and MirrorDesk's own adb daemon stops too, also one an earlier run left
   behind (0.12.0 left one running for days). Windows job objects end UxPlay and the windows even if the
   server crashes. A page open in another browser on this computer shows *MirrorDesk is off* and closes
   itself where the browser allows it; other devices' pages stay and pick up again when MirrorDesk is
   back. To only hide it from phones while the PC is on, use **Stop** under Devices → *Wi-Fi (AirPlay)*
   instead (the idle server uses almost no CPU).
6. Devices that signed in stay signed in across restarts (30 days; a new viewer or owner code
   signs out the devices that used the old one). See section 5 for the two codes.

This folder is standalone: it needs only the UxPlay/MSYS2 installation, uv (for setup) and its own
`.venv`. The Prototype 6 folder and its `Start-MirrorDesk-Web.cmd` are separate and unchanged; only one
MirrorDesk can run at a time (they share ports 8765, 5004 and 5006).

## 2. Docker (Linux host: ZimaOS, Ubuntu, Debian, Raspberry Pi OS)

**Paste into Dockge:** the image is published on GitHub as `ghcr.io/brother-ben/mirrordesk:<version>` (x86-64 and
ARM64; `.github/workflows/image.yml` builds it when a version tag such as `v0.13.0` is pushed).
`docker/compose.yaml` (any Linux host) and `docker/dockge/compose.yaml` (ZimaOS: data in
`/DATA/AppData/mirrordesk`, ZimaOS's Avahi) name that image, so pasting either into a new Dockge stack and
clicking *Deploy* is the whole install. A newer version: change the version in the `image:` line.

From the source (builds the image on the host):

```sh
docker compose up -d --build
docker compose logs mirrordesk    # owner and viewer codes + URLs
```

**Any Linux host / Dockge (universal):** the Linux installer's `--docker` mode builds `mirrordesk:<version>`
(also tagged with the stack's GitHub image name, so nothing is downloaded) and creates the stack
`docker/compose.yaml` — inside Dockge's stacks folder when Dockge runs (so it shows up
as the stack `mirrordesk`), else in `/opt/mirrordesk-docker` (ZimaOS/CasaOS: `/DATA/AppData/mirrordesk-stack`,
data in `/DATA/AppData/mirrordesk`). The stack file has no host paths besides `${MIRRORDESK_DATA_DIR:-./data}`;
settings go in the stack's `.env`. See section 11.

- **Host networking is required** (AirPlay mDNS and WebRTC use the host's real interfaces).
  Docker Desktop on Windows/macOS cannot advertise AirPlay to the LAN — use a Linux host.
- The image builds UxPlay **1.74** from source: upstream commit `d29dfab`, the same as the Windows build,
  from the checksum-pinned archive in `third_party/uxplay` (no git needed; Debian stable's 1.71 lacks `-vrtp`;
  the **v1.73.7 release rejects every iPhone's AirPlay code**, so the phone asks for it again and again —
  upstream fixed that on master the next day). UxPlay 1.74 announces AirPlay with its **own built-in mDNS
  responder**, so since 0.9.0 the container starts no Avahi by default (`MIRRORDESK_AVAHI=off`). The older
  modes still work: `MIRRORDESK_AVAHI=private` (a private D-Bus + Avahi in the container) and `host`
  (`docker compose -f docker-compose.yml -f docker-compose.host-avahi.yml up -d --build`).
- Settings, receiver key and diagnostics persist in `./data`.
- In a container **every browser signs in with a code** (there is no trusted "same computer" on a
  headless host; loopback forwarders such as userspace Tailscale or nginx would otherwise look local):
  the **owner code** for you, the **viewer code** for people who should only watch. Both are in the log.
- Fixed codes: set `MIRRORDESK_OWNER_CODE` and/or `MIRRORDESK_ACCESS_CODE` (viewer code) in
  `docker-compose.yml` (6–32 letters/digits). A pinned code cannot be rotated from the UI — change the
  variable and restart.
- The server runs as the unprivileged `mirrordesk` user; `docker stop` shuts it down gracefully.
- First built and run on ZimaOS 1.7.1 (see below); not yet tested on other Linux hosts.
- **Android** works in the container over Wi-Fi (Wireless debugging, a last resort; pair on Devices →
  Android → *Wi-Fi (Wireless debugging)*; section 4). The image
  contains Debian's adb (1:34.0.5) and the bundled scrcpy server; adb's keys persist in the data folder,
  so a paired phone stays paired across restarts.
- **Phones on USB** (Android with USB debugging, iPhone cable) plugged into the Docker host: bind the USB
  bus (`/dev/bus/usb:/dev/bus/usb` as a volume, so phones plugged in later appear), allow USB devices
  (`device_cgroup_rules: ["c 189:* rmw"]`) and set `MIRRORDESK_RUN_AS_ROOT=1` (USB device nodes belong to
  root). The ZimaOS compose file has these lines ready to uncomment. iPhone USB on Linux is untested.
  `MIRRORDESK_USB=0` / `MIRRORDESK_ANDROID=0` turn those inputs off.

### ZimaOS

**Built and running on the development ZimaOS box (ZimaOS v1.7.1, x86-64, Docker 27.5.1) on 2026-10-16:**
first build OK (image 1.05 GB), container healthy, AirPlay announced through ZimaOS's own Avahi (seen on
the LAN as `_airplay._tcp` / `_raop._tcp`), sign-in, status and the web page work from another PC; Android
waits for a phone with Debian's adb 34.0.5; server, UxPlay and adb run as the unprivileged user.

**On that box it runs as the Dockge stack `mirrordesk`** (`docker/dockge/compose.yaml`, copied to
`/DATA/AppData/big-bear-dockge/stacks/mirrordesk/compose.yaml`), data in `/DATA/AppData/mirrordesk`, host
network, ZimaOS's Avahi. Start/stop/logs/edit in Dockge. Until 0.13.0 that file named the locally built
`mirrordesk:<version>` with `pull_policy: never`; it now names the GitHub image. To build on the box instead,
copy the source there and run `sh docker/zimaos-install.sh`: it builds the image, gives it the image name
the stack uses and restarts the stack with it (it finds the stack from the running container's compose
labels; with no stack it creates a plain container as before).

ZimaOS quirk: for the SSH login user, `/DATA/.docker` (Docker's settings folder) is readable by root only,
which hides the installed `docker compose` and `docker buildx` plugins ("See 'docker --help'"). Use them
with `DOCKER_CONFIG=/tmp/mirrordesk-docker-config docker compose ...`; the install script does this itself.

`docker-compose.zimaos.yml` is a complete ZimaOS-flavoured compose file (data in
`/DATA/AppData/mirrordesk`, host networking, the host's Avahi, the USB block, `x-casaos` app metadata)
for systems that have the compose plugin:

1. Copy this folder to the box (Files app / network share), e.g. to `/DATA/AppData/mirrordesk-src`.
2. Enable SSH in ZimaOS, log in, then:
   ```sh
   cd /DATA/AppData/mirrordesk-src
   docker compose -f docker-compose.zimaos.yml up -d --build     # first build 5-15 min
   docker compose -f docker-compose.zimaos.yml logs mirrordesk    # owner/viewer codes + addresses
   ```
   Without the `docker compose` command: `sh docker/zimaos-install.sh` (plain `docker build` + `docker run`
   with the same settings; `MIRRORDESK_USB_PHONES=1` adds USB, `MIRRORDESK_ACCESS_CODE=…` fixes the code).
3. Open `http://<box>:8765/` and enter the owner code. If ZimaOS's own Avahi should advertise AirPlay,
   add `-f docker-compose.host-avahi.yml`.

## 3. USB cable (no network needed)

**Status: working on a real iPhone 11 (iOS, libusb-win32 driver, USB-C to Lightning cable) on
2026-10-14:** 828×1792 at about 60 fps with stereo sound, played in the browser as the phone's own
H.264 (no re-encoding), about 4 seconds from plug-in to picture. Other iPhone models and iOS versions
are untested.

The iPhone has a hidden USB mode that macOS QuickTime uses to record its screen. MirrorDesk switches
the phone into that mode and receives its screen (H.264 at the phone's full screen resolution) and sound
over the cable. No Wi-Fi, hotspot, network or internet is used, and nothing is installed on the phone.

How MirrorDesk connects (`mirrordesk/usbsource.py`), as worked out on the real phone:

1. **Trust.** The phone only sends its screen to a computer it trusts. Apple's software normally does
   this pairing, but the libusb driver hides the phone from it, so MirrorDesk pairs itself
   (`mirrordesk/lockdown.py`): the first time, the phone asks **Trust This Computer?** and for its
   passcode. The pairing is saved in `.runtime\usb-pairing\` and reused every time after.
2. **Capture mode.** MirrorDesk asks the phone for its hidden capture mode, then resets the USB port:
   the phone only changes mode when it reconnects, and on Windows it does not reconnect by itself.
   (You may hear the Windows device sound once.)
3. **Stream.** A second trusted session, then the capture channel (`mirrordesk/quicktime.py`, a Python
   port of [quicktime_video_hack](https://github.com/danielpaulus/quicktime_video_hack)). If the phone
   stays silent, MirrorDesk speaks first, as newer iOS versions expect.
4. **Hand back.** When mirroring stops, the phone is reset back to its normal mode.

### One-time setup on Windows: the libusb driver

Windows normally gives the iPhone to Apple's driver, which hides it from MirrorDesk. Cable mirroring
needs the **libusb-win32** driver on that phone instead.

> On the development PC this is already the case: the iPhone (serial ending …402E) shows up as
> *iPhone (Composite Parent)* under *libusb-win32 devices* in Device Manager. Skip to "Test it".

**One click (0.8.0):** plug the iPhone in, open **Devices** → *iPhone and iPad* → *USB cable* → **Install cable driver** and
allow the Windows prompt (only offered in the browser on that PC). MirrorDesk runs `wdi-simple`
(libwdi, built by `tools/build_wdi_simple.py`) elevated: it installs **libusb-win32 1.2.6.0** — the same
driver files as on the development PC (SHA-256-checked) — for exactly that phone, as Zadig does.
Unplug and replug the phone. **Undo:** *How cable mirroring works* → **restore Apple's driver** (one
prompt; removes the libusb packages made for Apple devices). Logs: `.runtime\logs\usb-driver-*.log`.
The development PC builds the tool once (needs MSYS2 gcc): `.venv\Scripts\python tools\build_wdi_simple.py`.

**By hand with Zadig** (the original method):

1. Plug the iPhone in and unlock it.
2. Download **Zadig** from <https://zadig.akeo.ie> and run it.
3. *Options* → tick **List All Devices**, untick **Ignore Hubs or Composite Parents**.
4. Pick **iPhone (Composite Parent)** (or *Apple Mobile Device USB Composite Device*), not an entry
   that says *Interface*.
5. Choose the **libusb-win32** driver and click *Replace Driver* / *Install Driver*.
6. Unplug the phone and plug it in again.

**Trade-off while this driver is on that phone:** iTunes / Apple Devices, Windows Photos import and
USB Personal Hotspot stop working with it. Wireless mirroring is not affected.

**To undo:** Device Manager → *libusb-win32 devices* → *iPhone (Composite Parent)* → *Uninstall device*,
tick *Attempt to remove the driver for this device*, then replug the phone (Windows goes back to Apple's
driver). Or, in an administrator PowerShell, `pnputil /enum-drivers` to find the libusb-win32 entry
(`oem7.inf` on the development PC) and `pnputil /delete-driver oem7.inf /uninstall`.

If the phone is plugged in but still on Apple's driver, the Devices page says so (*Driver needed*).

### Test it

**Quick check first (no browser):** quit MirrorDesk (only one program can use the phone's capture mode),
plug in and unlock the iPhone, then in this folder run:

```bat
.venv\Scripts\python -m mirrordesk.usbsource --seconds 15 --save usb-test.h264
```

It reports frames, resolution, frame rate, keyframes and seconds of sound, and ends with
`Result: OK` or `Result: FAILED`. `usb-test.h264` plays in VLC or `ffplay`. Add `--debug` to print every
USB step. The phone is switched back to its normal mode afterwards.

**Then in MirrorDesk:**

1. Start MirrorDesk (`Start-MirrorDesk-Console.cmd` shows log output).
2. Open **Devices** in the browser. The *USB cable* card should say *No phone*.
3. Plug in and unlock the iPhone. Within a few seconds the card shows *Connecting*, then *Mirroring*,
   and the phone screen appears on the Mirror page. The top bar shows *Phone: Mirroring · USB*.
4. While mirroring, the phone's status bar shows **9:41 and a full battery**. That is iOS's
   recording mode, not a fault.
5. Optional: turn the phone's Wi-Fi off to confirm nothing goes over the network.
6. Unplug: the card returns to *No phone* and wireless mirroring takes over again.
7. If something goes wrong: Developer → *Report a problem* → **No connection over USB cable** →
   *Mark issue*, then *Export diagnostics*. With the console launcher, run with
   `set MIRRORDESK_LOG_LEVEL=DEBUG` first for full USB logs.

The first time (or after the phone forgets this computer) the card shows **Waiting for Trust**: tap
*Trust* on the iPhone and enter its passcode. To make the phone forget this computer, delete the
phone's file in `.runtime\usb-pairing\` (or on the phone: Settings → General → Transfer or Reset
iPhone → Reset → Reset Location & Privacy).

| Card says | Meaning / fix |
|---|---|
| Driver needed | The phone is on Apple's driver: do the one-time setup above. |
| Not available | No libusb on this PC (no driver installed), pyusb missing (re-run setup), or `MIRRORDESK_USB=0`. |
| Waiting for Trust | Unlock the iPhone, tap *Trust*, enter the passcode. |
| Problem: "Don't Trust was chosen" | Unplug the phone, plug it in again and tap *Trust*. |
| Problem: "did not switch to cable mirroring" | Unplug the phone, plug it in again and unlock it. *Reconnect* retries at once. |
| Problem: "did not start sending its screen" | The phone's capture function is stuck (it happened during development after many experimental requests). Unplug and replug; if that does not help, **restart the iPhone**. |
| Picture freezes while the phone is in use | Note the time, then Developer → *Report a problem* → mark and export. *Reconnect* starts a fresh session. |
| Disconnected | **Disconnect** was clicked (player bar or Devices page). The phone is not mirrored again until it is unplugged and plugged in again, or *Reconnect* is clicked. |
| Problem: "usbmuxd keeps switching the iPhone back" | Linux: run the installer again and let it set up usbmuxd for cable mirroring (section 12). |

Turn cable mirroring off in Settings → *Mirror an iPhone plugged in by USB cable* (or start with `--no-usb`).

## 4. Android (USB or Wi-Fi)

**Status (0.8.0): implemented and tested against a protocol-exact simulated phone and the real adb
37.0.1; not yet tested with a real Android phone** (that is what the tester package is for).

How it works (`mirrordesk/androidsource.py`): Google's **adb** starts **scrcpy-server 5.0.1**
(Genymobile/scrcpy, Apache-2.0, bundled in `third_party/scrcpy`, checksum-verified) on the phone. It
encodes the screen with the phone's own H.264 encoder (keyframe every 2 s, max 60 fps, size and bit rate
from Settings) and captures the sound (Android 11+) as raw 48 kHz PCM. MirrorDesk reads both through an
adb port forward and passes the H.264 to browsers untouched. Nothing is installed on the phone.

- **Android tools:** Setup downloads Google's platform-tools (adb) into `tools\platform-tools` with a
  pinned SHA-256 (Google's license does not allow shipping them). Without them the Devices page (Android
  tab) shows **Install Android tools**. An adb on PATH or `MIRRORDESK_ADB` also works; Docker uses Debian's adb.
- **USB (the way to connect):** Developer options → *USB debugging*; plug in; tap **Allow** on the phone.
  Some phones need the maker's USB driver on Windows. Xiaomi/Redmi/POCO also need *USB debugging
  (Security settings)* for control.
- **Wi-Fi (Android 11+), a last resort:** Wireless debugging lets any computer paired with the phone
  control it over the network, so the page keeps it folded away under *Wi-Fi (Wireless debugging) — Last
  resort* with that warning; pair only on trusted networks and turn Wireless debugging off afterwards.
  **Show QR code** and *Pair device with QR code* on the phone (needs mDNS on the network; Windows may ask
  to allow adb through the firewall), or *Pair device with pairing code* and type the address, port and
  6-digit code. adb reconnects paired phones by itself afterwards. **Fixed in 0.11.0:** the QR code was a
  broken image in every browser (segno's `svg_inline()` leaves out the SVG namespace that an `<img>` needs),
  so QR pairing could never work.
- **Control:** click/drag on the picture (touch on touch screens, several fingers work), mouse wheel
  scrolls, the keyboard types (Esc = Back), **Back / Home / Apps** buttons. Settings → *Who can tap and type
  on the phone*: nobody / this computer only (default) / this computer and owner sign-ins.
- **Sound:** Android 11+ (Android 11: unlock before connecting). While mirroring, Android normally plays the
  sound here only; Settings → Devices → *Play audio on phone and MirrorDesk* (Android 13+, playback capture
  with duplication; apps can opt out of it) keeps it on both.
- **Keyframes on demand:** unlike AirPlay, MirrorDesk can ask the phone for a new keyframe, so a browser
  that joins late or loses packets recovers at once.
- **Several phones:** the Devices page lets you pick one; the same phone on cable and Wi-Fi prefers the
  cable. An iPhone mirroring by cable keeps the screen until it stops.
- **Disconnect** (player bar or Devices page) stops this phone's mirroring; it is not mirrored again until it
  is unplugged (or its Wi-Fi connection ends) or *Reconnect* is clicked.

**Other ways an Android phone could connect (research, 2026-10):**

| Way | Verdict |
|---|---|
| Google Cast ("Cast screen") | Not possible for MirrorDesk. Android's Cast sender only mirrors to receivers with a Google-issued device certificate (the Cast SDK enforces device authentication); open-source receivers work only with certificates extracted from real Cast hardware, which a product cannot ship. |
| Share from Chrome on the phone (`getDisplayMedia`) | Not possible: Chrome and Firefox for Android have no screen capture for web pages (MDN: not supported; Chrome 72–88 exposed it but always refused). |
| Miracast ("Smart View", "Screen Cast" on Samsung, Xiaomi, OnePlus, …; not Pixel) | Possible on Windows only: Windows 10 1903+ has a Miracast receiver API (`Windows.Media.Miracast.MiracastReceiver`), and the PC needs Wi-Fi Direct. A separate project. |
| A small MirrorDesk app for Android (MediaProjection + playback capture) | The way to drop Developer options: Android 5+ (sound 10+), one permission prompt, works over Wi-Fi or Tailscale like a browser. Needs an app built, signed and handed out (Play Store or APK). Recommended next step for Android. |
| USB debugging (scrcpy, today) | Works on every Android 5+ phone with a cable; needs Developer options once. |
| Wireless debugging (scrcpy, today) | Android 11+; works, but kept as a last resort (see above). |
- The adb daemon MirrorDesk starts is stopped when MirrorDesk quits (an adb that was already running
  for something else is left alone).

| Card says | Meaning / fix |
|---|---|
| Not set up | Android tools missing: **Install Android tools** (Windows) or install adb (Linux). |
| No phone | No phone with USB debugging / no paired Wi-Fi phone. |
| Allow on phone | Unlock the phone and tap *Allow* on "Allow USB debugging?". |
| Problem: "could not encode its screen" | Lower *Picture size* or *Picture quality* in Settings. |
| Problem: "blocks remote control" | Xiaomi family: enable *USB debugging (Security settings)*. |
| Disconnected | **Disconnect** was clicked: unplug and replug the phone, or *Reconnect*. |

Developer → *Android log* shows the phone-side messages (memory only).

## 5. Watching from other devices

Open **Viewers** → *Invite*: pick the address (Tailscale first when it runs, else this
computer's network), then **Copy** the link or let them scan the QR code. The link carries the viewer code
(`/login?code=…`), so opening it signs the device in at once (the code then leaves the address bar);
untick *Sign in with the link* to send the address only. Other devices stay signed in (cookie, 30 days).
The *Viewers* list shows the other devices watching (this computer, owner devices, viewers; their video
mode and how long), or *No viewers*. *All addresses* lists every address (Ethernet, Wi-Fi, Tailscale, USB
network, `hostname.local`) and adapter.

- **Viewer code** (stored as `access_code`): for people who should only watch. They get the mirrored
  screen and their own player controls (audio, quality, full screen) — no Devices, Viewers, Settings or
  Developer pages, no pairing code or phone names, no Disconnect, and the server refuses every other
  request (403). Installs from before owner codes keep their old access code as this viewer code.
- **Owner code** (Settings → *Access*): full control from your other devices. A viewer can switch with
  *Owner sign-in*. The computer running MirrorDesk never asks, unless *Ask for the owner code on this
  computer too* is on (same section; for computers other people use). The Quit command of the app menu and
  `mirrordesk stop` still work then: they prove themselves with a token in MirrorDesk's private data folder.
- **New code** for either signs out only the devices that used that code; the device that rotates the
  owner code stays signed in.
- The top bar's **0 viewers** counts the *other* devices watching (not this one). A browser only
  watches while a phone is connected: it joins when the phone starts and leaves 3 s after it stops, so an
  open page with no phone holds no viewer place (0.10.0 counted every open page, "1 of 4").

| Path | How |
|---|---|
| Ethernet / Wi-Fi LAN | Open `http://<receiver-ip>:8765/` on the other device. Wired receivers are fine: the phone only needs to *discover* the receiver (same router, not a guest network). |
| Tailscale | Install Tailscale on the receiver and the viewer; open the Tailscale URL (100.x or MagicDNS name). Media flows directly over the tailnet. Optional HTTPS: `tailscale serve --bg 8765` (proxied users still need a code). |
| Anything that lets the page through but not the video | Since 0.13.0 MirrorDesk is also a **TURN relay on its own web port** (`mirrordesk/turn.py`; the port tells HTTP and TURN apart by their first byte). Pages list it (`turn:<page host>:<port>?transport=tcp`, credentials from `/api/status` for signed-in pages); a browser uses it only when its direct checks fail, and its media then travels inside that TCP connection. It forwards only to the ICE sockets of MirrorDesk's own viewers. Seen on the user's ZimaOS box: friends reaching it through Tailscale got the page but never a picture, while the laptop's MirrorDesk worked the same way. Details → *network* says *through the page's connection* when it is used; diagnostics count `turn_allocations`, `turn_relayed_kb` and each viewer's `connection_kind`. Video packets are also smaller now (≤ ~1190 bytes, as browsers send theirs): aiortc's 1360-byte packets had to be split by VPNs, Tailscale and WireGuard carry at most 1280. |
| USB tether (Personal Hotspot) | Not the same as cable mirroring above. iPhone: cable + Settings → Personal Hotspot on (needs Apple's driver, a carrier hotspot plan and mobile data). The receiver gets a `172.20.10.x` "USB network" that a tethered laptop can watch on. **Experimental — not hardware-tested; whether iOS offers AirPlay over it is unknown.** |

## 6. The browser interface

The page was redesigned in 0.12.0 (design: the *MirrorDesk Web UI* canvas in Claude). The sidebar (a
bottom bar on phones) has four places; the top bar names the open one and shows the device, the number of
viewers and AirPlay's state.

- **Screen** — with no phone, the three ways to connect (each with its state and, for Wi-Fi, the code).
  With a phone, its screen and the player bar (**Disconnect** for the owner, **Audio off/on**, quality,
  details, pop out, full screen; icons only when the picture is narrow). The bar exists only while a phone
  is connected and hides 5 s after the pointer last moved; "Tap to play" appears only when the browser
  really blocked playback.
- **Devices** (0.13.0; *Phones* before) — *iPhone and iPad* (Wi-Fi/AirPlay: name, this session's code with **New code**, start /
  restart / stop; USB cable) and *Android* (USB debugging; Wi-Fi debugging folded away). While a phone
  mirrors, a bar at the top names it and has **Disconnect**. Opens on the tab of the phone that mirrors.
- **Viewers** — the invitation (link, QR code, viewer code), the viewers list, all addresses.
- **Settings** — four sections: *General* (name shown on devices, start Wi-Fi mirroring, ask before quitting, appearance, this
  computer), *Video and audio*, *Devices* (iPhone cable, Android), *Access* (people who can watch at once,
  fixed AirPlay ports, owner code, ask for it on this computer).
- **Quit** (bottom left of the side bar since 0.13.0; in the tab bar on narrow windows) — on the computer
  running MirrorDesk only (desktop installs). It asks *Quit MirrorDesk?* with *Don't ask me again*.
- Devices signed in with the viewer code see only **Screen** (and *Owner sign-in*).
- The switch "A new phone takes over from the one mirroring" is gone (0.12.0): a phone that connects
  while another one is connected always takes over (UxPlay `-nohold`), so the same phone can come back at
  once after its connection dropped. It needs the current session's code like any other.
- When the phone stops mirroring (Stop Mirroring on the iPhone, unplugging, Disconnect), every page
  shows *The phone stopped mirroring* and returns to "Ready to mirror" at once.
- **Developer** (under *Testing*) — report a problem / export diagnostics, stream details, viewers,
  phone discovery check, receiver log. These test tools are grouped so they can be removed for
  release: delete the *Testing* nav group and `#page-developer` in `mirrordesk/web/index.html`.

On wide screens a page opens beside the phone screen, so video keeps playing while you change
settings; on smaller screens it replaces it (Esc or × goes back). Phones get a bottom tab bar.

**Appearance** (Settings, saved per browser): themes *System*, *Light*, *Ash*, *Dark*, *Onyx*, and
color themes *Neutral*, *Ocean*, *Forest*, *Sunset*, *Lavender*, *Rose*, *Sand*, *Slate*, *Crimson*
that tint the surfaces and accent. The sun/moon button in the top bar switches light/dark and returns
to your chosen dark theme.

## 7. Quality and latency

- Default AirPlay request is **1920×1080 @ 60 fps** (the phone scales to the *height*: portrait iPhone ≈
  498×1080, landscape ≈ 1920×886). Prototype 6 requested 1280×720 @ 30 (portrait 334×720).
  The USB cable path ignores these settings: the phone sends its own full screen resolution.
- `1440p` is experimental (H.264 phones may cap at 1080p).
- **Auto** mode sends the phone's original H.264 to browsers that can decode it (Chrome/Edge/
  Safari/Android Chrome); others (e.g. Firefox) get **Compatibility** (re-encoded, adaptive bitrate,
  max height setting). A browser that cannot play passthrough automatically falls back to
  Compatibility, and if baseline H.264 also never decodes (e.g. Firefox on Linux without OpenH264) to
  VP8. A fallback is remembered per browser for 7 days only when the phone's own stream arrived and never
  played (a real decoding problem); changing the mode menu resets them.
- **Joining late (fixed in 0.11.0):** an iPhone sends a new keyframe only when its picture changes a lot
  (every 45–70 s while a tester's phone played a video, measured; never on a still screen). A browser can
  start the phone's own stream only at a keyframe or from a replay of the cached picture state (the GOP),
  and 0.10.0 gave that cache up after a minute, so anyone joining later saw nothing, in either mode,
  until the next keyframe. Now, once the GOP is too long to replay (6 MB or 1200 frames), the shared decoder
  keeps following it even with nobody watching ("warm"); a browser joining then starts **re-encoded**
  at once, and a browser stuck waiting for a keyframe is told to switch (the keyframe request answers
  `stuck`). Cost: one H.264 decode of the phone's stream while such a GOP lasts.
- A still phone screen sends no video; the last picture stays up ("screen unchanged") as long as the
  phone mirrors. **Stop Mirroring on the iPhone** is noticed at once (UxPlay's
  `raop_rtp_mirror->running is no longer true`, or *lost connection*): the page returns to "Ready to mirror"
  immediately instead of after a minute.
- Audio and video are separate WebRTC streams (no lip-sync coupling) and the browser is asked for
  minimum buffering. Settings → *Video and audio* → *Audio timing* → "Follow the phone's timing" waits for
  the phone's timestamps (more delay).
- **Sound stability (0.8.0).** Every input is converted once to 48 kHz stereo and leaves as exact 20 ms
  frames, one Opus packet each. Before, the iPhone's USB sound (85 ms blocks) became several packets with
  one shared timestamp and Chromium discarded 3 of 4 (measured on the real iPhone: 76% of the sound
  concealed → 0% after the fix, 3-minute runs). Big blocks are paced out at capture speed so the browser
  keeps a small buffer (~56 ms instead of ~156 ms). The QuickTime clock-skew answer now ignores a burst of
  buffered audio from an earlier session (it made the phone stutter).
- **Sound delay after pause/seek (fixed in 0.11.0).** UxPlay holds up to ~2.8 s of sound while it waits for
  a missing packet and then releases it all at once — after pausing, seeking or fast-forwarding on the phone.
  0.10.0 turned each such burst into dozens of small timestamp jumps; the browser filled every jump with
  invented sound and from then on played everything that much later (a tester's diagnostics: 1.5–1.8 s;
  measured with the same pattern in Chromium: 0.10.0 ~1100–1200 ms for good, 0.11.0 ~40–60 ms). Now
  each viewer's sound leaves on a steady 20 ms clock whose timestamps are real time: a burst of late sound
  is skipped down to one source block plus 30 ms; a tick the source is late for carries no packet (the
  browser conceals one packet, its delay does not grow); after 0.4 s of silence the clock stops and restarts
  at real time when sound returns. The Opus packets carry their own frame's timestamp (through aiortc's
  resampler and libopus' look-ahead the first packet after a jump kept the old timeline). The page no longer
  raises its own audio buffer.
- **The 1–3 s cutout itself (fixed in 0.11.1, in UxPlay).** 0.11.0 stopped the delay from building up,
  but the sound still cut out for ~2.8 s after every pause, seek or new video (a tester's 0.11.0
  diagnostics: exactly 2.76 s of sound dropped after each one). The cause was in UxPlay's audio buffer
  (`lib/raop_buffer.c`): the phone answers a pause or seek with a FLUSH and carries on with later packet
  numbers; UxPlay ignored the FLUSH and waited for the skipped packets until 256 newer ones (~2.8 s of
  AAC-ELD) had queued up behind them, then released them all at once. MirrorDesk now builds UxPlay with
  a small change (`third_party/uxplay/mirrordesk/`, diff in `mirrordesk.patch`): the buffer follows the
  FLUSH at once, and a missing packet is waited for at most 8 packets (~90 ms, enough for a resend).
  `tests/test_p7_uxplay_patch.py` compiles UxPlay's own buffer code, original and changed, and replays
  the packet patterns: original 255 packets held after a seek, changed 0, nothing lost or reordered.
  Every build applies it: Windows (`tools/build_uxplay_bundle.py`; a developer's MSYS2 checkout gets
  the three files copied in), Docker, and the Linux and macOS installers (which rebuild an existing
  receiver once: `uxplay/COMMIT` ends in `+mirrordesk.1`; 0.12.0's `+mirrordesk.2` adds the FLUSH report).
- **Stop Mirroring, reliably (0.11.1).** UxPlay's stop line could be handled before the last video
  packets of the stream it ended; they made the session look alive again ("paused", the last frame
  frozen for up to a minute). Seen intermittently in Firefox's e2e run (2 of 3). The hub now ignores
  AirPlay packets for 1 s after a session ends (a new phone takes seconds to connect); 4 of 4 since.
- **USB sound after a pause (fixed in 0.11.1).** The iPhone asks the computer how fast its clock runs
  (QuickTime "skew") and adapts its sound to the answer. The phone's sound clock stops while a video is
  paused, so measured across a pause our clock looked up to ~40% fast; the iPhone then sent its sound
  1.2–1.4x faster than real time for the next ~15 s, MirrorDesk had to drop the surplus (choppy sound),
  and the phone's own pacing suffered. The measurement now starts over when the two clocks jump apart
  (> 0.25 s between samples), and the answer is capped at 0.1% from real time (real clocks drift
  < 0.01%). Exported diagnostics carry the answer (`usb_skew_ppm`), the sound the source delivered
  (`source_audio_input_ms`, ~5000 per 5 s sample when real time), how late the server's event loop ran
  (`loop_lag_max_ms`, `loop_stalls` > 50 ms: the audio clock runs on it) and the re-encoder's backlog
  (`compat_queue`, `compat_dropped`). A synthetic 1178×2556 60 fps stream at 35 Mbps with a keyframe
  twice a second, re-encoded to VP8 for Chromium, ran on the development laptop with no sound dropped
  and no loop stall: the evidence points to the clock answer, not the computer's load (not yet
  re-tested with the iPhone on the cable).
- **Dropouts on Wi-Fi (fixed in 0.12.0).** 0.11.1's diagnostics from the user's ZimaOS box (iPhone over
  Wi-Fi) showed two patterns. (1) Every 5–15 s, 60 ms of sound skipped and 40–60 ms missing: now and then
  the phone's sound stalls for ~0.1 s on Wi-Fi and then arrives all at once; the fixed 40 ms backlog ran
  dry, and the sound that came late was skipped as stale. (2) After each seek, 120–300 ms skipped and about
  as much missing: when sound resumes, the phone sends its first ~0.17 s at once, ahead of time (AirPlay
  lets it run that far ahead), then the rest in real time; 0.11.1 kept 40 ms of that burst and ran dry
  until the phone's real-time sound caught up. Now each frame is due a backlog after the time the
  source's own pace says (the median arrival offset of packets that came at their own pace, so bursts
  and catch-ups do not move it); the backlog is the source's block size plus the worst stall of the last
  minute (a stall counts if the pace stays put afterwards, so a brief pause does not), up to 0.15 s. Sound
  sent ahead plays from its start; a stall drains the backlog and the late sound refills it; right after
  the source ran dry the clock waits until the backlog is full again; sound more than 0.2 s late is skipped
  at once, a little surplus leaves one quiet frame at a time with a 2 ms blend; in near silence a tick may
  be left empty to rebuild the backlog. **UxPlay now reports the phone's FLUSH** (pause, seek, next video:
  `MirrorDesk: audio flush` on stdout, change `mirrordesk.2`): the sound still queued stops at once and the
  next sound starts a new stream with its full backlog. Replaying the measured pattern (5 minutes, stalls
  of 60–110 ms every 5–15 s, a seek every 20–30 s; `tests/test_p7_media.py` and a simulation of 0.11.1's
  code): 39–44 audible dropouts → 1–3, sound skipped 1.6–2.0 s → 0. In Chromium with a 100 ms stall every
  2 s (`FAKE_UXPLAY_AUDIO_BURST=2,0.1`): 0.00% of the sound filled in by the browser, nothing skipped; with
  a seek every 3 s (`FAKE_UXPLAY_AUDIO_SEEK=3,0.3`): nothing skipped, nothing missing. Cost: on Wi-Fi that
  stalls, the sound runs ~0.1 s later than on a calm network (backlog ~120 ms instead of ~40 ms). Exported
  diagnostics add `source_audio_jitter_ms`, `source_audio_backlog_ms`, `source_audio_trimmed_ms`,
  `source_audio_held_ms`, `source_audio_flushes` and `source_audio_flushed_ms`.
- **The picture stalling for a moment every 15–20 s (fixed in 0.13.0).** The user's diagnostics (0.12.0,
  Windows and ZimaOS, iPhone over Wi-Fi) showed a freeze every 15–20 s on every viewer at once, the
  computer's own screen included, in both modes; a steady synthetic stream gave none in 2 minutes. Wi-Fi
  delivers the phone's video like its sound: nothing for 0.1–0.3 s (a lost packet resent, the phone's power
  saving while nobody touches it), then all of it at once. UxPlay's RTP output gave every frame the same
  timestamp, so nothing downstream could tell when the phone had shown it. **MirrorDesk's UxPlay change
  `mirrordesk.3`** (`renderers/video_renderer.c`) stamps forwarded frames with the phone's own frame time,
  and the hub's `VideoPacer` sends each frame on that schedule: its stream's usual arrival offset plus its
  position plus a delay that covers the worst stall of the last minute (as for sound, up to 0.35 s). The
  sound waits as long, so picture and sound stay together (before, the sound's backlog alone put it up to
  ~0.1 s behind the picture). A shorter delay is reached slowly, so frames never come in a burst. Chromium,
  synthetic stream with a 150 ms stall every 2 s: freezes 0 in 60 s (Automatic and Original), longest gap
  between frames under 85 ms after the first stall. Without the change (another UxPlay build) frames go out
  as they arrive, as before. Diagnostics: `source_video_timed`, `_jitter_ms`, `_delay_ms`, `_late_frames`,
  `_stalls`. Cost: on Wi-Fi that stalls, picture and sound run ~0.15–0.2 s later.
- **Original quality, reliably (0.13.0).** Choosing *Original* mid-stream used to wait for the iPhone's next
  keyframe (331 s apart on the user's phone) or replay a cached GOP of up to 1200 frames at once, which
  overflowed Chromium's frame buffer (800 frames): it dropped most frames, asked for a keyframe, got the replay
  again, and the server stalled up to 0.24 s each time (the user's ZimaOS diagnostics). Now replays are
  limited to 4 s / 1.5 MB and yield to the rest of the server between frames, and when the cache is too long
  the viewer is shown the current picture **re-encoded** (constrained baseline H.264 from the shared decoder)
  in the same connection until the phone's next keyframe, which then continues the phone's own video. A
  damaged picture (packet loss) recovers the same way. Diagnostics: `source_video_bridged`.
- Press **Details** on the video to see input, resolution, fps, bitrate, buffering, network RTT (and whether
  the video comes directly or through the page's connection) and the **audio** line (share of audio the
  browser had to fill in, dropouts). The same numbers go into exported diagnostics, plus the server's
  skipped and missed sound (`source_audio_dropped_ms`, `source_audio_missed_ms`).

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| Phone does not list MirrorDesk | Devices → *Wi-Fi (AirPlay)* = Ready; Developer → *Phone discovery check* (Windows); phone and receiver on the same non-guest network; on Ethernet try a cable straight to the router (powered boxes can filter mDNS). Or use the USB cable (section 3). |
| Phone connects, browser black | Details → mode/codec; switch quality to *Compatibility*; Developer → mark *Picture froze* and export. |
| Other device cannot open the page | Windows Firewall rule for Python (Private), the address from Viewers (Tailscale for other networks), the viewer code. |
| Other device opens the page but never gets a picture | Developer → mark *Viewer cannot connect* (or *Viewer cannot see the screen*) on the owner's page and export: diagnostics record each viewer that never connected (`viewer_failed`: browser and device type, how it reached MirrorDesk, page in the background or not). Since 0.13.0 such a browser falls back to the relay on the web port (section 5). |
| Unfamiliar addresses under Viewers → *All addresses* | Every network the computer has: Ethernet and Wi-Fi (the same computer can have both), Tailscale (100.x), `hostname.local`, and VPN or remote-access networks — on a ZimaOS box its ZeroTier interface (`zt…`, a 172.x address, the zerotier-one system service) and its ZimaNet tunnel (`tun0`, a 10.x address, the "ZimaNet Service"). Those work only for devices connected through that service. Docker's own bridges are never listed. |
| The phone asks for the code again | Expected after a session ended (MirrorDesk started again, New code, or 4 hours without a phone): every session has its own code, shown on the Screen and on Devices. |
| After an update the page shows only the Mirror tab and a player bar, nothing works | A browser ran its cached older script with the new page (up to 0.11.0, scripts had no cache rule). Since 0.11.1 the page's links name the build, scripts are re-checked on every load, and an open page reloads itself when MirrorDesk was updated; a page still open from 0.11.0 or older needs one manual reload. |
| "This browser blocks the video connection (WebRTC)" | The browser offered no network address for video: Brave → Settings → Privacy and security → *WebRTC IP handling policy* → Default; extensions that "prevent WebRTC leaks" do the same. |
| AirPlay shows `Problem` | Devices page message + Developer → *Receiver log* (UxPlay missing, too old for `-vrtp`, or crashed 5× in 2 min). |
| USB cable problems | See the table in section 3. |

## 9. Tests

```bat
.venv\Scripts\python -m pytest -q tests                              :: unit/integration (aiortc as browser)
.venv\Scripts\python -m playwright install chromium firefox webkit   :: once
.venv\Scripts\python -m pytest -m e2e tests\e2e                      :: Playwright UI suite, all browsers
.venv\Scripts\python -m pytest -m e2e tests\e2e --browser chromium   :: one browser
```

The e2e suite starts the real server with `tests/fake_uxplay.py` streaming synthetic H.264/L16 —
browser/OS compatibility evidence, **not** iPhone acceptance. Failures save screenshots and
Playwright traces to `test-results/p7-e2e/`.

USB cable tests (no hardware needed): `tests/test_p7_quicktime.py` checks every packet MirrorDesk sends
byte-for-byte against captures from real iPhones (`tests/fixtures/quicktime/`, from quicktime_video_hack)
and decodes a captured 1126×2436 keyframe with FFmpeg; `tests/test_p7_usb.py` drives the capture
thread with a simulated phone that replays those captures and uses the real iPhone 11's USB descriptors
(`tests/fixtures/iphone-usb/`); `tests/test_p7_lockdown.py` pairs with a simulated phone over usbmux,
lockdown and TLS. All tests run with `MIRRORDESK_USB=0`, so a phone plugged into the test machine is
never touched. `tests/test_p7_ui_text.py` keeps the interface
free of buzzwords, inline scripts/styles (blocked by the CSP) and missing element IDs.

Android tests use `tests/fake_adb.py`: a stand-in for adb 37.0.1 (same command output formats) that
also plays the phone side of the scrcpy 5.0.1 protocol with synthetic H.264 and a tone
(`tests/test_p7_android.py`, `tests/e2e/test_android.py`). Every test runs with `MIRRORDESK_ANDROID=0`
unless it supplies that fake, so a real phone or adb on the test machine is never used. Sound:
`tests/test_p7_media.py` pins the 20 ms framing and pacing; `test_usb_sized_audio_blocks_play_without_dropouts`
plays USB-sized audio blocks in real browsers and checks the browser's own concealment counters.
`tests/test_p7_usbdriver.py` covers the cable-driver installer without changing any driver.

The tester's 0.10.0 problems are reproduced by `tests/fake_uxplay.py` knobs and checked in real browsers
(`tests/e2e/test_ui.py`): `FAKE_UXPLAY_ENDLESS_GOP=1` (one keyframe, then P-frames encoded live, as an
iPhone mirroring a video) for joining late, `FAKE_UXPLAY_AUDIO_BURST=every,pause` (UxPlay's held sound
released at once) for the sound delay, `FAKE_UXPLAY_STOP_AFTER` (Stop Mirroring on the phone) and
`FAKE_UXPLAY_NO_MEDIA=1` (no phone: no viewer, no player bar, no "tap to play"). Run against the
packaged 0.10.0 they fail as the tester saw (no picture within 40 s; audio buffer ~1.1 s; Ready after 57 s;
"1 of 4"); 0.11.0 passes. `tests/test_p7_usbmuxd.py` runs the usbmuxd client against a stand-in usbmuxd
(`USBMUXD_SOCKET_ADDRESS`), and `tests/test_p7_usb.py` shares a simulated phone with a simulated usbmuxd.

## 10. Security model

On a desktop, same-computer browsers are trusted as the owner. Every other client — LAN, Tailscale,
USB, or anything behind a reverse proxy — needs a code: the owner code for full control, the viewer
code to watch only. Viewer sessions reach an allowlist (`VIEWER_PATHS` in `mirrordesk/security.py`:
the page, a status without secrets, their own WebRTC viewer); every other path, including any added
later, is owner-only. If you put a **loopback forwarder** in front
of MirrorDesk on the same machine (nginx `proxy_pass`, socat, `ssh -R`, `netsh portproxy`, Tailscale in
userspace mode), turn on Settings → *Access* → *Ask for the owner code on this computer too* or set
`MIRRORDESK_TRUST_LOCALHOST=0` — such forwarders make remote users look local. Containers never trust
localhost.

WebRTC media uses every usable interface even when the web UI is local-only (`--host 127.0.0.1`):
Firefox never connects to a loopback-only peer, and media connections need the per-session ICE
credentials plus DTLS. `MIRRORDESK_ICE_ADDRESSES=loopback` restores loopback-only media (Chrome/Edge
on the same computer only). Host-name allowlist (IP literals, localhost, this
machine's names, `*.ts.net`, `MIRRORDESK_ALLOWED_HOSTS`) blocks DNS rebinding; state changes need a
custom header and same origin; strict CSP; no third-party scripts. AirPlay itself remains
code-protected by UxPlay, with a code per session: a phone that entered it is trusted only until the
session ends (then a new code, a new receiver key and an empty register). UxPlay runs with `-reg`, so
a client that skips the code (pair-verify without pairing) is refused unless it paired in this
session — without `-reg`, UxPlay accepted any client that went straight to pair-verify. Do not port-forward MirrorDesk to the
internet — use Tailscale. An invitation link carries the viewer code: whoever has the link can watch
(never control); a new viewer code cancels every link sent before.

USB cable: the phone only sends its screen to a computer it trusts (confirmed on the real iPhone), and
the first pairing needs the user to tap Trust and enter the phone's passcode. After that, a trusted
phone plugged in while cable mirroring is on starts mirroring by itself (no PIN), so turn cable
mirroring off in Settings on a shared computer. `.runtime\usb-pairing\` holds the pairing records,
including private keys: treat them like passwords and never share them (diagnostics exports never
include them). Status and diagnostics never include the phone's serial number.

Android: controlling the phone is allowed from this computer only by default (Settings can turn it
off or allow every signed-in device). Input travels over a WebSocket that refuses any page from
another origin (browsers send no custom headers on WebSocket handshakes, so this replaces the header
check). The Wi-Fi pairing password in the QR code is never shown in status or logs. The iPhone cable
driver can only be installed or removed from the browser on the computer itself (the Windows prompt
appears there). A phone that trusts this computer's adb key (`%USERPROFILE%\.android` on Windows,
`/data/.android` in Docker) can be mirrored whenever it is plugged in or reachable over Wireless
debugging; revoke it on the phone (Developer options → Revoke USB debugging authorisations).

## 11. Tester packages (Windows, macOS, Linux, Docker)

Builds packages someone else can install like a product, with none of this PC's private data. Every
package has `INSTALL-GUIDE.html` (every install type on every OS), `TESTER-GUIDE.html` (connecting,
testing, exporting diagnostics) and `NOTICE.txt` (all rights reserved; third-party licenses unchanged).
The guides live in `packaging/common/` as page bodies with `__LINUX_FILE__`-style placeholders; the
packager fills in the file names and makes them complete HTML documents.

```bat
.venv\Scripts\python tools\build_uxplay_bundle.py     :: UxPlay + GStreamer/FFmpeg DLLs -> build\uxplay (verified without MSYS2)
.venv\Scripts\python tools\build_wdi_simple.py        :: cable-driver tool -> build\usb-driver
.venv\Scripts\python tools\make_tester_package.py     :: -> dist\MirrorDesk-Tester-<version>-<date>-Windows.zip, -macOS.zip,
                                                      ::    -Linux.zip, -compose.yaml   (--target windows|macos|linux|all)
```

| Package | What the tester does | What it installs |
|---|---|---|
| `…-Windows.zip` | unzip, `Setup-MirrorDesk.cmd` | below |
| `…-macOS.zip` | `sh ~/Downloads/MirrorDesk/install.sh` (or `Install MirrorDesk.command`) | section 12 |
| `…-Linux.zip` | unzip, `sh MirrorDesk/install.sh`, or from another computer: `scp -r MirrorDesk USER@SERVER:/tmp/ && ssh -t USER@SERVER sh /tmp/MirrorDesk/install.sh` | desktop app, systemd service or `--docker` stack (section 12) |
| `…-compose.yaml` | paste into Dockge after the image is built | the universal stack (section 2) |

Every package is a zip with one top folder `MirrorDesk`. The Linux one holds `install.sh`, `README.txt`, the
guides and `app/` (the program, also the Docker build context); until 0.10.0 it was one self-extracting
`.sh` file, which looked as if it contained nothing. The macOS and Linux zips store Unix permissions so
the scripts stay executable. Scripts are LF-only (a test checks).

### Windows package

- `build_uxplay_bundle.py` rebuilds UxPlay from the unmodified source folder of your build
  (`C:\msys64\home\<you>\mirrordesk-uxplay`, same options: Release, `USE_MDNS=ON`, `USE_DNS_SD=OFF`) in
  a temporary folder with `-ffile-prefix-map`, so the binary carries no local paths; your own build is
  not touched. It copies the 10 GStreamer plugins MirrorDesk's pipelines use, plus `playback` and
  `autodetect`, which UxPlay's startup check requires (`check_plugins` in `audio_renderer.c`), and every
  DLL they load (137 files, ~180 MB, ~70 MB zipped — mostly FFmpeg's codec libraries, needed by the
  AAC/ALAC decoder), records each file's MSYS2 package/version/licence (`BUNDLE.json`, `licenses/`,
  `SOURCES.txt`), then runs `uxplay -h` and checks every element and every plugin of that startup check
  with MSYS2 removed from PATH. It uses UxPlay's built-in mDNS responder, so Bonjour is not needed.
- **Fixed in 0.10.0:** the 0.8.0 and 0.9.0 Windows packages lacked `playback` and `autodetect`, so
  their UxPlay exited at once ("Required gstreamer plugin 'playback' not found") and wireless
  mirroring never started on a tester's PC (USB and Android were unaffected). `uxplay -h` does not
  run that check, which is how the earlier verification missed it.
- `make_tester_package.py` refuses to zip if any file is a settings/key/pairing/log/diagnostics/test
  file or contains (text or binary) the Windows user name, computer name, LAN addresses, current
  access code or PIN, the development iPhone's serial number, or `MIRRORDESK_PRIVATE_EMAIL`.
- On the tester's PC, `Setup-MirrorDesk.cmd` → `setup\setup.ps1` downloads uv 0.12.12 (SHA-256 pinned),
  installs Python 3.13 and the locked libraries **inside the folder** (`runtime\python`, `.venv`),
  downloads Google's adb (SHA-256 pinned), creates Start menu/desktop shortcuts, adds firewall rules
  for exactly MirrorDesk's Python, `uxplay.exe` and `adb.exe` on private networks (**one** Windows
  prompt) and starts the tray app. `Uninstall-MirrorDesk.cmd` removes rules, shortcuts and the
  sign-in entry. `TESTER-GUIDE.html` explains connecting, testing and exporting diagnostics.
- Verified on the development PC (0.10.0, 2026-10-18): package built, privacy gate (also proven to
  catch planted leaks), setup from the unpacked zip in a path with spaces (without the firewall prompt,
  16 s), server started from the installed copy with the receiver **running** on the bundled UxPlay
  ("Phones on this network can find …"), and Google adb installed. **Not verified:** the firewall prompt, the driver
  install prompt, and a fresh PC (no MSYS2, no libusb, other Windows build).
- Verified again for 0.11.0 (2026-10-19): privacy gate clean; setup from the unpacked zip in a path with spaces
  (15 s); receiver running on the bundled UxPlay; Android waiting with Google adb; `python -m mirrordesk --quit`
  stopped it in 2.6 s, UxPlay gone, quit token removed.
- Verified for 0.11.1 (2026-10-20): the bundle rebuilt with MirrorDesk's UxPlay change (`BUNDLE.json`:
  `mirrordesk_change`), its own check passed (`uxplay -h`, every element and required plugin); privacy
  gate clean; setup from the unpacked zip 18 s; receiver running on the bundled UxPlay; quit in 2.7 s.
- Verified for 0.12.0 (2026-10-20): the bundle rebuilt with change `mirrordesk.2` (the package tool now
  refuses a bundle built with another change); privacy gate clean for all packages; Windows setup from the
  unpacked zip 23 s, receiver running on the bundled UxPlay, quit in 3 s, nothing left running. Linux: a
  Fedora 44 desktop install of the 0.11.1 package updated to 0.12.0 in 13 s, rebuilt the receiver
  (`+mirrordesk.2`, the FLUSH report in the binary) and runs it with `-key`, `-reg` and `-nohold`; running the
  installer again skips the build; shellcheck clean. ZimaOS stack updated to 0.12.0 (healthy, new receiver
  key at start, `-reg`).
- Verified for 0.13.0 (2026-10-21): bundle rebuilt with `mirrordesk.3`; privacy gate clean for all packages;
  Windows setup from the unpacked zip 18 s, receiver running on the bundled UxPlay, quit in 3.6 s, nothing left
  running. Linux: the 0.12.0 Fedora 44 desktop install updated in 12 s, receiver rebuilt (`+mirrordesk.3`, FLUSH
  report in the binary), `-key`/`-reg`/`-nohold`; installer again skips the build; shellcheck clean. ZimaOS:
  image built with the new receiver (differs from 0.12.0's), healthy; a throwaway container of it was watched
  from Windows over the relay only (Chromium and Firefox: `relay` and the box's LAN address, video playing). Windows app
  window: open, a second start opens another window in it, Quit closes all of it in 3.6 s (no browser process
  with MirrorDesk's profile left, profile marked as cleanly closed). The full Chromium/Firefox e2e runs were cut
  short when the PC ran low on memory; the new and changed e2e tests passed in both browsers before that.

## 12. macOS and Linux (native installs)

The same server runs natively on macOS and Linux. The installers (`packaging/macos/install.sh`,
`packaging/linux/install.sh`) build UxPlay 1.74 from `third_party/uxplay` (SHA-256 checked) into
`<install>/uxplay/bin/uxplay`, which `receiver.py` prefers on every OS (platform `posix-bundle`), install
uv 0.12.12 (SHA-256 pinned per CPU) → Python 3.13 and the locked libraries inside the install folder, and
Google's adb where Google ships it (x86-64 Linux, macOS; ARM Linux uses the distribution's `adb`).

| | macOS | Linux desktop | Linux server |
|---|---|---|---|
| Needs | Apple Silicon + macOS 14, or Intel + macOS 12 (wheel minimums); Command Line Tools; Homebrew (`cmake pkgconf libplist openssl@3 gstreamer coreutils`) | 64-bit; apt, dnf or pacman (zypper best effort); sudo | same, over SSH |
| Folder | `~/Library/Application Support/MirrorDesk` | `~/.local/share/mirrordesk` | `/opt/mirrordesk`, data `/var/lib/mirrordesk` |
| Starts | `~/Applications/MirrorDesk.app` | app menu *MirrorDesk* (`mirrordesk.desktop`), or `mirrordesk start` | systemd `mirrordesk.service` (user `mirrordesk`) |
| At login | LaunchAgent `com.mirrordesk.app` (opens the app, no browser) | `~/.config/autostart/mirrordesk.desktop` | always on |
| Quit | Dock icon → Quit, or Quit at the bottom left of the page | app menu *Quit MirrorDesk* (also right-click *MirrorDesk* → Quit), `mirrordesk stop`, or Quit at the bottom left of the page | `systemctl stop` |

- **Background mode on macOS/Linux:** `MirrorDesk.pyw` (`--background`) now also gives macOS and Linux the
  desktop controls (Quit, start at login, `desktop.py`); there is no tray icon there. Docker and the
  Linux service run without `--background`, so the browser hides those controls as before.
- **MirrorDesk.app** is a stay-open AppleScript applet made on the Mac by `mirrordesk/unixsetup.py`
  (`osacompile`, MirrorDesk's icon and `NSLocalNetworkUsageDescription`, ad-hoc `codesign`): a Dock icon
  while MirrorDesk runs, click = open the page, Quit = stop MirrorDesk, and it quits itself when MirrorDesk
  is quit from the browser. Starting the server from the app makes the app the program macOS names when it
  asks for local-network access.
- **macOS has no "die with the parent"**: the receiver records UxPlay's PID in `<data>/uxplay.pid` and
  stops a UxPlay left by a crashed run before starting a new one (only if that PID is really `uxplay`).
  UxPlay's log is line-buffered through Homebrew's `gstdbuf` (the app starts with macOS's minimal PATH).
- **Linux launcher and command (0.11.0):** the app menu has *MirrorDesk* (starts it, or opens the page when it
  runs; right-click offers *Open* and *Quit*) and *Quit MirrorDesk*. The installer links
  `start-mirrordesk.sh` as `~/.local/bin/mirrordesk`: `mirrordesk start | open | stop | restart | status |
  log` (`--console` runs it in the terminal). Quitting uses `python -m mirrordesk --quit`, which asks the
  running MirrorDesk to quit with the token in its data folder and waits until it is gone (a desktop
  notification says so). The same script works on macOS from the install folder. MirrorDesk.app's Dock
  Quit and both installers use `--quit` too, so they work when the page asks for the owner code locally.
- **Quit, then start again at once:** a quitting MirrorDesk closes its page first and keeps its media ports a
  few seconds longer (open connections get up to 5 s, then UxPlay stops). In 0.10.0 a start in that window
  failed with "Another program is using MirrorDesk's ports" (seen in the 0.11.0 container tests after
  `mirrordesk stop` + `start`). Now `--quit` returns only when the ports are free, and a start that finds
  them held while nothing answers waits up to 15 s before asking. The desktop installer closes a running
  MirrorDesk before it replaces its files (it used to replace them first).
- **iPhone by cable:** not offered on macOS (macOS's usbmuxd and QuickTime own the phone). On Linux,
  usbmuxd (Linux's iPhone service) takes every plugged-in iPhone, so 0.10.0 could not claim it ("resource
  busy"; a tester on Fedora 44 was told to `systemctl stop usbmuxd`). Since 0.11.0 MirrorDesk shares the
  phone with usbmuxd: when the usbmux interface is busy it runs the trusted lockdown session through
  usbmuxd's socket (`mirrordesk/usbmuxd.py`), never sets a configuration usbmuxd already set, claims only
  the capture interface, and leaves the phone's USB mode to usbmuxd. Current usbmuxd switches a phone in
  capture mode straight back unless it runs with `USBMUXD_DEFAULT_DEVICE_MODE=2`, so the installer asks to
  add `/etc/systemd/system/usbmuxd.service.d/50-mirrordesk.conf` with that setting (default yes; the
  uninstaller removes it). With it, an iPhone's USB internet sharing (Personal Hotspot by cable) does not
  work on that computer. Without it the page says what to do. **Not yet tried with a real iPhone on Linux.**
- **Linux USB permissions:** `/etc/udev/rules.d/70-mirrordesk.rules` gives Apple phones and every Android
  ADB interface (class ff/42/01) to the signed-in user (`uaccess`) or, for the service, group `mirrordesk`.
- **Linux firewall:** fresh installs use fixed AirPlay ports 7100–7102 (`airplay_ports`); when ufw is active
  an app profile `MirrorDesk` (8765/tcp, 7100–7102 tcp+udp, 5353/udp, 32768–60999/udp) is allowed from
  10/8, 172.16/12, 192.168/16 and 100.64/10; with firewalld a `mirrordesk` service is added. macOS: when the
  Application Firewall is on, the installer offers to allow MirrorDesk's Python, UxPlay and adb.
- **Update:** run the newer installer; `.runtime`/data, `.venv`, Python and an up-to-date UxPlay are kept.
  **Uninstall:** `<install>/mirrordesk-installer.sh --uninstall` (`--purge` also deletes settings).

**Verified 2026-10-17** (throwaway containers on the ZimaOS box, x86-64; each: install, start, sign in
with the access code, receiver `running` with the installer-built UxPlay 1.74 (`posix-bundle`), Google adb,
the page loads; processes run as the unprivileged user):

| Test | Result |
|---|---|
| Debian 13 / Ubuntu 22.04 / Fedora 44 / Arch, `--server` (as root, no systemd) | pass; 226 / 177 / 89–174 / 91 s; Fedora 44's own `gstreamer1-plugin-libav` has `avdec_aac` (no RPM Fusion needed) |
| Ubuntu 24.04 desktop mode (user with sudo, `DISPLAY` set, chosen automatically) | pass: app-menu entry, AirPlay ports 7100–7102, `/api/desktop` = linux, start-at-login on/off writes/removes `~/.config/autostart/mirrordesk.desktop`, Quit stops it, `--uninstall` keeps `.runtime` |
| The one-line SSH install **from Windows PowerShell 5.1** (`scp …; ssh -t …`) into Debian 13 with systemd + sshd, user `pi` with sudo | pass: re-exec through sudo, service `mirrordesk` enabled + active (User=mirrordesk, Restart=on-failure), survives `systemctl restart`; same command again = update in 5 s (UxPlay/Python reused, code and settings kept); `ssh -t … sudo sh /opt/mirrordesk/mirrordesk-installer.sh --uninstall` removes service and files, keeps `/var/lib/mirrordesk` |
| `--docker` inside Docker-in-Docker **with Dockge 1.5.0** running there | pass: Dockge found, stack `/opt/stacks/mirrordesk` (compose.yaml + .env), host network, receiver running, address list + access code printed; second run keeps an edited compose file and the data; `--docker --uninstall` removes the container, keeps the stack folder |
| `mirrordesk:0.9.0` image built from the package (130 s), run in bridge mode | pass: healthy, `MIRRORDESK_AVAHI=off` (UxPlay's own mDNS), no Avahi/D-Bus processes |
| shellcheck 0.10.0 on every script | no warnings |

**Verified 2026-10-19 (0.11.0)**, throwaway Fedora 44 container on the ZimaOS box, desktop mode as a sudo
user (`DISPLAY` set; usbmuxd installed; `/run/systemd/system` present so the installer treats it as a
systemd computer): install in 68–101 s; receiver `running` (`posix-bundle`); the usbmuxd drop-in written
with `USBMUXD_DEFAULT_DEVICE_MODE=2`; *MirrorDesk* (with Open/Quit actions) and *Quit MirrorDesk* in the app
menu; `~/.local/bin/mirrordesk` → `start-mirrordesk.sh`; `mirrordesk stop/start/status`, three `restart`s in a
row, the *Quit MirrorDesk* entry's command; with the owner code required locally (and Docker's no-localhost
trust) a plain local quit gets 401 while the token quit works; a start right after a quit is up in 1 s; the
installer run again right after a start closes the running copy first and starts the new one; `--uninstall`
while running removes the entries, the command and the drop-in and keeps `.runtime`; shellcheck 0.10.0:
no warnings.

**Not verified:** anything on a real Mac (the macOS installer, MirrorDesk.app, the LaunchAgent, Local
Network permission); a Linux desktop with a real display and browser; ufw/firewalld rules and the udev
rule with a real phone; usbmuxd really honouring the drop-in with a real iPhone (systemd and usbmuxd did not
run in the container); ARM64 (Raspberry Pi); AirPlay from a real iPhone to a native Linux install.
