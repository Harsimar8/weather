"""Weather radar app: control the weather and watch it on the radar.

Run:  python main.py
"""
import math
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QPushButton, QScrollArea,
                               QSlider, QSpinBox, QVBoxLayout, QWidget)

from display import RadarDisplay
from fg_link import FGLink
from radar import RadarScanner
from weather import SCENARIOS, WeatherModel, FlightGearWeather, parse_metar
SIM_SPEED_KT = 250.0


def slider(lo, hi, value):
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(value)
    return s


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Weather Radar")
        self.weather = WeatherModel()
        self.scanner = RadarScanner()
        self.display = RadarDisplay(self.scanner)
        self.fg = None
        self.origin = None          # lat/lon that maps to (0, 0) NM
        self.ax = self.ay = 0.0     # aircraft position (NM)
        self.heading = 0.0
        self.fg_metar = None        # last METAR seen in FlightGear
        self.sent_metar = None      # last METAR we sent to FlightGear
        self.sent_state = None      # app weather at the moment we sent it
        self.was_fg_mode = False    # Mode was FlightGear on the last tick
        self.shown_state = None     # FlightGear values shown in the weather box
        self.synced_clouds = None   # Detailed Weather clouds already on the radar
        self.last_strike = None     # last lightning strike already applied
        self.notice = ("", 0.0)     # short message kept under the live status

        self._build_ui()
        self.regenerate()
        self.last = time.perf_counter()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)

    # ----------------------------------------------------------------- UI
    def _build_ui(self):
        panel = QVBoxLayout()

        src = QGroupBox("Source")
        f = QFormLayout(src)
        self.source = QComboBox()
        self.source.addItems(["Simulated", "FlightGear"])
        self.host = QLineEdit("localhost")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5401)
        self.connect_btn = QPushButton("Connect to FlightGear")
        self.connect_btn.clicked.connect(self.toggle_fg)
        self.status = QLabel("Not connected")
        f.addRow("Mode", self.source)
        f.addRow("Host", self.host)
        f.addRow("Telnet port", self.port)
        f.addRow(self.connect_btn)
        f.addRow(self.status)
        panel.addWidget(src)

        wx = QGroupBox("Weather")
        f = QFormLayout(wx)
        self.scenario = QComboBox()
        self.scenario.addItems(SCENARIOS)
        self.scenario.setCurrentText("Scattered Cb")
        self.scenario.currentTextChanged.connect(self.regenerate)
        self.intensity = slider(30, 150, 100)
        self.coverage = slider(50, 200, 100)
        self.wind_dir = slider(0, 359, 270)
        self.wind_kt = slider(0, 80, 25)
        self.regen = QPushButton("Regenerate storms")
        self.regen.clicked.connect(self.regenerate)
        self.send_btn = QPushButton("Send weather to FlightGear")
        self.send_btn.clicked.connect(self.send_weather)
        self.metar = QLabel()
        self.metar.setWordWrap(True)
        self.metar.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow("Scenario", self.scenario)
        f.addRow("Storm strength", self.intensity)
        f.addRow("Coverage", self.coverage)
        f.addRow("Wind from", self.wind_dir)
        f.addRow("Wind speed", self.wind_kt)
        f.addRow(self.regen)
        f.addRow(self.send_btn)
        f.addRow("METAR", self.metar)
        self.coverage.sliderReleased.connect(self.regenerate)
        panel.addWidget(wx)

        rd = QGroupBox("Radar")
        f = QFormLayout(rd)
        self.range = QComboBox()
        self.range.addItems(["10", "20", "40", "80", "160", "320"])
        self.range.setCurrentText("80")
        self.range.currentTextChanged.connect(self.full_refresh)
        self.gain = slider(50, 150, 100)
        self.atten = QCheckBox("Attenuation")
        self.atten.setChecked(True)
        self.turb = QCheckBox("TURB mode (40 NM)")
        save = QPushButton("Save radar image")
        save.clicked.connect(self.save_image)
        f.addRow("Range (NM)", self.range)
        f.addRow("Gain", self.gain)
        f.addRow(self.atten)
        f.addRow(self.turb)
        f.addRow(save)
        panel.addWidget(rd)

        sim = QGroupBox("Simulated aircraft")
        f = QFormLayout(sim)
        self.sim_heading = slider(0, 359, 0)
        self.time_scale = slider(1, 30, 10)
        f.addRow("Heading", self.sim_heading)
        f.addRow("Time speed-up", self.time_scale)
        panel.addWidget(sim)

        here = QGroupBox("FlightGear weather (live)")
        v = QVBoxLayout(here)
        self.fg_wx = QLabel("Connect to FlightGear to see its weather")
        self.fg_wx.setWordWrap(True)
        self.fg_wx.setTextFormat(Qt.RichText)
        v.addWidget(self.fg_wx)
        panel.addWidget(here)
        panel.addStretch()

        left = QWidget()
        left.setLayout(panel)
        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(320)
        root = QWidget()
        lay = QHBoxLayout(root)
        lay.addWidget(scroll)
        lay.addWidget(self.display, 1)
        self.setCentralWidget(root)
        self.resize(1100, 680)

    # ------------------------------------------------------------ actions
    def regenerate(self, *_):
        self.weather.coverage = self.coverage.value() / 100
        self.weather.generate(self.scenario.currentText(), self.ax, self.ay, self.heading)
        self.full_refresh()

    def full_refresh(self, *_):
        self.display.refresh_all(self._scan())

    def toggle_fg(self):
        if self.fg and self.fg.connected:
            self.fg.close()
            self.fg = None
            self.status.setText("Disconnected")
            self.connect_btn.setText("Connect to FlightGear")
            return
        try:
            self.fg = FGLink(self.host.text(), self.port.value())
            self.fg.connect()
        except OSError as exc:
            self.fg = None
            self.status.setText(f"Could not connect: {exc}")
            return
        self.origin = None
        self.fg_metar = None
        self.synced_clouds = self.last_strike = None
        self.source.setCurrentText("FlightGear")
        self.status.setText("Connected, waiting for data...")
        self.connect_btn.setText("Disconnect")

    def send_weather(self):
        metar = self.weather.to_metar()
        if not (self.fg and self.fg.connected):
            self.status.setText("Connect to FlightGear first")
            return
        try:
            self.fg.send_metar(metar)
            self.sent_metar = metar
            self.sent_state = self._wx_state()
            self._notify("Weather sent to FlightGear")
        except OSError as exc:
            self._notify(f"Send failed: {exc}")

    def _notify(self, text, seconds=4.0):
        """Show text for a few seconds, even while the status updates every frame."""
        self.notice = (text, time.perf_counter() + seconds)
        self._set_status("")

    def _set_status(self, text):
        msg, until = self.notice
        if msg and time.perf_counter() < until:
            text = f"{text}\n{msg}" if text else msg
        self.status.setText(text)

    def _show_fg_weather(self, s):
        """Fill the 'FlightGear weather' box with everything FlightGear reports."""
        def level(v):
            if v is None:
                return "?"
            if v <= 0.01:
                return "none"
            word = "light" if v < 0.35 else ("moderate" if v < 0.7 else "heavy")
            return f"{word} ({v:.2f})"

        def num(key, fmt, unit=""):
            v = s.get(key)
            return "?" if v is None else f"{v:{fmt}}{unit}"

        def vis(key):
            v = s.get(key)
            if v is None:
                return "?"
            return f"{v / 1000:.1f} km" if v >= 1000 else f"{v:.0f} m"

        rows = [("<b>At your plane</b>", ""),
                ("Visibility", vis("vis_m")),
                ("Rain", level(s.get("rain"))),
                ("Snow", level(s.get("snow"))),
                ("Turbulence", level(s.get("turb"))),
                ("Temp / dew point", f"{num('temp_c', '.0f')} / {num('dew_c', '.0f')} °C"),
                ("Humidity", num("rh", ".0f", " %")),
                ("Pressure (sea level)", f"{num('qnh_inhg', '.2f')} inHg")]

        if s.get("lw_code"):
            def zone(key):
                n = s.get(key)
                return "?" if n is None else ("<b>YES</b>" if n > 0 else "no")

            layers = []
            for i in range(4):
                octs, ft = s.get(f"layer{i}_oct"), s.get(f"layer{i}_ft")
                if octs and ft is not None:
                    layers.append(f"{octs:.0f}/8 at {ft:.0f} ft")
            alt = s.get("cloud_alt_ft")
            rows += [("<b>Detailed Weather</b>", ""),
                     ("Weather type", s["lw_code"].replace("_", " ")),
                     ("Station", s.get("station") or "?"),
                     ("Thunderstorms at station", "yes" if s.get("stn_thunder") else "no"),
                     ("Cloud layers", ", ".join(layers) or "none"),
                     ("Storm clouds read", str(len(s.get("clouds") or {}))),
                     ("Their altitude", "?" if not alt else
                      (f"{alt[0]:.0f} ft" if alt[0] == alt[1]
                       else f"{alt[0]:.0f} - {alt[1]:.0f} ft")),
                     ("Area visibility", vis("area_vis_m")),
                     ("Area rain", level(s.get("area_rain"))),
                     ("Area snow", level(s.get("area_snow"))),
                     ("Area turbulence", level(s.get("area_turb"))),
                     ("Thermal lift", num("area_lift", ".0f")),
                     ("Weather zones", num("zones", ".0f")),
                     ("Inside a rain zone", zone("in_rain")),
                     ("Inside a snow zone", zone("in_snow")),
                     ("Inside a turbulence zone", zone("in_turb")),
                     ("Inside a low-visibility zone", zone("in_vis")),
                     ("Inside a lift zone", zone("in_lift"))]
            if s.get("strike_lat"):
                rows.append(("Last lightning", f"{num('strike_alt', '.0f')} ft, "
                             f"{s['strike_lat']:.3f}, {s['strike_lon']:.3f}"))
        else:
            rows.append(("<i>Detailed Weather is off: no zones, layers or "
                         "cloud positions</i>", ""))

        html = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in rows)
        self.fg_wx.setText(f"<table cellspacing='2'>{html}</table>")

    def _wx_state(self):
        """What the app's weather looks like: same storms, scenario and strength."""
        return (self.weather.generation, self.weather.scenario, self.intensity.value())

    def save_image(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save radar image",
                                              "radar.png", "PNG (*.png)")
        if path:
            self.display.render_frame().save(path)

    # --------------------------------------------------------------- loop
    def _scan(self):
        return self.scanner.scan(
            self.weather, self.ax, self.ay, self.heading,
            float(self.range.currentText()), gain=self.gain.value() / 100,
            attenuation=self.atten.isChecked(), turb_mode=self.turb.isChecked())

    def _update_from_fg(self, follow_weather=True):
        """Position always follows FlightGear; wind and METAR only if follow_weather."""
        if not (self.fg and self.fg.connected):
            if self.fg and self.fg.error:
                self.status.setText(f"Connection lost: {self.fg.error}")
            return False
        s = self.fg.state
        if not s:
            return False
        if self.origin is None:
            self.origin = (s["lat"], s["lon"])
            self.ax = self.ay = 0.0
            self.heading = s["heading"]
            self.regenerate()
        self.ax, self.ay = self._to_nm(s["lat"], s["lon"])
        self.heading = s["heading"]
        if s is not self.shown_state:   # new values from FlightGear
            self.shown_state = s
            self._show_fg_weather(s)
        self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft")
        if not follow_weather:
            return True

        # Wind always comes from FlightGear; the sliders just show it.
        if "wind_from" in s:
            self.weather.wind_from = s["wind_from"]
            self.weather.wind_kt = s["wind_kt"]
            self.wind_dir.setValue(round(s["wind_from"]) % 360)
            self.wind_kt.setValue(round(s["wind_kt"]))

        if s.get("lw_code") and s.get("clouds"):
            self._update_detailed_weather(s)
            return True
        if s.get("lw_code") and s.get("lw_selected"):
            # Detailed Weather chosen but its engine made no clouds (not started
            # with Apply): fall back to the METAR.
            self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n"
                             "Detailed Weather has no clouds: using METAR")
        else:
            # Basic Weather (or a tile code left over from earlier).
            self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n"
                             "Basic Weather: storms drawn from the METAR")

        # Basic Weather: no positions, so build storms that match the METAR.
        # Weather changed inside FlightGear (not by our Send): show it on the radar.
        metar = s.get("metar", "")
        if self.weather.scenario not in SCENARIOS:
            # Just left Detailed Weather: replace its cloud echoes, even if the
            # METAR is unchanged or is the one we sent.
            self.fg_metar = metar
            self._apply_fg_weather(*(parse_metar(metar) or ("Clear", 1.0, 1.0)))
        elif metar and metar != self.fg_metar:
            self.fg_metar = metar
            parsed = parse_metar(metar)
            # Keep our storms if FlightGear still has the weather we sent (the
            # text may be stored a little differently) and we did not change them.
            ours = (self.sent_metar is not None
                    and parsed == parse_metar(self.sent_metar)
                    and self.sent_state == self._wx_state())
            if parsed and not ours:
                self._apply_fg_weather(*parsed)
        return True

    def _to_nm(self, lat, lon):
        """Lat/lon to x (east), y (north) in NM from the origin."""
        lat0, lon0 = self.origin
        return ((lon - lon0) * 60.0 * math.cos(math.radians(lat0)),
                (lat - lat0) * 60.0)

    def _update_detailed_weather(self, s):
        """Detailed Weather: storms sit on FlightGear's own clouds."""
        code, clouds = s["lw_code"], s["clouds"]
        if clouds is not self.synced_clouds or code != self.weather.scenario:
            self.synced_clouds = clouds
            self.weather.sync_clouds(
                {k: self._to_nm(*ll) for k, ll in clouds.items()}, code)
        strike = (s.get("strike_lat"), s.get("strike_lon"))
        if strike[0] and strike != self.last_strike:
            self.last_strike = strike
            self.weather.strike(*self._to_nm(*strike))
        self.intensity.setValue(100)
        self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n"
                         f"Detailed Weather: {len(clouds)} clouds ({code})")

    def _apply_fg_weather(self, scenario, intensity, coverage):
        self.scenario.blockSignals(True)
        self.scenario.setCurrentText(scenario)
        self.scenario.blockSignals(False)
        self.intensity.setValue(round(intensity * 100))
        self.coverage.setValue(round(coverage * 100))
        self.regenerate()

    def tick(self):
        now = time.perf_counter()
        dt = min(0.1, now - self.last)
        self.last = now

        self.weather.intensity = self.intensity.value() / 100
        self.weather.wind_from = float(self.wind_dir.value())
        self.weather.wind_kt = float(self.wind_kt.value())
        self.metar.setText(self.weather.to_metar())

        # While connected the aircraft always follows FlightGear. The Mode only
        # decides where the weather comes from: FlightGear, or the app (to Send).
        fg_mode = self.source.currentText() == "FlightGear"
        if fg_mode and not self.was_fg_mode:
            # Back in FlightGear mode: read FlightGear's weather again, so edits
            # made in Simulated mode (and not sent) leave the radar.
            self.fg_metar = None
        self.was_fg_mode = fg_mode
        fg_live = self._update_from_fg(follow_weather=fg_mode)
        fg_weather = fg_live and fg_mode
        fg_wind = fg_weather and "wind_from" in self.fg.state
        # Weather comes from FlightGear when live; the pilot only has radar controls.
        for w in (self.scenario, self.intensity):
            w.setEnabled(not fg_weather)
        # Coverage and storm layout cannot be sent in a METAR: locked whenever connected.
        self.coverage.setEnabled(not fg_live)
        self.regen.setEnabled(not fg_live)
        self.wind_dir.setEnabled(not fg_wind)
        self.wind_kt.setEnabled(not fg_wind)
        self.sim_heading.setEnabled(not fg_live)
        self.time_scale.setEnabled(not fg_live)
        # Send is for preparing weather: connected, but in Simulated mode.
        self.send_btn.setEnabled(fg_live and not fg_mode)
        if fg_live:
            scale, source = 1.0, "FLIGHTGEAR" if fg_mode else "FG  WX EDIT"
        else:
            scale, source = float(self.time_scale.value()), "SIM"
            self.heading = float(self.sim_heading.value())
            dist = SIM_SPEED_KT / 3600.0 * dt * scale
            self.ax += dist * math.sin(math.radians(self.heading))
            self.ay += dist * math.cos(math.radians(self.heading))

        self.weather.step(dt * scale)
        self.display.info.update(heading=self.heading, range=int(self.range.currentText()),
                                 gain=self.gain.value() / 100,
                                 turb=self.turb.isChecked(), source=source)
        self.display.advance(self._scan(), dt)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
