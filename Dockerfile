# MirrorDesk (Prototype 7) — headless phone-mirroring server with a browser UI:
# iPhone/iPad over AirPlay (UxPlay) or USB cable, Android over Wi-Fi (Wireless
# debugging) or USB (adb + scrcpy-server).
#
# Linux hosts only (amd64/arm64): AirPlay discovery needs real host networking,
# which Docker Desktop on Windows/macOS cannot provide to the LAN.
#   Published on GitHub as ghcr.io/<owner>/mirrordesk (.github/workflows/image.yml):
#     paste docker/compose.yaml (ZimaOS: docker/dockge/compose.yaml) into Dockge
#   Any Linux host / Dockge: the Linux installer (packaging/linux/install.sh --docker)
#     builds this image and creates the stack docker/compose.yaml
#   docker compose up -d --build        then open http://<host>:8765/
#   ZimaOS: docker-compose.zimaos.yml or docker/zimaos-install.sh
#
# UxPlay is built from a pinned upstream commit: 1.74 development (master), the
# same source as the Windows build, shipped in third_party/uxplay (checksum-pinned;
# with MirrorDesk's change in third_party/uxplay/mirrordesk, it is also the GPL
# corresponding source). Not the v1.73.7 release: it rejects every
# iPhone's AirPlay code ("client_proof_len 20 invalid", so the phone asks again
# and again); master dropped that check the next day. Debian stable ships 1.71,
# which lacks -vrtp/-artp. 1.74 announces AirPlay with its own built-in mDNS
# responder, so no Avahi is needed. Python dependencies are installed hash-locked
# from uv.lock.
ARG PYTHON_IMAGE=python:3.13-slim-trixie
ARG UXPLAY_COMMIT=d29dfabe102aba137170121077f55390e875310c
ARG UXPLAY_SHA256=d68b043bb7eaf141b506199f0c3aa4b8dc261b4f063cf8ccc9697f22b8539ce2

FROM ${PYTHON_IMAGE} AS uxplay-build
ARG UXPLAY_COMMIT
ARG UXPLAY_SHA256
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake ca-certificates pkg-config \
      libssl-dev libplist-dev libavahi-compat-libdnssd-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
 && rm -rf /var/lib/apt/lists/*
COPY third_party/uxplay/UxPlay-${UXPLAY_COMMIT}.tar.gz /src/uxplay.tar.gz
# MirrorDesk's change to UxPlay (third_party/uxplay/mirrordesk.patch): the sound carries on at once
# after a pause or seek on the phone, each FLUSH is reported to MirrorDesk, and forwarded video
# frames carry the phone's frame time, so MirrorDesk can even out what Wi-Fi delivers in bursts.
COPY third_party/uxplay/mirrordesk /src/uxplay-mirrordesk
RUN echo "${UXPLAY_SHA256}  /src/uxplay.tar.gz" | sha256sum -c - \
 && mkdir /src/uxplay \
 && tar -xzf /src/uxplay.tar.gz -C /src/uxplay --strip-components=1 \
 && cp -R /src/uxplay-mirrordesk/. /src/uxplay/ \
 && cmake -S /src/uxplay -B /src/build -DCMAKE_BUILD_TYPE=Release -DNO_X11_DEPS=ON \
 && cmake --build /src/build -j"$(nproc)" \
 && install -m 0755 /src/build/uxplay /usr/local/bin/uxplay \
 && strip /usr/local/bin/uxplay \
 && /usr/local/bin/uxplay -h | head -n 1

FROM ${PYTHON_IMAGE}
ARG UXPLAY_COMMIT
LABEL org.opencontainers.image.title="MirrorDesk" \
      org.opencontainers.image.description="Phone screen mirroring viewed in any browser: AirPlay (UxPlay), iPhone USB, Android (scrcpy)" \
      org.opencontainers.image.version="0.13.0" \
      org.opencontainers.image.licenses="GPL-3.0-or-later AND BSD-3-Clause AND Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MIRRORDESK_DATA=/data MIRRORDESK_HOST=0.0.0.0 MIRRORDESK_PORT=8765
# adb: Debian's build of Android's adb (Apache-2.0) for Android phones; its keys live in /data/.android.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tini dbus avahi-daemon libavahi-compat-libdnssd1 libplist-2.0-4 libssl3t64 libusb-1.0-0 adb \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i 's/^#\?enable-dbus=.*/enable-dbus=yes/; s/^#\?publish-workstation=.*/publish-workstation=no/' /etc/avahi/avahi-daemon.conf \
 && useradd --system --home-dir /data --shell /usr/sbin/nologin mirrordesk
COPY --from=uxplay-build /usr/local/bin/uxplay /usr/local/bin/uxplay
WORKDIR /app
COPY requirements-docker.txt ./
RUN pip install --no-cache-dir --require-hashes --only-binary :all: -r requirements-docker.txt
COPY mirrordesk ./mirrordesk
RUN echo "UxPlay built from https://github.com/FDH2/UxPlay commit ${UXPLAY_COMMIT} (GPL-3.0-or-later) with MirrorDesk's change to its audio and video handling (third_party/uxplay/mirrordesk.patch); see licenses/UxPlay-LICENSE.txt" > /app/UXPLAY-SOURCE.txt
COPY diagnostics.py discovery_probe.py discovery_selftest.py THIRD_PARTY_NOTICES.md ./
COPY licenses ./licenses
# scrcpy-server (Apache-2.0), verified against its pinned SHA-256 at every use.
COPY third_party/scrcpy ./third_party/scrcpy
COPY docker/entrypoint.sh /usr/local/bin/mirrordesk-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/mirrordesk-entrypoint \
 && chmod 0755 /usr/local/bin/mirrordesk-entrypoint \
 && python -m compileall -q mirrordesk \
 && command -v setpriv >/dev/null \
 && uxplay -h | grep -q -- '-vrtp' \
 && adb version \
 && python -c "from mirrordesk import androidtools; androidtools.scrcpy_server()"
VOLUME ["/data"]
EXPOSE 8765/tcp
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('MIRRORDESK_PORT','8765'),timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/mirrordesk-entrypoint"]
