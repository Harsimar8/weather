"""Radar scanner: turns the weather model into radar color levels.

Levels: 0 = black, 1 = green, 2 = yellow, 3 = red, 4 = magenta (TURB).
"""
import numpy as np

LEVEL_COLORS = np.array(
    [[0, 0, 0], [0, 200, 0], [230, 230, 0], [225, 0, 0], [225, 0, 225]],
    dtype=np.uint8)

TURB_MAX_RANGE_NM = 40.0  # Doppler turbulence detection limit


class RadarScanner:
    def __init__(self, n_rays=240, n_bins=200, half_angle=50.0, seed=3):
        self.n_rays = n_rays
        self.n_bins = n_bins
        self.half_angle = half_angle
        step = 2 * half_angle / n_rays
        self.rel_angles = np.linspace(-half_angle, half_angle, n_rays,
                                      endpoint=False) + step / 2
        self.bin_frac = (np.arange(n_bins) + 0.5) / n_bins
        self.rng = np.random.default_rng(seed)

    def scan(self, weather, ax, ay, heading, range_nm, gain=1.0,
             attenuation=True, turb_mode=False):
        """Scan the whole arc. Returns uint8 levels, shape (n_rays, n_bins)."""
        bearing = np.radians(heading + self.rel_angles)[:, None]
        r = (self.bin_frac * range_nm)[None, :]
        X = ax + r * np.sin(bearing)
        Y = ay + r * np.cos(bearing)
        refl, turb = weather.field(X, Y)

        if attenuation:
            # Heavy rain absorbs the beam, so weather behind it looks weaker
            dr = range_nm / self.n_bins
            loss = np.maximum(0.0, refl - 0.35) * 0.22 * dr
            absorbed = np.cumsum(loss, axis=1) - loss
            refl = refl * np.exp(-absorbed)

        speckle = 0.7 + 0.6 * self.rng.random(refl.shape)
        v = refl * gain * speckle

        levels = np.zeros(v.shape, dtype=np.uint8)
        levels[v >= 0.22] = 1
        levels[v >= 0.48] = 2
        levels[v >= 0.75] = 3
        if turb_mode:
            levels[(r <= TURB_MAX_RANGE_NM) & (levels >= 1) & (turb > 0.65)] = 4
        return levels
