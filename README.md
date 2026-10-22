# MirrorDesk

Mirror an iPhone, iPad or Android phone (screen and sound) to **any browser**: this PC, another computer
or phone on your network, or over Tailscale. The phone connects wirelessly (AirPlay) or with a **USB
cable** (no Wi-Fi or internet). One server (`python -m mirrordesk`) runs the AirPlay receiver (UxPlay
1.74) and the USB and Android inputs, and serves the controls and the video. Receiver start/stop,
pairing PIN, quality, appearance, diagnostics and viewing all happen in the browser.

Full guide: [`BROWSER-FIRST-GUIDE.md`](BROWSER-FIRST-GUIDE.md).

## Install with Docker (Dockge, ZimaOS, any Linux host)

In Dockge click **+ Compose**, name the stack `mirrordesk`, paste [`docker/compose.yaml`](docker/compose.yaml)
(on ZimaOS: [`docker/dockge/compose.yaml`](docker/dockge/compose.yaml)) and click **Deploy**. Docker downloads
the image `ghcr.io/brother-ben/mirrordesk:0.13.0` (x86-64 and ARM64) by itself. Without Dockge, on the host:

```sh
curl -fsSLO https://raw.githubusercontent.com/Brother-Ben/mirrordesk/main/docker/compose.yaml
docker compose up -d
docker compose logs mirrordesk    # owner and viewer codes + addresses
```

Then open `http://<host address>:8765/` and enter the owner code from the log. Host networking is required
(AirPlay discovery), so this needs a Linux host, not Docker Desktop on Windows or macOS.

