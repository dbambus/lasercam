#!/usr/bin/env python3
"""
Lasercam MJPEG server - replacement for mjpg-streamer + input_raspicam.

Uses picamera2/libcamera (Raspberry Pi OS bookworm or newer, 64-bit) and offers
the same HTTP endpoints as mjpg-streamer/output_http:

  /?action=stream    MJPEG stream (multipart/x-mixed-replace, boundarydonotcross)
  /?action=snapshot  single JPEG (latest frame)
  /?action=command   change a camera setting (id=..&value=..), as in mjpg-streamer
  /program.json, /input_0.json   description of the settings for control.htm
  /<file>            static files from the www directory (index.html, control.htm, ...)
"""
import argparse
import errno
import io
import json
import math
import logging
import mimetypes
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

BOUNDARY = "boundarydonotcross"

log = logging.getLogger("lasercam")

# control types as in mjpg-streamer (V4L2_CTRL_TYPE_*), as evaluated by control.htm
CTRL_INT, CTRL_BOOL, CTRL_MENU, CTRL_BUTTON = 1, 2, 3, 4

# settings for control.htm. "ctrl" is the libcamera control or a special function
# (AutoExposure, fps, quality, reset). Floats are mapped to integers with "scale".
# Controls the camera does not know (e.g. autofocus on the v2) are hidden.
CONTROLS = [
    {"id": 1, "name": "Auto Exposure", "ctrl": "AutoExposure", "type": CTRL_BOOL, "default": 1},
    {"id": 2, "name": "Exposure Time (us, manual)", "ctrl": "ExposureTime", "min": 100, "max": 1000000, "step": 100, "default": 20000},
    {"id": 3, "name": "Analogue Gain (x10, manual)", "ctrl": "AnalogueGain", "scale": 10, "min": 10, "max": 160, "default": 10},
    {"id": 4, "name": "Exposure Compensation (EV x10)", "ctrl": "ExposureValue", "scale": 10, "min": -40, "max": 40, "default": 0},
    {"id": 5, "name": "Exposure Mode", "ctrl": "AeExposureMode", "menu": ["Normal", "Short", "Long"]},
    {"id": 6, "name": "Metering Mode", "ctrl": "AeMeteringMode", "menu": ["Centre-weighted", "Spot", "Matrix"]},
    {"id": 7, "name": "Auto White Balance", "ctrl": "AwbEnable", "type": CTRL_BOOL, "default": 1},
    {"id": 8, "name": "White Balance Mode", "ctrl": "AwbMode",
     "menu": ["Auto", "Incandescent", "Tungsten", "Fluorescent", "Indoor", "Daylight", "Cloudy"]},
    {"id": 9, "name": "Brightness (-100..100)", "ctrl": "Brightness", "scale": 100, "min": -100, "max": 100, "default": 0},
    {"id": 10, "name": "Contrast (x100)", "ctrl": "Contrast", "scale": 100, "min": 0, "max": 400, "default": 100},
    {"id": 11, "name": "Saturation (x100)", "ctrl": "Saturation", "scale": 100, "min": 0, "max": 400, "default": 100},
    {"id": 12, "name": "Sharpness (x100)", "ctrl": "Sharpness", "scale": 100, "min": 0, "max": 1600, "default": 100},
    {"id": 13, "name": "Noise Reduction", "ctrl": "NoiseReductionMode", "menu": ["Off", "Fast", "High Quality"], "default": 1},
    {"id": 14, "name": "Autofocus Mode", "ctrl": "AfMode", "menu": ["Manual", "Auto", "Continuous"]},
    {"id": 15, "name": "Lens Position (dioptre x100, manual)", "ctrl": "LensPosition", "scale": 100, "min": 0, "max": 1500, "default": 100},
    {"id": 16, "name": "Autofocus Trigger", "ctrl": "AfTrigger", "type": CTRL_BUTTON},
    {"id": 20, "name": "Framerate (fps)", "ctrl": "fps", "min": 1, "max": 15},
    {"id": 21, "name": "JPEG Quality", "ctrl": "quality", "min": 1, "max": 100},
    {"id": 30, "name": "Reset to defaults", "ctrl": "reset", "type": CTRL_BUTTON},
]
SPECIAL_CONTROLS = ("AutoExposure", "fps", "quality", "reset")


