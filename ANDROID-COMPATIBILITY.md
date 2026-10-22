# Android and MirrorDesk: where things stand

*Research done 2026-10-20, written for MirrorDesk 0.11.1. Facts about Android, Chrome and Google's
rules change; re-check the sources below before acting on an old copy of this file.*

## The problem in one paragraph

An iPhone connects to MirrorDesk with nothing installed: AirPlay is built into iOS, and the
iPhone's own Screen Mirroring button finds MirrorDesk. Android has no equivalent. Today an
Android phone can only connect through **USB debugging** (Developer options, a cable, Google's
`adb` tool and the scrcpy server) or **Wireless debugging** (the same over Wi-Fi). Both need
Developer options, both hand the computer shell-level control of the phone while they are on,
and testers found them hard to set up and worrying to leave on.

## Every way an Android phone could connect

| Option | Nothing to install? | Verdict | Why |
|---|---|---|---|
| Google Cast (the Cast button) | yes | **Not possible** | Android only casts to receivers that hold a device certificate Google issues to approved device makers. The only open-source receivers (e.g. Shanocast) use keys copied out of another company's app: not legal to ship, and Google can block them. |
| Share from the phone's browser | yes | **Not possible** | Screen sharing for web pages (`getDisplayMedia`) is still unsupported in Chrome 155, Firefox and Samsung Internet on Android. (Safari on iOS 27 added it; irrelevant for Android.) |
| Miracast ("Smart View" on Samsung, Xiaomi, ...) | yes | Windows-only extra, later | Pixels cannot send Miracast. Of MirrorDesk's systems, only Windows can receive it reliably (needs Wi-Fi Direct; Microsoft's `MiracastReceiver` API is built for Store apps). Nothing for Linux, macOS or the ZimaOS box. |
| USB debugging (today) | Developer options | Keep as "Advanced" | Works on every host, best picture, and the only way to tap and type on the phone from the browser. But the computer gets full `adb` control while it is on. |
| Wireless debugging (today) | Developer options | **Remove** | CVE-2026-0073 (May 2026 Android bulletin, Critical, Android 14-16): anyone on the same Wi-Fi could run commands on a phone with Wireless debugging on, with no tap needed. Patched only on phones that got the update. |
| Third-party streaming app (ScreenStream, MIT) | install an app | Stopgap only | No Developer options, RTSP with TLS and a password. But ads in the Play version, and people would type a server address and password into someone else's app. |
| HDMI capture dongle | phone with USB-C video out | Niche extra | No software at all; needs ~$20 of hardware and a phone with video out (Samsung flagships, Pixel 8 and newer). |
| **MirrorDesk app for Android** | install once | **Recommended** | See below. |

Commercial mirroring products (AirDroid Cast and others) solve Android the same way: their own
app on the phone.

## The recommendation: a small MirrorDesk app

**For the person with the phone:** install the app once, scan the QR code on MirrorDesk's
Devices page, tap **Start mirroring**, then Android's **Start now**. Next time: open the app, tap
Start.

**Why it is the safest option**

- No Developer options and no debugging: the phone's security stays exactly as it is.
- View-only: the app can only send the picture and sound. MirrorDesk cannot control the phone,
  read its files or install anything.
- Android enforces the rest itself: it asks every time mirroring starts (Android 14+), shows a
  status-bar chip that stops it with one tap (15 QPR1+), stops when the phone locks, hides
  notifications, one-time codes and password fields while sharing (15+), and lets people share
  one app instead of the whole screen (14+).
- Nothing on the phone to attack: the phone connects out to MirrorDesk; it never opens a port.
- Paired by QR code: the code carries a one-time pairing secret and MirrorDesk's encryption key,
  so nobody else on the Wi-Fi can watch or impersonate MirrorDesk. Like iPhones since 0.12.0, a
  pairing would last one session: when it ends, every phone is forgotten.

**Why it is the least likely to cause problems**

