"""Follow the radar beam and print the weather at every nautical mile.

Uses the same code as the radar (weather.py precipitation_field, radar.py
beam shape and dBZ), with live FlightGear effect volumes.

  python beam_probe.py                  one beam straight ahead, 40 NM, tilt 0
  python beam_probe.py -b -30 -r 80 -t 1.5
                                        beam 30 deg left, 80 NM, tilt +1.5 deg
  python beam_probe.py --map            every beam (every 10 deg) as a grid
  python beam_probe.py --watch          repeat every 2 s (Ctrl+C to stop)

Writes the result to beam_probe.txt too. Ground clutter, terrain shadow and
rain attenuation are left out: this shows only the weather the beam meets.
"""
import argparse
import math
import os
import time
from datetime import datetime

import numpy as np

from radar import _OFFSETS, _WEIGHTS, CURVE_FT_PER_NM2, FT_PER_NM, DBZ_BANDS, to_dbz, dbz_to_z, z_to_dbz
from volume_logger import read_volumes, value
from weather import FlightGearWeather

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beam_probe.txt")
COLOURS = ("-", "GREEN", "YELLOW", "RED", "MAGENTA")
LETTER = {"-": ".", "GREEN": "G", "YELLOW": "Y", "RED": "R", "MAGENTA": "M",
          "SNOW": "s", "TURB": "T"}


def colour(dbz):
    level = sum(dbz >= b for b in DBZ_BANDS)
    return COLOURS[level]


def beam_msl(alt_ft, tilt_deg, nm):
    """Height (ft MSL) of the 7 beam samples at nm, bottom to top."""
    return [alt_ft + nm * FT_PER_NM * math.tan(math.radians(tilt_deg + o))
            + CURVE_FT_PER_NM2 * nm * nm for o in _OFFSETS]


def probe_point(wx, singles, x, y, heights):
    """Weather the beam sees at one point, like radar.py does it."""
    X, Y = np.array([x]), np.array([y])
    z_rain = z_snow = turb = 0.0
    hit = set()
    for w, h in zip(_WEIGHTS, heights):
        H = np.array([h])
        rain, snow, t = wx.precipitation_field(X, Y, H)
        if rain[0] > 0.01:
            z_rain += w * dbz_to_z(to_dbz(rain), np.array([True]))[0]
        if snow[0] > 0.01:
            z_snow += w * dbz_to_z(to_dbz(snow), np.array([True]))[0]
        turb += w * t[0]
        for i, one in singles:                     # which volumes the beam is inside
            r, s, tt = one.precipitation_field(X, Y, H)
            if r[0] > 0 or s[0] > 0 or tt[0] > 0:
                hit.add(i)
    rain_dbz = float(z_to_dbz(z_rain)) if z_rain else None
    snow_dbz = float(z_to_dbz(z_snow)) if z_snow else None
    return rain_dbz, snow_dbz, turb, sorted(hit)


def shown_as(rain_dbz, snow_dbz, turb, nm):
    c = colour(rain_dbz) if rain_dbz is not None else "-"
    if c == "-" and snow_dbz is not None and snow_dbz >= DBZ_BANDS[0]:
        c = "SNOW"
    turb_flag = turb > 0.5 and nm <= 40 and c != "-"
    return c, turb_flag


def setup():
    lat = value("/position/latitude-deg")
    lon = value("/position/longitude-deg")
    alt = value("/position/altitude-ft") or 0.0
    hdg = value("/orientation/heading-deg") or 0.0
    vols = read_volumes()
    wx = FlightGearWeather()
    wx.update(vols, (lat, lon), alt)                 # aircraft at x = y = 0
    singles = []
    for i, v in enumerate(vols):
        one = FlightGearWeather()
        one.update([v], (lat, lon), alt)
        singles.append((i, one))
    return lat, lon, alt, hdg, vols, wx, singles


def one_beam(args):
    lat, lon, alt, hdg, vols, wx, singles = setup()
    brg = (hdg + args.bearing) % 360
    out = ["=" * 92,
           f"TIME {datetime.now():%H:%M:%S}   aircraft {lat:.4f}, {lon:.4f}  alt {alt:.0f} ft  "
           f"heading {hdg:.0f}",
           f"BEAM {args.bearing:+.0f} deg from nose (true {brg:.0f})   tilt {args.tilt:+.2f} deg   "
           f"range {args.range} NM   volumes in FlightGear: {len(vols)}",
           "=" * 92,
           f"{'NM':>4} {'beam centre':>12} {'beam bottom-top (ft)':>22}  {'rain dBZ':>8} "
           f"{'snow dBZ':>8} {'turb':>5}  {'SCREEN':<10} volumes hit",
           "-" * 92]
    for nm in range(1, args.range + 1):
        b = math.radians(brg)
        x, y = nm * math.sin(b), nm * math.cos(b)
        h = beam_msl(alt, args.tilt, nm)
        r, s, t, hit = probe_point(wx, singles, x, y, h)
        c, tf = shown_as(r, s, t, nm)
        out.append(f"{nm:>4} {h[3]:>10.0f} ft {h[0]:>10.0f} - {h[-1]:<8.0f}  "
                   f"{'' if r is None else f'{r:.0f}':>8} {'' if s is None else f'{s:.0f}':>8} "
                   f"{t:>5.2f}  {c + (' +TURB' if tf else ''):<10} "
                   f"{', '.join(map(str, hit)) if hit else ''}")
    out.append("-" * 92)
    out.append("beam centre = height of the middle of the 3.5 deg beam (ft above sea level)")
    out.append("volumes hit = index of every effect volume the beam is inside "
               "(see volume_current.txt)")
    return "\n".join(out)


def every_beam(args):
    lat, lon, alt, hdg, vols, wx, singles = setup()
    bearings = list(range(-90, 91, args.step))
    out = ["=" * 80,
           f"TIME {datetime.now():%H:%M:%S}   alt {alt:.0f} ft  heading {hdg:.0f}  "
           f"tilt {args.tilt:+.2f}  volumes {len(vols)}",
           "Each column is one beam (deg from nose), each row one NM.",
           "G green  Y yellow  R red  M magenta  s snow  T turbulence  . nothing",
           "=" * 80,
           "  NM | " + " ".join(f"{b:>4}" for b in bearings)]
    for nm in range(args.range, 0, -1):            # far at the top, like the screen
        h = beam_msl(alt, args.tilt, nm)
        row = []
        for rel in bearings:
            b = math.radians(hdg + rel)
            r, s, t, _ = probe_point(wx, [], nm * math.sin(b), nm * math.cos(b), h)
            c, tf = shown_as(r, s, t, nm)
            row.append(f"{'T' if tf else LETTER[c]:>4}")
        out.append(f"{nm:>4} | " + " ".join(row))
    out.append("     +" + "-" * (5 * len(bearings)))
    out.append("       aircraft at the bottom centre (0 = straight ahead)")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("-b", "--bearing", type=float, default=0.0, help="deg from nose, - left / + right")
    p.add_argument("-r", "--range", type=int, default=40, help="NM to follow the beam")
    p.add_argument("-t", "--tilt", type=float, default=0.0, help="antenna tilt in degrees")
    p.add_argument("--map", action="store_true", help="show every beam as a grid")
    p.add_argument("--step", type=int, default=10, help="deg between beams in --map")
    p.add_argument("--watch", action="store_true", help="repeat every 2 s")
    args = p.parse_args()
    while True:
        try:
            text = every_beam(args) if args.map else one_beam(args)
        except Exception as e:
            text = f"cannot read FlightGear: {e}"
        print(text + "\n")
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        if not args.watch:
            break
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