class CameraControls:
    """Change camera settings at runtime; they are saved in a JSON file."""

    def __init__(self, picam2, encoder, args):
        self.picam2, self.encoder = picam2, encoder
        self.state_file = args.state_file
        self._lock = threading.Lock()
        available = picam2.camera_controls
        self.controls = []
        for spec in CONTROLS:
            c = {"type": CTRL_MENU if "menu" in spec else CTRL_INT, "scale": 1, "step": 1, **spec}
            name = c["ctrl"]
            if name == "AutoExposure":
                if "ExposureTime" not in available:
                    continue
            elif name == "fps":
                c["default"] = int(round(args.fps))
            elif name == "quality":
                c["default"] = args.quality
            elif name not in SPECIAL_CONTROLS:
                if name not in available:
                    continue
                self._adopt_camera_range(c, available[name])
            if c["type"] == CTRL_MENU:
                c.setdefault("min", 0)
                c.setdefault("max", len(c["menu"]) - 1)
            c.setdefault("min", 0)
            c.setdefault("max", 1)
            c.setdefault("default", c["min"])
            self.controls.append(c)
        self.by_id = {c["id"]: c for c in self.controls}
        self.by_ctrl = {c["ctrl"]: c for c in self.controls}
        self.values = {c["id"]: c["default"] for c in self.controls if c["type"] != CTRL_BUTTON}
        self._load()

    @staticmethod
    def _adopt_camera_range(c, camera_range):
        """Adapt limits and default to what the camera reports."""
        lo, hi, default = camera_range
        number = (int, float)
        if c["type"] == CTRL_INT:
            if isinstance(lo, number) and not isinstance(lo, bool):
                c["min"] = max(c["min"], math.ceil(lo * c["scale"]))
            if isinstance(hi, number) and not isinstance(hi, bool):
                c["max"] = min(c["max"], math.floor(hi * c["scale"]))
        if "default" not in c and isinstance(default, number):
            c["default"] = int(round(default * c["scale"]))
        if c["type"] == CTRL_INT:
            c["default"] = min(max(c["default"], c["min"]), c["max"])

    def _load(self):
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
        except FileNotFoundError:
            saved = {}
        except (OSError, ValueError) as e:
            log.warning("Cannot read saved settings (%s): %s", self.state_file, e)
            saved = {}
        for name, value in saved.items():
            c = self.by_ctrl.get(name)
            if c and c["id"] in self.values:
                self.values[c["id"]] = self._clamp(c, int(value))
        # only apply changed values, everything else stays as configured by picamera2
        for c in self.controls:
            if c["type"] != CTRL_BUTTON and self.values[c["id"]] != c["default"]:
                self._apply(c)
        if saved:
            log.info("Loaded saved camera settings: %s", saved)

    def _save(self):
        changed = {c["ctrl"]: self.values[c["id"]] for c in self.controls
                   if c["id"] in self.values and self.values[c["id"]] != c["default"]}
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(changed, f, indent=1)
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("Settings not saved (%s): %s", self.state_file, e)

    @staticmethod
    def _clamp(c, value):
        if c["type"] == CTRL_BOOL:
            return 1 if value else 0
        return min(max(value, c["min"]), c["max"])

    def _value(self, ctrl, default=None):
        c = self.by_ctrl.get(ctrl)
        return self.values.get(c["id"], default) if c else default

    def _apply_exposure(self):
        # picamera2: ExposureTime/AnalogueGain = 0 means automatic, otherwise manual
        if self._value("AutoExposure", 1):
            self.picam2.set_controls({"ExposureTime": 0, "AnalogueGain": 0})
        else:
            self.picam2.set_controls({"ExposureTime": self._value("ExposureTime", 20000),
                                      "AnalogueGain": self._value("AnalogueGain", 10) / 10})

    def _apply(self, c):
        name, value = c["ctrl"], self.values.get(c["id"])
        if name in ("AutoExposure", "ExposureTime", "AnalogueGain"):
            self._apply_exposure()
        elif name == "fps":
            frame_us = int(1_000_000 / value)
            self.picam2.set_controls({"FrameDurationLimits": (frame_us, frame_us)})
        elif name == "quality":
            self.encoder.q = value
        elif c["type"] == CTRL_BOOL:
            self.picam2.set_controls({name: bool(value)})
        elif c["type"] == CTRL_MENU or c["scale"] == 1:
            self.picam2.set_controls({name: int(value)})
        else:
            self.picam2.set_controls({name: value / c["scale"]})

    def command(self, cid, value):
        """As in mjpg-streamer: 0 on success, -1 on error."""
        c = self.by_id.get(cid)
        if c is None:
            return -1
        with self._lock:
            if c["ctrl"] == "reset":
                for other in self.controls:
                    if other["id"] in self.values:
                        self.values[other["id"]] = other["default"]
                for other in self.controls:
                    if other["type"] != CTRL_BUTTON:
                        self._apply(other)
            elif c["ctrl"] == "AfTrigger":
                self.picam2.set_controls({"AfTrigger": 0})  # 0 = Start
            else:
                # manual values switch off the respective automatic mode, as one would expect
                if c["ctrl"] in ("ExposureTime", "AnalogueGain") and "AutoExposure" in self.by_ctrl:
                    self.values[self.by_ctrl["AutoExposure"]["id"]] = 0
                if c["ctrl"] == "LensPosition" and "AfMode" in self.by_ctrl:
                    self.values[self.by_ctrl["AfMode"]["id"]] = 0
                    self._apply(self.by_ctrl["AfMode"])
                self.values[cid] = self._clamp(c, value)
                self._apply(c)
            self._save()
        log.info("Setting %s -> %s", c["name"], self.values.get(cid, "triggered"))
        return 0

    def input_json(self):
        controls = []
        for c in self.controls:
            item = {"name": c["name"], "id": str(c["id"]), "type": str(c["type"]),
                    "min": str(c["min"]), "max": str(c["max"]), "step": str(c["step"]),
                    "default": str(c["default"]), "value": str(self.values.get(c["id"], 0)),
                    "dest": "0", "flags": "0", "group": "0"}
            if c["type"] == CTRL_MENU:
                item["menu"] = {str(i): label for i, label in enumerate(c["menu"])}
            controls.append(item)
        return {"controls": controls, "formats": []}


