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
        # Where the volume came from: a click on the app's map, or Detailed Weather.
        n.getNode("source", 1).setValue(contains(ev, "wxradar_placed")
                                        ? "placed from map (" ~ ev.wxradar_placed ~ ")"
                                        : "Detailed Weather");
        n.getNode("placed-id", 1).setValue(contains(ev, "wxradar_id") ? ev.wxradar_id : 0);
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
# Everything each placed storm made, by its request number, so it can be removed:
#   vols: effect volumes, clouds: cloud objects, models: [cloud node, model node],
#   echo: FlightGear radar echo node
var placed = {};

var place_weather = func {
    var p = props.globals.getNode("/wxradar/place", 1);
    var id = "" ~ p.getValue("request");    # text key (telnet values may be text)
    var rec = {vols: [], clouds: [], models: [], echo: nil};
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
    # With threading on (the default) the create_* calls only QUEUE the cloud
    # models; FlightGear draws the queue when it finishes building a weather
    # tile. Remember the queue sizes so the new clouds can be drawn now.
    var lw = local_weather;
    var q_new = size(lw.cloudAssemblyArray);
    var q_old = size(lw.clouds_path);
    # FlightGear's radar echoes (/instrumentation/wxradar/storm[]) that exist
    # now, so the echo the create_* call adds can be replaced by ours.
    var wxr = props.globals.getNode("/instrumentation/wxradar", 1);
    var old_echo = {};
    foreach (var s; wxr.getChildren("storm"))
        old_echo[s.getIndex()] = 1;

    # Top of the weather. A shower's own volume ends 1000 ft above the cloud
    # base, so the radar beam passes over it: make it as tall as a real shower.
    var top = 20000;
    if (kind == "shower")
        top = base + 10000;
    elsif (kind == "thunderstorm" and radius > 5000)
        top = 24000;

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
        # Mark every volume this click made, so the export can tell them
        # from Detailed Weather's own volumes (see "source" in export_volumes).
        ev.wxradar_placed = kind;
        ev.wxradar_id = num(id);    # exported, so the app knows when it is gone
        append(rec.vols, ev);
        if (ev.rain_flag != 1) continue;
        ev.r1 = radius;
        ev.r2 = radius;
        ev.rain = rain;
        if (turb > 0) { ev.turb = turb; ev.turb_flag = 1; }
        else          { ev.turb = -1;   ev.turb_flag = 0; }
        if (kind == "shower") ev.alt_high = top;
        changed += 1;
    }

    # FlightGear radar echo with the asked size, rain and turbulence (showers
    # get none from Detailed Weather; storms get one with its default size).
    foreach (var s; wxr.getChildren("storm"))
        if (!contains(old_echo, s.getIndex()))
            s.remove();
    var i = 0;
    while (wxr.getChild("storm", i, 0) != nil) i += 1;
    var e = wxr.getChild("storm", i, 1);
    e.getNode("latitude-deg", 1).setValue(lat);
    e.getNode("longitude-deg", 1).setValue(lon);
    e.getNode("heading-deg", 1).setValue(0.0);
    e.getNode("base-altitude-ft", 1).setValue(base);
    e.getNode("top-altitude-ft", 1).setValue(top);
    e.getNode("radius-nm", 1).setValue(radius / 1852.0);
    e.getNode("reflectivity-norm", 1).setValue(rain);
    e.getNode("turbulence-norm", 1).setValue(turb);
    e.getNode("type", 1).setValue("single_cell");
    e.getNode("show", 1).setValue(1);
    rec.echo = e;
    placed[id] = rec;
    var shown = draw_queued_clouds(q_new, q_old, rec);
    p.getNode("result", 1).setValue(sprintf("OK: %s at %.4f %.4f, base %d ft, top %d ft, %d volume(s), %d cloud(s), FG echo %d",
                                            kind, lat, lon, base, top, changed, shown, i));
};

