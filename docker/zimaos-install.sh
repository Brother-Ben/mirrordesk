#!/bin/sh
# MirrorDesk on ZimaOS (or any Linux host) with plain docker - no "docker compose" needed.
# Same settings as docker-compose.zimaos.yml. Run from the MirrorDesk folder on the box:
#     sh docker/zimaos-install.sh
# Builds the image, then starts the container. When a Dockge (or other compose) stack owns
# the container, the stack is restarted with the new image instead, and its compose file
# holds the options (docker/dockge/compose.yaml is the stack file).
# Options (environment variables) for the plain container:
#     MIRRORDESK_ACCESS_CODE=CHANGE2ME   fixed viewer code (watch only) instead of a random one
#     MIRRORDESK_OWNER_CODE=CHANGE3ME    fixed owner code (full control) instead of a random one
#     MIRRORDESK_USB_PHONES=1            phones on USB (runs the server as root in the container)
#     MIRRORDESK_DATA_DIR=/DATA/AppData/mirrordesk   where settings and keys are kept
#     MIRRORDESK_AVAHI=host|private      default: the host's Avahi when it runs (ZimaOS does)
#     MIRRORDESK_STACK_DIR=/path         restart this compose stack (found automatically)
set -eu
cd "$(dirname "$0")/.."

IMAGE=mirrordesk:0.13.0
DATA="${MIRRORDESK_DATA_DIR:-/DATA/AppData/mirrordesk}"

# ZimaOS: the login user's Docker config (/DATA/.docker) is readable by root only, which hides
# Docker's compose and buildx plugins. A private config folder makes them visible again
# (nothing is lost: that config could not be read anyway).
if [ -z "${DOCKER_CONFIG:-}" ] && [ -e "$HOME/.docker" ] && [ ! -r "$HOME/.docker" ]; then
  export DOCKER_CONFIG="${TMPDIR:-/tmp}/mirrordesk-docker-config"
  mkdir -p "$DOCKER_CONFIG"
fi

STACK_DIR="${MIRRORDESK_STACK_DIR:-}"
if [ -z "$STACK_DIR" ]; then
  STACK_DIR=$(docker inspect mirrordesk --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)
fi

# Docker 23+ builds with BuildKit, which needs the buildx plugin; some hosts lack it.
if ! docker buildx version >/dev/null 2>&1; then
  export DOCKER_BUILDKIT=0
  echo "(buildx is not installed: using Docker's classic builder)"
fi

echo "Building $IMAGE (first build: 5-15 minutes)..."
docker build -t "$IMAGE" .

if [ -n "$STACK_DIR" ]; then
  # The stack names the image published on GitHub (ghcr.io/.../mirrordesk:<version>), or one
  # built for an earlier version: give the new build that name too, so the stack runs it without
  # downloading anything. A stack file that already names the new image is left alone (and so is
  # the old image, for going back).
  CURRENT=$(docker inspect mirrordesk --format '{{.Config.Image}}' 2>/dev/null || true)
  if [ -n "$CURRENT" ] && [ "$CURRENT" != "$IMAGE" ] && \
     ! cat "$STACK_DIR"/compose.yaml "$STACK_DIR"/compose.yml "$STACK_DIR"/docker-compose.yaml \
       "$STACK_DIR"/docker-compose.yml 2>/dev/null | grep -q "image: *$IMAGE *\$"; then
    docker tag "$IMAGE" "$CURRENT"
    echo "(The stack names the image $CURRENT: that name now also points to this build.)"
  fi
  echo "Restarting the stack in $STACK_DIR with the new image (settings in its compose file and $DATA are kept)..."
  if docker compose version >/dev/null 2>&1; then
    (cd "$STACK_DIR" && docker compose up -d --force-recreate)
  else
    # No compose plugin here: Dockge brings its own and sees the stack at the same path.
    DOCKGE=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /dockge(:|$)/ {print $1; exit}')
    if [ -z "$DOCKGE" ]; then
      echo "Start Dockge (or install docker compose) to restart the stack in $STACK_DIR." >&2
      exit 1
    fi
    docker exec -w "$STACK_DIR" "$DOCKGE" docker compose up -d --force-recreate
  fi
else
  # One mDNS responder per host: when the host already runs Avahi, UxPlay announces
  # AirPlay through it instead of starting a second one in the container.
  AVAHI="${MIRRORDESK_AVAHI:-}"
  if [ -z "$AVAHI" ]; then
    if [ -S /var/run/dbus/system_bus_socket ] && [ -S /var/run/avahi-daemon/socket ]; then AVAHI=host; else AVAHI=private; fi
  fi
  echo "AirPlay announcements: $AVAHI Avahi"

  echo "Replacing the mirrordesk container (settings in $DATA are kept)..."
  docker rm -f mirrordesk >/dev/null 2>&1 || true
  mkdir -p "$DATA"

  set -- --name mirrordesk --network host --restart unless-stopped --stop-timeout 15 -v "$DATA:/data"
  if [ "$AVAHI" = "host" ]; then
    set -- "$@" -e MIRRORDESK_AVAHI=host -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket \
      -v /var/run/avahi-daemon/socket:/var/run/avahi-daemon/socket
  fi
  if [ "${MIRRORDESK_USB_PHONES:-0}" = "1" ]; then
    set -- "$@" -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule "c 189:* rmw" -e MIRRORDESK_RUN_AS_ROOT=1
  fi
  if [ -n "${MIRRORDESK_ACCESS_CODE:-}" ]; then
    set -- "$@" -e "MIRRORDESK_ACCESS_CODE=$MIRRORDESK_ACCESS_CODE"
  fi
  if [ -n "${MIRRORDESK_OWNER_CODE:-}" ]; then
    set -- "$@" -e "MIRRORDESK_OWNER_CODE=$MIRRORDESK_OWNER_CODE"
  fi
  docker run -d "$@" "$IMAGE" >/dev/null
fi

echo "Started. Waiting for the first log lines..."
sleep 8
docker logs mirrordesk 2>&1 | tail -n 25
echo
echo "Open http://<this box's address>:8765/ and enter the owner code shown above."
echo "People who should only watch get the viewer code."
if [ -n "$STACK_DIR" ]; then
  echo "Manage it in Dockge (stack \"$(basename "$STACK_DIR")\"). Logs: docker logs -f mirrordesk"
else
  echo "Logs: docker logs -f mirrordesk    Stop: docker stop mirrordesk    Remove: docker rm -f mirrordesk"
fi
