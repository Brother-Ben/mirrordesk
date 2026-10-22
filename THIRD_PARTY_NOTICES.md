# Third-party notices and reference provenance

This document identifies inspected third-party material and the limits of its provenance. It does not assign a license to MirrorDesk Web's original code, replace upstream license texts, or constitute a complete binary-distribution license audit.

## macOS, Linux and Docker packages (0.9.0, 2026-10-16)

- **UxPlay source** `third_party/uxplay/UxPlay-d29dfabe102aba137170121077f55390e875310c.tar.gz`: the unmodified
  GitHub archive of commit `d29dfabe102aba137170121077f55390e875310c` (SHA-256
  `d68b043bb7eaf141b506199f0c3aa4b8dc261b4f063cf8ccc9697f22b8539ce2`, `third_party/uxplay/SOURCE.txt`),
  GPL-3.0-or-later. It is shipped in **every** tester package (Windows included), so each package carries the
  complete corresponding source of its UxPlay. The macOS and Linux installers build UxPlay from it on the
  tester's computer (no UxPlay binary is shipped for those systems), and the Dockerfile builds from it
  instead of fetching the commit with git.
- Installed on the tester's computer by the installers, **not redistributed**: Homebrew and its formulae
  (macOS: cmake, pkgconf, libplist, openssl@3, gstreamer, coreutils), the Linux distribution's packages
  (GStreamer, libplist, OpenSSL, build tools, libusb, adb on ARM), uv 0.12.12 (checksum pinned per
  platform), CPython 3.13 and the locked wheels, and Google's Platform-Tools on x86-64 Linux and macOS.
- MirrorDesk's own code is not licensed to testers for redistribution (`packaging/common/NOTICE.txt`); this
  does not restrict any third-party component's license.

## Windows tester package (0.8.0, 2026-10-16)

`tools/make_tester_package.py` builds `MirrorDesk-Tester-<version>.zip`. Unlike the earlier
prototype ZIPs it **does redistribute binaries**:

- **UxPlay** (`uxplay/bin/uxplay.exe`, GPL-3.0-or-later), built by `tools/build_uxplay_bundle.py`
  from <https://github.com/FDH2/UxPlay> commit `d29dfabe102aba137170121077f55390e875310c`
  (`v1.73.6-52-gd29dfab`), **unmodified source**, MSYS2 UCRT64 gcc, Release, `USE_MDNS=ON`,
  `USE_DNS_SD=OFF`, only `-ffile-prefix-map` added (removes local paths). That commit is the
  corresponding source; `uxplay/SOURCES.txt` states it and `uxplay/licenses/UxPlay-LICENSE.txt` is the GPL.
- **GStreamer, GLib, FFmpeg and their dependencies** (`uxplay/bin/*.dll`, `uxplay/lib/gstreamer-1.0/`):
  unmodified MSYS2 UCRT64 package binaries (LGPL/GPL and others). `uxplay/BUNDLE.json` lists every
  file with its SHA-256 and owning MSYS2 package + version; `uxplay/licenses/` holds each package's
  license files; sources are at <https://github.com/msys2/MINGW-packages> (recipes) and
  <https://repo.msys2.org/mingw/sources/> (source tarballs per version). MSYS2's FFmpeg is built with
  GPL components (e.g. x264/x265), so the bundle as a whole is distributed under the GPL.
- **wdi-simple** (`tools/usb-driver/wdi-simple.exe`): libwdi <https://github.com/pbatard/libwdi> tag
  `v1.5.1` (commit `9b23b82a2dd1cbffc16d46c212f92c6bf8c0c602`, unmodified; library LGPL-3.0, the
  wdi-simple sample GPL-3.0), built by `tools/build_wdi_simple.py`, embedding the **libusb-win32 1.2.6.0**
  driver (`libusb0.sys`/`libusb0.dll`, signed by its author; LGPL/GPL, see
  `tools/usb-driver/licenses/`) from <https://sourceforge.net/projects/libusb-win32/>. MirrorDesk runs
  it only when the user clicks "Install cable driver"; it installs the driver for that one iPhone.
