"""Radar scanner: turns the weather model into radar color levels.

The radar works in dBZ (reflectivity). Each point on the scan arc is looked at
by a beam that is tilted up or down and spreads with distance, so it only sees
the weather (and the ground) at the heights it passes through.

Levels: 0 = black, 1 = green (20-30 dBZ), 2 = yellow (30-40), 3 = red (40-50),
4 = magenta (50+ dBZ, or turbulence in WX+T), 5 = snow, 6 = mixed rain/snow,
7 = cyan (weak ground return in MAP mode).
"""
import math

import numpy as np

from weather import ValueNoise

LEVEL_COLORS = np.array(
    [[0, 0, 0], [0, 200, 0], [230, 230, 0], [225, 0, 0],
     [225, 0, 225], [0, 220, 255], [255, 255, 255], [0, 170, 170]],
    dtype=np.uint8)

MODES = ("OFF", "STBY", "TEST", "WX", "WX+T", "MAP")
SCANNING_MODES = ("TEST", "WX", "WX+T", "MAP")   # OFF and STBY send no beam
DBZ_BANDS = (20.0, 30.0, 40.0, 50.0)     # green, yellow, red, magenta
TURB_MAX_RANGE_NM = 40.0  # Doppler turbulence detection limit
TILT_LIMIT_DEG = 15.0
BEAM_WIDTH_DEG = 3.5      # half-power beam width of the antenna
FT_PER_NM = 6076.1
CURVE_FT_PER_NM2 = 0.661  # how far the ground drops below a level beam (4/3 earth)

# The beam is sampled at a few angles around its centre, weighted like a
# real antenna pattern (half power at +/- half the beam width).
_OFFSETS = np.linspace(-1.5, 1.5, 7) * BEAM_WIDTH_DEG / 2
_WEIGHTS = np.exp(-math.log(2) * (_OFFSETS / (BEAM_WIDTH_DEG / 2)) ** 2)
_WEIGHTS /= _WEIGHTS.sum()


def to_dbz(refl):
    """Weather model strength (about 1.0 = storm core) to dBZ."""
    return 20.0 + (refl - 0.22) * (10.0 / 0.265)


def dbz_to_z(dbz, valid):
    """dBZ to linear reflectivity, 0 where there is no echo."""
    return np.where(valid, 10.0 ** (np.clip(dbz, -30, 90) / 10.0), 0.0)


def z_to_dbz(z):
    return 10.0 * np.log10(np.maximum(z, 1e-6))


def beam_height_ft(alt_ft, tilt_deg, range_nm):
    """Height of the beam centre above the aircraft's datum at range_nm."""
    return (alt_ft + range_nm * FT_PER_NM * math.tan(math.radians(tilt_deg))
            + CURVE_FT_PER_NM2 * range_nm ** 2)


def auto_tilt(height_agl_ft, range_nm):
    """Tilt that puts the bottom of the beam on the ground just past the
    selected range, so the screen shows weather without ground clutter."""
    d = range_nm * 1.15
    agl = max(height_agl_ft, 0.0)
    # Low down, a beam aimed at a point far away would pass under the curved
    # ground before it. Then aim the bottom of the beam so it just grazes the
    # ground instead (on the runway that is about level).
    graze = math.sqrt(agl / CURVE_FT_PER_NM2)       # where it would touch first
    if graze < d:
        tan_edge = -2.0 * math.sqrt(CURVE_FT_PER_NM2 * agl) / FT_PER_NM
    else:
        tan_edge = -(agl + CURVE_FT_PER_NM2 * d * d) / (d * FT_PER_NM)
    edge = math.degrees(math.atan(tan_edge))
    tilt = edge + _OFFSETS[-1]      # lowest part of the beam we model
    # Quarter-degree steps like a real knob, rounded up so the beam never dips
    # into the ground.
    return max(-TILT_LIMIT_DEG, min(TILT_LIMIT_DEG, math.ceil(tilt * 4 - 1e-9) / 4))