# Take the clouds that place_weather just queued out of Detailed Weather's
# queues and draw them now (a few per frame), instead of waiting for the next
# weather tile. Returns how many clouds were taken.
var draw_queued_clouds = func(q_new, q_old, rec) {
    var lw = local_weather;
    # New cloud system (cloud objects).
    var mine_new = [];
    var n = size(lw.cloudAssemblyArray);
    if (n > q_new) {
        mine_new = subvec(lw.cloudAssemblyArray, q_new);
        setsize(lw.cloudAssemblyArray, q_new);
    }
    # Model-path clouds (rain textures, older cloud models).
    var mine_old = [];
    n = size(lw.clouds_path);
    if (n > q_old) {
        for (var i = q_old; i < n; i += 1)
            append(mine_old, [lw.clouds_path[i], lw.clouds_lat[i], lw.clouds_lon[i],
                              lw.clouds_alt[i], lw.clouds_orientation[i]]);
        setsize(lw.clouds_path, q_old);
        setsize(lw.clouds_lat, q_old);
        setsize(lw.clouds_lon, q_old);
        setsize(lw.clouds_alt, q_old);
        setsize(lw.clouds_orientation, q_old);
        if (lw.dynamics_flag == 1 and size(lw.clouds_mean_alt) > q_old) {
            setsize(lw.clouds_mean_alt, q_old);
            setsize(lw.clouds_flt, q_old);
            setsize(lw.clouds_evolution_timestamp, q_old);
        }
    }
    var k = 0;
    var step = func {
        var stop = math.min(size(mine_new) + size(mine_old), k + 20);
        for (; k < stop; k += 1) {
            if (k < size(mine_new)) {
                lw.create_cloud_new(mine_new[k]);
                append(rec.clouds, mine_new[k]);
            } else {
                var c = mine_old[k - size(mine_new)];
                lw.create_cloud(c[0], c[1], c[2], c[3], c[4]);
                # The nodes create_cloud just made (remember them for removal).
                var cn = props.globals.getNode("/local-weather/clouds", 1);
                var t = cn.getChild("tile", getprop("/local-weather/tiles/tile-counter") or 0, 0);
                var cl = (t == nil) ? nil : t.getChild("cloud", cn.getValue("placement-index") or 0, 0);
                var m = props.globals.getNode("/models", 1).getChild("model",
                            cn.getValue("model-placement-index") or 0, 0);
                append(rec.models, [cl, m]);
            }
        }
        if (k < size(mine_new) + size(mine_old))
            settimer(step, 0, 1);
    };
    call(step, [], nil, nil, var err = []);
    if (size(err)) logprint(LOG_WARN, "wxradar_export draw clouds: ", err[0]);
    return size(mine_new) + size(mine_old);
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

# Remove placed storms: clouds, effect volumes and the FlightGear radar echo.
var index_of = func(vec, x) {
    forindex (var i; vec)
        if (vec[i] == x) return i;
    return -1;
};

var remove_placed = func(id) {
    if (!contains(placed, id)) return [0, 0];
    var rec = placed[id];
    delete(placed, id);
    var lw = local_weather;
    var wtm = weather_tile_management;
    var nv = 0;
    foreach (var ev; rec.vols) {
        var i = index_of(lw.effectVolumeArray, ev);
        if (i < 0) continue;            # already gone (its tile was removed)
        lw.effectVolumeArray = wtm.delete_from_vector(lw.effectVolumeArray, i);
        lw.n_effectVolumeArray = size(lw.effectVolumeArray);
        nv += 1;
    }
    var nc = 0;
    foreach (var c; rec.clouds) {
        var i = index_of(wtm.cloudArray, c);
        if (i >= 0) wtm.cloudArray = wtm.delete_from_vector(wtm.cloudArray, i);
        c.remove();
        nc += 1;
    }
    foreach (var pair; rec.models) {
        var cl = pair[0];
        var m = pair[1];
        if (m != nil) {
            var path = m.getPath();
            for (var i = size(wtm.cloudSceneryArray) - 1; i >= 0; i -= 1)
                if (wtm.cloudSceneryArray[i].modelNode.getPath() == path)
                    wtm.cloudSceneryArray = wtm.delete_from_vector(wtm.cloudSceneryArray, i);
            m.remove();
        }
        if (cl != nil) cl.remove();
        nc += 1;
    }
    if (rec.echo != nil) rec.echo.remove();
    return [nv, nc];
};

# The app writes the numbers to remove ("all" = every placed storm) to
# /wxradar/place/remove-ids, then a new number in remove-request.
setlistener("/wxradar/place/remove-request", func(n) {
    if (!alive()) return;
    var p = props.globals.getNode("/wxradar/place", 1);
    if (n.getValue() == p.getValue("remove-done")) return;
    p.getNode("remove-done", 1).setValue(n.getValue());
    var text = p.getValue("remove-ids") or "";
    var ids = (text == "all") ? keys(placed) : split(",", text);
    var nv = 0;
    var nc = 0;
    var err = [];
    foreach (var id; ids) {
        if (id == "") continue;
        var r = call(remove_placed, ["" ~ id], nil, nil, err);
        if (size(err)) break;
        nv += r[0];
        nc += r[1];
    }
    if (text == "all" and !size(err)) {
        # Also volumes placed before this script was (re)loaded: not in 'placed'.
        var lw = local_weather;
        for (var i = size(lw.effectVolumeArray) - 1; i >= 0; i -= 1)
            if (contains(lw.effectVolumeArray[i], "wxradar_placed")) {
                lw.effectVolumeArray = weather_tile_management.delete_from_vector(
                    lw.effectVolumeArray, i);
                nv += 1;
            }
        lw.n_effectVolumeArray = size(lw.effectVolumeArray);
    }
    if (size(err)) {
        p.getNode("remove-result", 1).setValue("FAILED: " ~ err[0]);
        logprint(LOG_WARN, "wxradar_export remove: ", err[0]);
    } else
        p.getNode("remove-result", 1).setValue(sprintf("REMOVED: %d volume(s), %d cloud(s)", nv, nc));
}, 0, 1);

settimer(volume_loop, 2, 1);
settimer(terrain_loop, 3, 1);
