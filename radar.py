"""Radar scanner: turns the weather model into radar color levels.

Levels: 0 = black, 1-3 = rain, 4 = magenta (TURB), 5 = snow, 6 = mixed.
"""
import numpy as np

LEVEL_COLORS = np.array(
    [[0, 0, 0], [0, 200, 0], [230, 230, 0], [225, 0, 0],
     [225, 0, 225], [0, 220, 255], [255, 255, 255]],
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
        precipitation_field = getattr(weather, "precipitation_field", None)
        if precipitation_field is None:
            refl, turb = weather.field(X, Y)
            rain_refl = refl
            snow_refl = np.zeros_like(refl)
        else:
            rain_refl, snow_refl, turb = precipitation_field(X, Y)
            refl = np.maximum(rain_refl, snow_refl)

        if attenuation:
            # Heavy rain absorbs the beam, so weather behind it looks weaker
            dr = range_nm / self.n_bins
            loss = np.maximum(0.0, refl - 0.35) * 0.22 * dr
            absorbed = np.cumsum(loss, axis=1) - loss
            attenuation_factor = np.exp(-absorbed)
            rain_refl *= attenuation_factor
            snow_refl *= attenuation_factor

        speckle = 0.7 + 0.6 * self.rng.random(refl.shape)
        rain = rain_refl * gain * speckle
        snow = snow_refl * gain * speckle

        rain_levels = np.zeros(rain.shape, dtype=np.uint8)
        rain_levels[rain >= 0.22] = 1
        rain_levels[rain >= 0.48] = 2
        rain_levels[rain >= 0.75] = 3
        snow_detected = snow >= 0.22
        rain_detected = rain_levels > 0
        levels = rain_levels.copy()
        levels[snow_detected & ~rain_detected] = 5
        levels[snow_detected & rain_detected] = 6
        if turb_mode:
            levels[(r <= TURB_MAX_RANGE_NM) & (levels >= 1) & (turb > 0.65)] = 4
        return levels
