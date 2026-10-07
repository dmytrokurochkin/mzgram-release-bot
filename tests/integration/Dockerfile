# Debian 12 with systemd, to run install.sh the way it runs on a server.
# git and python3-venv are left out on purpose: install.sh must install them.
FROM debian:12

ENV container=docker
RUN apt-get update \
    && apt-get install -y --no-install-recommends systemd systemd-sysv dbus curl ca-certificates python3 python3-aiohttp procps \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl mask getty.target console-getty.service systemd-logind.service systemd-udevd.service systemd-udev-trigger.service

STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
