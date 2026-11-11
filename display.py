"""ND-style weather radar display widget (PySide6)."""
import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from radar import LEVEL_COLORS

W, H = 640, 480          # internal image size (scaled to the widget)
CX, CY, R = W / 2, H - 30, 410.0


class RadarDisplay(QWidget):
    def __init__(self, scanner, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 360)
        self.scanner = scanner
        self.half = scanner.half_angle
        self.nr, self.nb = scanner.n_rays, scanner.n_bins
        self.disp = np.zeros((self.nr, self.nb), dtype=np.uint8)
        self.sweep = -self.half
        self.sweep_dir = 1
        self.sweep_speed = 45.0  # degrees per second
        self.info = {"heading": 0.0, "range": 40, "gain": 1.0,
                     "turb": False, "source": "SIM"}
        self.frame = QImage()
        self._build_pixel_map()

    def _build_pixel_map(self):
        """For every screen pixel, work out which ray and range bin it shows."""
        ys, xs = np.mgrid[0:H, 0:W]
        dx, dy = xs - CX, CY - ys
        r = np.hypot(dx, dy)
        ang = np.degrees(np.arctan2(dx, dy))
        inside = (r < R) & (np.abs(ang) < self.half) & (dy > 0)
        ray = np.clip(((ang + self.half) / (2 * self.half) * self.nr).astype(int),
                      0, self.nr - 1)
        b = np.clip((r / R * self.nb).astype(int), 0, self.nb - 1)
        self.pix_index = np.where(inside, ray * self.nb + b, 0)
        self.pix_inside = inside

    # ----------------------------------------------------------- updating
    def refresh_all(self, levels):
        self.disp[:] = levels
        self.update()

    def advance(self, levels, dt):
        """Move the sweep line and copy the rays it just passed."""
        prev = self.sweep
        self.sweep += self.sweep_dir * self.sweep_speed * dt
        if self.sweep > self.half:
            self.sweep, self.sweep_dir = self.half, -1
        elif self.sweep < -self.half:
            self.sweep, self.sweep_dir = -self.half, 1
        lo, hi = sorted((prev, self.sweep))
        i0 = max(0, int((lo + self.half) / (2 * self.half) * self.nr))
        i1 = min(self.nr - 1, int(math.ceil((hi + self.half) / (2 * self.half) * self.nr)))
        self.disp[i0:i1 + 1] = levels[i0:i1 + 1]
        self.update()

    # ----------------------------------------------------------- drawing
    def render_frame(self):
        """Draw the radar picture plus overlays into a QImage."""
        rgb = LEVEL_COLORS[self.disp.ravel()[self.pix_index]]
        rgb[~self.pix_inside] = 0
        rgb = np.ascontiguousarray(rgb)
        img = QImage(rgb.data, W, H, 3 * W, QImage.Format_RGB888).copy()
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        self._draw_overlay(p)
        p.end()
        self.frame = img
        return img

    def _arc_rect(self, radius):
        return QRectF(CX - radius, CY - radius, 2 * radius, 2 * radius)

    def _polar(self, radius, rel_deg):
        a = math.radians(rel_deg)
        return QPointF(CX + radius * math.sin(a), CY - radius * math.cos(a))

    def _draw_overlay(self, p):
        half = self.half
        start16, span16 = int((90 - half) * 16), int(2 * half * 16)
        rng, hdg = self.info["range"], self.info["heading"]

        p.setPen(QPen(QColor("white"), 1.5))
        p.drawArc(self._arc_rect(R), start16, span16)

        pen = QPen(QColor(190, 190, 190), 1, Qt.DashLine)
        p.setPen(pen)
        for f in (0.25, 0.5, 0.75):
            p.drawArc(self._arc_rect(R * f), start16, span16)

        p.setFont(QFont("Arial", 9))
        p.setPen(QColor(77, 208, 225))
        for f in (0.25, 0.5, 0.75):
            pt = self._polar(R * f, -half + 5)
            p.drawText(pt + QPointF(-16, 0), f"{rng * f:g}")

        # Heading scale on the outer arc
        p.setPen(QPen(QColor("white"), 1))
        for d in range(0, 360, 5):
            rel = (d - hdg + 540) % 360 - 180
            if abs(rel) > half:
                continue
            length = 10 if d % 10 == 0 else 5
            p.drawLine(self._polar(R, rel), self._polar(R - length, rel))
            if d % 10 == 0:
                p.save()
                p.translate(self._polar(R - 22, rel))
                p.rotate(rel)
                p.setFont(QFont("Arial", 11 if d % 30 == 0 else 8))
                p.drawText(QRectF(-15, -8, 30, 16), Qt.AlignCenter, str(d // 10))
                p.restore()

        # Lubber line marker, sweep line, aircraft symbol
        p.setBrush(QColor(255, 212, 0))
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([QPointF(CX - 5, CY - R - 3),
                                 QPointF(CX + 5, CY - R - 3),
                                 QPointF(CX, CY - R + 8)]))
        p.setPen(QPen(QColor(255, 255, 255, 90), 1))
        p.drawLine(QPointF(CX, CY), self._polar(R, self.sweep))
        p.setPen(QPen(QColor(255, 212, 0), 3))
        p.drawLine(QPointF(CX - 15, CY), QPointF(CX + 15, CY))
        p.drawLine(QPointF(CX, CY - 10), QPointF(CX, CY + 13))
        p.drawLine(QPointF(CX - 6, CY + 11), QPointF(CX + 6, CY + 11))

        # Status text
        p.setFont(QFont("Arial", 10))
        g = self.info["gain"]
        p.setPen(QColor(0, 210, 0))
        p.drawText(10, 20, "WXR  GAIN " + ("CAL" if abs(g - 1) < 0.01 else f"{g:.2f}"))
        p.setFont(QFont("Arial", 8))
        p.setPen(QColor(210, 210, 210))
        p.drawText(10, 38, "RAIN G/Y/R   SNOW CYAN   MIXED WHITE")
        if self.info["turb"]:
            p.setPen(QColor(225, 0, 225))
            p.drawText(10, 53, "TURB")
        p.setPen(QColor(77, 208, 225))
        p.drawText(QRectF(W - 210, 6, 200, 18), Qt.AlignRight,
                   f"RNG {rng} NM   HDG {int(round(hdg)) % 360:03d}")
        p.drawText(QRectF(W - 210, 24, 200, 18), Qt.AlignRight, self.info["source"])

    def paintEvent(self, _event):
        img = self.render_frame()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("black"))
        scaled = img.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawImage((self.width() - scaled.width()) // 2,
                    (self.height() - scaled.height()) // 2, scaled)
        p.end()
