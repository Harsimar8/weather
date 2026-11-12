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
# Detailed Weather on/off: the flag FlightGear's own Weather dialog trusts. It
# turns true on Apply (the dialog's radio buttons change before Apply). The
# old tile code stays in the tree after switching back to Basic Weather.
LW_MODE = "/nasal/local_weather/enabled"     # true only while Detailed Weather runs
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
    "ground_ft": "/position/ground-elev-ft",  # terrain under the aircraft
    "gs_kt": "/velocities/groundspeed-kt",
    "tas_kt": "/instrumentation/airspeed-indicator/true-speed-kt",
    "hdg_mag": "/orientation/heading-magnetic-deg",
    "speed_up": "/sim/speed-up",             # time acceleration (1 = normal)
    "v_north_fps": "/velocities/speed-north-fps",   # ground track comes from these
    "v_east_fps": "/velocities/speed-east-fps",
    "vs_fps": "/velocities/vertical-speed-fps",     # climbing or descending (PWS)
    "hdg_bug": "/autopilot/settings/heading-bug-deg",  # selected heading (magnetic)
}
FROZEN = "/sim/freeze/master"                # true while FlightGear is paused

# Basic Weather settings (the Manual Configuration dialog uses the same ones;
# FlightGear also fills them from the METAR). Read every few seconds.
MANUAL = "/sim/gui/dialogs/metar/mode/manual-weather"
CLOUD_LAYERS, ALOFT, BOUNDARY = 5, 5, 2
CFG_PROPS = {"manual": MANUAL}
for _i in range(CLOUD_LAYERS):
    for _k, _n in (("cov", "coverage"), ("ft", "elevation-ft"), ("thick", "thickness-ft")):
        CFG_PROPS[f"cloud{_i}_{_k}"] = f"/environment/clouds/layer[{_i}]/{_n}"
for _where, _count in (("aloft", ALOFT), ("boundary", BOUNDARY)):
    for _i in range(_count):
        for _k, _n in (("ft", "elevation-ft"), ("temp", "temperature-degc"),
                       ("turb", "turbulence/magnitude-norm")):
            CFG_PROPS[f"{_where}{_i}_{_k}"] = f"/environment/config/{_where}/entry[{_i}]/{_n}"
SLOW_POLL_S = 2.0

# Terrain grid made by wxradar_export.nas: "lat,lon,spacing,n|elev,...|water"
TERRAIN_DATA = "/wxradar/terrain/data"

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
WEATHER_VOLUME_POLL_S = 2.0
CLOUD_CHUNK = 25      # clouds asked for in one go
MAX_TILES = 20        # newest cloud tiles to look at
# FlightGear live weather volumes exported by wxradar_export.nas
WX_VOLUME_COUNT = "/wxradar/volumes/count"

WX_VOLUME_FIELDS = (
    "geometry",
    "latitude-deg",
    "longitude-deg",
    "r1-m",
    "r2-m",
    "heading-deg",
    "base-altitude-ft",
    "top-altitude-ft",
    "visibility",
    "rain",
    "snow",
    "turbulence",
    "lift",
    "rain-flag",
    "snow-flag",
    "turbulence-flag",
    "visibility-flag",
    "lift-flag",
    "saturation",
    "saturation-flag",
    "valid",
)



FAST_PROPS = {**POSITION_PROPS, **WIND_PROPS, **LIGHTNING_PROPS,
              "metar": METAR_PROPERTY, "lw_code": LW_CODE, "frozen": FROZEN,
              "lw_mode": LW_MODE, "lw_tiles": LW_TILE_COUNTER,
              "station": LW_STATION, **NUMBER_PROPS}


def _num(text):
    """Property value as a float, or None if missing (FlightGear sends '')."""
    try:
        return float(text)
    except ValueError:
        return None


def _flag(text):
    return text.strip().lower() in ("1", "true")


def _parse_cfg(raw):
    """Basic Weather settings as plain lists."""
    clouds = []
    for i in range(CLOUD_LAYERS):
        cov = raw[f"cloud{i}_cov"].strip().lower()
        ft, thick = _num(raw[f"cloud{i}_ft"]), _num(raw[f"cloud{i}_thick"])
        if cov and ft is not None and ft > -9000:
            clouds.append((cov, ft, thick or 0.0))
    table = {}
    for where, count in (("aloft", ALOFT), ("boundary", BOUNDARY)):
        rows = []
        for i in range(count):
            ft = _num(raw[f"{where}{i}_ft"])
            if ft is not None:
                rows.append((ft, _num(raw[f"{where}{i}_temp"]),
                             _num(raw[f"{where}{i}_turb"]) or 0.0))
        table[where] = rows
    return {"manual": _flag(raw["manual"]), "clouds": clouds, **table}