class FrameBuffer(io.BufferedIOBase):
    """Holds the latest encoded JPEG and wakes up waiting clients."""

    def __init__(self):
        super().__init__()
        self._cond = threading.Condition()
        self.frame = None
        self.timestamp = 0.0
        self.seq = 0

    def writable(self):
        return True

    def write(self, buf):
        frame = bytes(buf)
        with self._cond:
            self.frame = frame
            self.timestamp = time.time()
            self.seq += 1
            self._cond.notify_all()
        return len(frame)

    def wait_for_frame(self, last_seq=0, timeout=10.0):
        """Returns (seq, jpeg, timestamp) as soon as a frame newer than last_seq is available."""
        with self._cond:
            if not self._cond.wait_for(lambda: self.seq != last_seq, timeout):
                return None
            return self.seq, self.frame, self.timestamp


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = 30  # drop stalled clients after 30 s

    def version_string(self):
        return "MJPG-Streamer/0.2"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.client_address[0], fmt % args)

    def send_std_headers(self):
        # same as STD_HEADER in mjpg-streamer/plugins/output_http/httpd.h
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "Mon, 3 Jan 2000 12:34:56 GMT")

    def do_GET(self):
        url = urlsplit(self.path)
        action = parse_qs(url.query).get("action", [""])[0]
        # as in mjpg-streamer: actions match by prefix (stream_0, snapshot_0, ...), unknown
        # actions (e.g. ?action=control) just serve the file or index.html
        try:
            if action.startswith("stream"):
                self.send_stream()
            elif action.startswith("snapshot"):
                self.send_snapshot()
            elif action == "command":
                self.send_command(parse_qs(url.query))
            elif url.path == "/program.json":
                self.send_json(self.server.program_json)
            elif url.path in ("/input_0.json", "/input.json"):
                self.send_json(self.server.controls.input_json())
            elif url.path == "/output_0.json":
                self.send_json({"controls": []})
            else:
                self.send_file(url.path)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def send_stream(self):
        frames = self.server.frames
        self.send_response(200)
        self.send_std_headers()
        self.send_header("Content-Type", f"multipart/x-mixed-replace;boundary={BOUNDARY}")
        self.end_headers()
        self.wfile.write(f"--{BOUNDARY}\r\n".encode())
        log.info("Stream started for %s", self.client_address[0])
        seq = 0
        try:
            while True:
                got = frames.wait_for_frame(seq)
                if got is None:
                    break
                seq, frame, ts = got
                self.wfile.write(
                    f"Content-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n"
                    f"X-Timestamp: {ts:.6f}\r\n\r\n".encode()
                )
                self.wfile.write(frame)
                self.wfile.write(f"\r\n--{BOUNDARY}\r\n".encode())
        finally:
            log.info("Stream ended for %s", self.client_address[0])

    def send_snapshot(self):
        got = self.server.frames.wait_for_frame(0)
        if got is None:
            self.send_error(503, "no frame available")
            return
        _, frame, ts = got
        self.send_response(200)
        self.send_std_headers()
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("X-Timestamp", f"{ts:.6f}")
        self.end_headers()
        self.wfile.write(frame)

    def send_json(self, obj):
        data = json.dumps(obj, indent=1).encode()
        self.send_response(200)
        self.send_std_headers()
        self.send_header("Content-Type", "application/x-javascript")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_command(self, query):
        if not self.server.commands_enabled:
            self.send_error(403, "commands disabled")
            return
        try:
            cid = int(query["id"][0])
            value = int(query.get("value", ["0"])[0])
            res = self.server.controls.command(cid, value) if query.get("dest", ["0"])[0] == "0" else -1
        except (KeyError, ValueError):
            self.send_error(400, 'no valid GET variable "id=..." / "value=..."')
            return
        except Exception:
            log.exception("Error while setting %s", query)
            res = -1
        data = f"{query['id'][0]}: {res}".encode()
        self.send_response(200)
        self.send_std_headers()
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path):
        www = self.server.www
        rel = unquote(path).lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(www, rel)) if www else ""
        if not www or not full.startswith(www + os.sep) or not os.path.isfile(full):
            self.send_error(404, "file not found")
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_std_headers()
        self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class DualStackServer(ThreadingHTTPServer):
    """Listens on IPv6 and IPv4 (like mjpg-streamer on [::]:8080 and 0.0.0.0:8080)."""

    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(port):
    try:
        return DualStackServer(("::", port), Handler)
    except OSError as e:
        if e.errno not in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
            raise
        log.warning("IPv6 not available, listening on IPv4 only")
        return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def start_camera(args, frames):
    from libcamera import Transform
    from picamera2 import Picamera2
    from picamera2.encoders import JpegEncoder
    from picamera2.outputs import FileOutput

    cameras = Picamera2.global_camera_info()
    if not cameras:
        log.error("No camera found (check rpicam-hello --list-cameras, ribbon cable, config.txt)")
        sys.exit(1)
    log.info("Cameras found: %s", cameras)

    picam2 = Picamera2(args.camera)
    frame_us = int(1_000_000 / args.fps)
    config = picam2.create_video_configuration(
        main={"size": (args.width, args.height)},
        transform=Transform(hflip=args.hflip, vflip=args.vflip),
        buffer_count=args.buffers,
        controls={"FrameDurationLimits": (frame_us, frame_us)},
    )
    picam2.configure(config)
    active = picam2.camera_configuration()
    log.info("Camera configured: main=%s sensor=%s", active.get("main"), active.get("sensor"))
    encoder = JpegEncoder(q=args.quality, num_threads=args.threads)
    picam2.start_recording(encoder, FileOutput(frames))
    return picam2, encoder, cameras[args.camera].get("Model", "camera")


