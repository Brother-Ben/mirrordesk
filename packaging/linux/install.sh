#!/bin/sh
# MirrorDesk installer for Linux (x86-64 and ARM64).
#
#   sh MirrorDesk/install.sh [options]      (in the unzipped Linux package)
#
# How MirrorDesk is installed (chosen automatically when no option is given):
#   --desktop   for the signed-in user of a Linux desktop: ~/.local/share/mirrordesk,
#               MirrorDesk and Quit MirrorDesk in the app menu, the "mirrordesk" command,
#               start-at-login from the Settings page.
#   --server    a headless computer or a server you reach over SSH: /opt/mirrordesk,
#               runs all the time as the systemd service "mirrordesk" (user mirrordesk,
#               data in /var/lib/mirrordesk).
#   --docker    builds the Docker image and creates the "mirrordesk" stack (in Dockge when
#               Dockge runs on this computer; ZimaOS, CasaOS, Unraid, TrueNAS ...).
# Options:
#   --port N            web page port (default 8765)
#   --access-code CODE  fixed viewer (watch-only) code for other devices (6-32 letters/digits)
#   --usb               --docker: also allow phones plugged into this computer by USB
#   --yes               never ask, take the defaults
#   --uninstall         remove MirrorDesk (add --purge to delete its settings too;
#                       add --docker to remove the Docker stack's container)
#
# Native installs need apt (Debian, Ubuntu, Raspberry Pi OS 64-bit, Mint, Pop!_OS), dnf
# (Fedora) or pacman (Arch, Manjaro); openSUSE (zypper) is best effort. Downloads are
# checked against pinned checksums; UxPlay is built from the source included here.
set -eu

UV_VERSION=0.12.12
UV_SHA256_X86_64=ab9b309d4586403f024e100abaceb396616e178a553e2500c36087d180f09509
UV_SHA256_AARCH64=fe08db50cc1b56cd1da7801065ed1103d27ed3f9571cd122386cfc7faf1b8df5
UXPLAY_COMMIT=d29dfabe102aba137170121077f55390e875310c
UXPLAY_SHA256=d68b043bb7eaf141b506199f0c3aa4b8dc261b4f063cf8ccc9697f22b8539ce2
# That source plus MirrorDesk's change (third_party/uxplay/mirrordesk): a receiver built without
# it is built again.
UXPLAY_BUILD="$UXPLAY_COMMIT+mirrordesk.3"
GST_ELEMENTS="appsrc h264parse rtph264pay rtpL16pay udpsink audioconvert audioresample volume avdec_aac avdec_alac"
SERVER_APP=/opt/mirrordesk
SERVER_DATA=/var/lib/mirrordesk
SERVICE=mirrordesk
UDEV_RULE=/etc/udev/rules.d/70-mirrordesk.rules
USBMUXD_DROPIN=/etc/systemd/system/usbmuxd.service.d/50-mirrordesk.conf
UFW_APP=/etc/ufw/applications.d/mirrordesk
PRIVATE_NETS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10"
AIRPLAY_PORT=7100

PKG=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
VERSION=$(sed -n 's/^MirrorDesk \([0-9][0-9.]*\).*/\1/p' "$PKG/VERSION.txt" 2>/dev/null | head -n 1)
[ -n "$VERSION" ] || VERSION=unknown

MODE=""
NATIVE_ONLY=0
PORT=8765
ACCESS_CODE=""
USB=0
YES=0
UNINSTALL=0
PURGE=0

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {   # the comment block at the top of this file
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --desktop) MODE=desktop ;;
    --server) MODE=server ;;
    --docker) MODE=docker ;;
    --native) NATIVE_ONLY=1 ;;
    --port) [ $# -ge 2 ] || die "--port needs a number"; PORT=$2; shift ;;
    --port=*) PORT=${1#--port=} ;;
    --access-code) [ $# -ge 2 ] || die "--access-code needs a value"; ACCESS_CODE=$2; shift ;;
    --access-code=*) ACCESS_CODE=${1#--access-code=} ;;
    --usb) USB=1 ;;
    --yes|-y) YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    --help|-h) usage ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
{ [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ]; } || die "--port must be between 1024 and 65535"
if [ -n "$ACCESS_CODE" ]; then
  ACCESS_CODE=$(printf '%s' "$ACCESS_CODE" | tr '[:lower:]' '[:upper:]')
  printf '%s' "$ACCESS_CODE" | grep -Eq '^[A-Z0-9]{6,32}$' || die "--access-code must be 6-32 letters or digits"
fi

[ "$(uname -s)" = Linux ] || die "this installer is for Linux. macOS: use the macOS package; Windows: Setup-MirrorDesk.cmd."
case "$(uname -m)" in
  x86_64|amd64) ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) die "MirrorDesk needs a 64-bit Intel/AMD or ARM computer (found $(uname -m)). On a Raspberry Pi, use the 64-bit Raspberry Pi OS." ;;
esac

if [ "$(id -u)" -eq 0 ]; then SUDO=""; elif have sudo; then SUDO=sudo; else SUDO=""; fi
need_root() {
  [ "$(id -u)" -eq 0 ] || have sudo || die "this step needs administrator rights: install sudo, or run the installer as root."
}
as_root() { need_root; $SUDO "$@"; }

