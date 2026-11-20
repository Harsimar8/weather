"""Move FlightGear's aircraft smoothly to another simulator's plane.

The other plane's position comes about 10 times a second. Setting FlightGear
only then makes its aircraft jump (at 480 kt: 25 m per jump), and the main
FlightGear link is sometimes busy for a second reading clouds and volumes.

So this uses its own telnet connection and its own thread, and sets the
position up to 30 times a second. Between packets it moves the aircraft on
along its track at its ground speed (prediction), so the motion is smooth.

Each update ends with a question, and the next update waits for the answer.
A slow FlightGear (few frames per second) then gets fewer updates instead of
a growing queue of old positions, which would make it lag and jerk.

FlightGear must run with --fdm=null, or its flight model moves it back.
"""
import math
import socket
import threading
import time

RATE_HZ = 30.0
MAX_PREDICT_S = 1.0      # packets stopped: do not fly on for ever


class FGDriver:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.target = None       # dict lat, lon, alt_ft, heading, gs_kt, track, t
        self.error = ""
        self.sent = 0
        self.fps = None          # FlightGear frames per second (from its answers)
        self._sock = None
        self._reader = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._close()

    def _close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._reader = None

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=5)
        s.sendall(b"data\r\n")              # raw mode: no prompts
        self._sock = s
        self._reader = s.makefile("r", encoding="utf-8", newline="\n")

    def _wait_answer(self):
        """Read the answer to the question at the end of an update."""
        line = self._reader.readline()
        if not line:
            raise OSError("FlightGear closed the connection")
        try:
            self.fps = float(line.strip())
        except ValueError:
            pass

    def position(self, now=None):
        """Where the plane is now: last packet moved on by its speed."""
        t = self.target
        if not t:
            return None
        dt = min(MAX_PREDICT_S, max(0.0, (now or time.monotonic()) - t["t"]))
        d_nm = t.get("gs_kt", 0.0) * dt / 3600.0
        trk = math.radians(t.get("track", t["heading"]))
        lat = t["lat"] + d_nm * math.cos(trk) / 60.0
        lon = t["lon"] + d_nm * math.sin(trk) / (60.0 * math.cos(math.radians(t["lat"])))
        return lat, lon, t["alt_ft"], t["heading"]

    def _loop(self):
        period = 1.0 / RATE_HZ
        while not self._stop.is_set():
            start = time.monotonic()
            pos = self.position()
            if pos:
                try:
                    if not self._sock:
                        self._connect()
                    lat, lon, alt, hdg = pos
                    self._sock.sendall((
                        f"set /position/latitude-deg {lat:.8f}\r\n"
                        f"set /position/longitude-deg {lon:.8f}\r\n"
                        f"set /position/altitude-ft {alt:.1f}\r\n"
                        f"set /orientation/heading-deg {hdg:.2f}\r\n"
                        "set /orientation/pitch-deg 0\r\n"
                        "set /orientation/roll-deg 0\r\n"
                        "get /sim/frame-rate\r\n").encode())
                    self._wait_answer()          # FlightGear has done this update
                    self.sent += 1
                    self.error = ""
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    self.error = str(exc)
                    self._close()
                    self._stop.wait(1.0)
            self._stop.wait(max(0.0, period - (time.monotonic() - start)))