- It uses Android's standard screen-sharing permission (MediaProjection, since Android 5) and
  playback capture for the sound (Android 10+): the same APIs Zoom, Meet and Discord use.
- MirrorDesk already handles this exact kind of stream: the Android USB path receives H.264
  video plus raw sound from scrcpy (`mirrordesk/androidsource.py`). The app would send the same
  format over an encrypted connection, so the player, late joining and the sound fixes all
  carry over.
- It works with every MirrorDesk host, including the ZimaOS box (no USB passthrough, no adb).
- The encoder handling can be adapted from scrcpy (Apache-2.0, already bundled; fine for a paid
  product), which is proven on thousands of phones. RootEncoder (Apache-2.0) is an alternative.

**Limits to accept**

- View-only: people who want to tap and type keep the USB-cable method.
- Apps that block recording (Netflix, most paid streaming apps) show black with no sound, the same
  as AirPlay and USB mirroring today.
- Sound needs Android 10 or newer, and it keeps playing on the phone too (Android copies it).
- Android asks for permission every time mirroring starts (14+), and mirroring stops when the
  phone locks.

## Getting the app to people

Google now requires apps on certified Android phones to come from verified developers:

- Enforced since 2026-09-30 in Brazil, Indonesia, Singapore and Thailand; worldwide in 2027.
- A free **limited distribution** account covers up to 20 devices (no ID, no fee): enough for
  testers.
- Selling needs a full account: $25 and government ID (Google Play, or a registered download
  through the Android Developer Console).
- Until enforcement reaches a tester's country, a signed APK can be installed directly ("install
  unknown apps"); `adb install` stays exempt.

## What building it involves

1. **MirrorDesk side:** an encrypted app connection (TLS with the key pinned through the QR code),
   one-time pairing and a paired-phone list, and a new input that feeds the existing Android
   pipeline. The Devices -> Android tab would lead with "MirrorDesk app", keep "USB cable
   (advanced)", and drop Wireless debugging.
2. **The app (Kotlin):** QR scan (Google code scanner: no camera permission), MediaProjection +
   MediaCodec H.264, playback capture for sound, a foreground service with a Stop button,
   reconnect, and "MirrorDesk ended the session" handling.
3. **Testing:** most of it here with Android's emulator (it supports screen and sound capture), an
   automated stand-in for the app in MirrorDesk's own tests (like `tests/fake_adb.py`), then a
   real phone.

## Open decisions (the owner's)

- Build the app? (Recommended.)
- Create the Google developer account (only needed beyond 20 test phones, or to sell).
- Later, optional: Miracast on Windows for Samsung users who will not install anything.

## Sources

- getDisplayMedia support: <https://caniuse.com/mdn-api_mediadevices_getdisplaymedia>
- Cast device certificates: <https://chromium.googlesource.com/openscreen/+/HEAD/cast/docs/USING.md>,
  <https://yingtongli.me/blog/2019/12/20/gcast-auth.html>, <https://github.com/coder0107git/shanocast>
- CVE-2026-0073: <https://source.android.com/docs/security/bulletin/2026/2026-05-01>
- Developer verification: <https://developer.android.com/developer-verification>,
  <https://developer.android.com/developer-verification/guides/android-developer-console>
- MediaProjection: <https://developer.android.com/media/grow/media-projection>
- Playback capture: <https://developer.android.com/media/platform/av-capture>
- Android 15 screen-sharing protections: <https://guardsquare.com/blog/android-15-screen-spying-protection>
- MiracastReceiver: <https://learn.microsoft.com/en-us/uwp/api/windows.media.miracast.miracastreceiver>
- ScreenStream: <https://github.com/dkrivoruchko/ScreenStream>; RootEncoder: <https://github.com/pedroSG94/RootEncoder>
- scrcpy audio: <https://github.com/Genymobile/scrcpy/blob/master/doc/audio.md>
- AirDroid Cast: <https://www.airdroid.com/guide/cast/>
