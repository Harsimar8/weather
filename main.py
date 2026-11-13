"""Weather radar app: control the weather and watch it on the radar.

Run:  python main.py
"""
import math
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDial, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QPushButton, QScrollArea, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

from display import RadarDisplay
from fg_link import FGLink
from radar import MODES, TILT_LIMIT_DEG, RadarScanner, auto_tilt, windshear
from weather import (PRECIP_KINDS, SCENARIOS, WeatherModel, FlightGearWeather,
                     parse_metar)
from basic_weather import LayerWeather, freezing_level_ft
from terrain import TerrainMap
SIM_SPEED_KT = 250.0
RESET_JUMP_NM = 5.0       # aircraft moved this far in one read: FlightGear was reset
GND_CLUTTER_SHARE = 0.01  # warn when ground echo paints over 1 % of the screen


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
        self.fg_weather = FlightGearWeather()
        self.layer_weather = LayerWeather()   # Basic Weather, Manual Configuration
        self.terrain = TerrainMap()
        self.freezing_ft = None               # freezing level, ft MSL
        self.manual_wx = False                # FlightGear uses Manual Configuration
        self.scanner = RadarScanner()
        self.display = RadarDisplay(self.scanner)
        self.fg = None
        self.origin = None          # lat/lon that maps to (0, 0) NM
        self.ax = self.ay = 0.0     # aircraft position (NM)
        self.heading = 0.0          # where the nose points (true)
        self.track = 0.0            # where the aircraft really goes (true); ND is track up
        self.sim_gs = SIM_SPEED_KT  # simulated ground speed (kt)
        self.pws = None             # windshear found by the PWS
        self.pws_next = 0.0         # next PWS look (s)
        self.pws_said = (None, 0.0)  # last spoken alert and when
        try:
            from PySide6.QtTextToSpeech import QTextToSpeech
            self.tts = QTextToSpeech(self)
        except Exception:           # no speech on this computer: beep instead
            self.tts = None
        self.fg_metar = None        # last METAR seen in FlightGear
        self.sent_metar = None      # last METAR we sent to FlightGear
        self.sent_state = None      # app weather at the moment we sent it
        self.was_fg_mode = False    # Mode was FlightGear on the last tick
        self.shown_state = None     # FlightGear values shown in the weather box
        self.synced_clouds = None   # Detailed Weather clouds already on the radar
        self.last_strike = None     # last lightning strike already applied
        self.notice = ("", 0.0)     # short message kept under the live status
        self.last_fg_pos = None     # lat/lon at the last FlightGear read (reset check)
        self.was_reinit = False     # FlightGear reset flag at the last read

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
        self.precip = QComboBox()
        self.precip.addItems(PRECIP_KINDS)
        self.precip.setToolTip("What falls from the storms. Snow shows cyan and "
                               "weaker; rain and snow together show white.")
        self.precip.currentTextChanged.connect(self._precip_changed)
        f.addRow("Scenario", self.scenario)
        f.addRow("Precipitation", self.precip)
        f.addRow("Storm strength", self.intensity)
        f.addRow("Coverage", self.coverage)
        f.addRow("Wind from", self.wind_dir)
        f.addRow("Wind speed", self.wind_kt)
        self.sim_freezing = QSpinBox()
        self.sim_freezing.setRange(0, 20000)
        self.sim_freezing.setSingleStep(1000)
        self.sim_freezing.setValue(12000)
        self.sim_freezing.setSuffix(" ft")
        self.sim_freezing.setToolTip("Height where it is 0 °C. Above it rain is ice "
                                     "and shows weaker on the radar.")
        f.addRow("Freezing level", self.sim_freezing)
        f.addRow(self.regen)
        self.burst_btn = QPushButton("Add microburst ahead (windshear test)")
        self.burst_btn.setToolTip("Puts a microburst 3.5 NM ahead. Fly below 1200 ft "
                                  "(Simulated aircraft > Altitude) to get the alerts.")
        self.burst_btn.clicked.connect(self.add_microburst)
        f.addRow(self.burst_btn)
        f.addRow(self.send_btn)
        f.addRow("METAR to send", self.metar)
        self.coverage.sliderReleased.connect(self.regenerate)
        panel.addWidget(wx)

        panel.addWidget(self._build_wxr_panel())

        sim = QGroupBox("Simulated aircraft")
        f = QFormLayout(sim)
        self.sim_heading = slider(0, 359, 0)
        self.sim_alt = QSpinBox()
        self.sim_alt.setRange(0, 45000)
        self.sim_alt.setSingleStep(1000)
        self.sim_alt.setValue(25000)
        self.sim_alt.setSuffix(" ft")
        self.time_scale = slider(1, 30, 10)
        self.sim_heading.setToolTip("Selected heading (the magenta bug). "
                                    "The aircraft turns to it at 3° per second.")
        f.addRow("Selected heading", self.sim_heading)
        f.addRow("Altitude", self.sim_alt)
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

    def _build_wxr_panel(self):
        """Weather radar control panel (pedestal) plus the EFIS WXR switch."""
        box = QGroupBox("Weather radar panel")
        box.setObjectName("wxrPanel")
        box.setStyleSheet("""
            #wxrPanel { background: #2a2e33; color: #e8e8e8; border: 1px solid #555;
                        border-radius: 6px; margin-top: 14px; padding-top: 6px; }
            #wxrPanel::title { subcontrol-origin: margin; left: 8px; color: palette(window-text); }
            #wxrPanel QLabel, #wxrPanel QCheckBox { color: #e8e8e8; }
            #wxrPanel QPushButton { background: #3b4046; color: #e8e8e8;
                        border: 1px solid #666; border-radius: 3px; padding: 4px 6px;
                        font-weight: bold; }
            #wxrPanel QPushButton:checked { background: #1d1f22; color: #3cff5a;
                        border: 1px solid #3cff5a; }
            #wxrPanel QPushButton:disabled { color: #777; }
        """)
        v = QVBoxLayout(box)

        top = QHBoxLayout()
        self.wxr_btn = QPushButton("WXR")
        self.wxr_btn.setCheckable(True)
        self.wxr_btn.setChecked(True)
        self.wxr_btn.setToolTip("EFIS WXR switch: shows the weather on the ND")
        self.wxr_btn.toggled.connect(self._wxr_changed)
        top.addWidget(QLabel("EFIS"))
        top.addWidget(self.wxr_btn)
        top.addStretch()
        top.addWidget(QLabel("Range"))
        self.range = QComboBox()
        self.range.addItems(["10", "20", "40", "80", "160", "320"])
        self.range.setCurrentText("80")
        self.range.currentTextChanged.connect(self.full_refresh)
        top.addWidget(self.range)
        top.addWidget(QLabel("NM"))
        v.addLayout(top)

        modes = QGridLayout()           # OFF STBY TEST / WX WX+T MAP
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for i, name in enumerate(MODES):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked(name == "WX")
            self.mode_group.addButton(b)
            modes.addWidget(b, i // 3, i % 3)
        self.mode_group.buttonClicked.connect(self.full_refresh)
        v.addLayout(modes)

        knobs = QHBoxLayout()
        # Tilt knob: -15 .. +15 degrees in quarter degrees
        tcol = QVBoxLayout()
        self.tilt = QDial()
        self.tilt.setRange(-60, 60)
        self.tilt.setValue(-4)
        self.tilt.setNotchesVisible(True)
        self.tilt.setFixedSize(80, 80)
        self.tilt_label = QLabel(alignment=Qt.AlignCenter)
        self.auto_tilt = QCheckBox("AUTO")
        self.auto_tilt.setChecked(True)
        self.auto_tilt.setToolTip("Tilt follows altitude and range, "
                                  "keeping ground returns off the screen")
        tcol.addWidget(QLabel("TILT", alignment=Qt.AlignCenter))
        tcol.addWidget(self.tilt, alignment=Qt.AlignCenter)
        tcol.addWidget(self.tilt_label)
        tcol.addWidget(self.auto_tilt, alignment=Qt.AlignCenter)
        knobs.addLayout(tcol)
        # Gain knob: CAL (calibrated) or a manual offset in dB
        gcol = QVBoxLayout()
        self.gain = QDial()
        self.gain.setRange(-10, 10)
        self.gain.setValue(0)
        self.gain.setNotchesVisible(True)
        self.gain.setFixedSize(80, 80)
        self.gain_label = QLabel(alignment=Qt.AlignCenter)
        self.gain_cal = QCheckBox("CAL")
        self.gain_cal.setChecked(True)
        self.gain_cal.setToolTip("Calibrated gain. Untick to set it by hand.")
        gcol.addWidget(QLabel("GAIN", alignment=Qt.AlignCenter))
        gcol.addWidget(self.gain, alignment=Qt.AlignCenter)
        gcol.addWidget(self.gain_label)
        gcol.addWidget(self.gain_cal, alignment=Qt.AlignCenter)
        knobs.addLayout(gcol)
        v.addLayout(knobs)

        self.atten = QCheckBox("Simulate attenuation")
        self.atten.setChecked(True)
        self.voice = QCheckBox("Windshear voice alerts")
        self.voice.setChecked(True)
        self.voice.setToolTip("Spoken PWS alerts: 'WINDSHEAR AHEAD', "
                              "'GO AROUND, WINDSHEAR AHEAD', 'MONITOR RADAR DISPLAY'")
        v.addWidget(self.voice)
        save = QPushButton("Save radar image")
        save.clicked.connect(self.save_image)
        v.addWidget(self.atten)
        v.addWidget(save)
        return box

    def _wxr_changed(self, on):
        for b in self.mode_group.buttons():
            b.setEnabled(on)
        if on:
            self.full_refresh()

    def _radar_mode(self):
        return self.mode_group.checkedButton().text()

    def _altitudes(self):
        """(aircraft altitude, ground elevation) in feet."""
        s = self.fg.state if (self.fg and self.fg.connected) else None
        if s:
            return s["alt_ft"], s.get("ground_ft", 0.0)
        return float(self.sim_alt.value()), 0.0

    def _tilt_deg(self):
        if self.auto_tilt.isChecked():
            alt, ground = self._altitudes()
            tilt = auto_tilt(alt - ground, float(self.range.currentText()))
            if self._radar_mode() == "MAP":
                tilt -= 2.0     # aim at the ground
            return max(-TILT_LIMIT_DEG, tilt)
        return self.tilt.value() / 4

    def _gain_db(self):
        return 0.0 if self.gain_cal.isChecked() else float(self.gain.value())

    # ------------------------------------------------------------ actions
    def regenerate(self, *_):
        self.weather.coverage = self.coverage.value() / 100
        self.weather.generate(self.scenario.currentText(), self.ax, self.ay, self.heading)
        self.full_refresh()

    def add_microburst(self):
        """Windshear test: a microburst 3.5 NM ahead on the present track."""
        self.weather.add_microburst(self.ax, self.ay, self.track)
        self.full_refresh()
        self._notify("Microburst 3.5 NM ahead. Below 1200 ft the PWS will alert.")

    def _speak(self, text):
        if self.tts is not None:
            self.tts.say(text)
        else:
            QApplication.beep()

    def _update_pws(self, now, agl_ft, vs_fps):
        """Predictive windshear: look a few times a second, alert like a Boeing."""
        if now >= self.pws_next:
            self.pws_next = now + 0.25
            self.pws = windshear(self._scan_weather(), self.ax, self.ay,
                                 self.track, agl_ft, self._altitudes()[1])
        level = self.pws["level"] if self.pws else None
        if level not in ("warning", "caution"):
            self.pws_said = (None, 0.0)
            return
        said, when = self.pws_said
        again = level == "warning" and now - when > 8.0     # warning repeats
        if (level != said or again) and self.voice.isChecked():
            if level == "caution":
                self._speak("Monitor radar display")
            elif vs_fps < -3.0:                             # descending: approach
                self._speak("Go around, windshear ahead")
            else:                                           # take-off
                self._speak("Windshear ahead, windshear ahead")
        if level != said or again:
            self.pws_said = (level, now)

    def _precip_changed(self, kind):
        self.weather.precip = kind
        self.full_refresh()

    def full_refresh(self, *_):
        self.display.refresh_all(self._scan())

    def toggle_fg(self):
        if self.fg:     # connected, or trying to reconnect
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
        self.terrain = TerrainMap()
        self.last_fg_pos, self.was_reinit = None, False
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

        cfg = s.get("cfg") or {}
        fl = self.freezing_ft
        rows = [("<b>At your plane</b>", ""),
                ("Weather mode", "Detailed Weather" if s.get("lw_selected")
                 else ("Basic Weather (Manual Configuration)" if cfg.get("manual")
                       else "Basic Weather")),
                ("Paused", "YES" if s.get("frozen") else "no"),
                ("Time speed-up", num("speed_up", ".0f", "x")),
                ("Heading true / mag", f"{num('heading', '.0f')}° / {num('hdg_mag', '.0f')}°"),
                ("Freezing level", "?" if fl is None else f"{fl:,.0f} ft"),
                ("Real terrain", f"{self.terrain.known:.0%} of grid known"
                 if self.terrain.elev is not None else "none yet (needs wxradar_export.nas)"),
                ("METAR", s.get("metar") or "?"),
                ("Radar weather volumes", str(
                    len(s.get("weather_volumes") or []))),
                ("Visibility", vis("vis_m")),
                ("Rain", level(s.get("rain"))),
                ("Snow", level(s.get("snow"))),
                ("Turbulence", level(s.get("turb"))),
                ("Wind", f"{num('wind_from', '.0f')}° from, "
                 f"{num('wind_kt', '.0f')} kt"),
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
        return (self.weather.generation, self.weather.scenario, self.intensity.value(),
                self.weather.precip)

    def save_image(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save radar image",
                                              "radar.png", "PNG (*.png)")
        if path:
            self.display.render_frame().save(path)

    # --------------------------------------------------------------- loop
    def _scan_weather(self):
        """The weather the radar looks at right now."""
        fg_on = (self.source.currentText() == "FlightGear"
                 and self.fg and self.fg.connected)
        if fg_on and self.fg_weather.volumes:
            return self.fg_weather          # Detailed Weather zones
        if fg_on and self.manual_wx:
            return self.layer_weather       # Basic Weather, Manual Configuration
        return self.weather

    def _scan(self, rays=None):
        """Radar picture for a slice of rays (all rays when None).

        The ND is track up, so the picture is turned to the track.
        """
        scan_weather = self._scan_weather()
        alt, ground = self._altitudes()
        connected = self.fg and self.fg.connected
        return self.scanner.scan(
            scan_weather, self.ax, self.ay, self.track,
            float(self.range.currentText()),
            alt_ft=alt, ground_ft=ground,
            tilt_deg=self._tilt_deg(),
            gain_db=self._gain_db(),
            mode=self._radar_mode(),
            attenuation=self.atten.isChecked(),
            freezing_ft=self.freezing_ft,
            terrain=self.terrain if connected else None,
            rays=rays,
        )

    def _update_from_fg(self, follow_weather=True):
        """Position always follows FlightGear; wind and METAR only if follow_weather."""
        if not (self.fg and self.fg.connected):
            if self.fg and self.fg.reconnecting:
                self.status.setText(f"Connection lost ({self.fg.error}).\n"
                                    "Reconnecting... FlightGear may be busy.")
            elif self.fg and self.fg.error:
                self.status.setText(f"Connection lost: {self.fg.error}")
            return False
        s = self.fg.state
        if not s:
            return False
        if self._fg_was_reset(s):
            self._on_fg_reset(follow_weather)
        # Read before the reset: its clouds and volumes may be old. Use only
        # the position until fresh data arrives.
        stale = s.get("gen", 0) != self.fg.cache_gen
        if self.origin is None:
            self.origin = (s["lat"], s["lon"])
            self.ax = self.ay = 0.0
            self.heading = s["heading"]
            if follow_weather:
                # The weather comes from FlightGear: start empty, never with
                # the app's own storms (it fills in below).
                self.weather.cells = []
                self.weather.scenario = ""
                self.display.refresh_all(0)
            else:
                self.regenerate()
        self.ax, self.ay = self._to_nm(s["lat"], s["lon"])
        self.heading = s["heading"]
        # Track from the ground velocity. When slow (taxi, pushback), or when
        # it is far from the heading (sliding, just moved, paused with old
        # values), it means nothing: use the heading, like a real IRS on ground.
        vn, ve = s.get("v_north_fps"), s.get("v_east_fps")
        self.track = self.heading
        if vn is not None and ve is not None and math.hypot(vn, ve) > 100.0:  # ~60 kt
            trk = math.degrees(math.atan2(ve, vn)) % 360.0
            if abs((trk - self.heading + 540.0) % 360.0 - 180.0) <= 45.0:
                self.track = trk
        cfg = s.get("cfg") or {}
        ground = s.get("ground_ft", 0.0)
        self.manual_wx = bool(cfg.get("manual")) and not s.get("lw_selected")
        if cfg:
            self.layer_weather.update(cfg, s.get("rain"), s.get("snow"), ground)
        if follow_weather:
            self.freezing_ft = freezing_level_ft(cfg, ground, s.get("alt_ft"), s.get("temp_c"))
        if stale:
            if follow_weather:
                self.weather.cells = []     # no made-up storms while we wait
                self.weather.scenario = ""  # rebuilt from FlightGear afterwards
            self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n"
                             "FlightGear was reset: reading the weather again...")
            return True
        if s.get("terrain"):
            self.terrain.load(s["terrain"], self._to_nm, self.origin[0])
        self.fg_weather.update(
            s.get("weather_volumes", []),
            self.origin,
            s.get("alt_ft", 0.0)
        )
        if s is not self.shown_state:   # new values from FlightGear
            self.shown_state = s
            self._show_fg_weather(s)
        again = f"  (reconnected {self.fg.reconnects}x)" if self.fg.reconnects else ""
        self._set_status(f"Connected{again}  ALT {s['alt_ft']:.0f} ft")
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
        if self.manual_wx and not self.fg_weather.volumes:
            # Manual Configuration: the METAR is not used, the layers are.
            what = ("rain from the cloud layers" if self.layer_weather.volumes
                    else "no rain or snow set")
            self._set_status(f"Connected{again}  ALT {s['alt_ft']:.0f} ft\n"
                             f"Basic Weather, Manual Configuration: {what}")
            return True
        if s.get("lw_selected") and not s.get("clouds"):
            # Detailed Weather is on, but has no clouds yet (building after
            # Apply, or paused). Until they come, storms are drawn from the
            # scenario's METAR: the right kind of weather (e.g. TSRA for
            # Thunderstorm), but at made-up places. The screen says so.
            if self.fg_weather.volumes:
                status = (f"FlightGear radar volumes: "
                          f"{len(self.fg_weather.volumes)}")
            elif s.get("frozen"):
                status = ("Detailed Weather on, but FlightGear is PAUSED:\n"
                          "it builds the weather after you unpause.\n"
                          "Until then: storms from the METAR (made-up places)")
            else:
                status = ("Detailed Weather building clouds...\n"
                          "Until then: storms from the METAR (made-up places)")
            self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n{status}")
        elif self.fg_weather.volumes:
            self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n"
                             f"FlightGear radar volumes: "
                             f"{len(self.fg_weather.volumes)}")
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
            self._apply_fg_weather(*(parse_metar(metar) or ("Clear", 1.0, 1.0, "Rain")))
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

    def _fg_was_reset(self, s):
        """True once when FlightGear resets: its reset flag turns on, or the
        aircraft jumps further than it could fly between two reads."""
        pos = (s["lat"], s["lon"])
        jump = False
        if self.last_fg_pos is not None:
            dy = (pos[0] - self.last_fg_pos[0]) * 60.0
            dx = (pos[1] - self.last_fg_pos[1]) * 60.0 * math.cos(math.radians(pos[0]))
            jump = math.hypot(dx, dy) > RESET_JUMP_NM
        self.last_fg_pos = pos
        reinit = bool(s.get("reinit"))
        started = reinit and not self.was_reinit
        self.was_reinit = reinit
        return jump or started

    def _on_fg_reset(self, follow_weather):
        """FlightGear was reset: drop everything read before, read it again.

        FlightGear keeps its Detailed Weather over a reset, so weather can come
        back on the radar: that is FlightGear's real weather, not old data.
        """
        self.fg.clear_cache()
        self.origin = None                  # centre the map on the new position
        self.synced_clouds = self.last_strike = None
        self.shown_state = None
        self.fg_metar = None
        self.fg_weather.update([], None, 0.0)
        self.terrain = TerrainMap()
        self.pws = None
        if follow_weather:
            self.weather.cells = []
            self.weather.scenario = ""
        self.scanner.ground_mask[:] = False
        self.display.refresh_all(0)
        self._notify("FlightGear was reset: radar cleared, weather read again", 6.0)

    def _wx_source(self):
        """Label for the screen: where the weather on the radar comes from,
        and whether it is only approximate (shown amber)."""
        fg_on = (self.source.currentText() == "FlightGear"
                 and self.fg and self.fg.connected)
        if not fg_on:
            return ("SIM WX", False)
        s = self.fg.state or {}
        if s.get("gen", 0) != self.fg.cache_gen:
            return ("WX RESET", True)
        if self.fg_weather.volumes:
            return (f"FG VOLUMES {len(self.fg_weather.volumes)}", False)
        if self.manual_wx:
            return ("FG LAYERS", False)
        if s.get("lw_code") and s.get("clouds"):
            return (f"CLOUD ECHO {len(s['clouds'])}", True)
        if s.get("lw_selected"):
            return ("METAR TEMP", True)     # Detailed Weather still building
        return ("FG METAR", True)

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
        radar_source = (
            f"FlightGear radar volumes: {len(self.fg_weather.volumes)}"
            if self.fg_weather.volumes
            else f"Detailed Weather: {len(clouds)} cloud echoes ({code})"
        )
        self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n{radar_source}")

    def _apply_fg_weather(self, scenario, intensity, coverage, precip="Rain"):
        self.scenario.blockSignals(True)
        self.scenario.setCurrentText(scenario)
        self.scenario.blockSignals(False)
        self.precip.blockSignals(True)
        self.precip.setCurrentText(precip)
        self.precip.blockSignals(False)
        self.weather.precip = precip
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
        for w in (self.scenario, self.intensity, self.precip):
            w.setEnabled(not fg_weather)
        # Coverage and storm layout cannot be sent in a METAR: locked whenever connected.
        self.coverage.setEnabled(not fg_live)
        self.regen.setEnabled(not fg_live)
        self.burst_btn.setEnabled(not fg_weather)
        self.wind_dir.setEnabled(not fg_wind)
        self.wind_kt.setEnabled(not fg_wind)
        self.sim_heading.setEnabled(not fg_live)
        self.time_scale.setEnabled(not fg_live)
        # Send is for preparing weather: connected, but in Simulated mode.
        self.send_btn.setEnabled(fg_live and not fg_mode)
        if not fg_weather:
            self.freezing_ft = float(self.sim_freezing.value())
        self.sim_freezing.setEnabled(not fg_weather)
        if fg_live:
            # Storms move in FlightGear's time: stopped while paused, faster
            # with time speed-up.
            s = self.fg.state
            scale = 0.0 if s.get("frozen") else max(0.0, s.get("speed_up", 1.0))
            source = "FLIGHTGEAR" if fg_mode else "FG  WX EDIT"
            if s.get("frozen"):
                source += "  PAUSED"
        else:
            scale, source = float(self.time_scale.value()), "SIM"
            # Turn towards the selected heading at 3 degrees per second.
            diff = (self.sim_heading.value() - self.heading + 540.0) % 360.0 - 180.0
            step = 3.0 * dt * scale
            self.heading = (self.heading + max(-step, min(step, diff))) % 360.0
            # Fly through the air at SIM_SPEED_KT; the wind carries the aircraft
            # sideways, so the track (where it really goes) is not the heading.
            h, to = math.radians(self.heading), math.radians(self.weather.wind_from + 180.0)
            wk = self.weather.wind_kt
            ve = SIM_SPEED_KT * math.sin(h) + wk * math.sin(to)
            vn = SIM_SPEED_KT * math.cos(h) + wk * math.cos(to)
            self.track = math.degrees(math.atan2(ve, vn)) % 360.0
            self.sim_gs = math.hypot(ve, vn)
            self.ax += ve / 3600.0 * dt * scale
            self.ay += vn / 3600.0 * dt * scale

        self.weather.step(dt * scale)
        self.layer_weather.step(dt * scale, self.weather.wind_from, self.weather.wind_kt)
        self.sim_alt.setEnabled(not fg_live)

        tilt, auto = self._tilt_deg(), self.auto_tilt.isChecked()
        self.tilt.setEnabled(not auto)
        self.gain.setEnabled(not self.gain_cal.isChecked())
        if auto:
            self.tilt.blockSignals(True)
            self.tilt.setValue(round(tilt * 4))
            self.tilt.blockSignals(False)
        self.tilt_label.setText(f"{tilt:+.1f}°" + (" A" if auto else ""))
        self.gain_label.setText("CAL" if self.gain_cal.isChecked()
                                else f"{self._gain_db():+.0f} dB")

        on = self.wxr_btn.isChecked()
        wf, wk = self.weather.wind_from, self.weather.wind_kt
        if fg_live:
            s = self.fg.state
            gs, tas = s.get("gs_kt"), s.get("tas_kt")
            wf, wk = s.get("wind_from", wf), s.get("wind_kt", wk)
        else:
            gs, tas = self.sim_gs, SIM_SPEED_KT
        # The ND shows magnetic directions (TRK ... MAG). FlightGear gives the
        # magnetic heading; the difference to true (variation) turns the rest.
        var = 0.0
        if fg_live and "hdg_mag" in self.fg.state:
            var = (self.heading - self.fg.state["hdg_mag"] + 180.0) % 360.0 - 180.0
        if fg_live:
            bug = self.fg.state.get("hdg_bug")           # already magnetic
        else:
            bug = float(self.sim_heading.value())

        # Predictive windshear: works on its own when low, whatever the radar mode.
        alt, ground = self._altitudes()
        vs = self.fg.state.get("vs_fps", 0.0) if fg_live else 0.0
        self._update_pws(time.perf_counter(), alt - ground, vs)
        rng = int(self.range.currentText())
        pws = self.pws
        if on and self._radar_mode() == "TEST":           # TEST shows the icon too
            pws = {"level": "warning", "a0": -20.0, "a1": 20.0,
                   "r0": rng * 0.12, "r1": rng * 0.25}

        self.display.info.update(gs=gs, tas=tas, wind_from=(wf - var) % 360, wind_kt=wk)
        self.display.info.update(up=(self.track - var) % 360, up_label="TRK",
                                 heading=(self.heading - var) % 360, hdg_bug=bug,
                                 range=rng, on=on, mode=self._radar_mode(), tilt=tilt,
                                 auto_tilt=auto, gain_db=self._gain_db(),
                                 gain_cal=self.gain_cal.isChecked(), source=source,
                                 pws=pws, wx_src=self._wx_source(),
                                 gnd_clutter=(on and self._radar_mode() in ("WX", "WX+T")
                                              and self.scanner.ground_share()
                                              > GND_CLUTTER_SHARE))
        self.display.advance(self._scan, dt)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