def watchdog(frames, timeout):
    """Exits the process if the camera stops delivering frames (systemd restarts it)."""
    last_seq, since = -1, time.monotonic()
    while True:
        time.sleep(1)
        if frames.seq != last_seq:
            last_seq, since = frames.seq, time.monotonic()
        elif time.monotonic() - since > timeout:
            log.error("No frame from the camera for %d s - exiting for restart", timeout)
            os._exit(1)


def parse_resolution(value):
    try:
        w, h = value.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid resolution: {value!r} (expected e.g. 3000x2100)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-r", "--resolution", type=parse_resolution, default=(3000, 2100))
    p.add_argument("-q", "--quality", type=int, default=99, help="JPEG quality 1-100")
    p.add_argument("-f", "--fps", type=float, default=5)
    p.add_argument("-p", "--port", type=int, default=8080)
    p.add_argument("-w", "--www", default="/usr/share/mjpg_streamer/www")
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--hflip", action="store_true")
    p.add_argument("--vflip", action="store_true")
    p.add_argument("--buffers", type=int, default=4)
    p.add_argument("--threads", type=int, default=4, help="threads for JPEG encoding")
    p.add_argument("--watchdog", type=int, default=20, help="seconds without a frame until restart")
    p.add_argument("--state-file", default="/var/lib/lasercam/controls.json",
                   help="settings changed via control.htm are saved here")
    p.add_argument("--no-commands", action="store_true", help="disable ?action=command (control.htm)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    args.width, args.height = args.resolution

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    frames = FrameBuffer()
    picam2, encoder, model = start_camera(args, frames)
    controls = CameraControls(picam2, encoder, args)
    threading.Thread(target=watchdog, args=(frames, args.watchdog), daemon=True).start()

    server = make_server(args.port)
    server.frames = frames
    server.controls = controls
    server.commands_enabled = not args.no_commands
    server.program_json = {
        "inputs": [{"id": "0", "name": f"libcamera ({model})", "plugin": "input_libcamera.so",
                    "args": f"-r {args.width}x{args.height} -q {args.quality} -f {args.fps:g}"}],
        "outputs": [],
    }
    server.www = os.path.realpath(args.www) if os.path.isdir(args.www) else None
    if server.www is None:
        log.warning("www directory %s missing - only ?action=stream/snapshot available", args.www)
    log.info("HTTP server running on port %d (%dx%d, q=%d, %.1f fps)",
             args.port, args.width, args.height, args.quality, args.fps)
    try:
        server.serve_forever()
    finally:
        picam2.stop_recording()


if __name__ == "__main__":
    main()