def storm_profile_db(refl, h):
    """Simulated storms have no heights, so give them one: stronger cells
    reach higher. Full strength in the lower half, fading towards the top."""
    top = 12000.0 + 30000.0 * np.clip(refl / 1.1, 0.0, 1.0)
    fade = -25.0 * np.clip((h - 0.5 * top) / (0.5 * top), 0.0, 1.0)
    return np.where((h >= 0) & (h <= top), fade, -np.inf)


def _smooth(a):
    """Soften a (rays x bins) array: 1-2-1 blur along both directions."""
    p = np.pad(a, 1, mode="edge")
    a = (p[:-2, 1:-1] + 2 * p[1:-1, 1:-1] + p[2:, 1:-1]) / 4
    p = np.pad(a, ((0, 0), (1, 1)), mode="edge")
    return (p[:, :-2] + 2 * p[:, 1:-1] + p[:, 2:]) / 4


# ---------------------------------------------------------------------------
# Predictive windshear (PWS)
# ---------------------------------------------------------------------------
PWS_ACTIVE_FT = 2300    # PWS looks for windshear below this height (AGL)
PWS_ALERT_FT = 1200     # ... and gives WINDSHEAR alerts below this
PWS_RANGE_NM = 5.0      # how far ahead it looks
PWS_WARN_NM, PWS_WARN_DEG = 3.0, 25.0   # warning area: close and nearly ahead
PWS_SPAN_DEG = 40.0     # caution area: +/- this either side
MICROBURST_DBZ = 45.0   # heavy rain ...
MICROBURST_TURB = 0.6   # ... with violent air near the ground = microburst


def windshear(weather, ax, ay, up_deg, agl_ft, ground_ft=0.0):
    """Look for microbursts low down, close ahead, like a real PWS.

    A real PWS measures the wind with Doppler: air rushing towards the plane
    on one side of a cell and away on the other. Here a microburst is heavy
    rain with violent air at 500 ft above the ground.

    Returns None, or a dict: level ('warning', 'caution' or 'icon' when too
    high for alerts), a0/a1 (degrees left/right of up), r0/r1 (NM).
    """
    if agl_ft > PWS_ACTIVE_FT:
        return None
    angs = np.arange(-PWS_SPAN_DEG, PWS_SPAN_DEG + 0.1, 2.0)
    rs = np.arange(0.3, PWS_RANGE_NM + 0.01, 0.1)
    b = np.radians(up_deg + angs)[:, None]
    X = ax + rs[None, :] * np.sin(b)
    Y = ay + rs[None, :] * np.cos(b)
    field = getattr(weather, "precipitation_field", None)
    if field is not None:
        rain, _snow, turb = field(X, Y, np.full(X.shape, ground_ft + 500.0))
    else:
        refl, turb = weather.field(X, Y)
        rain = refl * getattr(weather, "precip_mult", (1.0, 0.0))[0]
    hazard = (rain > 0.01) & (to_dbz(rain) >= MICROBURST_DBZ) & (turb >= MICROBURST_TURB)
    if not hazard.any():
        return None
    A = np.broadcast_to(angs[:, None], X.shape)
    Rg = np.broadcast_to(rs[None, :], X.shape)
    warn = hazard & (np.abs(A) <= PWS_WARN_DEG) & (Rg <= PWS_WARN_NM)
    if agl_ft > PWS_ALERT_FT:
        level = "icon"
    else:
        level = "warning" if warn.any() else "caution"
    return {"level": level, "a0": float(A[hazard].min()) - 1.0,
            "a1": float(A[hazard].max()) + 1.0,
            "r0": float(Rg[hazard].min()), "r1": float(Rg[hazard].max())}


