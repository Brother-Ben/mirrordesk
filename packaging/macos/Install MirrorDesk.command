#!/bin/sh
# Double-click to install or update MirrorDesk (opens Terminal). If macOS refuses to open it,
# see INSTALL-GUIDE.html ("macOS"), or run in Terminal:  sh <this folder>/install.sh
cd "$(dirname "$0")" || exit 1
sh ./install.sh "$@"
status=$?
echo
if [ "$status" -eq 0 ]; then echo "Finished. You can close this window."; else echo "Setup did not finish. Copy the messages above and send them to whoever gave you MirrorDesk."; fi
exit "$status"
