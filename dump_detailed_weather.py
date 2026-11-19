"""Save everything FlightGear's Detailed Weather can give, to one text file.

Part 1: every property under /local-weather, /nasal/local_weather and
        /environment (long lists are shortened to their first items).
Part 2: everything Detailed Weather keeps only inside Nasal, found by
        running wxradar_dump.nas inside FlightGear.

Start FlightGear with:  --httpd=8081
Turn on Detailed Weather and press Apply, wait until the clouds are there.
Run:                    python dump_detailed_weather.py
Output:                 detailed_weather_dump.txt (next to this script)
Only reads: nothing in FlightGear's weather is changed.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

FG = "http://127.0.0.1:8081"   # not "localhost": Windows tries IPv6 first and stalls
HERE = os.path.dirname(os.path.abspath(__file__))
NASAL_FILE = os.path.join(HERE, "wxradar_dump.nas")
OUT_FILE = os.path.join(HERE, "detailed_weather_dump.txt")

ROOTS = ("/instrumentation/wxradar",      # thunderstorm echoes made by Detailed Weather
         "/local-weather", "/nasal/local_weather", "/environment")
SHOW_REPEATS = 2      # of tile[0..99] or cloud[0..999], show only the first ones
SHOW_ALL = {"storm", "tile"}   # these are few and all matter: show every one
MAX_TEXT = 200        # characters shown from a long value


def get_node(path):
    """One property and its direct children, from FlightGear's JSON API."""
    url = FG + "/json" + urllib.parse.quote(path, safe="/[]-_.") + "?d=1"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def child_path(parent, child):
    if child.get("path"):
        return child["path"]
    name, idx = child["name"], child.get("index", 0)
    return f"{parent.rstrip('/')}/{name}" + (f"[{idx}]" if idx else "")


def show_value(v):
    s = str(v)
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"... ({len(s)} chars)"


def walk(path, lines, indent=""):
    """Write a property tree; repeated children (tile[0..n]) are shortened."""
    try:
        node = get_node(path)
    except urllib.error.HTTPError as e:
        lines.append(f"{indent}{path}: not found ({e.code})")
        return
    children = node.get("children", [])
    if not children:
        return
    groups = {}                         # name -> children with that name, in order
    for c in children:
        groups.setdefault(c["name"], []).append(c)
    for name in sorted(groups):
        group = sorted(groups[name], key=lambda c: c.get("index", 0))
        if len(group) > SHOW_REPEATS and name not in SHOW_ALL:
            first, last = group[0].get("index", 0), group[-1].get("index", 0)
            lines.append(f"{indent}{name}[{first}..{last}]  ({len(group)} of them, "
                         f"first {SHOW_REPEATS} shown)")
            group = group[:SHOW_REPEATS]
        for c in group:
            label = c["name"] + (f"[{c['index']}]" if len(groups[name]) > 1 else "")
            if c.get("nChildren"):
                lines.append(f"{indent}{label}/")
                walk(child_path(path, c), lines, indent + "    ")
            else:
                kind = c.get("type", "")
                lines.append(f"{indent}{label} = {show_value(c.get('value'))}"
                             + (f"   ({kind})" if kind else ""))


def run_nasal_dump():
    """Run wxradar_dump.nas inside FlightGear and return its text."""
    with open(NASAL_FILE, "r", encoding="ascii") as f:
        code = f.read()
    run_id = f"run-{time.time_ns()}"    # tells this run's answer from an older one
    code = code.replace('var RUN_ID = "console";', f'var RUN_ID = "{run_id}";', 1)
    payload = {"name": "", "children": [{"name": "script", "index": 0, "value": code}]}
    req = urllib.request.Request(FG + "/run.cgi?value=nasal",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    for _ in range(30):                 # wait up to 15 s for it to finish
        try:
            done = get_node("/wxradar/debug/finished-run").get("value")
        except urllib.error.HTTPError:
            done = None
        if done == run_id:
            url = FG + "/json/wxradar/debug/nasal-dump?d=0"
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r).get("value", "")
        time.sleep(0.5)
    return ("No answer from the Nasal dump. Paste wxradar_dump.nas into "
            "Debug > Nasal Console in FlightGear, press Execute, then run this again.")


def prop(path):
    """Value of one property, or None if it is not there."""
    try:
        return get_node(path).get("value")
    except Exception:
        return None


def weather_summary():
    """Which weather scenario is chosen and which METAR it stands for.

    A scenario such as "Fair weather" is just a fixed METAR from
    Environment/environment.xml with the station "XXXX" (no real airport);
    the walk below shows only the first 2 scenarios, so it is looked up here.
    """
    chosen = prop("/environment/weather-scenario")
    out = ["=== SUMMARY ===", f"  weather-scenario = {chosen}"]
    try:
        kids = get_node("/environment/weather-scenarios").get("children", [])
    except Exception:
        kids = []
    for c in kids:
        p = child_path("/environment/weather-scenarios", c)
        if prop(p + "/name") == chosen:
            out.append(f"  scenario METAR   = {prop(p + '/metar') or '(none: live or manual input)'}")
            desc = " ".join(str(prop(p + "/description") or "").split())
            out.append(f"  description      = {desc}")
            break
    out.append(f"  metar[0]/data    = {prop('/environment/metar[0]/data')}")
    out.append(f"  station-id       = {prop('/environment/metar[0]/station-id')}"
               "   (nearest airport; 'XXXX' in the METAR means a made-up report)")
    out.append(f"  tile-management  = {prop('/local-weather/tmp/tile-management')}"
               f"   tile-type = {prop('/local-weather/tmp/tile-type')}")
    out.append("")
    return out


def main():
    lines = [f"Detailed Weather dump  {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    try:
        enabled = get_node("/nasal/local_weather/enabled").get("value")
    except Exception as e:
        print(f"Cannot reach FlightGear at {FG}: {e}\nStart it with --httpd=8081.")
        return
    if not enabled:
        print("Warning: Detailed Weather is not running. Turn it on and press Apply first.")
        lines.append("WARNING: Detailed Weather was NOT running during this dump.\n")

    lines += weather_summary()
    lines.append("#" * 70)
    lines.append("# PART 1: PROPERTIES (what telnet / HTTP can read directly)")
    lines.append("#" * 70)
    for root in ROOTS:
        print(f"Reading {root} ...")
        lines += ["", f"=== {root} ==="]
        walk(root, lines, "  ")

    print("Running the Nasal dump inside FlightGear ...")
    lines += ["", "#" * 70,
              "# PART 2: INSIDE NASAL (only reachable through a .nas script)",
              "#" * 70, ""]
    try:
        lines.append(run_nasal_dump())
    except Exception as e:
        lines.append(f"Nasal dump failed: {e}")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Saved {len(lines)} lines to {OUT_FILE}")


if __name__ == "__main__":
    main()