ask() {   # ask "question" default(y|n) -> status 0 for yes
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then
    [ "$2" = y ]
    return
  fi
  if [ "$2" = y ]; then printf '%s [Y/n] ' "$1"; else printf '%s [y/N] ' "$1"; fi
  read -r answer || answer=""
  case "$answer" in
    [Yy]*) return 0 ;;
    [Nn]*) return 1 ;;
    *) [ "$2" = y ] ;;
  esac
}

TMP=$(mktemp -d "${TMPDIR:-/tmp}/mirrordesk-install.XXXXXX")
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

sha256_of() {
  if have sha256sum; then sha256sum "$1" | cut -d' ' -f1
  elif have shasum; then shasum -a 256 "$1" | cut -d' ' -f1
  else die "sha256sum is missing (install coreutils)"
  fi
}

download() {   # download URL FILE
  if have curl; then curl -fsSL --retry 3 -o "$2" "$1"
  elif have wget; then wget -q -O "$2" "$1"
  else die "curl or wget is needed to download $1"
  fi
}

http_ok() {   # http_ok URL
  if have curl; then curl -fsS -m 2 "$1" >/dev/null 2>&1
  elif have wget; then wget -q -T 2 -O /dev/null "$1" 2>/dev/null
  else return 1
  fi
}

package_manager() {
  for pm in apt-get dnf pacman zypper; do
    if have "$pm"; then echo "$pm"; return; fi
  done
  echo ""
}

graphical_session() {
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]
}

# ------------------------------------------------------------------ choose how to install
choose_mode() {
  PM=$(package_manager)
  [ -n "$MODE" ] && return
  if [ -n "$PM" ]; then
    if [ "$(id -u)" -ne 0 ] && graphical_session; then
      MODE=desktop
    else
      MODE=server
      if [ -n "${SUDO_USER:-}" ] && graphical_session; then
        say "(Run with sudo: installing as a service. For the desktop app, run the installer without sudo.)"
      fi
    fi
  elif [ "$NATIVE_ONLY" = 0 ] && have docker; then
    MODE=docker
  else
    die "no supported package manager (apt, dnf, pacman) and no Docker found. Install Docker and run this again with --docker."
  fi
}

# ------------------------------------------------------------------ native: system packages
install_packages() {
  step "Installing the system packages MirrorDesk needs ($PM)"
  need_root
  case "$PM" in
    apt-get)
      as_root env DEBIAN_FRONTEND=noninteractive apt-get update
      plist=libplist-dev
      apt-cache show libplist-dev >/dev/null 2>&1 || plist=libplist-2.0-dev
      pkgs="build-essential cmake pkg-config libssl-dev $plist libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
            gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
            gstreamer1.0-libav libusb-1.0-0 curl ca-certificates tar coreutils"
      [ "$ARCH" = x86_64 ] || pkgs="$pkgs adb"
      # shellcheck disable=SC2086
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $pkgs
      ;;
    dnf)
      pkgs="gcc gcc-c++ make cmake pkgconf-pkg-config openssl-devel libplist-devel gstreamer1-devel
            gstreamer1-plugins-base-devel gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free
            libusb1 curl ca-certificates tar coreutils binutils"
      [ "$ARCH" = x86_64 ] || pkgs="$pkgs android-tools"
      # shellcheck disable=SC2086
      as_root dnf install -y $pkgs
      as_root dnf install -y gstreamer1-plugin-libav >/dev/null 2>&1 || as_root dnf install -y gstreamer1-libav >/dev/null 2>&1 || true
      ;;
    pacman)
      pkgs="base-devel cmake pkgconf openssl libplist gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad
            gst-libav libusb curl tar coreutils"
      [ "$ARCH" = x86_64 ] || pkgs="$pkgs android-tools"
      # shellcheck disable=SC2086
      as_root pacman -Syu --needed --noconfirm $pkgs
      ;;
    zypper)
      warn "openSUSE support is untested."
      pkgs="gcc-c++ make cmake pkg-config libopenssl-3-devel libplist-2_0-devel gstreamer-devel
            gstreamer-plugins-base-devel gstreamer-plugins-base gstreamer-plugins-good gstreamer-plugins-bad
            gstreamer-plugins-libav libusb-1_0-0 curl tar coreutils"
      [ "$ARCH" = x86_64 ] || pkgs="$pkgs android-tools"
      # shellcheck disable=SC2086
      as_root zypper --non-interactive install $pkgs
      ;;
  esac
}

check_gstreamer() {
  have gst-inspect-1.0 || { warn "gst-inspect-1.0 not found; skipping the GStreamer check."; return 0; }
  missing=""
  for element in $GST_ELEMENTS; do
    gst-inspect-1.0 "$element" >/dev/null 2>&1 || missing="$missing $element"
  done
  if [ -n "$missing" ]; then
    warn "GStreamer elements missing:$missing"
    case "$missing" in
      *avdec_aac*) warn "Without avdec_aac the iPhone's sound over AirPlay stays silent. Fedora: enable RPM Fusion, install gstreamer1-plugin-libav (or gstreamer1-libav), then run this installer again." ;;
    esac
  fi
}

