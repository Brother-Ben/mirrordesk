#!/bin/sh
# MirrorDesk (macOS, Linux) from this folder. Usually MirrorDesk in Applications (macOS) or
# "MirrorDesk" / "Quit MirrorDesk" in the app menu (Linux) do this; the Linux installer also
# makes this script the command `mirrordesk` (a link in ~/.local/bin).
#   mirrordesk [start]     start in the background, like the app, and open the page
#   mirrordesk open        open the page (starts MirrorDesk first when it is not running)
#   mirrordesk stop        quit MirrorDesk (also: quit)
#   mirrordesk restart     quit, then start again
#   mirrordesk status      whether it runs, and its address
#   mirrordesk log         follow its log (Ctrl+C stops following)
#   mirrordesk --console   run in this terminal (Ctrl+C stops); more options go to MirrorDesk,
#                          for example --port 8766 or --host 127.0.0.1
set -eu
SCRIPT=$0
while [ -L "$SCRIPT" ]; do   # ~/.local/bin/mirrordesk -> this folder
  target=$(readlink "$SCRIPT")
  case "$target" in /*) SCRIPT=$target ;; *) SCRIPT=$(dirname -- "$SCRIPT")/$target ;; esac
done
ROOT=$(CDPATH='' cd -- "$(dirname -- "$SCRIPT")" && pwd)
PY="$ROOT/.venv/bin/python"
URL=http://127.0.0.1:8765/
LOG="${MIRRORDESK_DATA:-$ROOT/.runtime}/logs/mirrordesk.log"
if [ ! -x "$PY" ]; then
  echo "MirrorDesk is not set up in $ROOT: run the MirrorDesk installer first." >&2
  exit 1
fi
cd "$ROOT"

running() {
  if command -v curl >/dev/null 2>&1; then curl -fsS -m 2 "${URL}api/health" >/dev/null 2>&1
  else wget -q -T 2 -O /dev/null "${URL}api/health" 2>/dev/null
  fi
}

start() {   # MirrorDesk.pyw only opens the page when MirrorDesk already runs
  nohup "$PY" "$ROOT/MirrorDesk.pyw" --open-browser "$@" >/dev/null 2>&1 &
  echo "MirrorDesk is starting in the background: $URL"
  echo "Log: $LOG"
}

command=${1:-start}
case "$command" in
  start|open)
    [ $# -gt 0 ] && shift
    start "$@"
    ;;
  stop|quit)
    exec "$PY" -m mirrordesk --quit
    ;;
  restart)
    "$PY" -m mirrordesk --quit || true
    start
    ;;
  status)
    if running; then echo "MirrorDesk is running: $URL"; else echo "MirrorDesk is not running."; exit 3; fi
    ;;
  log)
    exec tail -n 40 -f "$LOG"
    ;;
  --console)
    shift
    exec "$PY" -m mirrordesk --open-browser "$@"
    ;;
  -h|--help|help)
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$SCRIPT"
    ;;
  -*)
    start "$@"   # options for MirrorDesk, such as --port 8766
    ;;
  *)
    echo "Unknown command: $command (start, open, stop, restart, status, log, --console)" >&2
    exit 2
    ;;
esac
