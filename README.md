# Lasercam
Raspberry Pi 4 as IP camera in the laser cutter

running in our lab on lasercam.lab.fablab.uni-erlangen.de

Used by VisiCam on brain (via `visicam-undistort`) and by the monitoring, see
[brain-docker-config](https://github.com/fau-fablab/brain-docker-config).

# Hardware

Raspberry Pi 4, with Raspberry Pi Camera Module v2 (IMX219), powered via PoE

# OS

64bit Raspberry Pi OS Lite (Debian 13 "trixie"), flashed with the Raspberry Pi Imager:
hostname `lasercam`, user `pi`, enable SSH.

User/PW: see brain:/mnt/secrets/passwords/lasercam.fablab.fau.de.txt

Until 2026 the lasercam ran mjpg-streamer with `input_raspicam` on 32bit Raspi OS (bullseye),
see the [old README](https://github.com/fau-fablab/lasercam/blob/2f4cb4b/README.md).
Current Raspberry Pi OS no longer has the legacy camera stack, so `input_raspicam` does not work anymore.
`lasercam_server.py` replaces mjpg-streamer: it captures with picamera2 (libcamera) and serves the same
port, URLs and web interface as mjpg-streamer did, with the same settings (3000x2100, quality 99, 5 fps).

# Setup

Check out this repository in `/opt/lasercam` and run the install script:

```sh
sudo apt install -y git
sudo git clone https://github.com/fau-fablab/lasercam.git /opt/lasercam
sudo /opt/lasercam/install.sh
```

It installs `python3-picamera2`, enables the camera auto detection in `config.txt`, creates the user
`lasercam`, downloads the web interface of mjpg-streamer to `/usr/share/mjpg_streamer/www`
(same version as before), sets the NTP server to `ntp0.fau.de` (the Pi has no RTC)
and enables the service `lasercam`.

It also enables automatic updates with `unattended-upgrades`: the Debian security and point release updates
(default config) and the Raspberry Pi archive (kernel, firmware, camera stack), with a reboot at 04:00 if needed.
Check with `sudo unattended-upgrade --dry-run --debug`.

Settings (resolution, quality, fps, ...) are in `/etc/default/lasercam`, the install script does not overwrite them.

To update: `cd /opt/lasercam && sudo git pull && sudo ./install.sh`

```sh
systemctl status lasercam
journalctl -u lasercam -f
```

## Usage

Fetch JPG from:  http://lasercam.lab.fablab.uni-erlangen.de:8080/?action=snapshot   (URL is only accessible from internal network)

MJPEG stream: http://lasercam.lab.fablab.uni-erlangen.de:8080/?action=stream

Camera settings: http://lasercam.lab.fablab.uni-erlangen.de:8080/control.htm (as in mjpg-streamer, also via
`?action=command&id=...&value=...`, IDs see `/input.json`). Changes are saved in `/var/lib/lasercam/controls.json`
and survive a restart, "Reset to defaults" reverts them. Careful: VisiCam detects the markers in the camera image.