# ------------------------------------------------------------------ native: files, UxPlay, Python
copy_app() {   # copy_app TARGET  (keeps .runtime, .venv, runtime, tools and uxplay from before)
  target=$1
  mkdir -p "$target"
  rm -rf "$target/mirrordesk" "$target/licenses" "$target/third_party"
  cp -R "$PKG/app/." "$target/"
  cp "$PKG/install.sh" "$target/mirrordesk-installer.sh"
  for doc in VERSION.txt INSTALL-GUIDE.html TESTER-GUIDE.html NOTICE.txt; do
    if [ -f "$PKG/$doc" ]; then cp "$PKG/$doc" "$target/$doc"; fi
  done
  chmod 0755 "$target/start-mirrordesk.sh" "$target/mirrordesk-installer.sh" 2>/dev/null || true
}

build_uxplay() {   # build_uxplay APP
  app=$1
  if [ -x "$app/uxplay/bin/uxplay" ] && [ "$(cat "$app/uxplay/COMMIT" 2>/dev/null)" = "$UXPLAY_BUILD" ]; then
    say "The AirPlay receiver (UxPlay 1.74) is already built."
    return 0
  fi
  step "Building the AirPlay receiver (UxPlay 1.74) from source (a few minutes)"
  tarball="$PKG/app/third_party/uxplay/UxPlay-$UXPLAY_COMMIT.tar.gz"
  [ -f "$tarball" ] || die "the UxPlay source is missing from this package ($tarball)."
  [ "$(sha256_of "$tarball")" = "$UXPLAY_SHA256" ] || die "the UxPlay source failed its checksum check. Download MirrorDesk again."
  # Built inside the install folder: /tmp is often mounted without permission to run programs.
  work="$app/uxplay/.build"
  rm -rf "$work"
  mkdir -p "$work/src"
  tar -xzf "$tarball" -C "$work/src" --strip-components=1
  # MirrorDesk's change: the sound carries on at once after a pause or seek (mirrordesk.patch).
  cp -R "$PKG/app/third_party/uxplay/mirrordesk/." "$work/src/"
  jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)
  [ "$jobs" -le 4 ] || jobs=4
  if ! cmake -S "$work/src" -B "$work/build" -DCMAKE_BUILD_TYPE=Release -DNO_X11_DEPS=ON \
      >"$work/cmake.log" 2>&1; then
    tail -n 40 "$work/cmake.log" >&2
    die "configuring UxPlay failed (messages above)."
  fi
  if ! cmake --build "$work/build" -j "$jobs" >"$work/build.log" 2>&1; then
    tail -n 40 "$work/build.log" >&2
    die "building UxPlay failed (messages above)."
  fi
  "$work/build/uxplay" -h 2>&1 | grep -q -- '-vrtp' || die "the UxPlay that was built does not work (no -vrtp)."
  mkdir -p "$app/uxplay/bin"
  cp "$work/build/uxplay" "$app/uxplay/bin/uxplay.new"
  rm -rf "$work"
  strip "$app/uxplay/bin/uxplay.new" 2>/dev/null || true
  chmod 0755 "$app/uxplay/bin/uxplay.new"
  mv -f "$app/uxplay/bin/uxplay.new" "$app/uxplay/bin/uxplay"
  printf '%s\n' "$UXPLAY_BUILD" >"$app/uxplay/COMMIT"
  printf 'UxPlay built on this computer from https://github.com/FDH2/UxPlay commit %s (GPL-3.0-or-later)\nwith MirrorDesk'"'"'s change to its audio and video handling.\nSource: third_party/uxplay/UxPlay-%s.tar.gz and third_party/uxplay/mirrordesk.patch; license: licenses/UxPlay-LICENSE.txt\n' \
    "$UXPLAY_COMMIT" "$UXPLAY_COMMIT" >"$app/uxplay/SOURCES.txt"
  say "Built $app/uxplay/bin/uxplay"
}

install_python() {   # install_python APP
  app=$1
  step "Python 3.13 and MirrorDesk's libraries (the first time downloads about 100 MB)"
  uv="$app/tools/uv/uv"
  if [ ! -x "$uv" ] || ! "$uv" --version 2>/dev/null | grep -q " $UV_VERSION"; then
    if [ "$ARCH" = x86_64 ]; then triple=x86_64-unknown-linux-gnu; sum=$UV_SHA256_X86_64
    else triple=aarch64-unknown-linux-gnu; sum=$UV_SHA256_AARCH64; fi
    download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$triple.tar.gz" "$TMP/uv.tar.gz"
    [ "$(sha256_of "$TMP/uv.tar.gz")" = "$sum" ] || die "the uv download failed its checksum check. Nothing was installed; try again later."
    mkdir -p "$TMP/uv" "$app/tools/uv"
    tar -xzf "$TMP/uv.tar.gz" -C "$TMP/uv" --strip-components=1
    cp "$TMP/uv/uv" "$uv"
    chmod 0755 "$uv"
  fi
  if ! UV_PYTHON_INSTALL_DIR="$app/runtime/python" UV_PYTHON_PREFERENCE=only-managed \
       UV_PROJECT_ENVIRONMENT="$app/.venv" UV_CACHE_DIR="$TMP/uv-cache" UV_NO_PROGRESS=1 \
       "$uv" sync --locked --no-dev --python 3.13 --project "$app"; then
    die "installing Python and its libraries failed (messages above). Check the internet connection and run the installer again."
  fi
  (cd "$app" && "$app/.venv/bin/python" -c "import mirrordesk, aiortc, av") || die "the Python environment does not work."
}

