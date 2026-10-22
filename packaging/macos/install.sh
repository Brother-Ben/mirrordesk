#!/bin/sh
# MirrorDesk installer for macOS (Apple Silicon: macOS 14 Sonoma or newer; Intel: macOS 12 or newer).
#
#   sh install.sh               install or update (double-click "Install MirrorDesk.command" does the same)
#   sh install.sh --uninstall   remove MirrorDesk (add --purge to delete its settings too)
#   sh install.sh --yes         never ask, take the defaults
#
# Installs into ~/Library/Application Support/MirrorDesk and creates ~/Applications/MirrorDesk.app.
# Needs Apple's Command Line Tools and Homebrew (the installer offers to install both), then
# builds the AirPlay receiver (UxPlay 1.74) from the source included here. Downloads are
# checked against pinned checksums. Nothing else on the Mac is changed, except firewall
# entries for MirrorDesk's programs when the macOS firewall is on (you are asked first).
set -eu

UV_VERSION=0.12.12
UV_SHA256_ARM64=46740540b63fdee9a6cb2e19baf3f1f475b850c440a33e63455087a6871263f1
UV_SHA256_X86_64=0dc8cd6c961582b0d140b5398f96b23502885277fb3464241456a2435e460dfa
UXPLAY_COMMIT=d29dfabe102aba137170121077f55390e875310c
UXPLAY_SHA256=d68b043bb7eaf141b506199f0c3aa4b8dc261b4f063cf8ccc9697f22b8539ce2
# That source plus MirrorDesk's change (third_party/uxplay/mirrordesk): a receiver built without
# it is built again.
UXPLAY_BUILD="$UXPLAY_COMMIT+mirrordesk.3"
GST_ELEMENTS="appsrc h264parse rtph264pay rtpL16pay udpsink audioconvert audioresample volume avdec_aac avdec_alac"
BREW_PACKAGES="cmake pkgconf libplist openssl@3 gstreamer coreutils"
FIREWALL=/usr/libexec/ApplicationFirewall/socketfilterfw
PORT=8765

PKG=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
VERSION=$(sed -n 's/^MirrorDesk \([0-9][0-9.]*\).*/\1/p' "$PKG/VERSION.txt" 2>/dev/null | head -n 1)
[ -n "$VERSION" ] || VERSION=unknown
APP="$HOME/Library/Application Support/MirrorDesk"
LAUNCHER="$HOME/Applications/MirrorDesk.app"

YES=0
UNINSTALL=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    --help|-h) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) printf 'Unknown option: %s (see --help)\n' "$arg" >&2; exit 1 ;;
  esac
done

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
ask() {   # ask "question" default(y|n)
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then [ "$2" = y ]; return; fi
  if [ "$2" = y ]; then printf '%s [Y/n] ' "$1"; else printf '%s [y/N] ' "$1"; fi
  read -r answer || answer=""
  case "$answer" in [Yy]*) return 0 ;; [Nn]*) return 1 ;; *) [ "$2" = y ] ;; esac
}
running() { curl -fsS -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; }
quit_running() {
  if running; then
    say "Closing the MirrorDesk that is running..."
    if [ -x "$APP/.venv/bin/python" ]; then   # --quit sends its quit token (0.11.0 and newer)
      (cd "$APP" && "$APP/.venv/bin/python" -m mirrordesk --quit --port "$PORT" >/dev/null 2>&1) || true
    fi
    if running; then
      curl -fsS -m 5 -X POST -H 'X-MirrorDesk: 1' "http://127.0.0.1:$PORT/api/desktop/quit" >/dev/null 2>&1 || true
      i=0
      while [ "$i" -lt 20 ] && running; do sleep 1; i=$((i + 1)); done
    fi
  fi
  osascript -e 'if application id "com.mirrordesk.app" is running then tell application id "com.mirrordesk.app" to quit' \
    >/dev/null 2>&1 || true
}

[ "$(uname -s)" = Darwin ] || die "this installer is for macOS."
[ "$(id -u)" -ne 0 ] || die "run the installer as yourself, not with sudo (it asks for the password when needed)."

