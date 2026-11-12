"""Real terrain from FlightGear (made by wxradar_export.nas): height and land/water."""
import math

import numpy as np


class TerrainMap:
    def __init__(self):
        self.text = None
        self.elev = None        # ft MSL, NaN where FlightGear has no scenery loaded
        self.water = None
        self.rough = None       # 0 = flat .. 1.5 = steep mountains

    @property
    def known(self):
        """Share of the grid where FlightGear gave a height."""
        return 0.0 if self.elev is None else float(np.mean(~np.isnan(self.elev)))

    def load(self, text, to_nm, origin_lat):
        """Read 'lat,lon,spacing,n|elev,...|water'. to_nm turns lat/lon into app NM."""
        if not text or text == self.text:
            return
        try:
            head, elev, water = text.split("|")
            lat, lon, spacing, n = head.split(",")
            lat, lon, spacing, n = float(lat), float(lon), float(spacing), int(n)
            e = np.array([np.nan if v == "n" else float(v) for v in elev.split(",")])
            w = np.frombuffer(water.encode(), dtype=np.uint8) == ord("1")
        except ValueError:
            return
        if e.size != n * n or w.size != n * n:
            return
        self.text = text
        self.n = n
        self.elev = e.reshape(n, n)           # [row south->north, column west->east]
        self.water = w.reshape(n, n)
        self.x0, self.y0 = to_nm(lat, lon)
        self.dy = spacing
        # Columns are 'spacing' NM apart at the grid's latitude; the app measures
        # east-west distance with the origin's latitude.
        self.dx = spacing * math.cos(math.radians(lat)) / max(0.01, math.cos(math.radians(origin_lat)))
        gy, gx = np.gradient(np.nan_to_num(self.elev, nan=np.nanmean(self.elev)
                                           if np.isfinite(self.elev).any() else 0.0), spacing)
        self.rough = np.clip(np.hypot(gx, gy) / 400.0, 0.0, 1.5)   # 400 ft per NM = hilly

    def sample(self, X, Y):
        """Terrain at app points: (elevation ft or NaN, water, roughness)."""
        if self.elev is None:
            return (np.full(np.shape(X), np.nan), np.zeros(np.shape(X), bool),
                    np.zeros(np.shape(X)))
        # Blend the four grid points around each point (bilinear), so the
        # picture is smooth instead of blocky.
        c = (self.n - 1) / 2
        fi = (X - self.x0) / self.dx + c
        fj = (Y - self.y0) / self.dy + c
        ok = (fi >= 0) & (fi <= self.n - 1) & (fj >= 0) & (fj <= self.n - 1)
        fi = np.clip(fi, 0, self.n - 1.001)
        fj = np.clip(fj, 0, self.n - 1.001)
        i0, j0 = fi.astype(int), fj.astype(int)
        u, v = fi - i0, fj - j0

        def blend(g):
            return ((g[j0, i0] * (1 - u) + g[j0, i0 + 1] * u) * (1 - v)
                    + (g[j0 + 1, i0] * (1 - u) + g[j0 + 1, i0 + 1] * u) * v)

        elev = np.where(ok, blend(self.elev), np.nan)
        water = ok & (blend(self.water.astype(float)) >= 0.5)
        return elev, water, np.where(ok, blend(self.rough), 0.0)