install_adb() {   # install_adb APP  (Google's adb on x86-64; ARM uses the system package)
  app=$1
  [ "$ARCH" = x86_64 ] || return 0
  step "Android tools (Google adb)"
  (cd "$app" && "$app/.venv/bin/python" -m mirrordesk.androidtools install --if-missing) \
    || warn "the Android tools could not be downloaded now; use \"Install Android tools\" on the Devices page later."
}

new_settings() {   # fresh installs: fixed AirPlay ports, so firewalls can allow them
  printf '{\n  "airplay_ports": "%s"\n}\n' "$AIRPLAY_PORT"
}

# ------------------------------------------------------------------ native: USB access and firewall
install_udev() {   # install_udev desktop|server
  [ -d /etc/udev ] || return 0
  step "USB access for phones (iPhone cable, Android USB debugging)"
  if [ "$1" = server ]; then grant='MODE="0660", GROUP="mirrordesk"'; else grant='TAG+="uaccess"'; fi
  cat >"$TMP/udev.rules" <<EOF
# MirrorDesk: phones on USB. Apple iPhone/iPad (cable mirroring) and any Android phone
# with USB debugging on (its ADB interface: class ff, subclass 42, protocol 01).
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="05ac", ATTR{idProduct}=="12[9a]?", $grant
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ENV{ID_USB_INTERFACES}=="*:ff4201:*", $grant
EOF
  as_root mkdir -p "$(dirname "$UDEV_RULE")"
  as_root cp "$TMP/udev.rules" "$UDEV_RULE"
  as_root chmod 0644 "$UDEV_RULE"
  as_root udevadm control --reload-rules >/dev/null 2>&1 || true
  as_root udevadm trigger --subsystem-match=usb --action=change >/dev/null 2>&1 || true
  say "Added $UDEV_RULE (unplug and plug in a phone that is already connected)."
}

usbmuxd_unit() {   # usbmuxd (Linux's iPhone service) is installed as a systemd service
  [ -d /run/systemd/system ] || return 1
  for dir in /etc/systemd/system /usr/lib/systemd/system /lib/systemd/system; do
    [ -f "$dir/usbmuxd.service" ] && return 0
  done
  return 1
}

usbmuxd_share() {   # iPhone by cable while usbmuxd runs (MirrorDesk shares the phone with it)
  usbmuxd_unit || return 0
  [ -f "$USBMUXD_DROPIN" ] && return 0
  step "iPhone by cable: letting usbmuxd share iPhones with MirrorDesk"
  say "usbmuxd (Linux's iPhone service) switches a plugged-in iPhone out of the USB mode that cable"
  say "mirroring needs. It can keep iPhones in that mode instead. Then an iPhone's internet sharing"
  say "by USB cable (Personal Hotspot) does not work on this computer; Wi-Fi hotspot still does."
  if ask "Let usbmuxd share iPhones with MirrorDesk?" y; then
    printf '[Service]\n# MirrorDesk: keep iPhones in the USB mode that allows cable mirroring (screen capture).\nEnvironment=USBMUXD_DEFAULT_DEVICE_MODE=2\n' \
      >"$TMP/usbmuxd.conf"
    as_root mkdir -p "$(dirname "$USBMUXD_DROPIN")"
    as_root cp "$TMP/usbmuxd.conf" "$USBMUXD_DROPIN"
    as_root chmod 0644 "$USBMUXD_DROPIN"
    as_root systemctl daemon-reload >/dev/null 2>&1 || true
    as_root systemctl try-restart usbmuxd >/dev/null 2>&1 || true
    say "Added $USBMUXD_DROPIN (unplug an iPhone that is plugged in and plug it in again)."
  else
    say "Skipped: iPhone by cable will not work while usbmuxd runs. Run the installer again to change that."
  fi
}

usbmuxd_unshare() {
  [ -f "$USBMUXD_DROPIN" ] || return 0
  as_root rm -f "$USBMUXD_DROPIN"
  as_root rmdir "$(dirname "$USBMUXD_DROPIN")" 2>/dev/null || true
  as_root systemctl daemon-reload >/dev/null 2>&1 || true
  as_root systemctl try-restart usbmuxd >/dev/null 2>&1 || true
}

firewall_open() {
  if have ufw && as_root ufw status 2>/dev/null | grep -q "Status: active"; then
    step "Firewall (ufw): allowing MirrorDesk from home and Tailscale networks"
    cat >"$TMP/ufw-app" <<EOF
[MirrorDesk]
title=MirrorDesk
description=Phone mirroring to browsers: web page, AirPlay, mDNS, WebRTC video
ports=$PORT/tcp|$AIRPLAY_PORT:$((AIRPLAY_PORT + 2))/tcp|$AIRPLAY_PORT:$((AIRPLAY_PORT + 2))/udp|5353/udp|32768:60999/udp
EOF
    as_root cp "$TMP/ufw-app" "$UFW_APP"
    as_root chmod 0644 "$UFW_APP"
    for net in $PRIVATE_NETS; do as_root ufw allow from "$net" to any app MirrorDesk >/dev/null; done
    say "ufw: MirrorDesk allowed from $PRIVATE_NETS"
  elif have firewall-cmd && as_root firewall-cmd --state >/dev/null 2>&1; then
    step "Firewall (firewalld): allowing MirrorDesk"
    as_root firewall-cmd --permanent --new-service=mirrordesk >/dev/null 2>&1 || true
    for port in "$PORT/tcp" "$AIRPLAY_PORT-$((AIRPLAY_PORT + 2))/tcp" "$AIRPLAY_PORT-$((AIRPLAY_PORT + 2))/udp" \
                5353/udp 32768-60999/udp; do
      as_root firewall-cmd --permanent --service=mirrordesk --add-port="$port" >/dev/null
    done
    as_root firewall-cmd --permanent --add-service=mirrordesk >/dev/null
    as_root firewall-cmd --reload >/dev/null
    say "firewalld: service \"mirrordesk\" added to the default zone"
  fi
}

