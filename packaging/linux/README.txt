MirrorDesk for Linux (tester build)
==================================

Mirror an iPhone, iPad or Android phone to any browser, with sound.
Needs: a 64-bit Linux computer (Intel/AMD or ARM, e.g. Raspberry Pi 4/5 with 64-bit OS) with
apt (Debian, Ubuntu, Raspberry Pi OS, Mint), dnf (Fedora) or pacman (Arch) - or Docker.

This folder is the whole program: app/ is MirrorDesk itself, install.sh installs it.

Install, from the folder that contains this MirrorDesk folder:
    sh MirrorDesk/install.sh             on the Linux computer itself
    sh MirrorDesk/install.sh --docker    as a Docker / Dockge stack
    sh MirrorDesk/install.sh --help      every option

From another computer over SSH (one line; Windows PowerShell 5: ; instead of &&):
    scp -r MirrorDesk USER@SERVER:/tmp/ && ssh -t USER@SERVER sh /tmp/MirrorDesk/install.sh

After a desktop install: MirrorDesk and Quit MirrorDesk are in the app menu, and in a
new terminal:
    mirrordesk start | stop | restart | status | log

INSTALL-GUIDE.html covers every way to install, update and remove MirrorDesk.
TESTER-GUIDE.html covers connecting phones and reporting problems.

Licenses: NOTICE.txt, app/THIRD_PARTY_NOTICES.md, app/licenses/, app/third_party/uxplay/SOURCE.txt