class FGLink:
    TIMEOUT_S = 10.0      # FlightGear can freeze for seconds (Apply, scenery)
    RETRY_S = 2.0         # wait between reconnect attempts
    BATCH = 400           # properties asked for in one go

    def __init__(self, host="localhost", port=5401, poll_hz=5.0):
        self.host, self.port = host, port
        self.poll_interval = 1.0 / poll_hz
        self.sock = None
        self.reader = None
        self.lock = threading.Lock()
        self.state = {}
        self.weather_volumes = []
        self.cfg = None             # Basic Weather settings
        self.terrain = None         # terrain grid text from wxradar_export.nas
        self.clouds = {}
        self.cloud_alt_ft = None    # (lowest, highest) Detailed Weather cloud
        self.connected = False
        self.reconnecting = False
        self.reconnects = 0
        self.error = ""
        self._thread = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ connect
    def _open(self):
        with self.lock:
            sock = socket.create_connection((self.host, self.port), timeout=5)
            sock.settimeout(self.TIMEOUT_S)
            self.sock = sock
            self.reader = sock.makefile("r", encoding="utf-8", newline="\n")
            self._send("data")  # raw mode: replies are just the value

    def _shut(self):
        with self.lock:
            try:
                if self.sock:
                    self.sock.close()
            except OSError:
                pass
            self.sock = None

    def connect(self):
        self._open()
        self.connected = True
        self.error = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.connected = False
        self.reconnecting = False
        try:
            if self.sock:
                self._send("quit")
        except OSError:
            pass
        self._shut()

    def _reconnect(self, exc):
        """Connection broke: keep trying until it works or close() is called."""
        self.connected = False
        self.reconnecting = True
        self.error = str(exc) or exc.__class__.__name__
        self._shut()
        while not self._stop.wait(self.RETRY_S):
            try:
                self._open()
            except OSError:
                continue
            self.connected = True
            self.reconnecting = False
            self.reconnects += 1
            return

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
            self.sock.settimeout(self.TIMEOUT_S)

    def get_many(self, paths):
        """Ask for several properties: one reply line per path, in order."""
        paths = list(paths)
        out = []
        with self.lock:
            if not self.sock:
                raise OSError("not connected")
            for k in range(0, len(paths), self.BATCH):
                chunk = paths[k:k + self.BATCH]
                self.sock.sendall("".join(f"get {p}\r\n" for p in chunk).encode("utf-8"))
                out += [self.reader.readline().strip() for _ in chunk]
        return out

    def get(self, path):
        return self.get_many([path])[0]

    def set(self, path, value):
        with self.lock:
            if not self.sock:
                raise OSError("not connected")
            self._send(f"set {path} {value}")
            time.sleep(0.05)
            self._drain()

    # ------------------------------------------------------------ polling
    def _poll_loop(self):
        bad = 0                     # bad replies in a row
        self._next_clouds = self._next_slow = 0.0
        while not self._stop.is_set():
            try:
                self._poll_once()
                bad = 0
            except ValueError as exc:
                # One odd reply (e.g. during a reset) is skipped. Several in a
                # row mean the replies are out of step: start a fresh link.
                bad += 1
                self.error = str(exc)
                if bad >= 3:
                    bad = 0
                    self._reconnect(exc)
            except Exception as exc:    # socket trouble or anything unexpected
                if self._stop.is_set():
                    break               # we are closing: not an error
                # Never let the reading thread die: start a fresh link instead.
                self._reconnect(exc if isinstance(exc, OSError)
                                else OSError(f"{exc.__class__.__name__}: {exc}"))
            self._stop.wait(self.poll_interval)

    def _poll_once(self):
        raw = dict(zip(FAST_PROPS, self.get_many(FAST_PROPS.values())))
        new = {k: float(raw[k]) for k in POSITION_PROPS}
        for k in (*WIND_PROPS, *LIGHTNING_PROPS, *NUMBER_PROPS):
            value = _num(raw[k])
            if value is not None:  # missing in this FlightGear version
                new[k] = value
        new["metar"] = raw["metar"]
        new["station"] = raw["station"]
        new["frozen"] = _flag(raw["frozen"])
        # Missing mode property (other versions): trust the tile code.
        detailed = raw["lw_mode"].lower() in ("1", "true", "")
        code = raw["lw_code"] if detailed else ""
        new["lw_code"] = code
        # True only when FlightGear clearly says Detailed Weather is chosen.
        new["lw_selected"] = raw["lw_mode"].lower() in ("1", "true")
        now = time.monotonic()
        if code and now >= self._next_clouds:
            self.clouds = self._read_clouds(int(_num(raw["lw_tiles"]) or 0))
            self._next_clouds = now + CLOUD_POLL_S
        if now >= self._next_slow:
            self._next_slow = now + SLOW_POLL_S
            self.weather_volumes = self._read_weather_volumes()
            cfg_raw = dict(zip(CFG_PROPS, self.get_many(CFG_PROPS.values())))
            self.cfg = _parse_cfg(cfg_raw)
            text = self.get(TERRAIN_DATA)
            if text and "|" in text:
                self.terrain = text

        new["clouds"] = self.clouds if code else {}
        new["cloud_alt_ft"] = self.cloud_alt_ft if code else None
        new["weather_volumes"] = self.weather_volumes
        new["cfg"] = self.cfg
        new["terrain"] = self.terrain
        self.state = new

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
    def _read_weather_volumes(self):
        """Read live weather volumes exported by FlightGear, in big batches."""
        count = int(_num(self.get(WX_VOLUME_COUNT)) or 0)
        paths = [f"/wxradar/volumes/volume[{i}]/{field}"
                 for i in range(count) for field in WX_VOLUME_FIELDS]
        values = self.get_many(paths) if paths else []
        n = len(WX_VOLUME_FIELDS)
        return [{field: _num(v) for field, v in zip(WX_VOLUME_FIELDS, values[i * n:(i + 1) * n])}
                for i in range(count)]

    def send_metar(self, metar):
        self.set(METAR_PROPERTY, metar)