class RadarScanner:
    def __init__(self, n_rays=360, n_bins=200, half_angle=90.0, seed=3):
        self.n_rays = n_rays
        self.n_bins = n_bins
        self.half_angle = half_angle
        step = 2 * half_angle / n_rays
        self.rel_angles = np.linspace(-half_angle, half_angle, n_rays,
                                      endpoint=False) + step / 2
        self.bin_frac = (np.arange(n_bins) + 0.5) / n_bins
        self.rng = np.random.default_rng(seed)
        self.terrain = ValueNoise(seed + 11)

    def test_pattern(self):
        """The TEST mode picture: bands of every color, outward from the plane."""
        bands = np.array([0, 1, 2, 3, 4, 3, 2, 1], dtype=np.uint8)
        idx = np.minimum((self.bin_frac * 10).astype(int), len(bands) - 1)
        row = np.where(self.bin_frac > 0.2, bands[idx], 0).astype(np.uint8)
        return np.tile(row, (self.n_rays, 1))

    def scan(self, weather, ax, ay, heading, range_nm, alt_ft=25000.0,
             ground_ft=0.0, tilt_deg=0.0, gain_db=0.0, mode="WX",
             attenuation=True, freezing_ft=None, terrain=None, rays=None):
        """Scan the whole arc. Returns uint8 levels, shape (n_rays, n_bins).

        freezing_ft: freezing level (ft MSL). Above it rain is ice and echoes
        weaker; just below it, melting snow echoes stronger (bright band).
        terrain: a TerrainMap with real heights and water, or None.
        """
        rays = slice(None) if rays is None else rays    # only these rays (sweep)
        i0, i1, _ = rays.indices(self.n_rays)
        if mode == "TEST":
            return self.test_pattern()[i0:i1]
        if mode not in SCANNING_MODES:                  # OFF / STBY: no beam
            return np.zeros((i1 - i0, self.n_bins), dtype=np.uint8)
        # Scan one extra ray each side so the smoothing has neighbours.
        e0, e1 = max(0, i0 - 1), min(self.n_rays, i1 + 1)
        keep = slice(i0 - e0, i0 - e0 + (i1 - i0))

        bearing = np.radians(heading + self.rel_angles[e0:e1])[:, None]
        r = (self.bin_frac * range_nm)[None, :]
        X = ax + r * np.sin(bearing)
        Y = ay + r * np.cos(bearing)
        shape = X.shape

        # Height (ft above the ground under the aircraft) of each beam sample.
        agl = alt_ft - ground_ft
        drop = CURVE_FT_PER_NM2 * r ** 2
        heights = [np.broadcast_to(
            agl + r * FT_PER_NM * math.tan(math.radians(tilt_deg + o)) + drop, shape)
            for o in _OFFSETS]

        # The ground at each point, on the same scale. Real terrain where
        # FlightGear has scenery, otherwise level with the ground under us.
        if terrain is not None:
            t_elev, t_water, t_rough = terrain.sample(X, Y)
            known = np.isfinite(t_elev)
            floor = np.where(known, t_elev - ground_ft, 0.0)
        else:
            known = np.zeros(shape, bool)
            floor = np.zeros(shape)

        fz = None if freezing_ft is None else freezing_ft - ground_ft

        def phase(h):
            """Rain echo change with height: ice -7 dB, melting layer +5 dB."""
            if fz is None:
                return 1.0
            return np.where(h > fz, 0.2, np.where(h > fz - 1000.0, 3.16, 1.0))

        fg_field = getattr(weather, "precipitation_field", None)
        z_rain = np.zeros(shape)
        z_snow = np.zeros(shape)
        turb = np.zeros(shape)
        ground = np.zeros(shape)          # part of the beam hitting the ground
        for w, h in zip(_WEIGHTS, heights):
            ground += w * (h < floor)

        if fg_field is None:
            refl2d, turb2d = weather.field(X, Y)
            m = refl2d > 0.01             # only work on points with weather
            q = refl2d[m]
            fm = floor[m]
            # Share of each storm that is rain and snow (e.g. a snow METAR)
            rain_mult, snow_mult = getattr(weather, "precip_mult", (1.0, 0.0))
            seen_r = np.zeros(q.shape)    # beam-weighted strength, rain
            seen_s = np.zeros(q.shape)    # ... snow (already ice: no freezing-level change)
            hits = np.zeros(q.shape)
            for w, h in zip(_WEIGHTS, heights):
                hm = h[m]
                prof = storm_profile_db(q, hm - fm)   # storms grow from the local ground
                ok = np.isfinite(prof)
                part = w * np.where(ok, 10.0 ** (np.where(ok, prof, 0) / 10.0), 0)
                seen_r += part * phase(hm)
                seen_s += part
                hits += w * ok
            for share, seen, out in ((rain_mult, seen_r, z_rain), (snow_mult, seen_s, z_snow)):
                if share > 0:
                    qs = q * share
                    z0 = 10.0 ** (np.minimum(to_dbz(qs), 90.0) / 10.0)
                    out[m] = np.where(qs > 0.01, z0 * seen, 0.0)
            turb[m] = turb2d[m] * hits
        else:
            for w, h in zip(_WEIGHTS, heights):
                above = h >= floor
                rain, snow, t = fg_field(X, Y, h + ground_ft)
                if rain.any():
                    z_rain += w * dbz_to_z(to_dbz(rain), (rain > 0.01) & above) * phase(h)
                if snow.any():
                    z_snow += w * dbz_to_z(to_dbz(snow), (snow > 0.01) & above)
                turb += w * t * above

        # Ground clutter: the lower the beam, the stronger. Real terrain: hills
        # and mountains bright, flat land weaker, water almost nothing (it
        # reflects the beam away), so coastlines show. Unknown terrain gets a
        # made-up patchy texture. MAP mode is made for the ground: brighter.
        z_ground = np.zeros(shape)
        g = ground > 0.02
        if g.any():
            gx, gy = X[g], Y[g]
            tex = (self.terrain(gx / 2.5, gy / 2.5)
                   + 0.5 * self.terrain(gx / 0.7 + 40, gy / 0.7)) ** 1.5
            ground_dbz = 14.0 + 18.0 * tex
            if terrain is not None:
                real = 16.0 + 14.0 * t_rough[g] + 8.0 * tex
                real = np.where(t_water[g], -5.0, real)
                ground_dbz = np.where(known[g], real, ground_dbz)
            ground_dbz = ground_dbz + (8.0 if mode == "MAP" else 0.0)
            z_ground[g] = ground[g] * 10.0 ** (ground_dbz / 10.0)

        # Terrain shadow: the part of the beam that already hit the ground
        # (a mountain, or the ground ahead) cannot reach anything behind it.
        blocked = np.maximum.accumulate(ground, axis=1)
        clear = 1.0 - np.concatenate([np.zeros((shape[0], 1)), blocked[:, :-1]], axis=1)
        z_rain, z_snow, z_ground = z_rain * clear, z_snow * clear, z_ground * clear

        if attenuation:
            # Heavy rain absorbs the beam, so anything behind it looks weaker.
            dr = range_nm / self.n_bins
            loss_db = np.maximum(0.0, z_to_dbz(z_rain) - 30.0) * 0.05 * dr
            path_db = np.cumsum(loss_db, axis=1) - loss_db
            factor = 10.0 ** (-path_db / 10.0)
            z_rain, z_snow, z_ground = z_rain * factor, z_snow * factor, z_ground * factor

        # Smooth picture: soften the echoes and the speckle a little, so edges
        # are rounded instead of grainy (like a real radar display).
        speckle = _smooth(self.rng.normal(0.0, 1.5, shape)) + gain_db
        z_rg, z_snow = _smooth(z_rain + z_ground), _smooth(z_snow)
        rain_dbz = z_to_dbz(z_rg) + speckle
        snow_dbz = z_to_dbz(z_snow) + speckle
        turb = _smooth(turb)

        levels = np.zeros(shape, dtype=np.uint8)
        if mode == "MAP":
            total = z_to_dbz(z_rg + z_snow) + speckle
            levels[total >= 20] = 7
            levels[total >= 32] = 2
            levels[total >= 44] = 4
            return levels[keep]

        for i, band in enumerate(DBZ_BANDS, start=1):
            levels[rain_dbz >= band] = i
        snow_detected = snow_dbz >= DBZ_BANDS[0]
        rain_detected = levels > 0
        levels[snow_detected & ~rain_detected] = 5
        levels[snow_detected & rain_detected & (levels < 4)] = 6
        if mode == "WX+T":
            levels[(r <= TURB_MAX_RANGE_NM) & (levels >= 1) & (turb > 0.65)] = 4
        return levels[keep]
