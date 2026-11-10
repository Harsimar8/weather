"""Connection to FlightGear through its telnet property interface.

Start FlightGear with:  --telnet=5401
"""
import socket
import threading
import time

# Property that takes a manual METAR string. Check it in FlightGear under
# Debug > Browse Internal Properties > environment, as names can differ
# between versions and weather systems.
METAR_PROPERTY = "/environment/metar/data"

POSITION_PROPS = {
    "lat": "/position/latitude-deg",
    "lon": "/position/longitude-deg",
    "alt_ft": "/position/altitude-ft",
    "heading": "/orientation/heading-deg",
}

# Live wind at the aircraft. Read only: the radar follows FlightGear's wind.
WIND_PROPS = {
    "wind_from": "/environment/wind-from-heading-deg",
    "wind_kt": "/environment/wind-speed-kt",
}

# Detailed Weather (Advanced Weather) publishes its weather type, its cloud
# positions and the latest lightning strike. Basic Weather leaves these empty.
LW_CODE = "/local-weather/tiles/code"
# Weather engine chosen in the Weather dialog ('1' = Detailed Weather). The
# old tile code stays in the tree after switching back to Basic Weather.
LW_MODE = "/sim/gui/dialogs/metar/mode/local-weather"
LW_TILE_COUNTER = "/local-weather/tiles/tile-counter"
LW_CLOUD = "/local-weather/clouds/tile[{t}]/cloud[{c}]/position/{name}"
LIGHTNING_PROPS = {
    "strike_lat": "/local-weather/lightning/latitude-deg",
    "strike_lon": "/local-weather/lightning/longitude-deg",
}

# Weather felt at the aircraft. Both weather engines fill these in.
HERE_PROPS = {
    "vis_m": "/environment/visibility-m",
    "rain": "/environment/rain-norm",          # 0 = none .. 1 = heavy
    "snow": "/environment/snow-norm",
    "turb": "/environment/turbulence/magnitude-norm",
    "temp_c": "/environment/temperature-degc",
    "dew_c": "/environment/dewpoint-degc",
    "rh": "/environment/relative-humidity",
    "qnh_inhg": "/environment/pressure-sea-level-inhg",
}

# Detailed Weather only: the weather of the area, the rain/snow/turbulence
# zones the aircraft is inside, the weather station and its cloud layers.
LW_EXTRA_PROPS = {
    "area_vis_m": "/local-weather/current/visibility-m",
    "area_rain": "/local-weather/current/rain-norm",
    "area_snow": "/local-weather/current/snow-norm",
    "area_turb": "/local-weather/current/turbulence",
    "area_lift": "/local-weather/current/thermal-lift",
    "in_rain": "/local-weather/effect-volumes/number-active-rain",
    "in_snow": "/local-weather/effect-volumes/number-active-snow",
    "in_turb": "/local-weather/effect-volumes/number-active-turb",
    "in_vis": "/local-weather/effect-volumes/number-active-vis",
    "in_lift": "/local-weather/effect-volumes/number-active-lift",
    "zones": "/local-weather/effect-volumes/number",
    "stn_thunder": "/local-weather/METAR/thunderstorm-flag",
    "strike_alt": "/local-weather/lightning/altitude-ft",
}
LW_STATION = "/local-weather/METAR/station-id"
LW_LAYERS = 4
LW_LAYER = "/local-weather/METAR/layer[{i}]/{name}"
LAYER_PROPS = {f"layer{i}_{k}": LW_LAYER.format(i=i, name=n)
               for i in range(LW_LAYERS)
               for k, n in (("oct", "cover-oct"), ("ft", "alt-agl-ft"))}
NUMBER_PROPS = {**HERE_PROPS, **LW_EXTRA_PROPS, **LAYER_PROPS}
CLOUD_POLL_S = 10.0   # cloud positions do not change, so read them now and then
CLOUD_CHUNK = 25      # clouds asked for in one go
MAX_TILES = 20        # newest cloud tiles to look at

FAST_PROPS = {**POSITION_PROPS, **WIND_PROPS, **LIGHTNING_PROPS,
              "metar": METAR_PROPERTY, "lw_code": LW_CODE,
              "lw_mode": LW_MODE, "lw_tiles": LW_TILE_COUNTER,
              "station": LW_STATION, **NUMBER_PROPS}


def _num(text):
    """Property value as a float, or None if missing (FlightGear sends '')."""
    try:
        return float(text)
    except ValueError:
        return None


