#!/bin/sh
# MirrorDesk container entrypoint: optional D-Bus/Avahi, then the server as non-root.
set -eu

DATA="${MIRRORDESK_DATA:-/data}"
mkdir -p "$DATA"
chown -R mirrordesk:mirrordesk "$DATA" 2>/dev/null || true

# UxPlay 1.74 announces AirPlay with its own built-in mDNS responder (UDP 5353 on the
# host network), so by default no Avahi is started or needed (MIRRORDESK_AVAHI=off).
# Older settings still work:
#  - MIRRORDESK_AVAHI=host (docker-compose.host-avahi.yml): check the host's mounted
#    D-Bus socket, as before.
#  - MIRRORDESK_AVAHI=private: start a private D-Bus + Avahi. /run is not a tmpfs in
#    Docker, so sockets/pids left by a previous run of this container are removed
#    first; their mere presence must never be mistaken for a live host daemon.
case "${MIRRORDESK_AVAHI:-off}" in
  host)
    if [ ! -S /var/run/dbus/system_bus_socket ]; then
      echo "mirrordesk: MIRRORDESK_AVAHI=host but /var/run/dbus/system_bus_socket is not mounted" >&2
      exit 1
    fi
    echo "mirrordesk: using the host's Avahi daemon"
    ;;
  private)
    echo "mirrordesk: starting private D-Bus + Avahi"
    mkdir -p /var/run/dbus /var/run/avahi-daemon
    rm -f /var/run/dbus/pid /var/run/dbus/system_bus_socket \
          /var/run/avahi-daemon/pid /var/run/avahi-daemon/socket
    dbus-uuidgen --ensure
    dbus-daemon --system --fork
    avahi-daemon --daemonize --no-chroot
    ;;
  *)
    echo "mirrordesk: AirPlay is announced by UxPlay's built-in mDNS responder"
    ;;
esac

# HOME holds adb's keys (/data/.android): phones stay authorised across restarts.
export HOME="$DATA"

# Phones on USB (Android via adb, iPhone cable) need write access to /dev/bus/usb,
# whose nodes belong to root on most hosts (and new nodes appear when a phone is
# plugged in later). MIRRORDESK_RUN_AS_ROOT=1 keeps root inside the container for
# that; leave it off when only AirPlay and Android over Wi-Fi are used.
if [ "${MIRRORDESK_RUN_AS_ROOT:-0}" = "1" ]; then
  echo "mirrordesk: running as root inside the container (USB phone access)"
  exec python -m mirrordesk --host "${MIRRORDESK_HOST:-0.0.0.0}" --port "${MIRRORDESK_PORT:-8765}" --data "$DATA" "$@"
fi

# setpriv execs in place (no forking parent), so tini's SIGTERM from `docker
# stop` reaches the server directly and graceful shutdown is not cut short.
exec setpriv --reuid=mirrordesk --regid=mirrordesk --init-groups -- \
  python -m mirrordesk --host "${MIRRORDESK_HOST:-0.0.0.0}" --port "${MIRRORDESK_PORT:-8765}" --data "$DATA" "$@"
