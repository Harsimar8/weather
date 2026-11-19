"""Aircraft position from another simulator, received over UDP.

The other simulator (or a small helper next to it, or fake_plane.py for
testing) sends one JSON line per packet, several times a second:

    {"lat": 28.5665, "lon": 77.1031, "hdg": 90, "alt": 5000}

Needed:   lat, lon (degrees), hdg (true heading, degrees), alt (ft MSL)
Optional: gs (ground speed, kt), trk (true track, degrees),
          gnd (ground elevation under the aircraft, ft)
"""
import json
import socket
import threading
import time

DEFAULT_PORT = 49100
STALE_S = 3.0          # no packet for this long: the link counts as lost


class UDPLink:
    def __init__(self, port=DEFAULT_PORT, host="0.0.0.0"):
        self.host, self.port = host, port
        self.state = {}             # last good position (see module doc)
        self.last_rx = 0.0          # time of the last good packet
        self.sender = None          # (ip, port) the packets come from
        self.packets = 0
        self.error = ""
        self._sock = None
        self._thread = None
        self._run = False

    def start(self):
        self.stop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))       # raises OSError if the port is taken
        sock.settimeout(0.5)
        self._sock, self._run, self.error = sock, True, ""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._run = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._sock:
            self._sock.close()
            self._sock = None

    @property
    def listening(self):
        return self._sock is not None

    def live(self):
        """True while packets keep coming."""
        return bool(self.state) and time.monotonic() - self.last_rx < STALE_S

    def _loop(self):
        while self._run:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue        # Windows reports old ICMP errors here: harmless
            except OSError as exc:
                self.error = str(exc)
                break
            try:
                msg = json.loads(data.decode("utf-8"))
                s = {"lat": float(msg["lat"]), "lon": float(msg["lon"]),
                     "heading": float(msg["hdg"]) % 360.0, "alt_ft": float(msg["alt"])}
                for key, name in (("gs", "gs_kt"), ("trk", "track"), ("gnd", "ground_ft")):
                    if msg.get(key) is not None:
                        s[name] = float(msg[key])
            except (ValueError, KeyError, TypeError) as exc:
                self.error = f"bad packet: {exc}"
                continue
            self.state, self.sender = s, addr
            self.last_rx = time.monotonic()
            self.packets += 1
            self.error = ""
