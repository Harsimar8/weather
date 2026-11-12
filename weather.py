"""Weather model: the storm cells that the radar scans.

Coordinates are in nautical miles (NM): x = east, y = north.
"""
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

SCENARIOS = [
    "Clear",
    "Scattered Cb",
    "Squall line",
    "Embedded cell in rain",
    "Isolated hook cell",
]


@dataclass
class Cell:
    x: float
    y: float
    sx: float          # size along its long axis (NM)
    sy: float          # size along its short axis (NM)
    rot: float         # orientation (radians)
    peak: float        # core strength (about 1.0 = red core)
    turb: float = 0.0  # turbulence (shown in TURB mode)
    phase: float = 0.0
    rate: float = 0.0  # how fast it grows and decays
    key: object = None  # FlightGear cloud this cell sits on (Detailed Weather)


# Radar strength of Detailed Weather clouds for each weather tile type.
# Fair-weather clouds give no echo; other types get a moderate one.
TILE_PEAK = {
    "high_pressure_core": 0.0, "high_pressure": 0.0, "high_pressure_border": 0.0,
    "low_pressure_border": 0.5, "low_pressure": 0.7, "low_pressure_core": 0.9,
    "warmfront": 0.7, "coldfront": 1.1, "thunderstorms": 1.2,
}
STORMY_TILES = {"coldfront", "thunderstorms"}


class ValueNoise:
    """Smooth 2D noise, used to give rain areas ragged, natural edges."""

    def __init__(self, seed, size=256):
        self.size = size
        self.grid = np.random.default_rng(seed + 99).random((size, size))

    def __call__(self, x, y):
        xi = np.floor(x).astype(np.int64)
        yi = np.floor(y).astype(np.int64)
        fx, fy = x - xi, y - yi
        u = fx * fx * (3 - 2 * fx)
        v = fy * fy * (3 - 2 * fy)
        s, g = self.size, self.grid
        x0, x1, y0, y1 = xi % s, (xi + 1) % s, yi % s, (yi + 1) % s
        top = g[x0, y0] * (1 - u) + g[x1, y0] * u
        bot = g[x0, y1] * (1 - u) + g[x1, y1] * u
        return top * (1 - v) + bot * v