TMP=$(mktemp -d "${TMPDIR:-/tmp}/mirrordesk-install.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
trap 'rm -rf "$TMP"; exit 130' INT TERM

firewall_programs() {   # the programs that receive connections (real files, not links)
  for program in "$APP/.venv/bin/python" "$APP/uxplay/bin/uxplay" "$APP/tools/platform-tools/adb"; do
    if [ -e "$program" ]; then
      (cd "$APP" && "$APP/.venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$program")
    fi
  done
}

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" = 1 ]; then
  step "Removing MirrorDesk"
  quit_running
  if [ -x "$APP/.venv/bin/python" ]; then
    (cd "$APP" && "$APP/.venv/bin/python" -m mirrordesk.unixsetup uninstall) || true
    if [ -x "$FIREWALL" ] && "$FIREWALL" --getglobalstate 2>/dev/null | grep -qi enabled; then
      say "Removing MirrorDesk's firewall entries (macOS asks for your password)..."
      firewall_programs | while IFS= read -r program; do sudo "$FIREWALL" --remove "$program" >/dev/null 2>&1 || true; done
    fi
  fi
  rm -rf "$LAUNCHER"
  if [ "$PURGE" = 1 ]; then
    rm -rf "$APP"
    say "MirrorDesk and its settings were removed."
  elif [ -d "$APP" ]; then
    find "$APP" -mindepth 1 -maxdepth 1 ! -name .runtime -exec rm -rf {} +
    say "MirrorDesk was removed. Its settings are kept in $APP/.runtime (run with --purge to delete them)."
  fi
  say "Homebrew and the packages it installed (GStreamer, cmake ...) were left in place."
  exit 0
fi

[ -d "$PKG/app/mirrordesk" ] || die "the MirrorDesk files are missing next to this installer ($PKG/app)."

# ------------------------------------------------------------------ system checks
ARCH=$(uname -m)
if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 1 ]; then ARCH=arm64; fi   # Terminal under Rosetta
MACOS=$(sw_vers -productVersion)
MAJOR=${MACOS%%.*}
case "$ARCH" in
  arm64) [ "$MAJOR" -ge 14 ] || die "on Apple Silicon, MirrorDesk needs macOS 14 Sonoma or newer (this Mac has $MACOS)." ;;
  x86_64) [ "$MAJOR" -ge 12 ] || die "on Intel Macs, MirrorDesk needs macOS 12 Monterey or newer (this Mac has $MACOS)." ;;
  *) die "unsupported processor: $ARCH" ;;
esac
say "MirrorDesk $VERSION for macOS $MACOS ($ARCH), into $APP"

step "Apple's Command Line Tools"
if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install >/dev/null 2>&1 || true
  die "macOS is installing the Command Line Tools now (a window opened). When that has finished, run this installer again."
fi
say "installed"

step "Homebrew"
BREW=$(command -v brew || true)
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  if [ -z "$BREW" ] && [ -x "$candidate" ]; then BREW=$candidate; fi
done
if [ -z "$BREW" ]; then
  say "MirrorDesk uses Homebrew (https://brew.sh) for GStreamer and the build tools."
  ask "Install Homebrew now? It asks for your Mac password." y || die "Homebrew is needed. Install it from https://brew.sh, then run this installer again."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || die "installing Homebrew failed (messages above)."
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -z "$BREW" ] && [ -x "$candidate" ]; then BREW=$candidate; fi
  done
  [ -n "$BREW" ] || die "Homebrew was installed, but brew was not found. Open a new Terminal window and run this installer again."
fi
eval "$("$BREW" shellenv)"
say "$BREW"

step "GStreamer and build tools from Homebrew (the first time takes a while)"
# shellcheck disable=SC2086
HOMEBREW_NO_INSTALL_CLEANUP=1 "$BREW" install $BREW_PACKAGES || die "Homebrew could not install: $BREW_PACKAGES"
missing=""
for element in $GST_ELEMENTS; do
  gst-inspect-1.0 "$element" >/dev/null 2>&1 || missing="$missing $element"
done
[ -z "$missing" ] || warn "GStreamer elements missing:$missing (the phone's picture or sound may not work)."

# ------------------------------------------------------------------ MirrorDesk files
step "Copying MirrorDesk"
quit_running
mkdir -p "$APP"
rm -rf "$APP/mirrordesk" "$APP/licenses" "$APP/third_party"
cp -R "$PKG/app/." "$APP/"
cp "$PKG/install.sh" "$APP/mirrordesk-installer.sh"
for doc in VERSION.txt INSTALL-GUIDE.html TESTER-GUIDE.html NOTICE.txt; do
  if [ -f "$PKG/$doc" ]; then cp "$PKG/$doc" "$APP/$doc"; fi
done
chmod 0755 "$APP/start-mirrordesk.sh" "$APP/mirrordesk-installer.sh"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

# ------------------------------------------------------------------ UxPlay
if [ -x "$APP/uxplay/bin/uxplay" ] && [ "$(cat "$APP/uxplay/COMMIT" 2>/dev/null)" = "$UXPLAY_BUILD" ]; then
  say "The AirPlay receiver (UxPlay 1.74) is already built."
