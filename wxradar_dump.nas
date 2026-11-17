# Lists everything Detailed Weather (the local_weather Nasal namespace) keeps
# inside Nasal: its lists, values, objects and functions.
#
# Sent by dump_detailed_weather.py through FlightGear's HTTP server. It can
# also be pasted into Debug > Nasal Console. The text goes to the property
# /wxradar/debug/nasal-dump and RUN_ID to /wxradar/debug/finished-run when done.
# It only reads: nothing in the weather is changed.

var RUN_ID = "console";   # dump_detailed_weather.py puts a new id here each run
var MAX_ITEMS = 2;    # items shown from each list
var MAX_KEYS = 80;    # fields shown from each hash / object
var MAX_DEPTH = 3;    # how deep to look inside lists and hashes
var MAX_TEXT = 100;   # characters shown from a text value

var out = [];
var put = func(s) { append(out, s); };

var as_text = func(v) { return (v == nil) ? "nil" : "" ~ v; };
var by_name = func(a, b) { return cmp(as_text(a), as_text(b)); };

var scalar_text = func(v) {
    var s = as_text(v);
    if (size(s) > MAX_TEXT) s = substr(s, 0, MAX_TEXT) ~ "...";
    return (num(v) == nil) ? '"' ~ s ~ '"' : s;
};

var describe = func(name, v, indent, depth) {
    var t = typeof(v);
    if (t == "vector") {
        put(indent ~ name ~ " = list of " ~ size(v));
        if (depth < MAX_DEPTH) {
            var n = math.min(size(v), MAX_ITEMS);
            for (var i = 0; i < n; i += 1)
                describe("[" ~ i ~ "]", v[i], indent ~ "    ", depth + 1);
        }
    } elsif (t == "hash") {
        var ks = sort(keys(v), by_name);
        var obj = (contains(v, "parents") and typeof(v.parents) == "vector") ? " (object)" : "";
        put(indent ~ name ~ " = hash with " ~ size(ks) ~ " fields" ~ obj);
        if (depth < MAX_DEPTH) {
            var shown = 0;
            foreach (var k; ks) {
                if (k == "parents") continue;
                if (shown >= MAX_KEYS) {
                    put(indent ~ "    ... " ~ (size(ks) - shown) ~ " more fields");
                    break;
                }
                describe(as_text(k), v[k], indent ~ "    ", depth + 1);
                shown += 1;
            }
        }
    } elsif (t == "func") {
        put(indent ~ name ~ " = function");
    } elsif (t == "scalar") {
        put(indent ~ name ~ " = " ~ scalar_text(v));
    } else {
        put(indent ~ name ~ " = " ~ t);
    }
};

var dump = func {
    put("Detailed Weather enabled: " ~ as_text(getprop("/nasal/local_weather/enabled")));
    put("Tile code: " ~ as_text(getprop("/local-weather/tiles/code")));
    put("");
    put("== Weather-related names in globals ==");
    foreach (var k; sort(keys(globals), by_name)) {
        var s = as_text(k);
        if (find("weather", s) >= 0 or find("Weather", s) >= 0 or find("lightning", s) >= 0
            or find("cloud", s) >= 0 or find("environment", s) >= 0)
            put("  " ~ s ~ " : " ~ typeof(globals[k]));
    }
    put("");
    if (!contains(globals, "local_weather")) {
        put("local_weather namespace NOT FOUND (is Detailed Weather loaded?)");
        return;
    }
    var ns = globals["local_weather"];
    var funcs = [];
    var data = [];
    foreach (var k; sort(keys(ns), by_name)) {
        if (typeof(ns[k]) == "func") append(funcs, as_text(k));
        else append(data, k);
    }
    # Thunderstorms: every one, not just the first items.
    if (contains(ns, "thunderstormArray")) {
        put("== All thunderstorms (" ~ size(ns.thunderstormArray) ~ ") ==");
        forindex (var i; ns.thunderstormArray)
            describe("storm[" ~ i ~ "]", ns.thunderstormArray[i], "  ", 1);
        put("");
    }
    put("== local_weather values and lists (" ~ size(data) ~ ") ==");
    foreach (var k; data)
        describe(as_text(k), ns[k], "  ", 0);
    put("");
    put("== local_weather functions (" ~ size(funcs) ~ ") ==");
    for (var i = 0; i < size(funcs); i += 4)
        put("  " ~ string.join(", ", subvec(funcs, i, math.min(4, size(funcs) - i))));
};

var err = [];
call(dump, [], nil, nil, err);
if (size(err)) put("ERROR: " ~ as_text(err[0]));
setprop("/wxradar/debug/nasal-dump", string.join("\n", out));
setprop("/wxradar/debug/finished-run", RUN_ID);
