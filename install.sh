#!/bin/bash
# Set up the lasercam on a Raspberry Pi (Raspberry Pi OS Lite, 64-bit).
# Run as root from the checkout in /opt/lasercam:
#   sudo /opt/lasercam/install.sh
set -euo pipefail

REPO_DIR="/opt/lasercam"
USER_NAME="lasercam"
WWW_DIR="/usr/share/mjpg_streamer/www"
# web interface of mjpg-streamer, same version as in the old installation
MJPG_STREAMER_COMMIT="310b29f4a94c46652b20c4b7b6e5cf24e532af39"
NTP_SERVER="ntp0.fau.de"
CONFIG_TXT="/boot/firmware/config.txt"

if [ "$(id -u)" != 0 ]; then
	echo "[!] Please run as root (sudo)" 1>&2
	exit 1
fi

if [ "$(cd "$(dirname "${0}")" && pwd)" != "${REPO_DIR}" ]; then
	echo "[!] The repository has to be checked out in ${REPO_DIR}" 1>&2
	exit 1
fi

# dependencies: picamera2 for the camera, simplejpeg for the JPEG encoding
apt-get update
apt-get install -y --no-install-recommends python3-picamera2 python3-simplejpeg curl

# camera: detect the camera module automatically
if ! grep -q '^camera_auto_detect=1' "${CONFIG_TXT}"; then
	sed -i '/^camera_auto_detect=/d' "${CONFIG_TXT}"
	printf '\n[all]\ncamera_auto_detect=1\n' >> "${CONFIG_TXT}"
	echo "[!] ${CONFIG_TXT} changed, please reboot afterwards" 1>&2
fi

# dedicated user with access to the camera
id "${USER_NAME}" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent \
	--shell /usr/sbin/nologin --gid video "${USER_NAME}"

# web interface
rm -rf "${WWW_DIR}"
mkdir -p "$(dirname "${WWW_DIR}")"
curl -fsSL "https://codeload.github.com/jacksonliam/mjpg-streamer/tar.gz/${MJPG_STREAMER_COMMIT}" \
	| tar -xz -C "$(dirname "${WWW_DIR}")" --strip-components=2 \
		"mjpg-streamer-${MJPG_STREAMER_COMMIT}/mjpg-streamer-experimental/www"

# NTP: the Pi has no RTC
mkdir -p /etc/systemd/timesyncd.conf.d
printf '[Time]\nNTP=%s\n' "${NTP_SERVER}" > /etc/systemd/timesyncd.conf.d/fau.conf
systemctl restart systemd-timesyncd

# service, keep local settings in /etc/default/lasercam
[ -e /etc/default/lasercam ] || cp "${REPO_DIR}/lasercam.default" /etc/default/lasercam
cp "${REPO_DIR}/lasercam.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable lasercam.service
systemctl restart lasercam.service