firewall_close() {
  if [ -f "$UFW_APP" ] && have ufw; then
    for net in $PRIVATE_NETS; do as_root ufw delete allow from "$net" to any app MirrorDesk >/dev/null 2>&1 || true; done
    as_root rm -f "$UFW_APP"
  fi
  if have firewall-cmd && as_root firewall-cmd --state >/dev/null 2>&1; then
    as_root firewall-cmd --permanent --remove-service=mirrordesk >/dev/null 2>&1 || true
    as_root firewall-cmd --permanent --delete-service=mirrordesk >/dev/null 2>&1 || true
    as_root firewall-cmd --reload >/dev/null 2>&1 || true
  fi
}

# ------------------------------------------------------------------ reporting
lan_addresses() {   # IPv4 addresses of real network adapters (not Docker's or VMs' bridges)
  if have ip; then
    ip -4 -o addr show scope global 2>/dev/null \
      | awk '$2 !~ /^(docker|br-|veth|virbr|lxc|cni|flannel|vmnet)/ {split($4, a, "/"); print a[1]}'
  else
    hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -v '^172\.17\.' || true
  fi
}

wait_health() {   # wait_health SECONDS
  i=0
  while [ "$i" -lt "$1" ]; do
    http_ok "http://127.0.0.1:$PORT/api/health" && return 0
    sleep 1
    i=$((i + 1))
  done
  return 1
}

quit_desktop() {   # quit_desktop APP  (the MirrorDesk this user runs; --quit sends its quit token)
  if [ -x "$1/.venv/bin/python" ]; then
    (cd "$1" && "$1/.venv/bin/python" -m mirrordesk --quit --port "$PORT" >/dev/null 2>&1) || true
  fi
  if wait_health 1; then   # a copy whose Python no longer works: ask without the token
    curl -fsS -m 5 -X POST -H 'X-MirrorDesk: 1' "http://127.0.0.1:$PORT/api/desktop/quit" >/dev/null 2>&1 || true
    i=0
    while [ "$i" -lt 20 ] && wait_health 1; do sleep 1; i=$((i + 1)); done
  fi
}

# The addresses MirrorDesk's own Viewers page lists (needs its Python; else `ip`).
URLS_PY="import sys; from mirrordesk import netinfo; print('\\n'.join(u['url'] for u in netinfo.viewer_urls(netinfo.list_interfaces(), int(sys.argv[1]))))"

print_addresses() {   # print_addresses ACCESS_CODE [APP | docker]
  urls=""
  if [ "${2:-}" = docker ]; then
    urls=$(dk exec -w /app mirrordesk python -c "$URLS_PY" "$PORT" 2>/dev/null || true)
  elif [ -n "${2:-}" ] && [ -x "$2/.venv/bin/python" ]; then
    urls=$(cd "$2" && "$2/.venv/bin/python" -c "$URLS_PY" "$PORT" 2>/dev/null) || urls=""
  fi
  if [ -z "$urls" ]; then
    for address in $(lan_addresses); do urls="$urls http://$address:$PORT/"; done
  fi
  [ -n "$urls" ] || urls="http://<this-computer's-address>:$PORT/"
  say ""
  say "Open MirrorDesk in a browser on any device of this network:"
  for url in $urls; do say "    $url"; done
  if [ -n "$1" ]; then say "Owner code (full control): $1"; say "People who should only watch get the viewer code from the Viewers page."; fi
}

read_code() {   # read_code SETTINGS_FILE (the owner code)
  as_root sed -n 's/.*"owner_code": *"\([A-Z0-9]*\)".*/\1/p' "$1" 2>/dev/null | head -n 1
}

# ------------------------------------------------------------------ desktop install
install_desktop() {
  [ "$(id -u)" -ne 0 ] || die "--desktop installs for your own account: run it without sudo (it asks for the password when needed)."
  APP="${XDG_DATA_HOME:-$HOME/.local/share}/mirrordesk"
  say "MirrorDesk $VERSION for $(id -un) on this desktop, into $APP"
  say "(The administrator password is asked for the system packages, USB access and the firewall.)"
  install_packages
  check_gstreamer
  step "Copying MirrorDesk"
  if wait_health 1; then   # before its files are replaced: a running copy still loads parts of its code
    say "Closing the MirrorDesk that is running, so the new version can replace it..."
    quit_desktop "$APP"
  fi
  copy_app "$APP"
  build_uxplay "$APP"
  install_python "$APP"
  install_adb "$APP"
  if [ ! -f "$APP/.runtime/settings.json" ]; then
    mkdir -p "$APP/.runtime"
    new_settings >"$APP/.runtime/settings.json"
  fi
  install_udev desktop
  usbmuxd_share
  firewall_open
  step "Starting MirrorDesk"
  (cd "$APP" && "$APP/.venv/bin/python" -m mirrordesk.unixsetup install)
  wait_health 60 || warn "MirrorDesk did not answer yet; its log is $APP/.runtime/logs/mirrordesk.log"
  say ""
  say "Done. Your browser shows MirrorDesk now (http://127.0.0.1:$PORT/)."
  say "App menu: \"MirrorDesk\" starts it or opens its page; \"Quit MirrorDesk\" stops it."
  say "Terminal: mirrordesk start | stop | restart | status | log   (open a new terminal first)"
  say "Other devices: the Viewers page has the link to send and the viewer code."
  say "Uninstall: sh $APP/mirrordesk-installer.sh --uninstall"
}

