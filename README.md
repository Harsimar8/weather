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
3. Click "Send weather to FlightGear" to send the current weather as a METAR.

Check the METAR property name in FlightGear under
Debug > Browse Internal Properties > environment. If it differs from
`/environment/metar/data`, change `METAR_PROPERTY` in `fg_link.py`.
