"""Pretend to be another simulator: send an aircraft position to the radar app.

Easiest: use the "Fake plane (test)" box in the app (left panel).
Or run it on its own, next to the app (Source > Mode: "Other simulator (UDP)"):

    python fake_plane.py                         # parked, heading 090
    python fake_plane.py --gs 250 --turn 1       # flying a slow circle
    python fake_plane.py --lat 51.47 --lon -0.45 --hdg 270 --alt 3000

Each packet is one JSON line, the same one a real simulator (or its helper)
should send:  {"lat": .., "lon": .., "hdg": .., "alt": .., "gs": .., "trk": ..}
"""
import argparse
import json
import math
import socket
import time

from udp_link import DEFAULT_PORT

DEFAULT_LAT, DEFAULT_LON = 28.5665, 77.1031     # Delhi (VIDP)


class FakePlane:
    """A plane that flies straight or turns, and sends its position over UDP."""

    def __init__(self, lat=DEFAULT_LAT, lon=DEFAULT_LON, hdg=90.0, alt=5000.0,
                 gs=0.0, turn=0.0, host="127.0.0.1", port=DEFAULT_PORT):
        self.lat, self.lon, self.hdg, self.alt = lat, lon, hdg % 360.0, alt
        self.gs, self.turn = gs, turn             # knots, degrees per second (+ right)
        self.host, self.port = host, port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def step(self, dt):
        """Fly dt seconds: ground speed along the heading, then turn."""
        d_nm = self.gs * dt / 3600.0
        h = math.radians(self.hdg)
        self.lat += d_nm * math.cos(h) / 60.0
        self.lon += d_nm * math.sin(h) / (60.0 * math.cos(math.radians(self.lat)))
        self.hdg = (self.hdg + self.turn * dt) % 360.0

    def message(self):
        return {"lat": round(self.lat, 7), "lon": round(self.lon, 7),
                "hdg": round(self.hdg, 2), "alt": self.alt, "gs": self.gs,
                "trk": round(self.hdg, 2)}

    def send(self):
        self.sock.sendto(json.dumps(self.message()).encode("utf-8"), (self.host, self.port))

    def close(self):
        self.sock.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="where the radar app runs")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--lat", type=float, default=DEFAULT_LAT)
    ap.add_argument("--lon", type=float, default=DEFAULT_LON)
    ap.add_argument("--hdg", type=float, default=90.0, help="true heading, degrees")
    ap.add_argument("--alt", type=float, default=5000.0, help="ft MSL")
    ap.add_argument("--gs", type=float, default=0.0, help="ground speed, kt (0 = parked)")
    ap.add_argument("--turn", type=float, default=0.0, help="turn rate, degrees/s")
    ap.add_argument("--hz", type=float, default=10.0, help="packets per second")
    a = ap.parse_args()

    plane = FakePlane(a.lat, a.lon, a.hdg, a.alt, a.gs, a.turn, a.host, a.port)
    dt = 1.0 / a.hz
    print(f"Sending to {a.host}:{a.port} at {a.hz:g} Hz. Ctrl+C to stop.")
    last_print = 0.0
    try:
        while True:
            plane.send()
            now = time.monotonic()
            if now - last_print > 2.0:
                print(f"  {plane.lat:.5f} {plane.lon:.5f}  hdg {plane.hdg:05.1f}  "
                      f"{plane.alt:.0f} ft  {plane.gs:.0f} kt")
                last_print = now
            time.sleep(dt)
            plane.step(dt)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        plane.close()


if __name__ == "__main__":
    main()