# ------------------------------------------------------------------ server install (runs as root)
install_server() {
  if [ "$(id -u)" -ne 0 ]; then
    have sudo || die "the service install needs administrator rights: install sudo, or run the installer as root."
    say "The service install runs as administrator; sudo asks for your password."
    rm -rf "$TMP"
    trap - EXIT
    # shellcheck disable=SC2046
    exec sudo sh "$0" --server --port "$PORT" ${ACCESS_CODE:+--access-code="$ACCESS_CODE"} $([ "$YES" = 1 ] && echo --yes)
  fi
  SYSTEMD=0
  if have systemctl && [ -d /run/systemd/system ]; then SYSTEMD=1; fi
  say "MirrorDesk $VERSION as a service, into $SERVER_APP (data in $SERVER_DATA)"
  install_packages
  check_gstreamer
  step "Copying MirrorDesk"
  if [ "$SYSTEMD" = 1 ] && systemctl is-active --quiet "$SERVICE"; then systemctl stop "$SERVICE"; fi
  copy_app "$SERVER_APP"
  build_uxplay "$SERVER_APP"
  install_python "$SERVER_APP"
  install_adb "$SERVER_APP"
  chown -R root:root "$SERVER_APP"
  chmod -R go-w "$SERVER_APP"
  if ! id mirrordesk >/dev/null 2>&1; then
    nologin=$(command -v nologin || echo /usr/sbin/nologin)
    useradd --system --home-dir "$SERVER_DATA" --no-create-home --shell "$nologin" --user-group mirrordesk \
      || die "could not create the user mirrordesk."
  fi
  mkdir -p "$SERVER_DATA"
  if [ ! -f "$SERVER_DATA/settings.json" ]; then new_settings >"$SERVER_DATA/settings.json"; fi
  chown -R mirrordesk:mirrordesk "$SERVER_DATA"
  chmod 0750 "$SERVER_DATA"
  chmod 0600 "$SERVER_DATA/settings.json"
  install_udev server
  usbmuxd_share
  firewall_open
  code_line=""
  if [ -n "$ACCESS_CODE" ]; then code_line="Environment=MIRRORDESK_ACCESS_CODE=$ACCESS_CODE"; fi
  cat >"$TMP/mirrordesk.service" <<EOF
[Unit]
Description=MirrorDesk - phone screen mirroring to any browser
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=mirrordesk
Group=mirrordesk
WorkingDirectory=$SERVER_APP
Environment=HOME=$SERVER_DATA
Environment=MIRRORDESK_DATA=$SERVER_DATA
Environment=PYTHONUNBUFFERED=1
$code_line
ExecStart=$SERVER_APP/.venv/bin/python -m mirrordesk --host 0.0.0.0 --port $PORT --data $SERVER_DATA
Restart=on-failure
RestartSec=3
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
  step "Starting the mirrordesk service"
  if [ "$SYSTEMD" = 1 ]; then
    cp "$TMP/mirrordesk.service" "/etc/systemd/system/$SERVICE.service"
    chmod 0644 "/etc/systemd/system/$SERVICE.service"
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1
    systemctl restart "$SERVICE"
    wait_health 60 || warn "MirrorDesk did not answer yet: journalctl -u $SERVICE -n 50"
  else
    warn "systemd is not running here, so MirrorDesk was not set up to start by itself. Start it with:"
    say "    su -s /bin/sh mirrordesk -c 'cd $SERVER_APP && HOME=$SERVER_DATA .venv/bin/python -m mirrordesk --port $PORT --data $SERVER_DATA'"
  fi
  code=$(read_code "$SERVER_DATA/settings.json")
  print_addresses "$code" "$SERVER_APP"
  say ""
  say "iPhone: Screen Mirroring -> \"MirrorDesk Web\", then the AirPlay code shown on the web page."
  say "Manage: sudo systemctl status|restart|stop $SERVICE    Logs: sudo journalctl -u $SERVICE -f"
  say "Uninstall: sudo sh $SERVER_APP/mirrordesk-installer.sh --uninstall"
}

# ------------------------------------------------------------------ Docker / Dockge install
DOCKER_SUDO=""
dk() {   # docker, through sudo (keeping the settings below) when this user may not use it
  if [ -n "$DOCKER_SUDO" ]; then
    sudo env ${DOCKER_CONFIG:+DOCKER_CONFIG="$DOCKER_CONFIG"} ${DOCKER_BUILDKIT:+DOCKER_BUILDKIT="$DOCKER_BUILDKIT"} docker "$@"
  else
    docker "$@"
  fi
}

