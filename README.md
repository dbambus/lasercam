# Lasercam
Raspberry Pi 4 as IP camera in the laser cutter

running in our lab on lasercam.lab.fablab.uni-erlangen.de

Used by VisiCam on brain (via `visicam-undistort`) and by the monitoring, see
[brain-docker-config](https://github.com/fau-fablab/brain-docker-config).

# Hardware

Raspberry Pi 4, with Raspberry Pi Camera Module v3 (IMX708), powered via PoE

Until November 2026 it had a Camera Module v2 (IMX219). The v3 has a 16:9 sensor and a different lens,
so after the change the camera calibration of VisiCam (`visicam-undistort` on brain) has to be done again.

The PoE supply has to deliver enough power: with too little the Pi throttles the CPU to 600 MHz and the
stream gets slower. Check with `vcgencmd get_throttled` (`0x0` is fine, bit 0 = under-voltage now,
bit 2 = throttled now) and `vcgencmd measure_clock arm`.

# OS

64bit Raspberry Pi OS Lite (Debian 13 "trixie"), flashed with the Raspberry Pi Imager:
hostname `lasercam`, user `pi`, enable SSH.

User/PW: see brain:/mnt/secrets/passwords/lasercam.fablab.fau.de.txt

Until 2026 the lasercam ran mjpg-streamer with `input_raspicam` on 32bit Raspi OS (bullseye),
see the [old README](https://github.com/fau-fablab/lasercam/blob/2f4cb4b/README.md).
Current Raspberry Pi OS no longer has the legacy camera stack, so `input_raspicam` does not work anymore.
`lasercam_server.py` replaces mjpg-streamer: it captures with picamera2 (libcamera) and serves the same
port, URLs and web interface as mjpg-streamer did. With the v2 it ran with the same settings as before
(3000x2100, quality 99, 5 fps), with the v3 it uses the full sensor (4608x2592, quality 90, 5 fps).

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
After changes to `lasercam.default` (e.g. the camera v3 settings) compare it with `/etc/default/lasercam` by hand.

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
and survive a restart, "Reset to defaults" reverts them.
The focus of the v3 is fixed with `--lens-position` in `/etc/default/lasercam`, "Lens Position" in
control.htm overrides it until "Reset to defaults". Careful: VisiCam detects the markers in the camera image.
