# Weather radar exporter for the wx_radar app.
#
# Every 2 s: copies Detailed Weather's effect volumes to /wxradar/volumes.
# All the time, a little per frame: builds a terrain grid around the aircraft
# (height and land/water) and puts it in /wxradar/terrain/data.
# On request: places a rain shower, Cb or thunderstorm (see place_weather).
#
# Timers run in real time (last argument 1), so this keeps working while
# FlightGear is paused.
#
# Safe to load more than once (Nasal folder at start-up, test_fg_export.py,
# Nasal console): every copy takes a new number and older copies stop.

var my_id = (getprop("/wxradar/loop-id") or 0) + 1;
setprop("/wxradar/loop-id", my_id);
var alive = func { return getprop("/wxradar/loop-id") == my_id; };

# ------------------------------------------------------------ weather volumes
var export_volumes = func {
    var root = props.globals.getNode("/wxradar/volumes", 1);
    var volumes = [];
    if (contains(globals, "local_weather") and contains(local_weather, "effectVolumeArray"))
        volumes = local_weather.effectVolumeArray;
    var count = size(volumes);

    for (var i = 0; i < count; i += 1) {
        var ev = volumes[i];
        var n = root.getNode("volume[" ~ i ~ "]", 1);
        n.getNode("geometry", 1).setValue(ev.geometry);
        n.getNode("latitude-deg", 1).setValue(ev.lat);
        n.getNode("longitude-deg", 1).setValue(ev.lon);
        n.getNode("r1-m", 1).setValue(ev.r1);
        n.getNode("r2-m", 1).setValue(ev.r2);
        n.getNode("heading-deg", 1).setValue(ev.phi);
        n.getNode("base-altitude-ft", 1).setValue(ev.alt_low);
        n.getNode("top-altitude-ft", 1).setValue(ev.alt_high);
        n.getNode("visibility", 1).setValue(ev.vis);
        n.getNode("rain", 1).setValue(ev.rain);
        n.getNode("snow", 1).setValue(ev.snow);
        n.getNode("turbulence", 1).setValue(ev.turb);
        n.getNode("lift", 1).setValue(ev.lift);
        n.getNode("visibility-flag", 1).setValue(ev.vis_flag);
        n.getNode("rain-flag", 1).setValue(ev.rain_flag);
        n.getNode("snow-flag", 1).setValue(ev.snow_flag);
        n.getNode("turbulence-flag", 1).setValue(ev.turb_flag);
        n.getNode("lift-flag", 1).setValue(ev.lift_flag);
        n.getNode("saturation-flag", 1).setValue(ev.sat_flag);
        n.getNode("saturation", 1).setValue(ev.sat);
        n.getNode("valid", 1).setValue(1);
    }
    # Remove volumes left over from an earlier, bigger export.
    foreach (var old; root.getChildren("volume"))
        if (old.getIndex() >= count)
            old.remove();
    root.getNode("count", 1).setIntValue(count);
};

var volume_loop = func {
    if (!alive()) return;
    call(export_volumes, [], nil, nil, var err = []);
    if (size(err)) logprint(LOG_WARN, "wxradar_export volumes: ", err[0]);
    settimer(volume_loop, 2, 1);
};

# ---------------------------------------------------------------- terrain grid
# GRID_N x GRID_N points, GRID_NM apart, centred on the aircraft. Scenery is
# only loaded near the aircraft, so far points can be unknown ("n").
var GRID_N = 61;
var GRID_NM = 1.5;
var PER_FRAME = 120;       # geodinfo calls per frame, to avoid stutter
var MOVE_NM = 6;           # start a new grid after flying this far
var MAX_AGE_S = 60;        # ... or after this long (scenery may have loaded)

var t = { lat: 0, lon: 0, dlat: 0, dlon: 0, k: 0, elev: [], water: [],
          busy: 0, done_lat: 999, done_lon: 999, done_time: -999 };

var start_grid = func {
    t.lat = getprop("/position/latitude-deg");
    t.lon = getprop("/position/longitude-deg");
    t.dlat = GRID_NM / 60;
    t.dlon = GRID_NM / (60 * math.cos(t.lat * D2R));
    t.k = 0;
    t.elev = [];
    t.water = [];
    t.busy = 1;
};