class FGLink:
    def __init__(self, host="localhost", port=5401, poll_hz=5.0):
        self.host, self.port = host, port
        self.poll_interval = 1.0 / poll_hz
        self.sock = None
        self.reader = None
        self.lock = threading.Lock()
        self.state = {}
        self.clouds = {}
        self.cloud_alt_ft = None    # (lowest, highest) Detailed Weather cloud
        self.connected = False
        self.error = ""
        self._thread = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ connect
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=3)
        self.sock.settimeout(2)
        self.reader = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self._send("data")  # raw mode: replies are just the value
        self.connected = True
        self.error = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.connected = False
        try:
            if self.sock:
                self._send("quit")
                self.sock.close()
        except OSError:
            pass
        self.sock = None

    # ------------------------------------------------------- low level I/O
    def _send(self, line):
        self.sock.sendall((line + "\r\n").encode("utf-8"))

    def _drain(self):
        """Throw away any unexpected leftover replies."""
        self.sock.setblocking(False)
        try:
            while self.sock.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass
        finally:
            self.sock.settimeout(2)

    def get_many(self, paths):
        """Ask for several properties at once: one reply line per path, in order."""
        with self.lock:
            self.sock.sendall("".join(f"get {p}\r\n" for p in paths).encode("utf-8"))
            return [self.reader.readline().strip() for _ in paths]

    def get(self, path):
        return self.get_many([path])[0]

    def set(self, path, value):
        with self.lock:
            self._send(f"set {path} {value}")
            time.sleep(0.05)
            self._drain()

    # ------------------------------------------------------------ polling
    def _poll_loop(self):
        next_clouds = 0.0
        while not self._stop.is_set():
            try:
                raw = dict(zip(FAST_PROPS, self.get_many(FAST_PROPS.values())))
                new = {k: float(raw[k]) for k in POSITION_PROPS}
                for k in (*WIND_PROPS, *LIGHTNING_PROPS, *NUMBER_PROPS):
                    value = _num(raw[k])
                    if value is not None:  # missing in this FlightGear version
                        new[k] = value
                new["metar"] = raw["metar"]
                new["station"] = raw["station"]
                # Missing mode property (other versions): trust the tile code.
                detailed = raw["lw_mode"] in ("1", "true", "")
                code = raw["lw_code"] if detailed else ""
                new["lw_code"] = code
                # True only when FlightGear clearly says Detailed Weather is chosen.
                new["lw_selected"] = raw["lw_mode"] in ("1", "true")
                if code and time.monotonic() >= next_clouds:
                    self.clouds = self._read_clouds(int(_num(raw["lw_tiles"]) or 0))
                    next_clouds = time.monotonic() + CLOUD_POLL_S
                new["clouds"] = self.clouds if code else {}
                new["cloud_alt_ft"] = self.cloud_alt_ft if code else None
                self.state = new
            except (OSError, ValueError) as exc:
                self.error = str(exc) or exc.__class__.__name__
                self.connected = False
                return
            time.sleep(self.poll_interval)

    def _read_clouds(self, counter):
        """Detailed Weather cloud positions as {(tile, index): (lat, lon)}.

        Also stores the lowest and highest cloud altitude in self.cloud_alt_ft.
        """
        names = ("latitude-deg", "longitude-deg", "altitude-ft")
        tiles = range(max(0, counter - MAX_TILES + 1), counter + 1)
        # Only tiles that still have clouds (old ones are removed as you fly).
        first = self.get_many([LW_CLOUD.format(t=t, c=0, name=names[0]) for t in tiles])
        clouds, alts = {}, []
        for t, lat0 in zip(tiles, first):
            if _num(lat0) is None:
                continue
            start = 0
            while True:
                idx = range(start, start + CLOUD_CHUNK)
                vals = self.get_many([LW_CLOUD.format(t=t, c=c, name=n)
                                      for c in idx for n in names])
                found = False
                for n, c in enumerate(idx):
                    lat, lon, alt = (_num(v) for v in vals[3 * n:3 * n + 3])
                    if lat is not None and lon is not None:
                        clouds[(t, c)] = (lat, lon)
                        if alt is not None:
                            alts.append(alt)
                        found = True
                if not found:
                    break
                start += CLOUD_CHUNK
        self.cloud_alt_ft = (min(alts), max(alts)) if alts else None
        return clouds

    def send_metar(self, metar):
        self.set(METAR_PROPERTY, metar)