> **Status (0.13.0, 2026-10-21):** development prototype. 0.13.0: the picture no longer stalls for a
> moment every 15–20 s on Wi-Fi (MirrorDesk's UxPlay change now passes the phone's frame times, and frames
> go out on the phone's own schedule with a delay that covers the stalls; the sound waits as long, so they
> stay together); **Original** quality shows the picture at once and recovers from damage (re-encoded until
> the phone's next keyframe, small replays only); viewers whose direct connection fails (seen through
> Tailscale on the ZimaOS box) get the video through MirrorDesk's own TURN relay on the web port, and video
> packets fit VPNs; the AirPlay code stays the same for the whole session (new at a restart, on **New
> code**, or after 4 hours without a phone; **Disconnect** keeps it); Windows opens MirrorDesk in its own
> window, and **Quit** (bottom left, with "are you sure?" and *Don't ask me again*) closes everything,
> including that window and MirrorDesk's adb; Desktop and Start-menu shortcuts; *Devices* instead of
> *Phones*.
> 0.12.0: the phone's audio no longer drops out
> on Wi-Fi that stalls now and then (each viewer's audio keeps a backlog sized to the stalls it has seen,
> and plays by the phone's own pace, so sound the phone sends ahead after a seek plays from its start);
> UxPlay reports the phone's FLUSH, so the old sound stops the moment you pause or seek; **every AirPlay
> session has its own code** and phones are forgotten when it ends (a new receiver key and an empty
> register, `-reg`); a new, plainer page (Screen, Phones, Viewers, Settings with sections).
> 0.11.1: the sound comes back at once after
> pausing, seeking or switching videos on an iPhone over Wi-Fi (MirrorDesk now builds UxPlay with a
> small fix to its audio buffer, which held all new sound back ~2.8 s: `third_party/uxplay/SOURCE.txt`);
> an iPhone on the USB cable no longer sends its sound up to 1.4x too fast after a pause (MirrorDesk's
> clock answer is re-measured after pauses and capped at real clock drift); a page can no longer run an
> older script after an update (versioned links, and open pages reload themselves); diagnostics record how
> late the server's audio clock runs. Android options: [`ANDROID-COMPATIBILITY.md`](ANDROID-COMPATIBILITY.md).
> 0.11.0 fixed what a tester found in 0.10.0
> (guide sections 4–7 and 12): viewers who join after the iPhone started now get a picture at once (an
> iPhone sends a keyframe only every minute or so, and the picture cache gave up after one); the sound no
> longer drifts 1–2 s behind after pausing or seeking on the phone; Stop Mirroring on the iPhone returns
> every page to "Ready to mirror" at once; a page with no phone holds no viewer place, shows no player bar
> and no "tap to play"; the AirPlay code is new at every start; the owner's **Disconnect** ends the phone's
> session; Android Wi-Fi pairing's QR code shows (it was a broken image) and Wireless debugging is a last
> resort; iPhone by cable on Linux shares the phone with usbmuxd; Linux gets "Quit MirrorDesk" in the
> app menu and a `mirrordesk` command. The page was reworked: **Phones** (iPhone/Android tabs), **Share**
> (an invitation link and QR code that sign viewers in), plainer settings, Quit in the top bar.
> 0.10.0: two codes. The **owner code** gives full control; the **viewer code** (the old access code)
> can only watch. Since 0.9.0: native **macOS** and **Linux**
> installs (desktop app, or a systemd service installed over SSH), a universal **Docker / Dockge** stack,
> and tester packages for all of them (guide sections 11 and 12). The Linux installs and the Dockge stack
> were verified in containers (Debian, Ubuntu, Fedora, Arch; systemd over SSH from Windows); the macOS
> installer has not run on a real Mac yet. **USB cable mirroring works on a real iPhone 11**
> (828×1792, about 60 fps, with sound); its sound no longer drops out (76% → 0% concealed, measured).
> **Android** (USB or Wi-Fi, with control from the browser; guide section 4) is new and tested against
> a simulated phone only. Wireless (AirPlay) mirroring with Prototype 7 is not yet tested on a real
> iPhone. The Docker image builds and runs on ZimaOS 1.7.1 (AirPlay announced on the LAN; no phone
> mirrored through it yet). A Windows **tester package** (guide section 11)
> bundles everything for someone else to install.

## Windows quick start

1. Once: double-click **`Setup-MirrorDesk.cmd`** (needs [uv](https://docs.astral.sh/uv/getting-started/installation/);
   creates this folder's `.venv`). Re-run it after updating MirrorDesk.
2. Double-click **`Start-MirrorDesk.cmd`** (or the *MirrorDesk* shortcut on the desktop or in the Start
   menu: `python -m mirrordesk.winsetup install --no-launch --no-firewall` makes them). MirrorDesk runs in the
   background and opens in its own window (Edge or Chrome in app mode; else your browser opens
   `http://127.0.0.1:8765/`). The MirrorDesk icon sits next to the clock (maybe under **^**):
   *Open MirrorDesk* / *Start with Windows* / *Quit MirrorDesk*.
3. iPhone: Control Center → Screen Mirroring → **MirrorDesk Web** → enter the code shown in the browser
   (one per session: it stays while MirrorDesk runs; a new code, and every phone forgotten, when MirrorDesk
   starts again, on **New code**, or after 4 hours without a phone).
   **Or by cable:** plug the iPhone in and unlock it; the first time, tap **Trust** on the phone (needs
   the libusb driver on Windows, see section 3 of the guide; already installed for the development iPhone).
4. Android: USB debugging on and plug in (tap *Allow*); see the **Devices** page, *Android* tab. Wi-Fi
   (Wireless debugging) is there too, as a last resort. The first time, *Install Android tools* downloads
   Google's adb (or run `.venv\Scripts\python -m mirrordesk.androidtools install`).
5. Other devices: **Viewers** → copy the invitation link (or let them scan its QR code): it signs them in to
   watch. Your own other devices use the owner code (Settings → *Access*) for full control.

`Start-MirrorDesk-Console.cmd` runs the same server with a visible console window (Ctrl+C stops).
If a MirrorDesk from another folder (e.g. Prototype 6) or an older copy is still running, the launcher
offers to close it first.

UxPlay is expected at `C:\msys64\home\<your user>\mirrordesk-uxplay\build\uxplay.exe` with MSYS2 at
`C:\msys64`, or as a self-contained folder `uxplay\` next to this README (what the tester package ships).
For other paths, `set MIRRORDESK_UXPLAY=...` / `set MIRRORDESK_MSYS2=...` and start with the console launcher.

## macOS and Linux (native)

Build the packages (`tools/make_tester_package.py`, guide section 11), then on the target computer:

```sh
sh ~/Downloads/MirrorDesk/install.sh                 # macOS (from the -macOS.zip): MirrorDesk.app
sh MirrorDesk/install.sh                             # Linux (from the -Linux.zip) desktop: app-menu entry; no desktop: systemd service
scp -r MirrorDesk USER@SERVER:/tmp/ && ssh -t USER@SERVER sh /tmp/MirrorDesk/install.sh   # a Linux server, from another computer
```

The installers build UxPlay 1.74 from `third_party/uxplay`; guide section 12 lists what goes where.
Linux desktop: *MirrorDesk* and *Quit MirrorDesk* in the app menu, and `mirrordesk start | stop | restart |
status | log` in a terminal. macOS: MirrorDesk in Applications (its Dock icon quits it).
`packaging/common/INSTALL-GUIDE.html` is the step-by-step guide for testers (every OS and method).

## Linux / Docker from the source (ZimaOS, Ubuntu, Debian, Raspberry Pi OS)

```sh
docker compose up -d --build
docker compose logs mirrordesk    # owner and viewer codes + URLs
```

**Any Linux host or Dockge:** `sh MirrorDesk/install.sh --docker` (from the Linux zip) builds the image and creates the
universal stack `docker/compose.yaml` (in Dockge when it runs there).
**ZimaOS:** `docker compose -f docker-compose.zimaos.yml up -d --build` (data in `/DATA/AppData/mirrordesk`),
or `sh docker/zimaos-install.sh` without the compose command; with the Dockge stack from
`docker/dockge/compose.yaml`, the script builds the image under the stack's image name and restarts the stack.
See section 2 of the guide.

## What is in this folder

| Path | Purpose |
|---|---|
| `mirrordesk/` | The Prototype 7 server, media hub, WebRTC, security, desktop/tray integration and web UI (`mirrordesk/web/`) |
| `mirrordesk/quicktime.py`, `mirrordesk/usbsource.py`, `mirrordesk/lockdown.py` | USB cable input: the iPhone's capture protocol, the libusb device handling, and pairing ("Trust This Computer") |
| `mirrordesk/usbdriver.py` | Windows: one-click install/removal of the libusb-win32 driver for a plugged-in iPhone (runs `wdi-simple` elevated) |
| `mirrordesk/androidsource.py`, `mirrordesk/androidtools.py`, `third_party/scrcpy/` | Android input: adb + scrcpy-server 5.0.1 (USB / Wireless debugging, phone control), Google adb download |
| `mirrordesk/winsetup.py`, `packaging/windows/` | Windows package: setup/uninstall steps (firewall rules, shortcuts), `setup.ps1` |
| `mirrordesk/unixsetup.py`, `packaging/macos/`, `packaging/linux/`, `packaging/unix/` | macOS/Linux packages: installers (`install.sh`), MirrorDesk.app / app-menu entry, the Linux package's README |
| `packaging/common/` | `INSTALL-GUIDE.html`, `TESTER-GUIDE.html`, `NOTICE.txt` (shipped in every package) |
| `third_party/uxplay/` | UxPlay 1.74 source (commit `d29dfab`, SHA-256 pinned): built by the macOS/Linux installers and the Docker image |
| `tools/build_uxplay_bundle.py`, `tools/build_wdi_simple.py`, `tools/make_tester_package.py` | Build the self-contained UxPlay, the cable-driver tool and the tester packages (guide section 11). `tools/platform-tools/` is the downloaded adb (not in the repository). |
| `Start-MirrorDesk.cmd`, `MirrorDesk.pyw` | Background start (tray icon, single instance, start with Windows) |
| `Start-MirrorDesk-Console.cmd` | Same server with a console window |
| `Setup-MirrorDesk.cmd` | Creates `.venv` from `pyproject.toml` + `uv.lock` |
| `launcher.py`, `backend.py`, `diagnostics.py`, `discovery_probe.py`, `discovery_selftest.py`, `ble_discovery.py`, `third_party/` | Shared modules from Prototype 6 that Prototype 7 still uses (UCRT64 receiver preflight, diagnostics sessions, the network discovery check). They are never started on their own. |
| `Dockerfile`, `docker-compose*.yml`, `docker/`, `requirements-docker.txt` | Linux container (UxPlay built from source, Debian adb); `docker/compose.yaml` universal Compose/Dockge stack and `docker/dockge/compose.yaml` the ZimaOS Dockge stack (both use the image published on GitHub); `docker-compose.zimaos.yml` + `docker/zimaos-install.sh` build it on a ZimaOS box |
| `.github/workflows/image.yml` | Builds the image for x86-64 and ARM64 and publishes it as `ghcr.io/brother-ben/mirrordesk:<version>` when a version tag (`v0.13.0`) is pushed |
| `tests/` | Prototype 7 tests: `test_p7_*.py`, Playwright browser tests in `tests/e2e/`, iPhone USB packets in `tests/fixtures/quicktime/` (from quicktime_video_hack, MIT) |
| `.runtime/` | Settings, access code, receiver key, USB pairing records (`usb-pairing/`), logs (never share it; not in the repository) |

## Tests

```bat
.venv\Scripts\python -m pytest -q tests
.venv\Scripts\python -m playwright install chromium firefox webkit   :: once
.venv\Scripts\python -m pytest -m e2e tests\e2e
```

## License

Copyright (c) 2026 the MirrorDesk author. All rights reserved: no license is granted to use, copy, change
or redistribute MirrorDesk's own code beyond viewing it here.

Third-party components keep their own licenses: [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and
`licenses/`. UxPlay, the AirPlay receiver (`third_party/uxplay`, including MirrorDesk's change to it), is free
software under the GNU GPL version 3 or later.
