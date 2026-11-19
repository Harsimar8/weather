"""Weather placement map: north-up plan view centred on the aircraft.

Left click places weather there, right click removes the placed storm under
the cursor, the mouse wheel changes the range. The map only draws and reports
clicks; MainWindow builds the weather.
"""
import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

RANGES_NM = [5, 10, 20, 40, 80, 160, 320]

BG = QColor(12, 18, 28)
RING = QColor(70, 90, 110)
TEXT = QColor(170, 190, 210)
PLANE = QColor(255, 255, 255)
COVER = QColor(60, 200, 120, 28)       # what the radar can see (180° ahead)
COVER_EDGE = QColor(60, 200, 120, 110)


def rain_colour(rain):
    """Same colour steps as the radar: green, yellow, red, magenta."""
    if rain < 0.3:
        return QColor(40, 200, 70)
    if rain < 0.6:
        return QColor(240, 220, 40)
    if rain < 0.9:
        return QColor(235, 50, 40)
    return QColor(230, 60, 230)


class PlaceMap(QWidget):
    placed = Signal(float, float)    # true bearing (deg), distance (NM) from the aircraft
    removed = Signal(float, float)   # same, for a right click
    range_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 320)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.range_nm = 40
        self.heading = 0.0
        self.track = 0.0
        self.radar_range = 40.0
        self.storms = []      # (dx, dy, r_nm, rain, label, placed) dx/dy NM east/north of aircraft
        self.info = ""        # line at the bottom (position)
        self.hover = None     # (bearing, dist) under the cursor

    # ------------------------------------------------------------ data in
    def set_view(self, heading, track, radar_range, storms, info):
        self.heading, self.track = heading, track
        self.radar_range, self.storms, self.info = radar_range, storms, info
        self.update()

    def set_range(self, nm):
        self.range_nm = nm
        self.range_changed.emit(nm)
        self.update()

    # ------------------------------------------------------------ geometry
    def _centre_scale(self):
        w, h = self.width(), self.height()
        radius = min(w, h) / 2 - 26
        return QPointF(w / 2, h / 2), radius / self.range_nm, radius

    def _to_screen(self, dx, dy):
        c, s, _ = self._centre_scale()
        return QPointF(c.x() + dx * s, c.y() - dy * s)

    def _to_polar(self, pos):
        """Screen point -> (true bearing, NM) from the aircraft."""
        c, s, _ = self._centre_scale()
        dx, dy = (pos.x() - c.x()) / s, (c.y() - pos.y()) / s
        return math.degrees(math.atan2(dx, dy)) % 360.0, math.hypot(dx, dy)

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, event):
        bearing, dist = self._to_polar(event.position())
        if event.button() == Qt.LeftButton:
            self.placed.emit(bearing, dist)
        elif event.button() == Qt.RightButton:
            self.removed.emit(bearing, dist)

    def mouseMoveEvent(self, event):
        self.hover = self._to_polar(event.position())
        self.update()

    def leaveEvent(self, _event):
        self.hover = None
        self.update()

    def wheelEvent(self, event):
        i = RANGES_NM.index(self.range_nm) if self.range_nm in RANGES_NM else 3
        i += -1 if event.angleDelta().y() > 0 else 1
        self.set_range(RANGES_NM[max(0, min(len(RANGES_NM) - 1, i))])

    # ------------------------------------------------------------ drawing
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), BG)
        c, s, R = self._centre_scale()
        small = QFont("Consolas", 8)
        p.setFont(small)

        # What the radar can see: 180° ahead of the track, out to its range.
        rr = min(self.radar_range, self.range_nm * 1.5) * s
        box = QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr)
        p.setPen(QPen(COVER_EDGE, 1, Qt.DashLine))
        p.setBrush(COVER)
        # Qt angles: 0 = east, counter-clockwise, 1/16 degree.
        p.drawPie(box, int((90 - (self.track + 90)) * 16), int(180 * 16))

        # Range rings and their labels.
        p.setBrush(Qt.NoBrush)
        for k in range(1, 5):
            r = R * k / 4
            p.setPen(QPen(RING, 1))
            p.drawEllipse(c, r, r)
            p.setPen(TEXT)
            p.drawText(QPointF(c.x() + 3, c.y() - r - 2), f"{self.range_nm * k / 4:g}")

        # Compass rose: ticks every 10°, numbers every 30°, N/E/S/W.
        for deg in range(0, 360, 10):
            a = math.radians(deg)
            long_tick = deg % 30 == 0
            r0 = R - (8 if long_tick else 4)
            p.setPen(QPen(RING, 1))
            p.drawLine(QPointF(c.x() + r0 * math.sin(a), c.y() - r0 * math.cos(a)),
                       QPointF(c.x() + R * math.sin(a), c.y() - R * math.cos(a)))
            if long_tick:
                label = {0: "N", 90: "E", 180: "S", 270: "W"}.get(deg, f"{deg // 10:02d}")
                p.setPen(TEXT)
                q = QPointF(c.x() + (R + 14) * math.sin(a), c.y() - (R + 14) * math.cos(a))
                p.drawText(QRectF(q.x() - 14, q.y() - 8, 28, 16), Qt.AlignCenter, label)

        # Storms: placed ones solid, other weather faint.
        for dx, dy, r_nm, rain, label, placed in self.storms:
            q = self._to_screen(dx, dy)
            rad = max(4.0, r_nm * s)
            col = rain_colour(rain)
            fill = QColor(col)
            fill.setAlpha(110 if placed else 45)
            p.setBrush(fill)
            p.setPen(QPen(col, 2 if placed else 1))
            p.drawEllipse(q, rad, rad)
            if placed:
                self._draw_cloud(p, q, max(7.0, min(rad, 16.0)))
                if label:
                    p.setPen(TEXT)
                    p.drawText(QPointF(q.x() + rad + 3, q.y() + 4), label)

        self._draw_plane(p, c)

        # Text: cursor bearing / distance and the aircraft position.
        p.setPen(TEXT)
        p.setFont(QFont("Consolas", 9))
        if self.hover:
            b, d = self.hover
            p.drawText(QPointF(8, 16), f"Cursor {b:03.0f}° {d:5.1f} NM")
        p.drawText(QPointF(8, self.height() - 8), self.info)
        p.drawText(QRectF(0, 4, self.width() - 8, 16), Qt.AlignRight,
                   f"Range {self.range_nm} NM (wheel)")
        p.end()

    def _draw_plane(self, p, c):
        a = math.radians(self.heading)
        pts = [(0, -12), (2, -4), (11, 2), (11, 4), (2, 1), (1, 8), (4, 11),
               (-4, 11), (-1, 8), (-2, 1), (-11, 4), (-11, 2), (-2, -4)]
        poly = QPolygonF([QPointF(c.x() + x * math.cos(a) - y * math.sin(a),
                                  c.y() + x * math.sin(a) + y * math.cos(a)) for x, y in pts])
        p.setPen(QPen(QColor(0, 0, 0), 1))
        p.setBrush(QBrush(PLANE))
        p.drawPolygon(poly)

    @staticmethod
    def _draw_cloud(p, q, size):
        """Small white cloud symbol on a placed storm."""
        r = size / 3
        path = QPainterPath()
        for ox, oy, k in ((-r, 0.2 * r, 1.0), (0, -0.4 * r, 1.25), (r, 0.2 * r, 1.0)):
            path.addEllipse(QPointF(q.x() + ox, q.y() + oy), r * k, r * k)
        path.addRect(QRectF(q.x() - r, q.y(), 2 * r, r * 1.2))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawPath(path.simplified())