- **scrcpy-server** `third_party/scrcpy/scrcpy-server-v5.0.1` (Apache-2.0, Copyright (C) 2018
  Genymobile, (C) 2018-2026 Romain Vimont; <https://github.com/Genymobile/scrcpy>), unmodified release
  file (SHA-256 `764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536`), license in
  `licenses/scrcpy-LICENSE.txt`. It is copied to the Android phone for each session and runs there.

Downloaded on the tester's PC by setup, **not redistributed**: uv 0.12.12 (Apache-2.0/MIT, checksum
pinned), CPython 3.13 (PSF) and the locked Python wheels (`uv.lock`), Google's **Android SDK
Platform-Tools 37.0.1** (adb; Android Software Development Kit License; SHA-256 pinned in
`mirrordesk/androidtools.py`), and on first use nothing else.

New Python dependency: **segno** `1.6.6` (BSD-3-Clause; draws the Wi-Fi pairing QR code).

## Package boundary (Prototype 6 ZIPs)

The Windows prototype ZIP does **not** redistribute a UxPlay executable, MSYS2/GStreamer DLLs, Python wheels, FFmpeg, or other dependency binaries. It contains application source/configuration, the two upstream BLE beacon Python sources identified below, and license texts; setup obtains pinned Python dependencies locally with the user's trusted `uv`. Other fetched upstream source/research files remain development references and are excluded from the Windows package.

## UxPlay

Upstream: <https://github.com/FDH2/UxPlay>

Fetched reference material retained in the development workspace:

- [`references/UxPlay-README.md`](references/UxPlay-README.md) — upstream README; its heading identifies **UxPlay 1.74 (Experimental)**.
- [`references/uxplay.cpp`](references/uxplay.cpp) — upstream main source.
- [`references/audio_renderer.c`](references/audio_renderer.c) — upstream audio renderer, including the L16 RTP forwarding path.
- [`references/video_renderer.c`](references/video_renderer.c) — upstream video renderer.
- [`references/uxplay-tree.json`](references/uxplay-tree.json) — GitHub tree response identifying repository `FDH2/UxPlay` and tree SHA **`d29dfabe102aba137170121077f55390e875310c`**, with per-path blob identifiers. Recorded API URL: <https://api.github.com/repos/FDH2/UxPlay/git/trees/d29dfabe102aba137170121077f55390e875310c>.
- [`licenses/UxPlay-LICENSE.txt`](licenses/UxPlay-LICENSE.txt) in the Windows prototype package — **GNU General Public License, version 3, 29 June 2007**, full text copied from the inspected reference.

The inspected C/C++ file headers state **GPL version 3 or, at your option, any later version** and retain their original copyright notices. These include Florian Draschbacher (2019) and F. Duncanh (2021–23 in the main/audio sources and 2021–24 in the video renderer). Consult each original header and the full fetched license for the exact notices and terms; preserve them when redistributing the reference files.

**Provenance limit:** the saved metadata pins a Git **tree**, not a separately verified commit or release tag. No claim is made here that the fetched files were independently hash-verified against every recorded blob, or that the user's existing Windows executable was built from this tree. A local hash-comparison attempt during documentation review required execution approval and did not run. Obtain and record the executable's actual build provenance before distributing or qualifying it. The launcher probes the separately installed executable's supported options; it does not build these reference sources.

UxPlay is an external runtime dependency; its fetched source excerpts are still third-party material even though this project does not compile them. Any redistribution of UxPlay binaries requires a review of the applicable GPL source and notice obligations; these few reference files are not a complete corresponding-source offer.

## Prototype 7 additions (2026-10-14)

- **Docker image (`Dockerfile`) builds and contains a UxPlay binary** compiled from the upstream commit
  `d29dfabe102aba137170121077f55390e875310c` (UxPlay 1.74 development, the same source as the Windows
  build; <https://github.com/FDH2/UxPlay/tree/d29dfabe102aba137170121077f55390e875310c>, GPL-3.0-or-later;
  recorded in the image as `/app/UXPLAY-SOURCE.txt`) plus Debian packages
  (GStreamer, Avahi, libplist, OpenSSL) under their own licenses. **Publishing or handing out that image
  is a binary distribution:** it must ship or offer the corresponding UxPlay source for that exact commit and
  keep `licenses/UxPlay-LICENSE.txt` (copied into the image). Building it locally for personal use is not
  distribution. Debian package sources are available from Debian per its archive policy.
