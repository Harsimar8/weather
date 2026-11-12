"""FlightGear Basic Weather from its settings (Manual Configuration).

Cloud layers, the rain/snow sliders and the turbulence-by-altitude table are
turned into something the radar can scan. Also works out the freezing level.
"""
import math

import numpy as np

from weather import ValueNoise

COVERAGE_FRAC = {"clear": 0.0, "few": 0.2, "scattered": 0.45, "broken": 0.75,
                 "overcast": 1.0, "cirrus": 0.0}   # cirrus is too high and thin to rain


class LayerWeather:
    """Rain and snow falling from FlightGear's cloud layers.

    Basic Weather gives no rain positions, so the rain is drawn in patches
    that cover about as much of the sky as the clouds do, from the ground up
    to the top of the highest raining layer. Turbulence comes from the
    altitude table (Aloft / Boundary), at the height the beam looks at.
    """

    def __init__(self, seed=21):
        self.noise = ValueNoise(seed)
        rng = np.random.default_rng(seed)
        sx, sy = rng.uniform(0, 256, 20000), rng.uniform(0, 256, 20000)
        self._sample = np.sort(self._pattern(sx, sy))   # turns coverage into a threshold
        self.drift = np.zeros(2)
        self.rain = self.snow = 0.0
        self.cover = 0.0
        self.top_ft = 0.0
        self.threshold = 1.0
        self.turb_alt = np.array([0.0])
        self.turb_val = np.array([0.0])
        self.volumes = []               # non-empty when there is rain or snow

    def _pattern(self, x, y):
        return 0.7 * self.noise(x, y) + 0.3 * self.noise(x * 3 + 17, y * 3 + 17)

    def update(self, cfg, rain, snow, ground_ft):
        self.rain, self.snow = max(0.0, rain or 0.0), max(0.0, snow or 0.0)
        raining = [(COVERAGE_FRAC.get(c, 0.0), ft, thick)
                   for c, ft, thick in cfg.get("clouds", [])
                   if COVERAGE_FRAC.get(c, 0.0) > 0]
        if raining:
            self.cover = max(f for f, _, _ in raining)
            self.top_ft = max(ft + thick for _, ft, thick in raining)
        else:   # rain set without clouds: FlightGear still makes it rain
            self.cover, self.top_ft = 0.6, ground_ft + 10000.0
        k = int(np.clip(1.0 - self.cover, 0.0, 0.999) * len(self._sample))
        self.threshold = self._sample[k] if self.cover < 0.999 else -1.0
        rows = [(ft, t) for ft, _, t in cfg.get("aloft", [])]
        rows += [(ground_ft + ft, t) for ft, _, t in cfg.get("boundary", [])]  # AGL
        rows.sort()
        if rows:
            self.turb_alt = np.array([r[0] for r in rows])
            self.turb_val = np.array([r[1] for r in rows])
        self.volumes = [1] if (self.rain > 0.01 or self.snow > 0.01) else []

    def step(self, dt, wind_from, wind_kt):
        """Rain patches drift with the wind."""
        to = math.radians(wind_from + 180.0)
        d = wind_kt / 3600.0 * dt
        self.drift += (d * math.sin(to), d * math.cos(to))

    def precipitation_field(self, X, Y, H=None):
        """Rain, snow, turbulence at points X, Y (NM) and heights H (ft MSL)."""
        shape = np.broadcast_shapes(np.shape(X), np.shape(H) if H is not None else ())
        if not self.volumes:
            return np.zeros(shape), np.zeros(shape), np.zeros(shape)
        # The radar asks several times per scan for the same points (once per
        # beam slice), so the rain pattern is worked out once and reused.
        key = (id(X), id(Y), np.shape(X), float(np.ravel(X)[0]), float(np.ravel(Y)[-1]),
               tuple(self.drift), self.threshold)
        if key != getattr(self, "_key", None):
            n = self._pattern((X - self.drift[0]) / 6.0, (Y - self.drift[1]) / 6.0)
            edge = np.clip((n - self.threshold) / 0.06, 0.0, 1.0)       # soft edges
            core = np.clip((n - self.threshold) / 0.25, 0.0, 1.0)       # heavier inside
            # Heavier inside each patch, and never perfectly even (even overcast rain varies).
            self._strength = edge * (0.6 + 0.4 * core) * (0.7 + 0.6 * n)
            self._key = key
        strength = np.broadcast_to(self._strength, shape)
        inside = 1.0 if H is None else ((H >= 0) & (H <= self.top_ft)) * 1.0
        rain = strength * inside * (0.15 + 1.05 * min(self.rain, 1.0)) * (self.rain > 0.01)
        snow = strength * inside * (0.15 + 0.65 * min(self.snow, 1.0)) * (self.snow > 0.01)
        h = np.broadcast_to(H if H is not None else 0.0, shape)
        turb = np.interp(h, self.turb_alt, self.turb_val) * (strength > 0)
        return rain, snow, turb

    def field(self, X, Y):
        rain, snow, turb = self.precipitation_field(X, Y)
        return np.maximum(rain, snow), turb


def freezing_level_ft(cfg, ground_ft, alt_ft=None, temp_c=None):
    """Height (ft MSL) where the air reaches 0 degC.

    Uses FlightGear's temperature-by-altitude table. Without it, works from
    the temperature at the aircraft (about 2 degC colder every 1000 ft).
    """
    rows = []
    if cfg:
        rows = [(ft, t) for ft, t, _ in cfg.get("aloft", []) if t is not None]
        rows += [(ground_ft + ft, t) for ft, t, _ in cfg.get("boundary", []) if t is not None]
        rows.sort()
    if len(rows) >= 2:
        for (a0, t0), (a1, t1) in zip(rows, rows[1:]):
            if t0 > 0 >= t1:
                return a0 + (a1 - a0) * t0 / (t0 - t1)
        if rows[0][1] <= 0:
            return ground_ft                       # freezing at the ground
        a, t = rows[-1]
        return a + t / 2.0 * 1000.0                # warm everywhere: go on up
    if alt_ft is not None and temp_c is not None:
        return max(ground_ft, alt_ft + temp_c / 2.0 * 1000.0)
    return None