var grid_step = func {
    var total = GRID_N * GRID_N;
    var c = (GRID_N - 1) / 2;
    var stop = math.min(total, t.k + PER_FRAME);
    for (; t.k < stop; t.k += 1) {
        var j = int(t.k / GRID_N);       # row: south to north
        var i = t.k - j * GRID_N;        # column: west to east
        var g = geodinfo(t.lat + (j - c) * t.dlat, t.lon + (i - c) * t.dlon);
        if (g == nil) {
            append(t.elev, "n");
            append(t.water, "?");
        } else {
            append(t.elev, sprintf("%d", g[0] * M2FT));
            append(t.water, (g[1] != nil and !g[1].solid) ? "1" : "0");
        }
    }
    if (t.k >= total) {
        var head = sprintf("%.6f,%.6f,%.3f,%d", t.lat, t.lon, GRID_NM, GRID_N);
        setprop("/wxradar/terrain/data",
                head ~ "|" ~ string.join(",", t.elev) ~ "|" ~ string.join("", t.water));
        t.busy = 0;
        t.done_lat = t.lat;
        t.done_lon = t.lon;
        t.done_time = systime();
    }
};

var terrain_loop = func {
    if (!alive()) return;
    if (!t.busy) {
        var here = geo.aircraft_position();
        var there = geo.Coord.new().set_latlon(t.done_lat, t.done_lon);
        var moved = t.done_lat == 999 ? 1e9 : here.distance_to(there) / 1852;
        if (moved > MOVE_NM or systime() - t.done_time > MAX_AGE_S)
            start_grid();
    }
    if (t.busy) {
        call(grid_step, [], nil, nil, var err = []);
        if (size(err)) {
            logprint(LOG_WARN, "wxradar_export terrain: ", err[0]);
            t.busy = 0;
            t.done_time = systime();
        }
    }
    settimer(terrain_loop, t.busy ? 0 : 1, 1);
};

# ------------------------------------------------------------ placing weather
# The app (click on the radar) writes /wxradar/place/* and then a new number
# in /wxradar/place/request. FlightGear's own Detailed Weather code builds the
# clouds and the effect volume; the new volume is then set to the size, rain
# and turbulence that were asked for. The answer goes to /wxradar/place/result.
#   kind          shower | cb | thunderstorm
#   latitude-deg, longitude-deg, radius-m, rain (0..1), turbulence (0..1, 0 = none),
#   base-agl-ft   cloud base above the ground at that spot
var place_weather = func {
    var p = props.globals.getNode("/wxradar/place", 1);
    if (!contains(globals, "local_weather") or !contains(local_weather, "effectVolumeArray")
        or !getprop("/nasal/local_weather/enabled")) {
        p.getNode("result", 1).setValue("FAILED: Detailed Weather is off");
        return;
    }
    # Values come over telnet as text: make them numbers.
    var kind = p.getValue("kind");
    var lat = num(p.getValue("latitude-deg"));
    var lon = num(p.getValue("longitude-deg"));
    var radius = num(p.getValue("radius-m"));
    var rain = num(p.getValue("rain"));
    var turb = num(p.getValue("turbulence"));
    var base_agl = num(p.getValue("base-agl-ft"));
    if (lat == nil or lon == nil or radius == nil or rain == nil or turb == nil or base_agl == nil) {
        p.getNode("result", 1).setValue("FAILED: missing or bad values");
        return;
    }
    var g = geodinfo(lat, lon);
    var ground = (g == nil) ? (getprop("/position/ground-elev-ft") or 0) : g[0] * M2FT;
    var base = ground + base_agl;

    var vols = local_weather.effectVolumeArray;
    var before = size(vols);
    if (kind == "shower")
        local_weather.create_cumulonimbus_cloud_rain(lat, lon, base, 1.0, rain);
    elsif (kind == "cb")
        local_weather.create_small_thunderstorm(lat, lon, base, 0.0);
    elsif (radius > 5000)
        local_weather.create_big_thunderstorm(lat, lon, base, 0.0);
    else
        local_weather.create_medium_thunderstorm(lat, lon, base, 0.0);

    vols = local_weather.effectVolumeArray;     # FlightGear may replace the vector
    var changed = 0;
    for (var i = before; i < size(vols); i += 1) {
        var ev = vols[i];
        if (ev.rain_flag != 1) continue;
        ev.r1 = radius;
        ev.r2 = radius;
        ev.rain = rain;
        if (turb > 0) { ev.turb = turb; ev.turb_flag = 1; }
        else          { ev.turb = -1;   ev.turb_flag = 0; }
        changed += 1;
    }
    p.getNode("result", 1).setValue(sprintf("OK: %s at %.4f %.4f, base %d ft, %d volume(s)",
                                            kind, lat, lon, base, changed));
};

setlistener("/wxradar/place/request", func(n) {
    if (!alive()) return;
    var p = props.globals.getNode("/wxradar/place", 1);
    if (n.getValue() == p.getValue("done")) return;
    p.getNode("done", 1).setValue(n.getValue());
    call(place_weather, [], nil, nil, var err = []);
    if (size(err)) {
        p.getNode("result", 1).setValue("FAILED: " ~ err[0]);
        logprint(LOG_WARN, "wxradar_export place: ", err[0]);
    }
}, 0, 1);

settimer(volume_loop, 2, 1);
settimer(terrain_loop, 3, 1);
