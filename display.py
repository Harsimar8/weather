"""ND-style weather radar display widget (PySide6), Boeing style, track up."""
import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from radar import LEVEL_COLORS, SCANNING_MODES

W, H = 640, 480          # internal image size (scaled to the widget)
CX, CY, R = W / 2, H - 50, 370.0
ARC_SPAN = 62.0          # compass arc (it leaves the screen past this)
RING_SPAN = 90.0         # range rings go round to the sides, like the weather

WHITE = QColor(235, 235, 235)
GREEN = QColor(0, 220, 0)
CYAN = QColor(70, 225, 255)        # Boeing: radar mode and tilt are cyan
AMBER = QColor(255, 170, 0)
RED = QColor(255, 35, 35)
MAGENTA = QColor(235, 70, 235)


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
        self.info = {"up": 0.0, "heading": 0.0, "up_label": "TRK", "hdg_bug": None,
                     "range": 40, "on": True, "mode": "WX", "tilt": 0.0,
                     "auto_tilt": False, "gain_db": 0.0, "gain_cal": True,
                     "source": "SIM", "pws": None}
        self.frame = QImage()
        self._build_pixel_map()

    def _build_pixel_map(self):
        """For every screen pixel, work out which ray and range bin it shows."""
        ys, xs = np.mgrid[0:H, 0:W]
        dx, dy = xs - CX, CY - ys
        r = np.hypot(dx, dy)
        ang = np.degrees(np.arctan2(dx, dy))
        inside = (r < R) & (np.abs(ang) < self.half) & (dy >= 0)
        ray = np.clip(((ang + self.half) / (2 * self.half) * self.nr).astype(int),
                      0, self.nr - 1)
        b = np.clip((r / R * self.nb).astype(int), 0, self.nb - 1)
        self.pix_index = np.where(inside, ray * self.nb + b, 0)
        self.pix_inside = inside

    # ----------------------------------------------------------- updating
    def transmitting(self):
        return self.info["on"] and self.info["mode"] in SCANNING_MODES

    def refresh_all(self, levels):
        self.disp[:] = levels
        self.update()

    def advance(self, scan_rays, dt):
        """Move the sweep line and scan only the rays it just passed.

        scan_rays(rays) returns levels for that slice of rays.
        """
        if not self.transmitting():
            self.disp[:] = 0
            self.update()
            return
        prev = self.sweep
        self.sweep += self.sweep_dir * self.sweep_speed * dt
        if self.sweep > self.half:
            self.sweep, self.sweep_dir = self.half, -1
        elif self.sweep < -self.half:
            self.sweep, self.sweep_dir = -self.half, 1
        lo, hi = sorted((prev, self.sweep))
        i0 = max(0, int((lo + self.half) / (2 * self.half) * self.nr))
        i1 = min(self.nr - 1, int(math.ceil((hi + self.half) / (2 * self.half) * self.nr)))
        self.disp[i0:i1 + 1] = scan_rays(slice(i0, i1 + 1))
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

    @staticmethod
    def _rel(deg, up):
        """Direction deg as degrees right of straight up (-180..180)."""
        return (deg - up + 540.0) % 360.0 - 180.0

    @staticmethod
    def _runs(p, x, y, parts):
        """Draw text pieces of different sizes/colors on one baseline."""
        for text, size, color in parts:
            p.setFont(QFont("Arial", size))
            p.setPen(color)
            p.drawText(QPointF(x, y), text)
            x += p.fontMetrics().horizontalAdvance(text)
        return x

    def _draw_overlay(self, p):
        """Boeing-style ND in MAP mode: compass arc, range rings, track box."""
        info = self.info
        rng, up = info["range"], info["up"]
        top = CY - R
        p.setBrush(Qt.NoBrush)

        # Range rings (solid), round to the sides; half range labelled
        start16, span16 = int((90 - RING_SPAN) * 16), int(2 * RING_SPAN * 16)
        p.setPen(QPen(WHITE, 1.3))
        for f in (0.25, 0.5, 0.75):
            p.drawArc(self._arc_rect(R * f), start16, span16)
        self._runs(p, CX - 34, CY - R * 0.5 + 16, [(f"{rng / 2:g}", 11, WHITE)])

        # Compass arc: ticks every 5 deg, numbers every 30 deg
        start16, span16 = int((90 - ARC_SPAN) * 16), int(2 * ARC_SPAN * 16)
        p.setPen(QPen(WHITE, 1.6))
        p.drawArc(self._arc_rect(R), start16, span16)
        for d in range(0, 360, 5):
            rel = self._rel(d, up)
            if abs(rel) > ARC_SPAN:
                continue
            p.setPen(QPen(WHITE, 1.6))
            p.drawLine(self._polar(R, rel), self._polar(R - (12 if d % 10 == 0 else 6), rel))
            if d % 30 == 0:
                p.save()
                p.translate(self._polar(R - 26, rel))
                p.rotate(rel)
                p.setFont(QFont("Arial", 13))
                p.drawText(QRectF(-20, -10, 40, 20), Qt.AlignCenter, str(d // 10))
                p.restore()

        self._draw_heading_marks(p)

        # Track line, sweep, aircraft symbol
        p.setPen(QPen(MAGENTA, 2))
        p.drawLine(QPointF(CX, CY - 20), QPointF(CX, top))
        if self.transmitting() and info["mode"] != "TEST":
            p.setPen(QPen(QColor(255, 255, 255, 70), 1))
            p.drawLine(QPointF(CX, CY), self._polar(R, self.sweep))
        p.setPen(QPen(WHITE, 2))
        p.drawPolygon(QPolygonF([QPointF(CX, CY - 20), QPointF(CX - 11, CY + 6),
                                 QPointF(CX + 11, CY + 6)]))
        p.drawLine(QPointF(CX, CY - 8), QPointF(CX, CY + 6))

        # Box at the top: "TRK [233] MAG"
        p.setPen(QPen(WHITE, 1.6))
        p.drawPolygon(QPolygonF([QPointF(CX - 7, top - 12), QPointF(CX + 7, top - 12),
                                 QPointF(CX, top)]))
        box = QRectF(CX - 30, top - 42, 60, 26)
        p.drawRect(box)
        p.setFont(QFont("Arial", 16))
        p.drawText(box, Qt.AlignCenter, f"{int(round(up)) % 360:03d}")
        p.setFont(QFont("Arial", 12))
        p.setPen(GREEN)
        p.drawText(QRectF(CX - 90, top - 42, 55, 26), Qt.AlignRight | Qt.AlignVCenter,
                   info["up_label"])
        p.drawText(QRectF(CX + 35, top - 42, 55, 26), Qt.AlignLeft | Qt.AlignVCenter, "MAG")

        # Top left: ground speed, true airspeed, wind
        gs, tas = info.get("gs"), info.get("tas")
        self._runs(p, 10, 22, [("GS", 9, WHITE), ("---" if gs is None else f"{gs:.0f}", 14, WHITE),
                               ("  TAS", 9, WHITE),
                               ("---" if tas is None else f"{tas:.0f}", 14, WHITE)])
        wf, wk = info.get("wind_from"), info.get("wind_kt")
        if wf is not None and wk is not None and wk >= 1:
            self._runs(p, 10, 42, [(f"{wf % 360:03.0f}°/{wk:.0f}", 13, WHITE)])
            p.save()
            p.translate(26, 66)
            p.rotate(wf - up)                 # arrow points where the wind blows to
            p.setPen(QPen(WHITE, 1.6))
            p.drawLine(QPointF(0, 12), QPointF(0, -12))
            p.drawLine(QPointF(0, 12), QPointF(-4, 5))
            p.drawLine(QPointF(0, 12), QPointF(4, 5))
            p.restore()

        # Top right: data source and range
        p.setFont(QFont("Arial", 12))
        p.setPen(MAGENTA)
        p.drawText(QRectF(W - 210, 6, 200, 20), Qt.AlignRight, info["source"])
        p.setPen(WHITE)
        p.drawText(QRectF(W - 210, 26, 200, 20), Qt.AlignRight, f"RNG {rng} NM")

        self._draw_pws(p)
        self._draw_wxr_status(p)

    def _draw_heading_marks(self, p):
        """Heading pointer (where the nose points) and the selected heading bug."""
        info = self.info
        up = info["up"]
        # Heading pointer: small white triangle just inside the compass arc.
        rel = self._rel(info["heading"], up)
        if abs(rel) <= ARC_SPAN:
            p.save()
            p.translate(self._polar(R - 2, rel))
            p.rotate(rel)
            p.setPen(QPen(WHITE, 1.6))
            p.drawPolygon(QPolygonF([QPointF(0, 0), QPointF(-6, 12), QPointF(6, 12)]))
            p.restore()
        # Selected heading bug: magenta notched box outside the arc. If it is
        # off the arc it waits at the end, like on the real ND.
        bug = info.get("hdg_bug")
        if bug is not None:
            rel = max(-ARC_SPAN, min(ARC_SPAN, self._rel(bug, up)))
            p.save()
            p.translate(self._polar(R + 1, rel))
            p.rotate(rel)
            p.setPen(QPen(MAGENTA, 2.2))
            p.drawPolyline(QPolygonF([QPointF(-10, 0), QPointF(-10, -10), QPointF(-4, -10),
                                      QPointF(-4, -4), QPointF(4, -4), QPointF(4, -10),
                                      QPointF(10, -10), QPointF(10, 0)]))
            p.restore()

    def _draw_pws(self, p):
        """Predictive windshear: red/black striped icon, amber edge lines, text."""
        pws = self.info.get("pws")
        if not pws:
            return
        scale = R / self.info["range"]
        r0 = max(6.0, pws["r0"] * scale)
        r1 = max(r0 + 14.0, pws["r1"] * scale)
        a0, a1 = pws["a0"], pws["a1"]
        if a1 - a0 < 8:                          # keep it visible on long ranges
            mid = (a0 + a1) / 2
            a0, a1 = mid - 4, mid + 4
        # Red and black bands across the windshear area
        bands = 5
        steps = [a0 + (a1 - a0) * k / 12 for k in range(13)]
        p.setPen(Qt.NoPen)
        for k in range(bands):
            ra = r0 + (r1 - r0) * k / bands
            rb = r0 + (r1 - r0) * (k + 1) / bands
            pts = [self._polar(rb, a) for a in steps] + [self._polar(ra, a) for a in reversed(steps)]
            p.setBrush(RED if k % 2 == 0 else QColor("black"))
            p.drawPolygon(QPolygonF(pts))
        p.setBrush(Qt.NoBrush)
        # Two amber lines from the edges of the area to the outer arc
        p.setPen(QPen(AMBER, 4))
        for a in (a0, a1):
            p.drawLine(self._polar(r1, a), self._polar(R, a))
        # Text: red WINDSHEAR = warning, amber = caution
        if pws["level"] in ("warning", "caution"):
            p.setFont(QFont("Arial", 16, QFont.Bold))
            p.setPen(RED if pws["level"] == "warning" else AMBER)
            p.drawText(QRectF(W - 270, CY - R * 0.56, 200, 26),
                       Qt.AlignRight | Qt.AlignVCenter, "WINDSHEAR")

    def _draw_wxr_status(self, p):
        """Weather radar annunciation on the left (cyan): mode, tilt, gain."""
        info = self.info
        if not info["on"]:
            return
        mode = info["mode"]
        x, y = 10, CY - 120
        p.setFont(QFont("Arial", 13))
        if mode == "OFF":
            p.setPen(AMBER)
            p.drawText(QPointF(x, y), "WXR")
            p.drawText(QPointF(x, y + 20), "OFF")
            return
        p.setPen(CYAN)
        if mode == "STBY":
            p.drawText(QPointF(x, y), "WXR")
            p.drawText(QPointF(x, y + 20), "STBY")
            return
        if mode == "TEST":
            p.drawText(QPointF(x, y), "WXR")
            p.drawText(QPointF(x, y + 20), "TEST")
            p.drawText(QRectF(0, CY - R * 0.62, W, 24), Qt.AlignCenter, "WXR TEST")
            return
        label = {"WX": "WXR", "WX+T": "WX+T", "MAP": "MAP"}[mode]
        tilt = info["tilt"]
        tilt_txt = ("0" if abs(tilt) < 0.05 else f"{tilt:+.1f}") + ("A" if info["auto_tilt"] else "")
        p.drawText(QPointF(x, y), label)
        p.drawText(QPointF(x, y + 20), tilt_txt)
        if not info["gain_cal"]:
            p.drawText(QPointF(x, y + 40), "VAR")

    def paintEvent(self, _event):
        img = self.render_frame()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("black"))
        scaled = img.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawImage((self.width() - scaled.width()) // 2,
                    (self.height() - scaled.height()) // 2, scaled)
        p.end()
