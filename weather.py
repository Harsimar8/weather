"""Weather model: the storm cells that the radar scans.

Coordinates are in nautical miles (NM): x = east, y = north.
"""
import math
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

    # ------------------------------------------------------------ scenarios
    def generate(self, scenario, ax, ay, heading):
        """Create storm cells ahead of the aircraft at (ax, ay)."""
        self.scenario = scenario
        self.cells = []
        self.drift[:] = 0.0
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

    # ------------------------------------------------------------- dynamics
    def step(self, dt):
        """Advance time by dt seconds: cells drift with the wind."""
        self.time += dt
        to = math.radians(self.wind_from + 180.0)
        dist = self.wind_kt / 3600.0 * dt
        dx, dy = dist * math.sin(to), dist * math.cos(to)
        for c in self.cells:
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
            rain = "-RA" if i < 0.7 else ("RA" if i < 1.1 else "+RA")
            if stormy:
                rain = ("+TSRA" if i >= 1.1 else "TSRA")
            vis = "8000" if i < 0.7 else ("4000" if i < 1.1 else "2000")
            clouds = "BKN020 OVC080"
            if stormy:
                clouds = "FEW025CB BKN030 OVC080"
            parts = [vis, rain, clouds]
        return " ".join(["XXXX", stamp, wind, *parts, "18/15", "Q1008"])


COVER = {"FEW": 0.5, "SCT": 0.8, "BKN": 1.0, "OVC": 1.3}


def parse_metar(metar):
    """Turn a METAR from FlightGear into (scenario, intensity, coverage).

    The reverse of WeatherModel.to_metar. Returns None if there is nothing to read.
    """
    tokens = metar.split()
    if len(tokens) < 3:
        return None
    rain = next((t for t in tokens if any(w in t for w in ("TS", "RA", "SH", "DZ"))
                 and not t.startswith("RE")), "")
    clouds = [t for t in tokens if t[:3] in COVER]
    cb = any(t.endswith(("CB", "TCU")) for t in clouds)
    coverage = max((COVER[t[:3]] for t in clouds), default=0.5)
    if "TS" in rain or cb:
        scenario = "Scattered Cb"
    elif rain:
        scenario = "Embedded cell in rain"
    else:
        return "Clear", 0.3, coverage
    if rain.startswith("+"):
        intensity = 1.3
    elif rain.startswith("-"):
        intensity = 0.5
    else:
        intensity = 1.0 if "TS" in rain else 0.9
    return scenario, intensity, coverage
