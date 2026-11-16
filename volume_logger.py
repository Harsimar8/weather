"""Log FlightGear effect volumes every time the weather changes.

Reads /wxradar/volumes (filled by wxradar_export.nas) through FlightGear's
HTTP server and writes:
  volume_log.txt      - every change, with time, raw values and meaning
  volume_current.txt  - only the latest weather (overwritten each change)

Start FlightGear with:  --httpd=8080
Run:                    python volume_logger.py
Stop:                   Ctrl+C
"""

import json
import math
import os
import time
import urllib.request
from datetime import datetime

FG = "http://127.0.0.1:8080/json"   # not "localhost": Windows tries IPv6 first and stalls
POLL_S = 2.0
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "volume_log.txt")
CURRENT_FILE = os.path.join(HERE, "volume_current.txt")

SHAPES = {1: "circle", 2: "ellipse", 3: "rectangle"}
FIELDS = ("geometry", "latitude-deg", "longitude-deg", "r1-m", "r2-m", "heading-deg",
          "base-altitude-ft", "top-altitude-ft",
          "visibility", "visibility-flag", "rain", "rain-flag", "snow", "snow-flag",
          "turbulence", "turbulence-flag", "lift", "lift-flag",
          "saturation", "saturation-flag")
MEANING = {
    "geometry": "shape: 1 circle, 2 ellipse, 3 rectangle",
    "latitude-deg": "centre latitude",
    "longitude-deg": "centre longitude",
    "r1-m": "size 1 in metres (radius / half-length)",
    "r2-m": "size 2 in metres (radius / half-width)",
    "heading-deg": "rotation of the shape",
    "base-altitude-ft": "bottom of the volume, ft above sea level",
    "top-altitude-ft": "top of the volume, ft above sea level",
    "visibility": "visibility inside, metres",
    "rain": "rain 0..1  (drives radar colour)",
    "snow": "snow 0..1",
    "turbulence": "turbulence 0..1  (TURB mode)",
    "lift": "vertical air movement (thermal lift)",
    "saturation": "humidity / cloud wetness 0..1",
}


def get(path, depth=1):
    with urllib.request.urlopen(f"{FG}{path}?d={depth}", timeout=5) as r:
        return json.load(r)


def value(path):
    try:
        return get(path, 0).get("value")
    except Exception:
        return None


def read_volumes():
    root = get("/wxradar/volumes", 3)
    vols = []
    for node in root.get("children", []):
        if node["name"] == "volume":
            vols.append({c["name"]: c.get("value") for c in node.get("children", [])})
    return vols


def signature(vols):
    """What counts as a weather change. Position is left out because the
    volumes drift slowly with the wind; only big moves (about 1 km) count."""
    sig = []
    for v in vols:
        row = []
        for k in FIELDS:
            x = v.get(k)
            if k in ("latitude-deg", "longitude-deg") and x is not None:
                x = round(x, 2)
            elif isinstance(x, float):
                x = round(x, 3)
            row.append(x)
        sig.append(tuple(row))
    return tuple(sorted(sig, key=str))


def distance_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(a))


def level(x):
    if x <= 0.05: return "none"
    if x < 0.3: return "light"
    if x < 0.6: return "moderate"
    if x < 0.85: return "heavy"
    return "very heavy"


def radar_colour(rain):
    dbz = 20 + (rain - 0.22) * (10 / 0.265)          # same as radar.py to_dbz
    if dbz < 20: name = "nothing (below 20 dBZ)"
    elif dbz < 30: name = "GREEN"
    elif dbz < 40: name = "YELLOW"
    elif dbz < 50: name = "RED"
    else: name = "MAGENTA"
    return f"{dbz:.0f} dBZ -> {name}"