- The image installs the same hash-locked Python wheels as `uv.lock` (`requirements-docker.txt`, exported
  with `uv export --no-dev`), including aiortc (BSD-3-Clause, `licenses/aiortc-LICENSE.txt`) and PyAV
  (BSD-3-Clause; its wheels bundle FFmpeg under LGPL/GPL terms — review before redistributing the image).
- **Playwright** (`playwright==1.64.0`, Apache-2.0) is a development/test dependency only; its browser
  builds are downloaded to the developer's machine and are never packaged.
- Apart from the USB cable input below, no third-party source is vendored by Prototype 7; the rest of
  `mirrordesk/` and its web UI are original code.
- **Background mode (Windows only, `sys_platform == 'win32'` markers, so not installed in Docker):**
  `pystray==0.19.5` (notification-area icon; LGPL-3.0 per its metadata, `COPYING`/`COPYING.LGPL` in the
  wheel), its dependency `six==1.17.0` (MIT) and `pillow==12.3.0` (MIT-CMU, draws the icon). They are
  downloaded by setup into `.venv`, imported unmodified, and not redistributed by this project; a future
  bundled Windows package must ship their license files (and keep pystray replaceable, per the LGPL).

## USB cable input (Prototype 7, 2026-10-14)

- **quicktime_video_hack** — <https://github.com/danielpaulus/quicktime_video_hack> (branch `main`,
  fetched 2026-10-14), **MIT License**, Copyright (c) 2019 danielpaulus. Full text:
  [`licenses/quicktime_video_hack-LICENSE.txt`](licenses/quicktime_video_hack-LICENSE.txt).
  - `mirrordesk/quicktime.py` is a Python port of its reverse-engineered protocol (packet layouts,
    handshake order, the device-info dictionaries and clock-reference offsets). It is a derived work of
    that MIT code; keep the copyright notice and license when redistributing.
  - `tests/fixtures/quicktime/` contains 25 of its captured packet fixtures, copied **unmodified**, with
    the license alongside (`LICENSE-quicktime_video_hack.txt`). They are test data only.
  - The separate Windows port (chotgpt/quicktime_video_hack_windows) was read for its device handling;
    **none of its code or binaries is used**. Three behaviours were learned from it and reimplemented:
    the computer sends PING (plus vendor request 0x40/0x40) when the phone is silent, CLEAR_FEATURE is
    not sent, and a stuck capture function needs the phone to restart.
- **go-ios** — <https://github.com/danielpaulus/go-ios> (branch `main`, fetched 2026-10-14), **MIT
  License**, Copyright (c) 2019 danielpaulus. Full text: [`licenses/go-ios-LICENSE.txt`](licenses/go-ios-LICENSE.txt).
  `mirrordesk/lockdown.py` follows its pairing request, pair-record fields, certificate layout and
  StartSession/TLS flow (rewritten in Python; no Go code copied). The usbmux-over-USB framing is
  implemented from the protocol's published behaviour; libimobiledevice's usbmuxd (GPL) was not used.
- **cryptography** (Apache-2.0 / BSD) — already installed as a dependency of aiortc; used to create the
  pairing certificates.
- **pyusb** `pyusb==1.3.1` — BSD 3-Clause (`LICENSE` in its wheel). Installed by setup into `.venv`
  and in the Docker image via `requirements-docker.txt`; not redistributed.
- **libusb-win32 driver** (`libusb0.sys`/`libusb0.dll`, LGPL) — on the development PC installed with
  Zadig; since 0.8.0 the tester package can install the same 1.2.6.0 driver itself (wdi-simple, see the
  tester-package section). MirrorDesk then loads the `libusb0.dll` the driver installed.
- **libusb-1.0** — Debian package `libusb-1.0-0` (LGPL-2.1-or-later) added to the Docker runtime image.

"iPhone", "iPad", "QuickTime" and "AirPlay" identify the interoperability targets only; no Apple
software is included or endorsed.

## UxPlay AirPlay Bluetooth LE beacon (Prototype 6)