find_docker() {
  have docker || die "Docker is not installed (see https://docs.docker.com/engine/install/)."
  # ZimaOS: the login user's Docker config folder is root-only, which hides compose and buildx.
  if [ -z "${DOCKER_CONFIG:-}" ] && [ -e "$HOME/.docker" ] && [ ! -r "$HOME/.docker" ]; then
    DOCKER_CONFIG="$TMP/docker-config"
    export DOCKER_CONFIG
    mkdir -p "$DOCKER_CONFIG"
  fi
  if docker info >/dev/null 2>&1; then return 0; fi
  if [ "$(id -u)" -ne 0 ] && have sudo && sudo docker info >/dev/null 2>&1; then DOCKER_SUDO=1; return 0; fi
  die "Docker is not running, or this user may not use it (add yourself to the docker group, or use sudo)."
}

find_dockge() {   # prints "container stacks_dir" for a running Dockge, or nothing
  for name in $(dk ps --format '{{.Names}} {{.Image}}' 2>/dev/null | awk 'tolower($2) ~ /dockge/ {print $1}'); do
    stacks=$(dk inspect "$name" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
      | sed -n 's/^DOCKGE_STACKS_DIR=//p' | head -n 1)
    [ -n "$stacks" ] || stacks=/opt/stacks
    # Dockge keeps its stacks folder at the same path on the host and in its container.
    if dk inspect "$name" --format '{{range .Mounts}}{{.Source}}={{.Destination}}{{println}}{{end}}' 2>/dev/null \
        | grep -qx "$stacks=$stacks"; then
      echo "$name $stacks"
      return 0
    fi
  done
}

