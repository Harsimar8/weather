"""Weather radar app: control the weather and watch it on the radar.

Run:  python main.py
"""
import math
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDial, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QPushButton, QRadioButton, QScrollArea, QSlider, QSpinBox, QSplitter,
                               QToolButton,
                               QVBoxLayout, QWidget)

from display import RadarDisplay
from place_map import PlaceMap
from udp_link import DEFAULT_PORT as UDP_PORT, UDPLink
from fake_plane import DEFAULT_LAT, DEFAULT_LON, FakePlane
from fg_drive import FGDriver
from fg_link import PLACE, PLACE_RESULT, FGLink
from radar import MODES, TILT_LIMIT_DEG, RadarScanner, auto_tilt, windshear
from weather import (PRECIP_KINDS, SCENARIOS, Cell, WeatherModel, FlightGearWeather,
                     parse_metar)
from basic_weather import LayerWeather, freezing_level_ft
from terrain import TerrainMap
SIM_SPEED_KT = 250.0
RESET_JUMP_NM = 5.0       # aircraft moved this far in one read: FlightGear was reset
GND_CLUTTER_SHARE = 0.01  # warn when ground echo paints over 1 % of the screen
UDP_MODE = "Other simulator (UDP)"   # aircraft position from udp_link.py
# Weather you can place by clicking on the placement map:
# name: (kind for wxradar_export.nas, radius km, rain %, turbulence %, base ft AGL)
PLACE_KINDS = {
    "Rain shower": ("shower", 2, 40, 0, 3000),
    "Cumulonimbus (Cb)": ("cb", 3, 80, 60, 2500),
    "Thunderstorm": ("thunderstorm", 5, 100, 80, 2500),
}


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
        self.no_weather = FlightGearWeather()  # always empty: fallback switched off
        self.layer_weather = LayerWeather()   # Basic Weather, Manual Configuration
        self.terrain = TerrainMap()
        self.freezing_ft = None               # freezing level, ft MSL
        self.manual_wx = False                # FlightGear uses Manual Configuration
        self.scanner = RadarScanner()
        self.display = RadarDisplay(self.scanner)
        self.fg = None
        self.udp = None             # other simulator (UDP), see udp_link.py
        self.placed = []            # storms placed on the map (see _place)
        self.map_next = 0.0         # next map redraw (s)
        self.fake = None            # built-in fake plane (sends UDP to this app)
        self.fake_next = 0.0        # next fake plane packet (s)
        self.fdm_checked = False    # FlightGear's flight model checked (must be null to drive it)
        self.udp_error = ""         # why listening on the UDP port failed
        self.udp_retry = 0.0        # when to try listening again
        self.driver = None          # moves FlightGear's aircraft smoothly (fg_drive.py)
        self.trail = []             # (x, y) NM every second: drawn on the map
        self.trail_next = 0.0
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
        self.lw_warning = ""        # Detailed Weather built nothing: why (status top)
        self.lw_empty_since = None  # when Detailed Weather was first seen with nothing built
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
        self.source.addItems(["Simulated", "FlightGear", UDP_MODE])
        self.source.currentTextChanged.connect(self._source_changed)
        self.udp_port = QSpinBox()
        self.udp_port.setRange(1, 65535)
        self.udp_port.setValue(UDP_PORT)
        self.udp_port.setToolTip("Port the other simulator (or fake_plane.py) sends to.")
        self.udp_port.editingFinished.connect(lambda: self._source_changed(self.source.currentText()))
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
        f.addRow("UDP port (other sim)", self.udp_port)
        f.addRow(self.connect_btn)
        f.addRow(self.status)
        # How FlightGear's rain volumes are drawn: as FlightGear gives them
        # (flat), or with a real storm's shape, Doppler-like turbulence and
        # F-factor windshear filled in by the app.
        self.shape_real = QRadioButton("Real data")
        self.shape_realistic = QRadioButton("Realistic")
        self.shape_real.setToolTip("FlightGear's volumes exactly as given: one flat "
                                   "colour per volume, turbulence from its value.")
        self.shape_realistic.setToolTip(
            "Same volumes (place, size, heights, drift from FlightGear), drawn like a "
            "real storm: strong core, weaker ragged edges, weaker near the top.\n"
            "Turbulence: Doppler-like patches (5 m/s and up), strongest on core walls.\n"
            "Windshear: F-factor from a microburst model under heavy rain.")
        self.shape_real.setChecked(True)
        shape_group = QButtonGroup(src)
        shape_row = QHBoxLayout()
        for b in (self.shape_real, self.shape_realistic):
            shape_group.addButton(b)
            shape_row.addWidget(b)
        shape_row.addStretch()
        shape_group.buttonClicked.connect(self._shape_changed)
        f.addRow("Echo shape", shape_row)
        panel.addWidget(src)
        panel.addWidget(self._build_fake_panel())

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
        scroll.setMinimumWidth(240)
        root = QWidget()
        lay = QVBoxLayout(root)
        # Opens / closes the weather placement map.
        self.map_btn = QPushButton("Show weather map")
        self.map_btn.setCheckable(True)
        self.map_btn.setToolTip("Open or close the map for placing weather (Ctrl+M).")
        self.map_btn.setShortcut("Ctrl+M")
        self.map_btn.toggled.connect(self._toggle_map)
        top = QHBoxLayout()
        top.addWidget(self.map_btn)
        top.addStretch()
        lay.addLayout(top)
        # Controls | radar | map, side by side; the bars between them are draggable.
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(self.display)
        lay.addWidget(self.splitter, 1)
        # Weather placement map: beside the radar while open.
        self.place_map = PlaceMap()
        self.place_map.placed.connect(self._map_click)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.addWidget(self.place_map, 1)
        right.addWidget(self._build_place_panel())
        self.map_panel = QWidget()
        self.map_panel.setLayout(right)
        self.map_panel.setMinimumWidth(300)
        self.splitter.addWidget(self.map_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.map_panel.hide()                       # closed until the button opens it
        self.setCentralWidget(root)
        self.resize(1150, 760)
        self.splitter.setSizes([320, 830, 400])

    def _toggle_map(self, on):
        """Open or close the weather placement map beside the radar."""
        self.map_btn.setText("Hide weather map" if on else "Show weather map")
        if on:
            sizes = self.splitter.sizes()
            if sizes[2] < 300:          # first time open: make room for it
                self.resize(self.width() + 400, self.height())
                self.splitter.setSizes([sizes[0], sizes[1], 400])
            self.map_panel.show()
            self._update_map(force=True)
        else:
            self.map_panel.hide()
            self.resize(max(900, self.width() - self.map_panel.width()), self.height())

    def _build_fake_panel(self):
        """Built-in fake plane: stands in for the other simulator while testing.
        It sends real UDP packets to this app, like fake_plane.py does."""
        # Small arrow button that opens / closes the box (closed at start).
        outer = QWidget()
        v = QVBoxLayout(outer)
        v.setContentsMargins(0, 0, 0, 0)
        arrow = QToolButton()
        arrow.setText("✈  Fake plane (test)")
        arrow.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        arrow.setArrowType(Qt.RightArrow)
        arrow.setCheckable(True)
        arrow.setAutoRaise(True)
        arrow.setToolTip("Show / hide the fake plane controls")
        box = QGroupBox()
        box.setVisible(False)

        def fold(open_):
            arrow.setArrowType(Qt.DownArrow if open_ else Qt.RightArrow)
            box.setVisible(open_)
        arrow.toggled.connect(fold)
        v.addWidget(arrow)
        v.addWidget(box)
        f = QFormLayout(box)
        self.fake_btn = QPushButton("Start fake plane")
        self.fake_btn.setCheckable(True)
        self.fake_btn.setToolTip("Sends the plane's position to this app over UDP.\n"
                                 "Mode switches to 'Other simulator (UDP)'.")
        self.fake_btn.toggled.connect(self._fake_toggled)
        self.fake_gs = QSpinBox()
        self.fake_gs.setRange(0, 600)
        self.fake_gs.setSingleStep(10)
        self.fake_gs.setSuffix(" kt")
        self.fake_gs.setToolTip("Ground speed. 0 = parked.")
        self.fake_turn = QDoubleSpinBox()
        self.fake_turn.setRange(-6.0, 6.0)
        self.fake_turn.setSingleStep(0.5)
        self.fake_turn.setDecimals(1)
        self.fake_turn.setSuffix(" °/s")
        self.fake_turn.setToolTip("Turn rate: + turns right, - turns left, 0 = straight.\n"
                                  "3 °/s is a normal airliner turn (2 minutes per circle).")
        self.fake_hdg = QSpinBox()
        self.fake_hdg.setRange(0, 359)
        self.fake_hdg.setWrapping(True)
        self.fake_hdg.setSingleStep(10)
        self.fake_hdg.setValue(90)
        self.fake_hdg.setSuffix("°")
        self.fake_hdg.setToolTip("Heading (true): 0 north, 90 east, 180 south, 270 west.")
        self.fake_alt = QSpinBox()
        self.fake_alt.setRange(0, 45000)
        self.fake_alt.setSingleStep(500)
        self.fake_alt.setValue(5000)
        self.fake_alt.setSuffix(" ft")
        self.fake_lat = QLineEdit(f"{DEFAULT_LAT}")
        self.fake_lon = QLineEdit(f"{DEFAULT_LON}")
        jump = QPushButton("Move plane to this position")
        jump.clicked.connect(self._fake_jump)
        for w in (self.fake_gs, self.fake_turn, self.fake_hdg, self.fake_alt):
            w.valueChanged.connect(self._fake_changed)
        self.fake_info = QLabel("Stopped")
        pos_row = QHBoxLayout()
        pos_row.addWidget(self.fake_lat)
        pos_row.addWidget(self.fake_lon)
        f.addRow(self.fake_btn)
        f.addRow("Speed", self.fake_gs)
        f.addRow("Turn", self.fake_turn)
        f.addRow("Heading", self.fake_hdg)
        f.addRow("Altitude", self.fake_alt)
        f.addRow("Lat / Lon", pos_row)
        f.addRow(jump)
        f.addRow(self.fake_info)
        return outer

    def _fake_position(self):
        try:
            return float(self.fake_lat.text()), float(self.fake_lon.text())
        except ValueError:
            self.fake_info.setText("Lat / Lon must be numbers, e.g. 28.5665  77.1031")
            return None

    def _fake_toggled(self, on):
        self.fake_btn.setText("Stop fake plane" if on else "Start fake plane")
        if not on:
            if self.fake:
                self.fake.close()
                self.fake = None
            self.fake_info.setText("Stopped (the plane stays where it was)")
            return
        pos = self._fake_position()
        if pos is None:
            self.fake_btn.setChecked(False)
            return
        if self.source.currentText() != UDP_MODE:
            self.source.setCurrentText(UDP_MODE)     # starts listening for it
        self.fake = FakePlane(pos[0], pos[1], self.fake_hdg.value(), self.fake_alt.value(),
                              self.fake_gs.value(), self.fake_turn.value(),
                              port=self.udp_port.value())
        self.fake_last = self.fake_next = time.perf_counter()

    def _fake_changed(self, *_):
        """Speed, turn, heading and altitude change at once, also while flying."""
        if self.fake:
            self.fake.gs = float(self.fake_gs.value())
            self.fake.turn = float(self.fake_turn.value())
            self.fake.hdg = float(self.fake_hdg.value())
            self.fake.alt = float(self.fake_alt.value())

    def _fake_jump(self):
        pos = self._fake_position()
        if pos and self.fake:
            self.fake.lat, self.fake.lon = pos
        elif pos:
            self.fake_info.setText("Press Start: the plane starts at this position.")

    def _fly_fake(self, now):
        """Move the fake plane and send its position (10 times a second)."""
        if not self.fake:
            return
        self.fake.step(now - self.fake_last)
        self.fake_last = now
        if now < self.fake_next:
            return
        self.fake_next = now + 0.1
        self.fake.port = self.udp_port.value()
        try:
            self.fake.send()
        except OSError as exc:
            self.fake_info.setText(f"Send failed: {exc}")
            return
        if self.fake.turn:                          # show the heading as it turns
            self.fake_hdg.blockSignals(True)
            self.fake_hdg.setValue(round(self.fake.hdg) % 360)
            self.fake_hdg.blockSignals(False)
        self.fake_info.setText(f"Flying: {self.fake.lat:.4f} {self.fake.lon:.4f}  "
                               f"HDG {self.fake.hdg:03.0f}  {self.fake.gs:.0f} kt")

    def _build_place_panel(self):
        """Settings for the weather placed by clicking on the map beside the radar."""
        box = QGroupBox("Place weather on the map")
        f = QFormLayout(box)
        # What a click on the map does. Off by default: no storm by accident.
        self.click_mode = QButtonGroup(self)
        mode_row = QHBoxLayout()
        for i, (text, tip) in enumerate((
                ("Off", "Clicking the map does nothing"),
                ("Add storm", "Each click on the map adds one storm there"),
                ("Remove storm", "Click a storm on the map to remove it"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            self.click_mode.addButton(b, i)
            mode_row.addWidget(b)
        self.click_mode.button(0).setChecked(True)
        self.click_mode.idToggled.connect(self._click_mode_changed)
        self.hold_still = QCheckBox("Hold aircraft still (no flying)")
        self.hold_still.setToolTip(
            "Simulated: the aircraft stops moving.\n"
            "FlightGear: pauses FlightGear. Placing weather still works while paused.\n"
            "Other simulator (UDP): the position always comes from that simulator.")
        self.hold_still.toggled.connect(self._hold_changed)
        self.place_kind = QComboBox()
        self.place_kind.addItems(PLACE_KINDS)
        self.place_radius = QSpinBox()
        self.place_radius.setRange(1, 10)
        self.place_radius.setSuffix(" km radius")
        self.place_rain = slider(5, 100, 40)
        self.place_turb = slider(0, 100, 0)
        self.place_base = QSpinBox()
        self.place_base.setRange(500, 10000)
        self.place_base.setSingleStep(500)
        self.place_base.setSuffix(" ft AGL")
        self.place_rain_lbl, self.place_turb_lbl = QLabel(), QLabel()
        self.place_rain.valueChanged.connect(self._place_labels)
        self.place_turb.valueChanged.connect(self._place_labels)
        self.place_kind.currentTextChanged.connect(self._place_defaults)
        clear_btn = QPushButton("Clear placed weather")
        clear_btn.clicked.connect(self._clear_placed)
        self.place_status = QLabel(
            "Press 'Add storm', then click the map around the aircraft (360°). Every "
            "click adds one more storm. With FlightGear, real clouds + volumes are "
            "built there (needs Detailed Weather and wxradar_export.nas).")
        self.place_status.setWordWrap(True)
        rain_row, turb_row = QHBoxLayout(), QHBoxLayout()
        rain_row.addWidget(self.place_rain)
        rain_row.addWidget(self.place_rain_lbl)
        turb_row.addWidget(self.place_turb)
        turb_row.addWidget(self.place_turb_lbl)
        f.addRow("Map click", mode_row)
        f.addRow(self.hold_still)
        f.addRow("Type", self.place_kind)
        f.addRow("Size", self.place_radius)
        f.addRow("Rain", rain_row)
        f.addRow("Turbulence", turb_row)
        f.addRow("Cloud base", self.place_base)
        f.addRow(clear_btn)
        f.addRow(self.place_status)
        self._place_defaults(self.place_kind.currentText())
        return box

    def _hold_changed(self, on):
        """Hold the aircraft where it is, so weather can be placed around it."""
        if self.fg and self.fg.connected:
            try:
                self.fg.set("/sim/freeze/master", "true" if on else "false")
            except OSError as exc:
                self.place_status.setText(f"Could not pause FlightGear: {exc}")
                return
            self.place_status.setText("FlightGear paused: click the map to place weather."
                                      if on else "FlightGear running again.")

    def _click_mode_changed(self, mode_id, on):
        if not on:
            return
        self.place_map.setCursor(Qt.ArrowCursor if mode_id == 0 else
                                 Qt.CrossCursor if mode_id == 1 else
                                 Qt.PointingHandCursor)
        self.place_status.setText(("Map clicks are OFF.", "ADD: click the map to put a storm there.",
                                   "REMOVE: click a storm on the map to remove it.")[mode_id])

    def _map_click(self, bearing, dist_nm):
        """A left click on the map: add, remove or nothing (the mode buttons)."""
        mode = self.click_mode.checkedId()
        if mode == 1:
            self._place(bearing, dist_nm)
        elif mode == 2:
            self._unplace(bearing, dist_nm)

    def _place_defaults(self, name):
        _kind, radius, rain, turb, base = PLACE_KINDS[name]
        self.place_radius.setValue(radius)
        self.place_rain.setValue(rain)
        self.place_turb.setValue(turb)
        self.place_base.setValue(base)
        self._place_labels()

    def _place_labels(self, *_):
        rain = self.place_rain.value() / 100
        # Same sums as weather.py and radar.py: what colour this rain paints.
        dbz = 20 + (0.15 + 1.05 * rain - 0.22) * (10 / 0.265)
        colour = ("GREEN" if dbz < 30 else "YELLOW" if dbz < 40
                  else "RED" if dbz < 50 else "MAGENTA")
        self.place_rain_lbl.setText(f"{rain:.2f} {colour}")
        turb = self.place_turb.value() / 100
        self.place_turb_lbl.setText("none" if turb == 0 else f"{turb:.2f}")

    def _fg_driven(self):
        """Other simulator mode with FlightGear connected: FlightGear's aircraft
        follows the other plane and FlightGear supplies the weather."""
        return (self.source.currentText() == UDP_MODE
                and self.fg is not None and self.fg.connected)

    def _fg_wx_mode(self):
        """The radar shows FlightGear's weather."""
        return self.source.currentText() == "FlightGear" or self._fg_driven()

    def _fg_placing(self):
        """Placed weather goes to FlightGear (real clouds + volumes)."""
        return (self._fg_wx_mode() and self.fg is not None and self.fg.connected
                and bool(self.fg.state))

    def _place(self, bearing, dist_nm):
        """Map click: build the chosen weather at that true bearing and distance."""
        name = self.place_kind.currentText()
        kind = PLACE_KINDS[name][0]
        radius_km = self.place_radius.value()
        r_nm = radius_km / 1.852
        rain = self.place_rain.value() / 100
        turb = self.place_turb.value() / 100
        b = math.radians(bearing)
        x = self.ax + dist_nm * math.sin(b)
        y = self.ay + dist_nm * math.cos(b)
        where = f"{dist_nm:.1f} NM, bearing {bearing:03.0f}° true"
        label = f"#{len(self.placed) + 1}"
        if self._fg_placing():
            # Around the other plane when it drives FlightGear (exact position).
            s = (self.udp.state if self._fg_driven() and self.udp and self.udp.live()
                 else self.fg.state)
            lat0 = s["lat"]
            lat = lat0 + dist_nm * math.cos(b) / 60.0
            lon = s["lon"] + dist_nm * math.sin(b) / (60.0 * math.cos(math.radians(lat0)))
            try:
                fg_id = self.fg.place_weather(kind, lat, lon, radius_km * 1000.0, rain,
                                              turb, self.place_base.value())
            except OSError as exc:
                self.place_status.setText(f"Send failed: {exc}")
                return
            self.placed.append({"x": x, "y": y, "r": r_nm, "rain": rain,
                                "cell": None, "label": label, "fg_id": fg_id})
            self.place_status.setText(f"{name} sent to FlightGear, {where}. Waiting...")
            QTimer.singleShot(2500, self._check_place)
        else:
            cell = Cell(x, y, r_nm, r_nm, 0.0, 0.2 + 1.1 * rain, turb)
            self.weather.cells.append(cell)
            self.placed.append({"x": x, "y": y, "r": r_nm, "rain": rain,
                                "cell": cell, "label": label})
            self.full_refresh()
            self.place_status.setText(f"{name} placed, {where}. "
                                      f"{len(self.placed)} placed in all.")
        rel = (bearing - self.track + 540.0) % 360.0 - 180.0
        self.display.mark(rel, dist_nm)
        self._update_map(force=True)

    def _placed_pos(self, item):
        """Where a placed storm is now (app storms drift with the wind)."""
        c = item["cell"]
        return (c.x, c.y) if c is not None else (item["x"], item["y"])

    def _unplace(self, bearing, dist_nm):
        """Map click in Remove mode: remove the placed storm under the cursor."""
        b = math.radians(bearing)
        x = self.ax + dist_nm * math.sin(b)
        y = self.ay + dist_nm * math.cos(b)
        best, best_d = None, None
        for item in self.placed:
            px, py = self._placed_pos(item)
            d = math.hypot(px - x, py - y)
            if d <= max(item["r"], self.place_map.range_nm * 0.04) and (
                    best_d is None or d < best_d):
                best, best_d = item, d
        if best is None:
            return
        self.placed.remove(best)
        if best["cell"] is not None:
            self.weather.cells = [c for c in self.weather.cells if c is not best["cell"]]
            self.full_refresh()
            self.place_status.setText(f"Removed {best['label']}. {len(self.placed)} left.")
        else:
            self._fg_remove([best.get("fg_id")], f"Removed {best['label']}")
        self._update_map(force=True)

    def _fg_remove(self, ids, what):
        """Delete placed storms inside FlightGear too (clouds, volumes, echo)."""
        ids = [i for i in ids if i is not None]
        if not ids or not (self.fg and self.fg.connected):
            self.place_status.setText(f"{what} from the map (FlightGear not connected).")
            return
        try:
            self.fg.remove_weather(ids)
        except OSError as exc:
            self.place_status.setText(f"{what} from the map; FlightGear: {exc}")
            return
        self.place_status.setText(f"{what}: removing it in FlightGear...")
        QTimer.singleShot(1500, lambda: self._check_remove(what))

    def _check_remove(self, what):
        if not (self.fg and self.fg.connected):
            return
        try:
            result = self.fg.get(PLACE + "/remove-result")
        except OSError as exc:
            result = f"no answer ({exc})"
        if result in ("", "waiting"):
            result = ("no answer: load the new wxradar_export.nas (restart FlightGear), "
                      "FlightGear keeps the clouds until then")
        self.place_status.setText(f"{what}. FlightGear: {result}")

    def _clear_placed(self):
        in_fg = self.fg is not None and self.fg.connected
        mine = {id(p["cell"]) for p in self.placed if p["cell"] is not None}
        self.weather.cells = [c for c in self.weather.cells if id(c) not in mine]
        self.placed = []
        self.full_refresh()
        self._update_map(force=True)
        self.place_status.setText("Placed weather cleared.")
        if in_fg:
            # Every storm placed in this FlightGear run, also from before a restart.
            self._fg_remove(["all"], "Placed weather cleared")

    def _check_place(self):
        if not (self.fg and self.fg.connected):
            return
        try:
            result = self.fg.get(PLACE_RESULT)
        except OSError as exc:
            result = f"no answer ({exc})"
        if result in ("", "waiting"):
            result = ("No answer from FlightGear. Load the new wxradar_export.nas "
                      "(restart FlightGear or run test_fg_export.py).")
        elif result.startswith("OK"):
            result += ". The radar shows it within a few seconds."
        self.place_status.setText(result)

    # ------------------------------------------------- other simulator (UDP)
    def _source_changed(self, mode):
        """Start listening for the other simulator in its mode, stop otherwise."""
        if mode == UDP_MODE:
            port = self.udp_port.value()
            if self.udp and self.udp.listening and self.udp.port == port:
                return
            if self.udp:
                self.udp.stop()
            self.udp = UDPLink(port)
            try:
                self.udp.start()
            except OSError as exc:
                self.udp_error = str(exc)
                self.status.setText(f"UDP port {port}: {exc}")
                self.udp = None
                return
            self.udp_error = ""
            self.origin = None      # the first packet becomes the map's (0, 0)
            self.trail = []
            # Only the weather placed on the map: no made-up scenario storms.
            self.scenario.setCurrentText("Clear")
            self.regenerate()
            self.status.setText(f"Listening on UDP port {port}.\n"
                                "Run: python fake_plane.py")
        elif self.udp:
            self.udp.stop()
            self.udp = None
            self.status.setText("Not connected")

    def _drive_fg(self):
        """Move FlightGear's aircraft to the other plane. FlightGear then builds
        its Detailed Weather around it, and the radar shows that weather."""
        if not self.fdm_checked:
            self.fdm_checked = True
            try:
                fdm = self.fg.get("/sim/flight-model")
            except OSError:
                fdm = ""
            if fdm != "null":
                self._notify(f"FlightGear flight model is '{fdm}': it will fight the "
                             "other plane's position. Start FlightGear with --fdm=null", 15.0)
        if not (self.udp and self.udp.live()):
            if self.driver:
                self.driver.target = None       # no packets: leave FlightGear alone
            return
        if self.driver is None:
            # Own connection, 30 times a second, moved on between packets.
            self.driver = FGDriver(self.fg.host, self.fg.port)
        u = self.udp.state
        gs = u.get("gs_kt", 0.0)
        self.driver.target = {"lat": u["lat"], "lon": u["lon"], "alt_ft": u["alt_ft"],
                              "heading": u["heading"], "gs_kt": gs,
                              "track": u.get("track", u["heading"]), "t": self.udp.last_rx}

    def _stop_driver(self):
        if self.driver:
            self.driver.stop()
            self.driver = None

    def _update_from_udp(self):
        """Aircraft position from the other simulator. True while packets come."""
        if self.udp is None or not self.udp.listening:
            # Listening failed or stopped: try again (about once a second).
            if time.monotonic() >= self.udp_retry:
                self.udp_retry = time.monotonic() + 1.0
                self.udp = None
                self._source_changed(UDP_MODE)
            if self.udp is None:
                self.status.setText(f"NOT listening on UDP port {self.udp_port.value()}:\n"
                                    f"{self.udp_error}\nRetrying...")
                return False
        if not (self.udp and self.udp.live()):
            if self.udp:
                wait = (f"no data for {time.monotonic() - self.udp.last_rx:.0f} s"
                        if self.udp.packets else "waiting for data")
                err = f"\n{self.udp.error}" if self.udp.error else ""
                self.status.setText(f"UDP port {self.udp.port}: {wait}.\n"
                                    f"Run: python fake_plane.py{err}")
            return False
        s = self.udp.state
        if self.origin is None:
            self.origin = (s["lat"], s["lon"])
        self.ax, self.ay = self._to_nm(s["lat"], s["lon"])
        self.heading = s["heading"]
        gs = s.get("gs_kt", 0.0)
        self.track = s.get("track", self.heading) if gs > 30.0 else self.heading
        self.sim_gs = gs
        fg = ("FlightGear follows it, weather from FlightGear" if self._fg_driven()
              else "FlightGear not connected: only weather placed on the map")
        self._set_status(f"UDP from {self.udp.sender[0]}: {self.udp.packets} packets\n"
                         f"{s['lat']:.4f} {s['lon']:.4f}  HDG {self.heading:03.0f}  "
                         f"{s['alt_ft']:.0f} ft\n{fg}")
        return True

    def _update_map(self, force=False):
        """Redraw the placement map (about 10 times a second)."""
        now = time.perf_counter()
        if not force and (now < self.map_next or not self.map_panel.isVisible()):
            return
        self.map_next = now + 0.1
        storms = []
        # Other weather the radar sees, drawn faint.
        placed_ids = {id(p["cell"]) for p in self.placed if p["cell"] is not None}
        wx = self._scan_weather()
        if wx is self.weather:
            for c in self.weather.cells:
                if id(c) not in placed_ids:
                    storms.append((c.x - self.ax, c.y - self.ay, max(c.sx, c.sy),
                                   max(0.0, (c.peak - 0.2) / 1.1), "", False))
        elif wx is self.fg_weather:
            for v in self.fg_weather.volumes:
                d = v["data"]
                rain = float(d.get("rain") or 0.0) if d.get("rain-flag", 1) else 0.0
                if rain > 0.05:             # the radar sees rain only
                    storms.append((v["x"] - self.ax, v["y"] - self.ay,
                                   max(v["r1"], v["r2"]), rain, "", False))
        live_ids = {id(c) for c in self.weather.cells}
        for p in self.placed:
            if p["cell"] is not None and id(p["cell"]) not in live_ids:
                continue                    # replaced by FlightGear's own weather
            px, py = self._placed_pos(p)
            storms.append((px - self.ax, py - self.ay, p["r"], p["rain"], p["label"], True))
        # Remember where the aircraft was, once a second, for 10 minutes.
        if now >= self.trail_next:
            self.trail_next = now + 1.0
            if self.trail and math.hypot(self.ax - self.trail[-1][0],
                                         self.ay - self.trail[-1][1]) > 5.0:
                self.trail = []             # jumped somewhere else: start a new trail
            self.trail = (self.trail + [(self.ax, self.ay)])[-600:]
        trail = [(x - self.ax, y - self.ay) for x, y in self.trail]
        alt, _ground = self._altitudes()
        info = (f"HDG {self.heading:03.0f}  {alt:.0f} ft  GS {self.sim_gs:.0f} kt  "
                f"Placed {len(self.placed)}")
        self.place_map.set_view(self.heading, self.track, float(self.range.currentText()),
                                storms, info, trail)

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
        self.fallback_wx = QCheckBox("No FlightGear volumes: draw weather from\n"
                                     "METAR / clouds (made-up places)")
        self.fallback_wx.setChecked(True)
        self.fallback_wx.setToolTip(
            "When FlightGear has no weather volumes (Basic Weather, Detailed Weather "
            "still building, or a scenario that built nothing), the app can draw "
            "storms from the METAR or on FlightGear's cloud positions. Their places "
            "are made up. Untick to show only FlightGear's real volumes: the radar "
            "then stays empty when there are none (Basic Weather too). "
            "Manual Configuration cloud layers are not affected.")
        self.fallback_wx.toggled.connect(lambda _on: self.full_refresh())
        save = QPushButton("Save radar image")
        save.clicked.connect(self.save_image)
        v.addWidget(self.atten)
        v.addWidget(self.fallback_wx)
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
        if self.source.currentText() == UDP_MODE:
            s = self.udp.state if self.udp else {}
            if s:
                fg = self.fg.state if (self.fg and self.fg.connected) else {}
                return s["alt_ft"], s.get("ground_ft", fg.get("ground_ft", 0.0))
            return float(self.sim_alt.value()), 0.0
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
        # Storms placed on the map stay when new scenario storms are made.
        self.weather.cells += [p["cell"] for p in self.placed if p["cell"] is not None]
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
            fg_live = self.fg and self.fg.connected
            speed = ((self.fg.state.get("gs_kt") if fg_live else None)
                     or self.sim_gs)
            self.pws = windshear(self._scan_weather(), self.ax, self.ay,
                                 self.track, agl_ft, self._altitudes()[1],
                                 speed_kt=speed)
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

    def _shape_changed(self, *_):
        """Echo shape switched: redraw FlightGear's volumes the new way."""
        self.fg_weather.realistic = self.shape_realistic.isChecked()
        self.pws_next = 0.0             # look for windshear again at once
        self.full_refresh()

    def full_refresh(self, *_):
        self.display.refresh_all(self._scan())

    def toggle_fg(self):
        if self.fg:     # connected, or trying to reconnect
            self._stop_driver()
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
        self.fdm_checked = False
        if self.source.currentText() != UDP_MODE:   # UDP: FlightGear follows the other plane
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
        if self.lw_warning:
            text = f"{self.lw_warning}\n{text}" if text else self.lw_warning
        self.status.setText(text)

    def _fallback_text(self):
        """What the radar shows while FlightGear has no volumes."""
        return ("storms from the METAR (made-up places)" if self.fallback_wx.isChecked()
                else "nothing (no volumes, fallback off)")

    def _check_lw_built(self, s):
        """Warn when Detailed Weather is on but FlightGear built nothing.

        FlightGear only writes the reason to its log file, so without this the
        radar would show METAR storms over an empty sky with no explanation.
        """
        empty = (s.get("lw_selected") and not self.fg_weather.volumes
                 and s.get("lw_tiles", 0) == 0 and not s.get("lw_loop"))
        if not empty:
            self.lw_empty_since = None
            self.lw_warning = ""
            return
        now = time.perf_counter()
        if self.lw_empty_since is None:
            self.lw_empty_since = now
        mgmt = (s.get("lw_mgmt") or "").strip()
        ttype = (s.get("lw_type") or "").strip()
        name = (s.get("scenario") or "").strip() or "This scenario"
        radar = f"Radar: {self._fallback_text()}"
        if mgmt and mgmt != "METAR" and ttype in ("manual", "live", ""):
            # Shown at once: this never builds, however long you wait.
            self.lw_warning = (f"⚠ Detailed Weather built nothing:\n"
                               f"\"{name}\" has no tile, so Tile Selection Mode\n"
                               f"must be METAR (now: {mgmt}). Set METAR, press Apply.\n"
                               f"{radar}")
        elif now - self.lw_empty_since > 30.0:
            # Normal building takes ~20 s after Apply; longer means it is stuck.
            why = ("FlightGear is PAUSED: unpause, then press Apply."
                   if s.get("frozen") else
                   "Press Apply in the Weather dialog (see fgfs.log for the error).")
            self.lw_warning = f"⚠ Detailed Weather is on but built nothing.\n{why}\n{radar}"
        else:
            self.lw_warning = ""

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
        fg_on = self._fg_wx_mode() and self.fg and self.fg.connected
        if fg_on and self.fg_weather.volumes:
            return self.fg_weather          # Detailed Weather zones
        if fg_on and self.manual_wx:
            return self.layer_weather       # Basic Weather, Manual Configuration
        if fg_on and not self.fallback_wx.isChecked():
            return self.no_weather          # no volumes, fallback off: nothing
        return self.weather

    def _scan(self, rays=None):
        """Radar picture for a slice of rays (all rays when None).

        The ND is track up, so the picture is turned to the track.
        """
        scan_weather = self._scan_weather()
        alt, ground = self._altitudes()
        connected = (self.fg and self.fg.connected
                     and (self.source.currentText() != UDP_MODE or self._fg_driven()))
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
            self.lw_warning = ""
            self.lw_empty_since = None
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
        self._check_lw_built(s)
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
                          f"Until then: {self._fallback_text()}")
            else:
                status = ("Detailed Weather building clouds...\n"
                          f"Until then: {self._fallback_text()}")
            self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n{status}")
        elif self.fg_weather.volumes:
            self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n"
                             f"FlightGear radar volumes: "
                             f"{len(self.fg_weather.volumes)}")
        else:
            # Basic Weather (or a tile code left over from earlier).
            self._set_status(f"Connected  ALT {s['alt_ft']:.0f} ft\n"
                             f"Basic Weather: {self._fallback_text()}")

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
        # Placed FlightGear storms were stored from the old map centre.
        self.placed = [p for p in self.placed if p["cell"] is not None]
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
        fg_on = self._fg_wx_mode() and self.fg and self.fg.connected
        if not fg_on:
            return ("SIM WX", False)
        s = self.fg.state or {}
        if s.get("gen", 0) != self.fg.cache_gen:
            return ("WX RESET", True)
        if self.fg_weather.volumes:
            shaped = " SHAPED" if self.fg_weather.realistic else ""
            return (f"FG VOLUMES {len(self.fg_weather.volumes)}{shaped}", False)
        if self.manual_wx:
            return ("FG LAYERS", False)
        if not self.fallback_wx.isChecked():
            return ("NO VOLUMES", True)     # fallback off: radar left empty
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
            if self.fallback_wx.isChecked()
            else f"Detailed Weather: no volumes, fallback off ({len(clouds)} clouds not drawn)"
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
        udp_mode = self.source.currentText() == UDP_MODE
        fg_driven = self._fg_driven()
        if fg_driven:
            self._drive_fg()
        else:
            self._stop_driver()
        fg_mode = self.source.currentText() == "FlightGear" or fg_driven
        if fg_mode and not self.was_fg_mode:
            # Back in FlightGear mode: read FlightGear's weather again, so edits
            # made in Simulated mode (and not sent) leave the radar.
            self.fg_metar = None
        self.was_fg_mode = fg_mode
        # Other simulator: its position only, the FlightGear link is not used.
        # With FlightGear connected, FlightGear follows it and gives the weather.
        fg_live = (False if udp_mode and not fg_driven
                   else self._update_from_fg(follow_weather=fg_mode))
        if fg_live and udp_mode:
            self._update_from_udp()     # exact position straight from the other plane
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
        self.sim_heading.setEnabled(not fg_live and not udp_mode)
        self.time_scale.setEnabled(not fg_live and not udp_mode)
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
            # Keep the box in step if FlightGear is paused / unpaused there (P key).
            frozen = bool(s.get("frozen"))
            if self.hold_still.isChecked() != frozen:
                self.hold_still.blockSignals(True)
                self.hold_still.setChecked(frozen)
                self.hold_still.blockSignals(False)
        elif udp_mode:
            # Position from the other simulator; storms drift in real time.
            scale = 1.0
            source = "OTHER SIM" if self._update_from_udp() else "OTHER SIM  NO DATA"
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
            if self.hold_still.isChecked():
                self.sim_gs = 0.0                    # parked: weather is placed around it
            else:
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
            # FlightGear's own true and magnetic heading: their difference is
            # the variation, even if FlightGear points elsewhere than the
            # other plane (only a few degrees, e.g. about 1 at Delhi).
            true_hdg = self.fg.state.get("heading", self.heading)
            var = (true_hdg - self.fg.state["hdg_mag"] + 180.0) % 360.0 - 180.0
            if abs(var) > 30.0:
                var = 0.0           # nonsense value: do not turn the display by it
        if udp_mode and self.udp and self.udp.live():
            # Speed of the other plane, not of FlightGear's aircraft.
            gs = tas = self.udp.state.get("gs_kt", 0.0)
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
        self._update_map()
        self._fly_fake(now)


if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        win = MainWindow()
        win.show()
        sys.exit(app.exec())
    except KeyboardInterrupt:
        print("\nWeather Radar application stopped.")
