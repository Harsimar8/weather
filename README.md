# Weather radar app

An Airbus-style weather radar display that you control from Python.
It works on its own with simulated flight, and can connect to FlightGear.

## Install

    pip install -r requirements.txt

## Run

    python main.py

## Weather radar panel

The display is a Boeing-style ND in MAP mode (heading box, compass arc,
range rings, GS/TAS and wind). The radar annunciation (for example
`WX+T` / `-1.5A`) is on the left.

- **WXR**: the EFIS switch. Off removes the weather from the ND.
- **Mode**: `OFF`, `STBY` (radar on but not sending a beam), `TEST` (color
  test pattern plus the windshear test icon), `WX`, `WX+T` (turbulence in
  magenta within 40 NM) and `MAP` (ground mapping: cyan/yellow/magenta).
- **Predictive windshear (PWS)**: works on its own below 2300 ft above the
  ground, whatever the mode. It looks 5 NM ahead (+/- 40 deg) for a
  microburst (heavy rain with violent air low down). Shown as red/black
  bands with two amber lines to the edge of the screen. Below 1200 ft:
  amber `WINDSHEAR` + "Monitor radar display" (caution, farther out), or red
  `WINDSHEAR` + "Windshear ahead" (take-off) / "Go around, windshear ahead"
  (descending) when within 3 NM and +/- 25 deg. To test in Simulated mode:
  set Altitude below 1200 ft and press "Add microburst ahead".
- **Track up**: the ND is turned to the track (`TRK ... MAG`), where the
  aircraft really goes with the wind. A white triangle on the compass shows
  the heading and the magenta notched box the selected heading (FlightGear's
  heading bug; in Simulated mode the aircraft turns to it at 3 deg/s).
- The scan covers +/- 90 deg and is smoothed, like a real radar picture.
- **Tilt**: -15 to +15 degrees. `AUTO` keeps the bottom of the beam on the
  ground just past the selected range (shown with an `A`). Tilt down by hand
  and ground clutter fills the screen.
- **Gain**: `CAL` (calibrated) or a manual offset of -10 to +10 dB (`VAR`).
- **Range**: 10, 20, 40, 80, 160 or 320 NM.

Colors follow reflectivity: green 20-30 dBZ, yellow 30-40, red 40-50,
magenta 50+ (snow is cyan, mixed rain and snow is white). The beam is 3.5
degrees wide and only sees weather at the heights it passes through, so a
storm can be missed if the beam goes over its top. Simulated storms get a
height from their strength. FlightGear weather volumes use their own base and
top altitudes.

## Files

- `main.py`: the window, controls, and update loop
- `weather.py`: storm cells, scenarios, wind drift, and the METAR builder
- `radar.py`: scans the weather and turns it into green/yellow/red/magenta levels
- `display.py`: draws the radar screen (arc, range rings, heading scale, sweep)
- `fg_link.py`: talks to FlightGear over telnet

## Using it with FlightGear

1. Start FlightGear with these extra options:

       --telnet=5401 --disable-real-weather-fetch

   Load/run `wxradar_export.nas` once in FlightGear. It refreshes the exported
   weather-volume properties every 2 s while FlightGear is running.
2. In the app, click "Connect to FlightGear". The radar now follows the
   aircraft's position and heading.
3. While connected in FlightGear mode, the radar follows FlightGear's weather
   and only the radar controls (range, gain, attenuation, TURB) can be used:
   - Wind always comes from FlightGear (the wind sliders are locked).
   - Scenario, storm strength, coverage and "Regenerate storms" are locked.
     If the METAR changes inside FlightGear, they update to match it.
   - Snow in the METAR is read too: `SN`/`SG` (any intensity, also `SHSN`,
     `TSSN`) gives snow (cyan, weaker echo), and rain with snow (`RASN`)
     gives mixed (white). The **Precipitation** box shows which one.
   - FlightGear's exported weather volumes (`/wxradar/volumes`) are checked
     every 2 s in either weather mode. Rain is green/yellow/red, snow is cyan,
     mixed rain and snow is white, and strong turbulence can be magenta in TURB
     mode. This requires `wxradar_export.nas` to keep the volume properties
     updated; the radar only draws areas FlightGear exports as spatial volumes.
   - If no volumes are exported, **Basic Weather** falls back to approximate
     echoes derived from the METAR (not FlightGear's exact weather locations).
     The live FlightGear panel still shows available conditions at the aircraft.
   - If no volumes are exported and **Detailed Weather** is running, the app
     reads FlightGear's cloud positions (`/local-weather/clouds/...`) every
     10 s and puts an echo on each cloud, strength from the weather type
     (`/local-weather/tiles/code`). Lightning strikes
     (`/local-weather/lightning`) turn the nearest cloud into a red storm core
     with turbulence. The app moves the echoes with FlightGear's wind.
4. To set FlightGear's weather from the app, switch Mode to "Simulated"
   while still connected, set up the weather, and click
   "Send weather to FlightGear" (only enabled in that case). The aircraft
   keeps following FlightGear's position and heading in this mode.
   Coverage stays locked while connected, because a METAR cannot carry it.

### More from FlightGear

- **Magnetic heading**: the ND shows `HDG ... MAG` from FlightGear's magnetic
  heading, and the wind is turned to magnetic too.
- **Pause and speed-up**: storms stop drifting while FlightGear is paused
  (`PAUSED` on the ND) and drift faster with time speed-up.
- **Connection**: the app waits up to 10 s for an answer (FlightGear freezes
  for a few seconds on Apply or while loading scenery). If the link drops it
  reconnects by itself and says so under the Connect button. One odd reply is
  skipped instead of dropping the link.
- **Basic Weather, Manual Configuration**: the radar uses the cloud layers
  (coverage, base, thickness), the Rain/Snow sliders and the turbulence of
  the Aloft/Boundary table at the height the beam looks at. Without Manual
  Configuration the METAR is used, as before.
- **Freezing level**: from FlightGear's temperature-by-altitude table (in
  Simulated mode, set it in the Weather box). Above it rain is ice and shows
  about 7 dB weaker; just below it melting snow shows about 5 dB stronger
  (the bright band).
- **Real terrain**: `wxradar_export.nas` also builds a 90 x 90 NM grid of
  terrain height and land/water around the aircraft (rebuilt every 6 NM or
  60 s). Ground clutter and MAP mode then show real hills, mountains and
  coastlines (water gives almost no echo), and mountains hide what is behind
  them. Only scenery FlightGear has loaded is known; beyond it the old
  made-up ground texture is used.

`wxradar_export.nas` can be loaded more than once (Nasal folder at start-up
plus `test_fg_export.py`): each copy stops the older ones, so the work is
never doubled.

Check the METAR property name in FlightGear under
Debug > Browse Internal Properties > environment. If it differs from
`/environment/metar/data`, change `METAR_PROPERTY` in `fg_link.py`.