install_docker() {
  find_docker
  IMAGE="mirrordesk:$VERSION"
  # The stack file names the image published on GitHub (ghcr.io/.../mirrordesk): this build gets
  # that name too, so the stack runs it without downloading anything.
  STACK_IMAGE=$(sed -n 's#^    image: *\([^ ]*\)/mirrordesk:.*#\1/mirrordesk#p' "$PKG/app/docker/compose.yaml" | head -n 1)
  STACK_IMAGE="${STACK_IMAGE:-mirrordesk}:$VERSION"
  DATA_DEFAULT=./data
  if [ -d /DATA/AppData ]; then DATA_DEFAULT=/DATA/AppData/mirrordesk; fi   # ZimaOS / CasaOS

  step "Building the Docker image $IMAGE (the first build takes 5-15 minutes)"
  if ! dk buildx version >/dev/null 2>&1; then
    DOCKER_BUILDKIT=0
    export DOCKER_BUILDKIT
    say "(buildx is not installed: using Docker's classic builder)"
  fi
  dk build -t "$IMAGE" -t "$STACK_IMAGE" "$PKG/app" || die "the image build failed (messages above)."

  DOCKGE=""
  STACK_DIR=""
  found=$(find_dockge)
  if [ -n "$found" ]; then DOCKGE=${found%% *}; STACK_DIR="${found#* }/mirrordesk"; fi
  existing=$(dk inspect mirrordesk --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)
  if [ -n "$existing" ]; then
    STACK_DIR=$existing    # update the stack that already runs MirrorDesk, wherever it is
  elif [ -z "$STACK_DIR" ]; then
    if [ -d /DATA/AppData ]; then STACK_DIR=/DATA/AppData/mirrordesk-stack; else STACK_DIR=/opt/mirrordesk-docker; fi
  fi

  step "Stack folder: $STACK_DIR"
  if [ -d "$STACK_DIR" ]; then probe=$STACK_DIR; else probe=$(dirname "$STACK_DIR"); fi
  if [ -w "$probe" ]; then RUN=""; else need_root; RUN=$SUDO; fi
  $RUN mkdir -p "$STACK_DIR"
  if [ -f "$STACK_DIR/compose.yaml" ] || [ -f "$STACK_DIR/docker-compose.yml" ] || [ -f "$STACK_DIR/compose.yml" ] \
      || [ -f "$STACK_DIR/docker-compose.yaml" ]; then
    say "Keeping the stack's own compose file (its settings stay as they are)."
    current=$(dk inspect mirrordesk --format '{{.Config.Image}}' 2>/dev/null || true)
    if [ -n "$current" ] && [ "$current" != "$IMAGE" ]; then
      dk tag "$IMAGE" "$current"
      say "(The stack names the image $current: that name now also points to this build.)"
    fi
  else
    sed -e "s#^    image: .*#    image: $STACK_IMAGE#" "$PKG/app/docker/compose.yaml" >"$TMP/compose.yaml"
    if [ "$USB" = 1 ]; then
      sed -e 's|^      # - /dev/bus/usb:/dev/bus/usb|      - /dev/bus/usb:/dev/bus/usb|' \
          -e 's|^    # device_cgroup_rules:|    device_cgroup_rules:|' \
          -e 's|^    #   - "c 189:\* rmw"|      - "c 189:* rmw"|' "$TMP/compose.yaml" >"$TMP/compose.usb.yaml"
      mv "$TMP/compose.usb.yaml" "$TMP/compose.yaml"
    fi
    {
      echo "MIRRORDESK_DATA_DIR=$DATA_DEFAULT"
      echo "MIRRORDESK_PORT=$PORT"
      if [ -n "$ACCESS_CODE" ]; then echo "MIRRORDESK_ACCESS_CODE=$ACCESS_CODE"; fi
      if [ "$USB" = 1 ]; then echo "MIRRORDESK_RUN_AS_ROOT=1"; fi
    } >"$TMP/stack.env"
    $RUN cp "$TMP/compose.yaml" "$STACK_DIR/compose.yaml"
    $RUN cp "$TMP/stack.env" "$STACK_DIR/.env"
  fi
  case "$DATA_DEFAULT" in /*) mkdir -p "$DATA_DEFAULT" 2>/dev/null || true ;; esac   # else Docker creates it

  # A plain container (not from a stack) with the same name would block the stack.
  if [ -z "$existing" ] && dk inspect mirrordesk >/dev/null 2>&1; then
    say "Replacing the old mirrordesk container (its data folder is kept)."
    dk rm -f mirrordesk >/dev/null
  fi

  step "Starting the stack"
  if dk compose version >/dev/null 2>&1; then
    (cd "$STACK_DIR" && dk compose up -d --force-recreate) || die "docker compose could not start MirrorDesk."
  elif [ -n "$DOCKGE" ]; then
    dk exec -w "$STACK_DIR" "$DOCKGE" docker compose up -d --force-recreate || die "Dockge could not start the stack."
  elif have docker-compose; then
    (cd "$STACK_DIR" && $RUN docker-compose up -d --force-recreate) || die "docker-compose could not start MirrorDesk."
  else
    die "Docker Compose is missing. Install it (package docker-compose-plugin) or Dockge, and run this again."
  fi
  wait_health 90 || warn "MirrorDesk did not answer yet; see: docker logs mirrordesk"
  code=$(dk logs mirrordesk 2>&1 | tr -d '\r' | sed -n 's/.*Owner code (full control) for .*: \([A-Z0-9]\{6,32\}\) *$/\1/p' | tail -n 1)
  print_addresses "$code" docker
  say ""
  if [ -n "$DOCKGE" ]; then say "Manage it in Dockge: stack \"$(basename "$STACK_DIR")\"."; fi
  say "Logs: docker logs -f mirrordesk    Settings: $STACK_DIR/.env"
  say "Update: run a newer MirrorDesk installer with --docker. Remove: the installer with --docker --uninstall."
}

# ------------------------------------------------------------------ uninstall
uninstall_docker() {
  find_docker
  dir=$(dk inspect mirrordesk --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)
  if [ -n "$dir" ] && [ -d "$dir" ] && dk compose version >/dev/null 2>&1; then
    (cd "$dir" && dk compose down) || true
  fi
  dk rm -f mirrordesk >/dev/null 2>&1 || true
  say "Stopped and removed the mirrordesk container."
  if [ -n "$dir" ]; then say "Its stack folder is kept: $dir (delete it, or the stack in Dockge, to remove its settings and data)."; fi
  if ask "Also delete the MirrorDesk images?" n; then
    for image in $(dk images --format '{{.Repository}}:{{.Tag}}' | grep -E '(^|/)mirrordesk:'); do dk rmi "$image" >/dev/null 2>&1 || true; done
  fi
}

uninstall_native() {
  desktop_app="${XDG_DATA_HOME:-$HOME/.local/share}/mirrordesk"
  if [ "$(id -u)" -ne 0 ] && [ -d "$desktop_app" ]; then
    step "Removing MirrorDesk for $(id -un) ($desktop_app)"
    quit_desktop "$desktop_app"
    (cd "$desktop_app" && "$desktop_app/.venv/bin/python" -m mirrordesk.unixsetup uninstall) || true
    if [ "$PURGE" = 1 ]; then
      rm -rf "$desktop_app"
    else
      find "$desktop_app" -mindepth 1 -maxdepth 1 ! -name .runtime -exec rm -rf {} +
      say "Settings kept in $desktop_app/.runtime (run with --purge to delete them)."
    fi
  fi
  if [ -d "$SERVER_APP" ] || [ -f "/etc/systemd/system/$SERVICE.service" ]; then
    step "Removing the mirrordesk service ($SERVER_APP)"
    if have systemctl; then
      as_root systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
      as_root rm -f "/etc/systemd/system/$SERVICE.service"
      as_root systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    as_root rm -rf "$SERVER_APP"
    if [ "$PURGE" = 1 ]; then
      as_root rm -rf "$SERVER_DATA"
      as_root userdel mirrordesk >/dev/null 2>&1 || true
    else
      say "Settings kept in $SERVER_DATA (run with --purge to delete them and the user mirrordesk)."
    fi
  fi
  if [ -f "$UDEV_RULE" ]; then
    as_root rm -f "$UDEV_RULE"
    as_root udevadm control --reload-rules >/dev/null 2>&1 || true
  fi
  usbmuxd_unshare
  firewall_close
  say "MirrorDesk was removed. The system packages it installed (GStreamer, build tools) were left in place."
}

# ------------------------------------------------------------------ main
if [ "$UNINSTALL" = 1 ]; then
  if [ "$MODE" = docker ]; then uninstall_docker; else uninstall_native; fi
  exit 0
fi

[ -d "$PKG/app/mirrordesk" ] || die "the MirrorDesk files are missing next to this installer ($PKG/app)."
choose_mode
case "$MODE" in
  desktop) install_desktop ;;
  server) install_server ;;
  docker) install_docker ;;
esac
