"""Check the weather map and the radar.

Runs without a screen and without any simulator:  python test_place_map.py

1. Other simulator (UDP, fake_plane packets) WITHOUT FlightGear: placing is
   refused and the radar stays empty (weather only comes from FlightGear).
2. Simulated mode, aircraft held still on heading 090: clicks on the map at
   bearings all round the aircraft show at the right place on the radar.
"""
import json
import os
import socket
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

import main
from udp_link import DEFAULT_PORT

HDG = 90.0
RANGE_NM = 40


def send(sock, n=5):
    for _ in range(n):
        msg = {"lat": 28.5665, "lon": 77.1031, "hdg": HDG, "alt": 5000, "gs": 0}
        sock.sendto(json.dumps(msg).encode(), ("127.0.0.1", DEFAULT_PORT))
        time.sleep(0.02)


def strongest(win):
    """(relative bearing, NM, level) of the strongest radar return."""
    levels = win._scan()
    i, j = np.unravel_index(np.argmax(levels), levels.shape)
    sc = win.scanner
    return float(sc.rel_angles[i]), float(sc.bin_frac[j] * RANGE_NM), int(levels[i, j])


def main_test():
    app = QApplication(sys.argv)
    win = main.MainWindow()
    win.range.setCurrentText(str(RANGE_NM))
    win.wxr_btn.setChecked(True)
    win.auto_tilt.setChecked(True)
    win.source.setCurrentText(main.UDP_MODE)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send(sock)
    time.sleep(0.2)
    win.tick()
    app.processEvents()
    assert win.udp and win.udp.live(), "no UDP data received"
    print(f"Aircraft from UDP: heading {win.heading:.0f}, track {win.track:.0f}, "
          f"alt {win._altitudes()[0]:.0f} ft")
    assert abs(win.heading - HDG) < 0.1
    win._place(90, 15)
    win.tick()
    ok = not win.placed and not win.weather.cells and strongest(win)[2] == 0
    print(f"{'OK  ' if ok else 'FAIL'} UDP without FlightGear: placing refused, radar empty")
    if not ok:
        return 1
    win.udp.stop()

    # The map itself, in Simulated mode (aircraft held still, heading 090).
    win.source.setCurrentText("Simulated")
    win.scenario.setCurrentText("Clear")
    win.regenerate()
    win.hold_still.setChecked(True)
    win.sim_heading.setValue(int(HDG))
    win.heading = win.track = HDG
    win.tick()

    failures = 0
    for bearing, dist, should_show in [(120, 20, True), (60, 25, True), (90, 15, True),
                                       (200, 20, False), (270, 20, False), (0, 20, True)]:
        win._clear_placed()
        win.place_kind.setCurrentText("Thunderstorm")
        win._place(bearing, dist)
        win.tick()
        rel_want = (bearing - HDG + 540) % 360 - 180
        rel, rng, lvl = strongest(win)
        shown = lvl > 0
        ok = shown == should_show
        if shown and should_show:
            ok = abs(rel - rel_want) < 6 and abs(rng - dist) < 4
        failures += not ok
        print(f"{'OK  ' if ok else 'FAIL'} placed {bearing:03d}° {dist} NM "
              f"(relative {rel_want:+.0f}°): radar "
              + (f"echo at {rel:+.0f}°, {rng:.0f} NM, level {lvl}" if shown else "nothing")
              + ("" if should_show else "  (behind/abeam: radar sees 180° ahead only)"))

    # Many clicks: each one adds one more storm.
    win._clear_placed()
    for b in range(0, 360, 30):
        win._place(float(b), 20.0)
    n_cells, n_placed = len(win.weather.cells), len(win.placed)
    ok = n_cells == 12 and n_placed == 12
    failures += not ok
    print(f"{'OK  ' if ok else 'FAIL'} 12 clicks all round -> {n_placed} placed, "
          f"{n_cells} storms in the radar's weather")

    # Right click removes the one under the cursor.
    win._unplace(90.0, 20.0)
    ok = len(win.placed) == 11 and len(win.weather.cells) == 11
    failures += not ok
    print(f"{'OK  ' if ok else 'FAIL'} right click at 090° 20 NM -> {len(win.placed)} left")

    # The map gets the storms to draw.
    win._update_map(force=True)
    drawn = sum(1 for s in win.place_map.storms if s[5])
    ok = drawn == 11
    failures += not ok
    print(f"{'OK  ' if ok else 'FAIL'} map draws {drawn} placed storms")
    img = win.grab()
    img.save("test_place_map.png")
    print("Screenshot: test_place_map.png")

    print("ALL OK" if not failures else f"{failures} FAILED")
    return failures


if __name__ == "__main__":
    sys.exit(main_test())