else
  step "Building the AirPlay receiver (UxPlay 1.74) from source (a few minutes)"
  tarball="$APP/third_party/uxplay/UxPlay-$UXPLAY_COMMIT.tar.gz"
  [ "$(shasum -a 256 "$tarball" | cut -d' ' -f1)" = "$UXPLAY_SHA256" ] \
    || die "the UxPlay source failed its checksum check. Download MirrorDesk again."
  work="$APP/uxplay/.build"
  rm -rf "$work"
  mkdir -p "$work/src"
  tar -xzf "$tarball" -C "$work/src" --strip-components=1
  # MirrorDesk's change: the sound carries on at once after a pause or seek (mirrordesk.patch).
  cp -R "$APP/third_party/uxplay/mirrordesk/." "$work/src/"
  if ! cmake -S "$work/src" -B "$work/build" -DCMAKE_BUILD_TYPE=Release -DNO_X11_DEPS=ON >"$work/cmake.log" 2>&1; then
    tail -n 40 "$work/cmake.log" >&2
    die "configuring UxPlay failed (messages above)."
  fi
  if ! cmake --build "$work/build" -j "$(sysctl -n hw.ncpu)" >"$work/build.log" 2>&1; then
    tail -n 40 "$work/build.log" >&2
    die "building UxPlay failed (messages above)."
  fi
  "$work/build/uxplay" -h 2>&1 | grep -q -- '-vrtp' || die "the UxPlay that was built does not work (no -vrtp)."
  mkdir -p "$APP/uxplay/bin"
  cp "$work/build/uxplay" "$APP/uxplay/bin/uxplay"
  chmod 0755 "$APP/uxplay/bin/uxplay"
  rm -rf "$work"
  printf '%s\n' "$UXPLAY_BUILD" >"$APP/uxplay/COMMIT"
  printf 'UxPlay built on this Mac from https://github.com/FDH2/UxPlay commit %s (GPL-3.0-or-later)\nwith MirrorDesk'"'"'s change to its audio and video handling.\nSource: third_party/uxplay/UxPlay-%s.tar.gz and third_party/uxplay/mirrordesk.patch; license: licenses/UxPlay-LICENSE.txt\n' \
    "$UXPLAY_COMMIT" "$UXPLAY_COMMIT" >"$APP/uxplay/SOURCES.txt"
  say "Built $APP/uxplay/bin/uxplay"
fi

# ------------------------------------------------------------------ Python
step "Python 3.13 and MirrorDesk's libraries (the first time downloads about 100 MB)"
UV="$APP/tools/uv/uv"
if [ ! -x "$UV" ] || ! "$UV" --version 2>/dev/null | grep -q " $UV_VERSION"; then
  if [ "$ARCH" = arm64 ]; then triple=aarch64-apple-darwin; sum=$UV_SHA256_ARM64
  else triple=x86_64-apple-darwin; sum=$UV_SHA256_X86_64; fi
  curl -fsSL --retry 3 -o "$TMP/uv.tar.gz" "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$triple.tar.gz"
  [ "$(shasum -a 256 "$TMP/uv.tar.gz" | cut -d' ' -f1)" = "$sum" ] \
    || die "the uv download failed its checksum check. Nothing was installed; try again later."
  mkdir -p "$TMP/uv" "$APP/tools/uv"
  tar -xzf "$TMP/uv.tar.gz" -C "$TMP/uv" --strip-components=1
  cp "$TMP/uv/uv" "$UV"
  chmod 0755 "$UV"
fi
if ! UV_PYTHON_INSTALL_DIR="$APP/runtime/python" UV_PYTHON_PREFERENCE=only-managed \
     UV_PROJECT_ENVIRONMENT="$APP/.venv" UV_CACHE_DIR="$TMP/uv-cache" UV_NO_PROGRESS=1 \
     "$UV" sync --locked --no-dev --python 3.13 --project "$APP"; then
  die "installing Python and its libraries failed (messages above). Check the internet connection and run the installer again."
fi
(cd "$APP" && "$APP/.venv/bin/python" -c "import mirrordesk, aiortc, av") || die "the Python environment does not work."

# ------------------------------------------------------------------ firewall
if [ -x "$FIREWALL" ] && "$FIREWALL" --getglobalstate 2>/dev/null | grep -qi enabled; then
  step "macOS firewall"
  say "The firewall is on. Without an entry for MirrorDesk, macOS asks \"accept incoming network"
  say "connections?\" the first time each program starts (choose Allow)."
  if ask "Add the entries now? It asks for your Mac password." y; then
    (cd "$APP" && "$APP/.venv/bin/python" -m mirrordesk.androidtools install --if-missing) >/dev/null 2>&1 || true
    firewall_programs | while IFS= read -r program; do
      sudo "$FIREWALL" --add "$program" >/dev/null && sudo "$FIREWALL" --unblockapp "$program" >/dev/null \
        && say "allowed: $program"
    done
  fi
fi

# ------------------------------------------------------------------ app, Android tools, start
step "Setting up MirrorDesk.app"
(cd "$APP" && "$APP/.venv/bin/python" -m mirrordesk.unixsetup install)

say ""
say "Done. MirrorDesk runs now and your browser shows it (http://127.0.0.1:$PORT/)."
say "  - While it runs, its icon is in the Dock: click it to open the page; Quit it there to stop MirrorDesk."
say "  - Next time: open MirrorDesk from your Applications folder (in your home folder) or Spotlight."
say "  - If macOS asks to let MirrorDesk find devices on your local network, choose Allow."
say "  - iPhone: Screen Mirroring -> \"MirrorDesk Web\" (your Mac itself may be listed too; choose MirrorDesk)."
say "Uninstall: sh \"$APP/mirrordesk-installer.sh\" --uninstall"
