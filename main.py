"""Weather radar app: control the weather and watch it on the radar.

Run:  python main.py
"""
import math
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QPushButton, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

from display import RadarDisplay
from fg_link import FGLink
from radar import RadarScanner
from weather import SCENARIOS, WeatherModel, parse_metar

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
        regen = QPushButton("Regenerate storms")
        regen.clicked.connect(self.regenerate)
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
        f.addRow(regen)
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
        panel.addStretch()

        left = QWidget()
        left.setLayout(panel)
        left.setFixedWidth(300)
        root = QWidget()
        lay = QHBoxLayout(root)
        lay.addWidget(left)
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
            self.status.setText("Weather sent to FlightGear")
        except OSError as exc:
            self.status.setText(f"Send failed: {exc}")

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

    def _update_from_fg(self):
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
        lat0, lon0 = self.origin
        self.ax = (s["lon"] - lon0) * 60.0 * math.cos(math.radians(lat0))
        self.ay = (s["lat"] - lat0) * 60.0
        self.heading = s["heading"]
        self.status.setText(f"Connected  ALT {s['alt_ft']:.0f} ft")

        # Wind always comes from FlightGear; the sliders just show it.
        if "wind_from" in s:
            self.weather.wind_from = s["wind_from"]
            self.weather.wind_kt = s["wind_kt"]
            self.wind_dir.setValue(round(s["wind_from"]) % 360)
            self.wind_kt.setValue(round(s["wind_kt"]))

        # Weather changed inside FlightGear (not by our Send): show it on the radar.
        metar = s.get("metar", "")
        if metar and metar != self.fg_metar:
            self.fg_metar = metar
            parsed = parse_metar(metar)
            if parsed and metar != self.sent_metar:
                self._apply_fg_weather(*parsed)
        return True

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

        fg_live = self.source.currentText() == "FlightGear" and self._update_from_fg()
        fg_wind = fg_live and "wind_from" in self.fg.state
        self.wind_dir.setEnabled(not fg_wind)
        self.wind_kt.setEnabled(not fg_wind)
        if fg_live:
            scale, source = 1.0, "FLIGHTGEAR"
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
