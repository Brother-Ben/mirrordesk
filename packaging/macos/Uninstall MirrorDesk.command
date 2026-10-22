#!/bin/sh
# Double-click to remove MirrorDesk from this Mac (its settings are kept unless you add --purge).
cd "$(dirname "$0")" || exit 1
sh ./install.sh --uninstall "$@"
status=$?
echo
echo "You can close this window."
exit "$status"
