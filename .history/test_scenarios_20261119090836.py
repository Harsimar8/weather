"""Try every Weather Conditions scenario in Detailed Weather and report what
FlightGear builds and what the radar app would show.

For each scenario this does what the Weather dialog's Apply does (select the
scenario, copy its tile settings, set its METAR, clear and restart Detailed
Weather), waits until the weather is built, then reads:
  - tile mode / tile type / tile code
  - the effect volumes (rain, turbulence, lift)
  - which source the radar app would use:
      FG VOLUMES   at least one volume (real FlightGear positions)
      CLOUD ECHO   no volumes, but cloud positions readable (made-up strength)
      FG METAR     no volumes, no cloud positions: storms from the METAR text

Start FlightGear with --httpd=8081, Detailed Weather on, wxradar_export.nas
running. Run:  python test_scenarios.py   (about 1 minute per scenario)
Output:        scenario_test.txt (next to this script)
It CHANGES FlightGear's weather; the scenario you had is selected again at the end.
"""


import json
import os
import time
import urllib.request
from datetime import datetime

import volume_logger as vl
from weather import parse_metar, TILE_PEAK

FG = "http://127.0.0.1:8081"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(HERE, "scenario_test.txt")
SKIP = {"Live data", "Manual input"}   # need internet / typed METAR
MAX_WAIT_S = 70
STABLE_POLLS = 4                         # same volume count this many polls = built

APPLY = r'''
var name = "%s";
var wsn = props.globals.getNode("/environment/weather-scenarios");
foreach (var s; wsn.getChildren("scenario")) {
    if (s.getNode("name").getValue() != name) continue;
    setprop("/environment/params/metar-updates-environment", 1);
    setprop("/environment/realwx/enabled", 0);
    setprop("/environment/config/enabled", 1);
    var lwp = s.getNode("local-weather");
    if (lwp != nil) props.copy(lwp, props.globals.getNode("/local-weather/tmp", 1));
    else {
        setprop("/local-weather/tmp/tile-type", "manual");
        setprop("/local-weather/tmp/tile-management", "METAR");
    }
    setprop("/environment/weather-scenario", name);
    var m = s.getNode("metar");
    if (m != nil) setprop("/environment/metar/data", m.getValue());
}
if (getprop("/nasal/local_weather/enabled")) local_weather.clear_all();
setprop("/local-weather/tiles/code", "");   # stays "" if nothing gets built
setprop("/nasal/local_weather/enabled", "false");
setprop("/nasal/local_weather/enabled", "true");
settimer(func local_weather.set_tile(), 3.0);
'''


def run_nasal(code):
    body = {"name": "", "children": [{"name": "script", "index": 0, "value": code}]}
    req = urllib.request.Request(FG + "/run.cgi?value=nasal",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    urllib.request.urlopen(req, timeout=15).read()


def scenario_names():
    root = vl.get("/environment/weather-scenarios", 2)
    names = []
    for c in root.get("children", []):
        if c["name"] == "scenario":
            for k in c.get("children", []):
                if k["name"] == "name":
                    names.append(k.get("value"))
    return names


def cloud_positions_readable():
    root = vl.get("/local-weather/clouds", 1)
    return any(c["name"] == "tile" for c in root.get("children", []))


def wait_built():
    """Wait until Detailed Weather has finished and the volume count is steady."""
    start, last, same = time.time(), None, 0
    time.sleep(6)                                    # set_tile runs 3 s after Apply
    while time.time() - start < MAX_WAIT_S:
        try:
            n = len(vl.read_volumes())
        except Exception:
            n = None
        busy = any(vl.value(f"/local-weather/tmp/{k}") not in (None, "idle")
                   for k in ("thread-status", "convective-status", "presampling-status"))
        same = same + 1 if (n == last and not busy) else 0
        last = n
        if same >= STABLE_POLLS:
            break
        time.sleep(2)
    return time.time() - start


def on(v, k):
    return v.get(f"{k}-flag") == 1


def summarize(name):
    vols = vl.read_volumes()
    code = vl.value("/local-weather/tiles/code")
    mode = vl.value("/local-weather/tmp/tile-management")
    ttype = vl.value("/local-weather/tmp/tile-type")
    metar = (vl.value("/environment/metar/data") or "").strip()

    rain = [v["rain"] for v in vols if on(v, "rain") and (v.get("rain") or 0) > 0]
    snow = [v["snow"] for v in vols if on(v, "snow") and (v.get("snow") or 0) > 0]
    shown = [r for r in rain if 0.15 + 1.05 * r >= 0.22]      # >= 20 dBZ on the radar
    turb = [v["turbulence"] for v in vols if on(v, "turbulence")]
    turb_shown = [v for v in vols if on(v, "turbulence") and v["turbulence"] > 0.5
                  and on(v, "rain") and v["rain"] > 0]
    lift = [v for v in vols if on(v, "lift")]

    if vols:
        source = f"FG VOLUMES {len(vols)}  (real FlightGear positions)"
    elif code and cloud_positions_readable():
        peak = TILE_PEAK.get(code, 0.6)
        source = (f"CLOUD ECHO  (made-up cells on clouds, strength {peak}"
                  + (", nothing shown" if peak <= 0 else "") + ")")
    else:
        p = parse_metar(metar)
        what = p[0] if p else "nothing"
        source = f"FG METAR  -> '{what}'" + (
            "  (empty radar)" if what in ("Clear", "nothing") else "  (made-up places)")

    def rng(xs):
        return f"{min(xs):.2f}-{max(xs):.2f}" if xs else "-"

    if not code:
        return [f"### {name}", "  NOT BUILT: Detailed Weather did not build anything "
                "(paused, or set_tile aborted - see FlightGear console)", ""]
    return [
        f"### {name}",
        f"  METAR      : {metar}",
        f"  FlightGear : mode '{mode}', tile-type '{ttype}', tiles/code '{code}'",
        f"  Volumes    : {len(vols)} total | rain {len(rain)} (shown {len(shown)}, "
        f"rain {rng(rain)}) | snow {len(snow)} | turbulence {len(turb)} "
        f"({rng(turb)}, shown in WX+T {len(turb_shown)}) | lift {len(lift)}",
        f"  Radar app  : {source}",
        "",
    ]


def paused():
    return any(vl.value(p) in (True, 1, "true") for p in ("/sim/freeze/master", "/sim/freeze/clock"))


def main():
    if paused():
        print("FlightGear is PAUSED (press p). Detailed Weather builds nothing while "
              "paused, so this test would only show 0 volumes. Unpause and run again.")
        return
    before = vl.value("/environment/weather-scenario")
    names = [n for n in scenario_names() if n not in SKIP]
    lines = [f"Detailed Weather scenario test  {datetime.now():%Y-%m-%d %H:%M:%S}",
             f"Aircraft at lat {vl.value('/position/latitude-deg')}, "
             f"lon {vl.value('/position/longitude-deg')}", ""]
    for name in names:
        print(f"{datetime.now():%H:%M:%S}  {name} ...", flush=True)
        try:
            run_nasal(APPLY % name)
            took = wait_built()
            block = summarize(name)
            block[-1:-1] = [f"  Built in   : about {took:.0f} s"]
        except Exception as e:
            block = [f"### {name}", f"  FAILED: {e}", ""]
        print("\n".join(block), flush=True)
        lines += block
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if before:
        print(f"Selecting '{before}' again ...")
        run_nasal(APPLY % before)
    print(f"Done: {OUT_FILE}")


if __name__ == "__main__":
    main()