The package vendors these upstream sources **unmodified**, with their copyright and
SPDX `LGPL-2.1-or-later` notices preserved, from the user-approved handoff for UxPlay
revision `d29dfabe102aba137170121077f55390e875310c`:

- `third_party/uxplay_beacon/uxplay-beacon.py` — (c) F. Duncanh, October 2025;
  SHA-256 `ccb2f00316148d091f2921c0ac07547e2c455c43cb0d2daf851437aced2e9d68`.
- `third_party/uxplay_beacon/uxplay_beacon_module_winrt.py` — (c) F. Duncanh, March 2026;
  SHA-256 `79e0e84e3994cf209345dbd7fa71971c67689873bc1f18816a773be65ea1a9ca`.

Hashes were verified against the handoff's `SHA256SUMS`; that confirms the approved
handoff bytes, not independent build provenance for the installed receiver. Upstream:
<https://github.com/FDH2/UxPlay/tree/d29dfabe102aba137170121077f55390e875310c>.
Complete LGPL 2.1 text: `licenses/UxPlay-beacon-LGPL-2.1.txt`, copied verbatim from
Debian's `/usr/share/common-licenses/LGPL-2.1`; authoritative reference:
<https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html>.
The full corresponding Python sources are supplied in the ZIP; users may modify or
replace them under their license. No LGPL/GPL election or upstream notice was changed.
This separately launched optional helper is not a new pairing or authentication method.

`requirements-ble.txt` adds six hash-pinned Windows CPython 3.13 x64 wheels for psutil
and WinRT projections. Setup obtains them into `.venv` with no dependency resolution;
`typing_extensions` is already in `uv.lock`. No wheels are redistributed. These source
notices do not constitute a complete license inventory for the downloaded packages.

**Standalone Prototype 7 folder:** the two beacon sources are kept because the shared
`launcher.py` module imports `ble_discovery.py`, but Prototype 7 never starts the beacon.
`requirements-ble.txt` and the BLE wheels are not part of this folder, and
`Setup-MirrorDesk.cmd` does not install them.

## aiortc

Upstream: <https://github.com/aiortc/aiortc>

The project directly pins `aiortc==1.14.0` in [`pyproject.toml`](pyproject.toml). Fetched references are [`references/aiortc-media.py`](references/aiortc-media.py) and [`references/aiortc-webcam.py`](references/aiortc-webcam.py). Their exact fetched revision is not established by the inspected files; do not assume it matches the runtime pin.

The package's [`licenses/aiortc-LICENSE.txt`](licenses/aiortc-LICENSE.txt) contains the **BSD 3-Clause** terms copied from the inspected reference and begins:

> Copyright (c) Jeremy Lainé.  
> All rights reserved.

Retain that full license, including its source/binary redistribution conditions, non-endorsement condition, and warranty/liability disclaimer. The link above points to the complete local text, not an invented replacement license.

## MediaMTX research reference

[`references/mediamtx-publish.html`](references/mediamtx-publish.html) is saved research material concerning publishing media. MediaMTX is **not a runtime dependency of this implementation**. No separate license for that downloaded HTML was established during this review, and no MediaMTX code license is asserted to cover it. Check the original documentation's applicable terms before republishing the saved page.

## Other installed/external dependencies

Direct Python dependency pins in `pyproject.toml` also include `aiohttp==3.13.3`, `av==16.1.0` (PyAV), and `numpy==2.3.5`; development dependencies include pytest and pytest-asyncio. [`uv.lock`](uv.lock) records the resolved dependency graph. aiortc/PyAV and their wheels have further dependencies and potentially bundled native components. Windows reception separately relies on the user's MSYS2/UCRT64 and GStreamer installation; the Linux generated-media smoke uses an external FFmpeg executable. uv is a setup tool.

No license labels for these components are invented here. Review their actual installed distributions' LICENSE/COPYING/NOTICE files, upstream source, wheel contents, and the exact FFmpeg/GStreamer build configuration before distributing an environment or executable bundle. The two fetched licenses above are **not** a complete notice inventory for the dependency graph. Setup downloads dependencies; it does not turn their licenses into this project's license.

The generated test pattern and sine wave described in `media-result.json` are fixture output, not captured iPhone content and not an Apple endorsement. Product names are used only to identify interoperability targets and dependencies.
