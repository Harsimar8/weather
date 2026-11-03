# Weather radar app

An Airbus-style weather radar display that you control from Python.
It works on its own with simulated flight, and can connect to FlightGear.

## Install

    pip install -r requirements.txt

## Run

    python main.py

## Files

- `main.py`: the window, controls, and update loop
- `weather.py`: storm cells, scenarios, wind drift, and the METAR builder
- `radar.py`: scans the weather and turns it into green/yellow/red/magenta levels
- `display.py`: draws the radar screen (arc, range rings, heading scale, sweep)
- `fg_link.py`: talks to FlightGear over telnet

## Using it with FlightGear

1. Start FlightGear with these extra options:

       --telnet=5401 --disable-real-weather-fetch

2. In the app, click "Connect to FlightGear". The radar now follows the
   aircraft's position and heading.
3. While connected in FlightGear mode, the radar follows FlightGear's weather
   and only the radar controls (range, gain, attenuation, TURB) can be used:
   - Wind always comes from FlightGear (the wind sliders are locked).
   - Scenario, storm strength, coverage and "Regenerate storms" are locked.
     If the METAR changes inside FlightGear, they update to match it.
   - With **Basic Weather**, FlightGear gives no storm positions, so the app
     builds storms that match the METAR (right type, invented places).
   - With **Detailed Weather** running, the app reads FlightGear's cloud
     positions (`/local-weather/clouds/...`) every 10 s and puts an echo on
     each cloud, strength from the weather type (`/local-weather/tiles/code`).
     Lightning strikes (`/local-weather/lightning`) turn the nearest cloud
     into a red storm core with turbulence. The app moves the echoes with
     FlightGear's wind.
4. To set FlightGear's weather from the app, switch Mode to "Simulated"
   while still connected, set up the weather, and click
   "Send weather to FlightGear" (only enabled in that case). The aircraft
   keeps following FlightGear's position and heading in this mode.
   Coverage stays locked while connected, because a METAR cannot carry it.

Check the METAR property name in FlightGear under
Debug > Browse Internal Properties > environment. If it differs from
`/environment/metar/data`, change `METAR_PROPERTY` in `fg_link.py`.