class WeatherModel:
    def __init__(self, seed=7):
        self.rng = np.random.default_rng(seed)
        self.noise = ValueNoise(seed)
        self.cells: list[Cell] = []
        self.scenario = "Scattered Cb"
        self.intensity = 1.0   # storm strength slider (0.3 .. 1.5)
        self.coverage = 1.0    # how many cells (0.5 .. 2.0)
        self.wind_from = 270.0  # degrees, direction wind blows FROM
        self.wind_kt = 25.0
        self.time = 0.0
        self.drift = np.zeros(2)
        self.generation = 0    # goes up each time new storms are made
        self.precip = "Rain"   # what falls: one of PRECIP_KINDS

    @property
    def precip_mult(self):
        """(rain, snow) share of each storm's strength, for the radar."""
        return PRECIP_MULT.get(self.precip, (1.0, 0.0))

    # ------------------------------------------------------------ scenarios
    def generate(self, scenario, ax, ay, heading):
        """Create storm cells ahead of the aircraft at (ax, ay)."""
        self.scenario = scenario
        self.cells = []
        self.drift[:] = 0.0
        self.generation += 1
        h = math.radians(heading)
        rnd = self.rng

        def add(right, ahead, sx, sy, rot_deg, peak, turb=0.0):
            x = ax + right * math.cos(h) + ahead * math.sin(h)
            y = ay - right * math.sin(h) + ahead * math.cos(h)
            self.cells.append(Cell(
                x, y, sx, sy, math.radians(rot_deg) + h, peak, turb,
                phase=rnd.uniform(0, 2 * math.pi), rate=rnd.uniform(0.002, 0.01)))

        if scenario == "Scattered Cb":
            for _ in range(max(1, round(7 * self.coverage))):
                sx = rnd.uniform(3, 8)
                peak = rnd.uniform(0.8, 1.3)
                add(rnd.uniform(-60, 60), rnd.uniform(15, 140), sx,
                    sx * rnd.uniform(0.55, 0.95), rnd.uniform(0, 180), peak,
                    1.0 if peak > 1.1 else 0.0)
        elif scenario == "Squall line":
            n = max(4, round(16 * self.coverage))
            for i in range(n):
                right = -80 + 160 * i / (n - 1)
                ahead = 50 + 6 * math.sin(i * 0.7)
                peak = rnd.uniform(0.8, 1.4)
                add(right, ahead, 6, 3.5, 15, peak, 1.0 if peak > 1.15 else 0.0)
        elif scenario == "Embedded cell in rain":
            add(0, 60, 55, 22, 0, 0.6)  # wide, steady rain area
            for _ in range(max(1, round(2 * self.coverage))):
                add(rnd.uniform(-30, 30), rnd.uniform(45, 80), 3.5, 3, 0, 1.35, 1.0)
        elif scenario == "Isolated hook cell":
            add(5, 35, 6, 2.5, 20, 1.25, 1.0)   # main body
            add(11, 33, 2, 2, 0, 0.9, 1.0)      # hook curling round
            add(13, 30, 1.8, 1.8, 0, 0.8, 1.0)
            add(11, 27.5, 1.6, 1.6, 0, 0.7, 0.0)

    def add_microburst(self, ax, ay, heading, ahead=3.5):
        """A small, violent storm cell (downburst) ahead: for testing the PWS."""
        h = math.radians(heading)
        self.cells.append(Cell(ax + ahead * math.sin(h), ay + ahead * math.cos(h),
                               1.3, 1.1, 0.0, 1.45, 1.0, phase=0.0, rate=0.0))

    def sync_clouds(self, clouds, code):
        """Put a cell on each Detailed Weather cloud from FlightGear.

        clouds maps a cloud id to its (x, y) in NM. Every cell is moved to its
        cloud's position; new clouds get a cell, vanished ones lose theirs.
        """
        if code != self.scenario:
            self.scenario, self.cells = code, []
        base = TILE_PEAK.get(code, 0.6)
        if base <= 0:
            self.cells = []
            return
        self.cells = [c for c in self.cells if c.key in clouds]
        # Keep each cell exactly on its FlightGear cloud, so they never drift apart.
        for c in self.cells:
            c.x, c.y = clouds[c.key]
        have = {c.key for c in self.cells}
        rnd = self.rng
        for key, (x, y) in clouds.items():
            if key in have:
                continue
            size = rnd.uniform(2.0, 3.5)
            peak = base * rnd.uniform(0.6, 1.15)
            self.cells.append(Cell(
                x, y, size, size * rnd.uniform(0.7, 1.0), rnd.uniform(0, math.pi),
                peak, 1.0 if code in STORMY_TILES and peak > 1.0 else 0.0,
                phase=rnd.uniform(0, 2 * math.pi), rate=rnd.uniform(0.002, 0.01),
                key=key))

    def strike(self, x, y):
        """Lightning hit near (x, y): the nearest cell becomes a storm core."""
        near = min(self.cells, key=lambda c: (c.x - x) ** 2 + (c.y - y) ** 2,
                   default=None)
        if near and math.hypot(near.x - x, near.y - y) < 5.0:
            near.peak = max(near.peak, 1.35)
            near.turb = 1.0

    # ------------------------------------------------------------- dynamics
    def step(self, dt):
        """Advance time by dt seconds: cells drift with the wind."""
        self.time += dt
        to = math.radians(self.wind_from + 180.0)
        dist = self.wind_kt / 3600.0 * dt
        dx, dy = dist * math.sin(to), dist * math.cos(to)
        for c in self.cells:
            if c.key is not None:
                continue  # FlightGear cloud: its position comes only from FlightGear
            c.x += dx
            c.y += dy
        self.drift += (dx, dy)

    def field(self, X, Y):
        """Return (reflectivity, turbulence) arrays for world points X, Y."""
        refl = np.zeros_like(X)
        turb = np.zeros_like(X)
        for c in self.cells:
            dx, dy = X - c.x, Y - c.y
            cr, sr = math.cos(c.rot), math.sin(c.rot)
            u = dx * cr - dy * sr
            w = dx * sr + dy * cr
            g = np.exp(-(u * u / (c.sx * c.sx) + w * w / (c.sy * c.sy)))
            life = 1.0 + 0.15 * math.sin(self.time * c.rate + c.phase)
            refl += c.peak * life * g
            turb += c.turb * g
        # Ragged texture that moves with the storms
        nx, ny = X - self.drift[0], Y - self.drift[1]
        tex = (0.55 + 0.45 * self.noise(nx / 3.5, ny / 3.5)
               + 0.35 * self.noise(nx / 1.2 + 50, ny / 1.2 + 50))
        return refl * tex * self.intensity, turb * tex

    # ---------------------------------------------------------------- METAR
    def to_metar(self):
        """Describe the current weather as a METAR string for FlightGear."""
        stamp = datetime.now(timezone.utc).strftime("%d%H%MZ")
        wind = f"{int(self.wind_from) % 360:03d}{int(self.wind_kt):02d}KT"
        if self.scenario == "Clear" or not self.cells:
            parts = ["9999", "FEW050"]
        else:
            stormy = any(c.turb > 0 for c in self.cells) and self.intensity > 0.8
            i = self.intensity
            code = PRECIP_CODE.get(self.precip, "RA")
            sign = "-" if i < 0.7 else ("" if i < 1.1 else "+")
            rain = sign + code
            if stormy:
                rain = ("+" if i >= 1.1 else "") + "TS" + code
            vis = "8000" if i < 0.7 else ("4000" if i < 1.1 else "2000")
            if self.precip != "Rain":
                vis = "3000" if i < 0.7 else ("1500" if i < 1.1 else "0800")
            clouds = "BKN020 OVC080"
            if stormy:
                clouds = "FEW025CB BKN030 OVC080"
            parts = [vis, rain, clouds]
        # Snow needs cold air: send a temperature that matches.
        temps = {"Snow": "M03/M05", "Rain and snow": "01/M01"}.get(self.precip, "18/15")
        return " ".join(["XXXX", stamp, wind, *parts, temps, "Q1008"])