def explain(v, ac):
    """Plain-English lines for one volume."""
    on = lambda k: v.get(f"{k}-flag") == 1
    g = int(v.get("geometry") or 0)
    r1, r2 = (v.get("r1-m") or 0) / 1000, (v.get("r2-m") or 0) / 1000
    if g == 3:
        size = f"{2 * r1:.0f} x {2 * r2:.0f} km box"
    elif g == 2:
        size = f"ellipse {2 * r1:.0f} x {2 * r2:.0f} km"
    else:
        size = f"circle {2 * r1:.0f} km across"
    base, top = v.get("base-altitude-ft") or 0, v.get("top-altitude-ft") or 0
    lines = [f"Shape : {size}, turned {v.get('heading-deg', 0):.0f} deg",
             f"Height: {base:.0f} ft to {top:.0f} ft ({top - base:.0f} ft thick)"]

    if ac["lat"] is not None:
        d = distance_nm(ac["lat"], ac["lon"], v["latitude-deg"], v["longitude-deg"])
        inside_h = base <= (ac["alt"] or 0) <= top
        lines.append(f"Centre: {d:.1f} NM from aircraft; aircraft altitude "
                     f"{'IS' if inside_h else 'is NOT'} inside this height band")

    effects = []
    if on("visibility"):
        effects.append(f"visibility {v['visibility']:.0f} m")
    if on("rain"):
        effects.append(f"rain {v['rain']:.2f} ({level(v['rain'])})")
    if on("snow"):
        effects.append(f"snow {v['snow']:.2f} ({level(v['snow'])})")
    if on("turbulence"):
        effects.append(f"turbulence {v['turbulence']:.2f} ({level(v['turbulence'])})")
    if on("lift"):
        effects.append(f"lift {v['lift']:.1f}")
    if on("saturation"):
        effects.append(f"humidity {v['saturation']:.2f}")
    lines.append("Sets  : " + (", ".join(effects) if effects else "nothing"))

    # What kind of air this probably is
    if on("rain") and v["rain"] > 0.05:
        kind = "RAIN CELL / shower" + (" with turbulence (storm)" if on("turbulence")
                                        and v["turbulence"] > 0.3 else "")
    elif on("snow") and v["snow"] > 0.05:
        kind = "SNOW area"
    elif on("visibility") and v["visibility"] < 5000:
        kind = "CLOUD layer (low visibility inside)"
    elif base == 0 and on("saturation"):
        kind = "air below the cloud base (humidity zone)"
    elif on("turbulence") and v["turbulence"] > 0:
        kind = "TURBULENCE zone (clear air)"
    elif on("lift"):
        kind = "THERMAL / lift zone"
    else:
        kind = "weather setting zone"
    lines.append(f"Means : {kind}")

    rain = v["rain"] if on("rain") else 0.0
    lines.append(f"Radar : {radar_colour(rain) if rain > 0 else 'no echo (no rain in this volume)'}"
                 + ("; shows in TURB mode" if on("turbulence") and v["turbulence"] > 0 else ""))
    return lines


def report(vols, ac, weather):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = ["=" * 78,
           f"TIME     : {now}",
           f"WEATHER  : {weather}",
           f"AIRCRAFT : lat {ac['lat']}, lon {ac['lon']}, alt {ac['alt'] or 0:.0f} ft",
           f"VOLUMES  : {len(vols)}",
           "=" * 78]
    if not vols:
        out.append("No effect volumes. Detailed Weather is off, or not loaded yet,")
        out.append("or wxradar_export.nas is not running. Radar shows no Detailed Weather.")
    rainy = 0
    for i, v in enumerate(vols):
        out.append(f"\n--- volume {i} " + "-" * 60)
        out.append("  RAW VALUES" + " " * 25 + "MEANING")
        for k in FIELDS:
            x = v.get(k)
            shown = f"{x:.6g}" if isinstance(x, (int, float)) else str(x)
            note = MEANING.get(k, "1 = value above is used, 0 = ignore it"
                               if k.endswith("-flag") else "")
            if not k.endswith("-flag") and k in MEANING and v.get(f"{k}-flag") == 0:
                note += "  [flag 0: not used]"
            out.append(f"  {k:<20}{shown:>14}   {note}")
        out.append("  EXPLANATION")
        out += ["    " + line for line in explain(v, ac)]
        if v.get("rain-flag") == 1 and (v.get("rain") or 0) > 0.05:
            rainy += 1
    out.append("\nSUMMARY: " + (f"{rainy} rain volume(s) -> radar shows echoes"
                                if rainy else "no rain volumes -> radar stays empty"))
    return "\n".join(out) + "\n\n"


def main():
    print(f"Watching FlightGear effect volumes every {POLL_S:.0f} s ...")
    print(f"Log      : {LOG_FILE}\nCurrent  : {CURRENT_FILE}\nCtrl+C to stop.\n")
    last = None
    while True:
        try:
            vols = read_volumes()
            ac = {"lat": value("/position/latitude-deg"),
                  "lon": value("/position/longitude-deg"),
                  "alt": value("/position/altitude-ft")}
            detailed = value("/nasal/local_weather/enabled")
            code = value("/local-weather/tiles/code")
            weather = (f"Detailed Weather ON, tile type '{code}'" if detailed
                       else "Basic Weather (Detailed Weather OFF)")
            sig = (signature(vols), weather)
            if sig != last:
                text = report(vols, ac, weather)
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(text)
                with open(CURRENT_FILE, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"{datetime.now():%H:%M:%S}  weather changed: {len(vols)} volume(s), "
                      f"{weather} -> logged")
                last = sig
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"{datetime.now():%H:%M:%S}  cannot read FlightGear: {e}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
