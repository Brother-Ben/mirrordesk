"""MirrorDesk in the background: no console window, output in .runtime/logs,
Quit and start-at-sign-in in the browser.

Windows: started by Start-MirrorDesk.cmd and the "Start with Windows" sign-in entry,
always with this project's .venv\\Scripts\\pythonw.exe; an icon next to the clock
(Open / Start with Windows / Quit). macOS: started by MirrorDesk.app (Dock icon).
Linux: started by the MirrorDesk app-menu entry. For a visible console use
Start-MirrorDesk-Console.cmd (Windows) or start-mirrordesk.sh --console (macOS, Linux).
"""
import multiprocessing
import sys

from mirrordesk.__main__ import main

if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main(['--background', *sys.argv[1:]]))