COVER = {"FEW": 0.5, "SCT": 0.8, "BKN": 1.0, "OVC": 1.3}

# What falls from the storms. Snow echoes much weaker than rain (about 0.62
# of the strength, the same ratio FlightGear volumes get); mixed rain and snow
# shows both, each a bit weaker.
PRECIP_KINDS = ["Rain", "Snow", "Rain and snow"]
PRECIP_MULT = {"Rain": (1.0, 0.0), "Snow": (0.0, 0.62), "Rain and snow": (0.6, 0.6)}
PRECIP_CODE = {"Rain": "RA", "Snow": "SN", "Rain and snow": "RASN"}


# A METAR weather group: intensity, descriptor, then precipitation codes.
# e.g. -RA, +SHSN, TSRA, RASN, -SG, TS. A whole group must match, so station
# names (KSNA) or recent weather (RERA) are not mistaken for weather.
WX_GROUP = re.compile(r"^(\+|-|VC)?(MI|PR|BC|DR|BL|SH|TS|FZ)?((?:DZ|RA|SN|SG|PL|GR|GS|IC|UP)*)$")
SNOW_CODES = ("SN", "SG")                    # snow, snow grains
RAIN_CODES = ("RA", "DZ", "GR", "GS", "PL")  # rain, drizzle, hail, ice pellets


def parse_metar(metar):
    """Turn a METAR into (scenario, intensity, coverage, precipitation type).

    The reverse of WeatherModel.to_metar. Returns None if there is nothing to read.
    """
    tokens = metar.split()
    if len(tokens) < 3:
        return None
    groups = []                              # (intensity sign, descriptor, codes)
    for t in tokens[2:]:                     # skip station and time
        m = WX_GROUP.match(t)
        if m and (m.group(3) or m.group(2) == "TS"):
            groups.append((m.group(1) or "", m.group(2) or "", m.group(3)))
    codes = "".join(g[2] for g in groups)
    snow = any(c in codes for c in SNOW_CODES)
    rain = any(c in codes for c in RAIN_CODES)
    thunder = any(g[1] == "TS" for g in groups)
    clouds = [t for t in tokens if t[:3] in COVER]
    cb = any(t.endswith(("CB", "TCU")) for t in clouds)
    coverage = max((COVER[t[:3]] for t in clouds), default=0.5)
    kind = "Rain and snow" if (snow and rain) else ("Snow" if snow else "Rain")
    if thunder or cb:
        scenario = "Scattered Cb"
    elif snow or rain:
        scenario = "Embedded cell in rain"
    else:
        return "Clear", 0.3, coverage, "Rain"
    sign = next((g[0] for g in groups if g[2]), "")
    if sign == "+":
        intensity = 1.3
    elif sign == "-":
        intensity = 0.5
    else:
        intensity = 1.0 if thunder else 0.9
    return scenario, intensity, coverage, kind




class FlightGearWeather:
    """Convert FlightGear weather volumes into radar reflectivity."""

    def __init__(self):
        self.volumes = []
        self.origin = None
        self.altitude_ft = 0.0

    def update(self, volumes, origin, altitude_ft):
        """Update live volumes and aircraft position."""
        self.origin = origin
        self.altitude_ft = altitude_ft
        self.volumes = []

        if origin is None:
            return

        lat0, lon0 = origin

        for v in volumes:
            lat = v.get("latitude-deg")
            lon = v.get("longitude-deg")

            if lat is None or lon is None:
                continue

            # Convert volume centre to NM from aircraft reference origin.
            cx = (lon - lon0) * 60.0 * math.cos(math.radians(lat0))
            cy = (lat - lat0) * 60.0

            self.volumes.append({
                "data": v,
                "x": cx,
                "y": cy,
                "r1": (v.get("r1-m") or 0) / 1852.0,
                "r2": (v.get("r2-m") or 0) / 1852.0,
                "angle": math.radians(v.get("heading-deg") or 0),
            })

    def precipitation_field(self, X, Y, H=None):
        """Return rain, snow, and turbulence arrays for the weather volumes.

        H is the height (ft) of each point, for example where the radar beam
        is. Without it the aircraft's own altitude is used everywhere.
        """
        rain_refl = np.zeros_like(X, dtype=float)
        snow_refl = np.zeros_like(X, dtype=float)
        turb = np.zeros_like(X, dtype=float)

        for item in self.volumes:
            v = item["data"]

            # Only the part of a volume at the beam's height gives an echo.
            base = v.get("base-altitude-ft")
            top = v.get("top-altitude-ft")

            if base is None or top is None:
                continue

            if H is None:
                if not (base <= self.altitude_ft <= top):
                    continue
                in_height = True
            else:
                in_height = (H >= base) & (H <= top)
                if not np.any(in_height):
                    continue

            dx = X - item["x"]
            dy = Y - item["y"]

            r1 = item["r1"]
            r2 = item["r2"]
            angle = item["angle"]
            geometry = int(v.get("geometry") or 0)

            # Rotate points into the weather volume's local coordinates.
            u = dx * math.cos(angle) - dy * math.sin(angle)
            w = dx * math.sin(angle) + dy * math.cos(angle)

            if geometry == 1:
                inside = (dx * dx + dy * dy) <= r1 * r1
            elif geometry == 2:
                if r1 <= 0 or r2 <= 0:
                    continue
                inside = (u / r1) ** 2 + (w / r2) ** 2 <= 1.0
            elif geometry == 3:
                inside = (np.abs(u) <= r1) & (np.abs(w) <= r2)
            else:
                continue
            inside = inside & in_height

            # FlightGear rain intensity is not measured dBZ.
            if v.get("rain-flag") == 1:
                rain = max(0.0, min(1.0, v.get("rain") or 0.0))
                rain_refl = np.maximum(
                    rain_refl, inside * (0.15 + 1.05 * rain))

            if v.get("snow-flag") == 1:
                snow = max(0.0, min(1.0, v.get("snow") or 0.0))
                snow_refl = np.maximum(
                    snow_refl, inside * (0.15 + 0.65 * snow))

            if v.get("turbulence-flag") == 1:
                strength = max(
                    0.0, min(1.0, v.get("turbulence") or 0.0)
                )
                turb = np.maximum(turb, inside * strength)

        return rain_refl, snow_refl, turb

    def field(self, X, Y):
        """Return combined reflectivity and turbulence for existing callers."""
        rain, snow, turb = self.precipitation_field(X, Y)
        return np.maximum(rain, snow), turb
