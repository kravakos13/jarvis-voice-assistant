"""Animated holographic head drawn with QPainter (no OpenGL/WebEngine needed -> robust, offline).

The head is a procedural 3D mesh (gui/headmesh.py): floating head without a neck, realistic proportions,
ears (bald, like the 3D head); rendered as a wireframe + point cloud with a fresnel rim glow, lip sync moves the jaw.

States: idle (breathing, blinking, eye movement), listening (reacts to mic level),
thinking (orbiting arcs), speaking (lip sync from TTS envelope), muted (dim).
"""
from __future__ import annotations

import math
import random
import time

import numpy as np

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen,
                           QRadialGradient)
from PySide6.QtCore import QLineF
from PySide6.QtGui import QPolygonF
from PySide6.QtWidgets import QWidget

from . import headmesh as hm

CYAN = QColor(70, 220, 255)
DEEP = QColor(20, 120, 255)
STATE_LABELS = {"idle": "", "sleeping": "СПЛЮ", "listening": "СЛУШАЮ", "thinking": "ДУМАЮ", "speaking": "", "muted": "МИКРОФОН ВЫКЛ",
                "loading": "ЗАГРУЗКА"}



def _a(v: float) -> int:
    """Alpha clamped to 0..255 (glow strength can exceed 1.0 while speaking)."""
    return 0 if v != v else max(0, min(255, int(v)))

class HologramFace(QWidget):
    def __init__(self, parent=None, fps: int = 40, clock=time.monotonic):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(320, 320)
        self.clock = clock
        self.t0 = clock()
        self.state = "loading"
        self.mic_level = 0.0
        self._mic_smooth = 0.0
        # lip sync
        self.env: list = []
        self.wid: list = []
        self.env_dt = 0.02
        self.env_start = 0.0
        self.mouth_open = 0.0
        self.mouth_wide = 0.5
        # eyes
        self.gaze = QPointF(0, 0)
        self.gaze_target = QPointF(0, 0)
        self.next_saccade = 0.0
        self.blink_start = -10.0
        self.next_blink = 2.0
        self.glitch_until = 0.0
        self.glitch_dx = 0.0
        self.next_glitch = 4.0
        rnd = random.Random(7)
        self.particles = [[rnd.random(), rnd.random(), 0.2 + rnd.random() * 0.8, rnd.random() * 6.28] for _ in range(70)]
        self.cyan, self.deep = QColor(CYAN), QColor(DEEP)
        self.base_fps = fps
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(int(1000 / max(10, fps)))

    # ------------------------------------------------------------ API (call from GUI thread)
    def set_color(self, color) -> None:
        """Hologram tint (hex string or QColor); the deep tone is derived from it."""
        c = QColor(color)
        if not c.isValid():
            return
        self.cyan = c
        h, sat, v, _ = c.getHsv()
        self.deep = QColor.fromHsv((h + 10) % 360, min(255, sat + 30), max(80, v - 60))
        self.update()

    def set_animation(self, level: str) -> None:
        """full / lite (weak PCs) / off (almost static)."""
        fps = {"full": self.base_fps, "lite": 20, "off": 8}.get(level, self.base_fps)
        self.timer.setInterval(int(1000 / max(5, fps)))

    def set_state(self, state: str) -> None:
        self.state = state

    def set_mic_level(self, level: float) -> None:
        self.mic_level = max(0.0, min(1.0, level))

    def speak(self, env: list, wid: list, dt: float = 0.02, start: float | None = None) -> None:
        self.env, self.wid, self.env_dt = env or [], wid or [], dt
        self.env_start = self.clock() if start is None else start
        self.state = "speaking"

    def speech_end(self) -> None:
        self.env, self.wid = [], []

    # ------------------------------------------------------------ animation update
    def _tick(self, now: float) -> None:
        t = now - self.t0
        # lip sync
        target_open, target_wide = 0.0, 0.5
        if self.env:
            i = int((now - self.env_start) / self.env_dt)
            if 0 <= i < len(self.env):
                target_open = self.env[i]
                target_wide = self.wid[i] if i < len(self.wid) else 0.5
            elif i >= len(self.env):
                self.env = []
        k = 0.55 if target_open > self.mouth_open else 0.35
        self.mouth_open += (target_open - self.mouth_open) * k
        self.mouth_wide += (target_wide - self.mouth_wide) * 0.25
        self._mic_smooth += (self.mic_level - self._mic_smooth) * 0.3
        # eyes
        if t > self.next_saccade:
            r = 0.9 if self.state != "thinking" else 0.4
            self.gaze_target = QPointF(random.uniform(-r, r), random.uniform(-0.5, 0.5))
            if self.state == "thinking":
                self.gaze_target = QPointF(random.uniform(-0.3, 0.3), -0.7)
            self.next_saccade = t + random.uniform(0.8, 2.8)
        self.gaze += (self.gaze_target - self.gaze) * 0.18
        if t > self.next_blink:
            self.blink_start = t
            self.next_blink = t + random.uniform(2.5, 6.0)
        if t > self.next_glitch:
            self.glitch_until = t + random.uniform(0.05, 0.12)
            self.glitch_dx = random.choice([-1, 1]) * random.uniform(3, 8)
            self.next_glitch = t + random.uniform(3, 9)

    def _blink(self, t: float) -> float:
        d = t - self.blink_start
        if 0 <= d < 0.16:
            return 1 - math.sin(d / 0.16 * math.pi) * 0.92
        return 1.0

    # ------------------------------------------------------------ drawing helpers
    @staticmethod
    def _glow_path(p: QPainter, path: QPainterPath, color: QColor, width: float, strength: float = 1.0) -> None:
        p.setBrush(Qt.NoBrush)
        for w, a in ((width * 7, 18), (width * 4, 35), (width * 2.2, 70), (width, 230)):
            c = QColor(color)
            c.setAlpha(_a(a * strength))
            p.setPen(QPen(c, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)

    # ------------------------------------------------------------ paint
    def paintEvent(self, _ev) -> None:  # noqa: N802
        now = self.clock()
        self._tick(now)
        t = now - self.t0
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        state = self.state
        dim = 0.45 if state == "muted" else 1.0
        flicker = 0.93 + 0.07 * math.sin(t * 37) * math.sin(t * 13.3) if random.random() > 0.02 else 0.75
        strength = dim * flicker
        base = QColor(self.cyan)
        if state == "thinking":
            base = QColor(120, 170, 255)
        elif state == "listening":
            base = QColor(110, 240, 255)

        # backdrop glow
        g = QRadialGradient(QPointF(W / 2, H * 0.45), max(W, H) * 0.6)
        g.setColorAt(0, QColor(10, 80, 140, _a(150 * dim)))
        g.setColorAt(0.55, QColor(5, 30, 70, _a(90 * dim)))
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), QBrush(g))

        # perspective grid floor
        horizon, floor_y = H * 0.80, H
        grid = QColor(base)
        for i in range(-12, 13):
            grid.setAlpha(_a(40 * dim))
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(W / 2 + i * 14, horizon), QPointF(W / 2 + i * 80, floor_y))
        off = (t * 0.35) % 1.0
        for j in range(8):
            z = (j + off) / 8
            y = horizon + (floor_y - horizon) * z * z
            grid.setAlpha(_a(60 * z * dim))
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(0, y), QPointF(W, y))

        # particles
        for part in self.particles:
            x, y0, sp, ph = part
            y = (y0 - t * 0.03 * sp) % 1.0
            a = int((0.4 + 0.6 * abs(math.sin(t * 2 * sp + ph))) * 140 * dim)
            c = QColor(base)
            c.setAlpha(_a(a))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            r = 0.8 + sp * 1.6
            p.drawEllipse(QPointF(x * W, y * H * 0.95), r, r)

        # head geometry
        s = min(W, H) * 0.33 * (1 + 0.012 * math.sin(t * 1.6))
        cx = W / 2 + 4 * math.sin(t * 0.37)
        cy = H * 0.44 + 3 * math.sin(t * 0.8)
        if t < self.glitch_until:
            cx += self.glitch_dx
        yaw = 0.18 * math.sin(t * 0.33) + self.gaze.x() * 0.05

        # ring (listening = mic level, speaking = amplitude, thinking = orbit)
        ring_r = s * 1.28
        level = self._mic_smooth if state == "listening" else (self.mouth_open * 0.8 if state == "speaking" else 0.0)
        ring = QPainterPath()
        ring.addEllipse(QPointF(cx, cy), ring_r * (1 + 0.06 * level), ring_r * (1 + 0.06 * level))
        self._glow_path(p, ring, base, 1.2, strength * (0.35 + level))
        if state == "listening":
            for k in range(36):   # equalizer ticks
                ang = k / 36 * 2 * math.pi
                amp = level * (0.5 + 0.5 * math.sin(t * 9 + k * 1.7)) * s * 0.18
                a1 = QPointF(cx + math.cos(ang) * (ring_r + 6), cy + math.sin(ang) * (ring_r + 6))
                a2 = QPointF(cx + math.cos(ang) * (ring_r + 8 + amp), cy + math.sin(ang) * (ring_r + 8 + amp))
                c = QColor(base)
                c.setAlpha(_a(200 * strength))
                p.setPen(QPen(c, 2.2, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(a1, a2)
        if state in ("thinking", "loading"):
            for k in range(3):
                start = (t * (90 + k * 40) + k * 120) % 360
                arc = QPainterPath()
                rr = ring_r + 10 + k * 9
                rect = QRectF(cx - rr, cy - rr, rr * 2, rr * 2)
                arc.arcMoveTo(rect, start)
                arc.arcTo(rect, start, 70 - k * 15)
                self._glow_path(p, arc, QColor(140, 160, 255) if k == 1 else base, 1.6, strength)

        pitch = 0.05 * math.sin(t * 0.5) + (-0.08 if state == "thinking" else 0.0)
        self._draw_head(p, cx, cy, s * 0.92, yaw, pitch, base, strength, t, state)

        # state label
        label = STATE_LABELS.get(state, "")
        if label:
            if state == "thinking":
                label += "." * (int(t * 3) % 4)
            f = QFont("Consolas", max(9, int(s * 0.07)))
            f.setLetterSpacing(QFont.AbsoluteSpacing, 3)
            p.setFont(f)
            c = QColor(base)
            c.setAlpha(_a(220 * strength))
            p.setPen(c)
            p.drawText(QRectF(0, cy + s * 1.38, W, 30), Qt.AlignHCenter | Qt.AlignTop, label)
        p.end()

    # ------------------------------------------------------------ 3D head
    def _head_model(self, lite: bool):
        key = "lite" if lite else "full"
        cache = getattr(self, "_heads", None)
        if cache is None:
            cache = self._heads = {}
        if key not in cache:
            cache[key] = hm.build_head(28, 44, 0) if lite else hm.build_head(40, 60, 0)
        return cache[key]

    @staticmethod
    def _bucket_lines(p: QPainter, xs, ys, alpha, color: QColor, width: float, levels: int = 6) -> None:
        """Segments (x0,y0,x1,y1) grouped by alpha -> a few drawLines calls (fast)."""
        lv = np.clip((alpha * levels).astype(int), 0, levels - 1)
        lv[alpha < 0.03] = -1                       # invisible segments are skipped
        for k in range(levels):
            sel = np.nonzero(lv == k)[0]
            if not len(sel):
                continue
            c = QColor(color)
            c.setAlpha(_a((k + 0.5) / levels * 255))
            p.setPen(QPen(c, width))
            seg = np.stack([xs[0][sel], ys[0][sel], xs[1][sel], ys[1][sel]], 1).tolist()
            p.drawLines([QLineF(a, b, c2, d) for a, b, c2, d in seg])

    def _curve(self, pts, R, cx, cy, s) -> QPainterPath:
        X, Y, _ = hm.project(pts, R, cx, cy, s)
        path = QPainterPath(QPointF(X[0], Y[0]))
        for x, y in zip(X[1:], Y[1:]):
            path.lineTo(x, y)
        return path

    def _draw_head(self, p: QPainter, cx, cy, s, yaw, pitch, base: QColor, strength: float, t: float, state: str) -> None:
        lite = self.timer.interval() > 40
        H = self._head_model(lite)
        R = hm.rotation(yaw, pitch)
        mo = self.mouth_open if state == "speaking" or self.env else self.mouth_open * 0.5
        mo = max(0.0, min(1.0, mo))
        V = H.verts + H.jaw * mo
        X, Y, Z = hm.project(V, R, cx, cy, s)
        nz = (H.normals @ R.T)[..., 2]
        fres = 1.0 - np.clip(nz, 0.0, 1.0)
        light = np.clip((H.normals @ R.T) @ np.array([-0.45, 0.55, 0.70]), 0.0, 1.0)   # key light: relief reads
        alpha = np.where(nz > 0, 0.06 + 0.70 * fres ** 1.6 + 0.30 * light ** 3, 0.03) * strength
        ny = (H.normals @ R.T)[..., 1]
        alpha *= np.clip(1.0 + (ny + 0.35) * 2.0, 0.0, 1.0)               # hide the underside of the jaw
        alpha[H.verts[..., 1] < -0.86] = 0.0                                # no clutter of tiny rings under the chin
        alpha[:2] *= 0.3

        # soft volume glow behind the head
        g = QRadialGradient(QPointF(cx, cy), s * 1.05)
        c0 = QColor(base); c0.setAlpha(_a(55 * strength))
        g.setColorAt(0, c0)
        c1 = QColor(self.deep); c1.setAlpha(_a(25 * strength))
        g.setColorAt(0.7, c1)
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen); p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), s * 0.95, s * 1.08)

        # wireframe: latitude rings + meridians (every 2nd)
        xa, ya = (X, np.roll(X, -1, 1)), (Y, np.roll(Y, -1, 1))
        a_lat = (alpha + np.roll(alpha, -1, 1)) / 2
        self._bucket_lines(p, (xa[0].ravel(), xa[1].ravel()), (ya[0].ravel(), ya[1].ravel()), a_lat.ravel() * 0.75,
                           base, 1.0)
        Xm, Ym, Am = X[:, ::2], Y[:, ::2], alpha[:, ::2]
        self._bucket_lines(p, (Xm[:-1].ravel(), Xm[1:].ravel()), (Ym[:-1].ravel(), Ym[1:].ravel()),
                           ((Am[:-1] + Am[1:]) / 2).ravel() * 0.6, base, 1.0)
        # point cloud (vertices), brightest on the rim
        front = (nz.ravel() > -0.05) & (alpha.ravel() > 0.04)
        pts_a = (alpha.ravel() * 1.1)[front]
        px, py = X.ravel()[front], Y.ravel()[front]
        lv = np.clip((pts_a * 4).astype(int), 0, 3)
        for k in range(4):
            sel = np.nonzero(lv == k)[0]
            if not len(sel):
                continue
            c = QColor(220, 250, 255) if k == 3 else QColor(base)
            c.setAlpha(_a((k + 1) / 4 * 230))
            p.setPen(QPen(c, 1.8 if k >= 2 else 1.3, Qt.SolidLine, Qt.RoundCap))
            p.drawPoints(QPolygonF([QPointF(x, y) for x, y in zip(px[sel].tolist(), py[sel].tolist())]))

        # hair: sparse shimmering strands, more transparent than the face
        hair_paths = [QPainterPath(), QPainterPath(), QPainterPath()]
        tips = []
        for i, st in enumerate(H.hair):
            hx, hy, hz = hm.project(st, R, cx, cy, s)
            if hz.mean() < -0.15:
                continue
            k = int(1.5 + 1.5 * math.sin(t * 1.7 + i * 0.73))
            k = max(0, min(2, k))
            path = hair_paths[k]
            path.moveTo(hx[0], hy[0])
            for x, y in zip(hx[1:], hy[1:]):
                path.lineTo(x, y)
            if i % 3 == 0:
                tips.append(QPointF(hx[-1], hy[-1]))
        for k, path in enumerate(hair_paths):
            c = QColor(base).lighter(115)
            c.setAlpha(_a((70 + 55 * k) * strength))
            p.setPen(QPen(c, 1.0, Qt.SolidLine, Qt.RoundCap)); p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        if tips:
            c = QColor(200, 245, 255); c.setAlpha(_a((90 + 60 * math.sin(t * 2.3)) * strength))
            p.setPen(QPen(c, 1.6, Qt.SolidLine, Qt.RoundCap))
            p.drawPoints(QPolygonF(tips))

        # ears (helix / antihelix / concha / lobe on both sides)
        for i, ear in enumerate(H.ears):
            side = 1 if i < 4 else -1
            ear_nz = float(R[2, 0]) * side + float(R[2, 2]) * 0.25   # outward ear normal, z after rotation
            vis = max(0.07, min(1.0, 0.45 + 1.6 * ear_nz))           # the far ear is hidden behind the head
            self._glow_path(p, self._curve(ear, R, cx, cy, s), base, 1.1 if i % 4 == 0 else 0.8,
                            strength * vis * (1.1 if i % 4 == 0 else 0.7))

        # facial feature lines
        C = H.curves
        lift = 0.03 * (self._mic_smooth if state == "listening" else 0) + (0.025 if state == "thinking" else 0)
        for nm in ("brow_l", "brow_r"):
            self._glow_path(p, self._curve(C[nm] + np.array([0, lift, 0.01]), R, cx, cy, s), base, 1.5, strength * 0.9)
        self._glow_path(p, self._curve(C["nose_bridge"], R, cx, cy, s), base, 0.8, strength * 0.35)
        self._glow_path(p, self._curve(C["nose_base"], R, cx, cy, s), base, 1.0, strength * 0.6)
        for nm in ("ala_l", "ala_r"):
            self._glow_path(p, self._curve(C[nm], R, cx, cy, s), base, 1.0, strength * 0.7)

        # eyes: lids (blink) + iris following the gaze
        blink = self._blink(t)
        for side, nm in ((-1, "l"), (1, "r")):
            up, lo = C["lid_up_" + nm], C["lid_lo_" + nm]
            lid = lo + (up - lo) * blink
            upper = self._curve(lid + np.array([0, 0, 0.004]), R, cx, cy, s)
            lower = self._curve(lo + np.array([0, 0, 0.004]), R, cx, cy, s)
            eye = QPainterPath(upper)
            lx, ly, _ = hm.project(lo[::-1], R, cx, cy, s)
            for x, y in zip(lx, ly):
                eye.lineTo(x, y)
            eye.closeSubpath()
            if blink > 0.3:
                ec = H.eyes[0 if side < 0 else 1]
                iris = ec + np.array([self.gaze.x() * 0.035, -self.gaze.y() * 0.012 + 0.002, 0.02])
                ix, iy, _ = hm.project(iris[None, :], R, cx, cy, s)
                ix, iy = float(ix[0]), float(iy[0])
                ir = s * 0.045
                p.save(); p.setClipPath(eye)
                ig = QRadialGradient(QPointF(ix, iy), ir * 1.5)
                ig.setColorAt(0, QColor(210, 250, 255, _a(255 * strength)))
                ic2 = QColor(base); ic2.setAlpha(_a(210 * strength))
                ig.setColorAt(0.45, ic2)
                ig.setColorAt(1, QColor(0, 0, 0, 0))
                p.setPen(Qt.NoPen); p.setBrush(QBrush(ig))
                p.drawEllipse(QPointF(ix, iy), ir * 1.5, ir * 1.5)
                p.setBrush(QColor(5, 30, 60, _a(220 * strength)))
                p.drawEllipse(QPointF(ix, iy), ir * 0.33, ir * 0.33)
                p.restore()
            self._glow_path(p, upper, base, 1.3, strength)
            self._glow_path(p, lower, base, 0.9, strength * 0.6)

        # lips: upper lip stays, lower lip and jaw follow the lip-sync envelope
        jaw_off = np.array([0.0, -0.15 * mo, -0.035 * mo])
        mid = C["lip_mid"]
        lower_top = mid + jaw_off * hm._g(mid[:, 0], 0.16)[:, None]
        if mo > 0.04:
            inner = self._curve(mid, R, cx, cy, s)
            bx, by, _ = hm.project(lower_top[::-1], R, cx, cy, s)
            for x, y in zip(bx, by):
                inner.lineTo(x, y)
            inner.closeSubpath()
            ig = QRadialGradient(inner.boundingRect().center(), max(4.0, inner.boundingRect().width() / 2))
            ig.setColorAt(0, QColor(0, 8, 20, _a(235 * strength)))
            ic = QColor(self.deep); ic.setAlpha(_a(150 * strength))
            ig.setColorAt(1, ic)
            p.setPen(Qt.NoPen); p.setBrush(QBrush(ig))
            p.drawPath(inner)
        self._glow_path(p, self._curve(C["lip_top"], R, cx, cy, s), base, 1.3, strength * 0.9)
        self._glow_path(p, self._curve(mid, R, cx, cy, s), base, 1.4, strength)
        if mo > 0.04:
            self._glow_path(p, self._curve(lower_top, R, cx, cy, s), base, 1.1, strength * 0.8)
        self._glow_path(p, self._curve(C["lip_bot"] + jaw_off, R, cx, cy, s), base, 1.1, strength * 0.7)

        # scan band sweeping over the head
        band_y = cy - s + ((t * 0.45) % 1.0) * 2.1 * s
        band = QLinearGradient(QPointF(0, band_y - 14), QPointF(0, band_y + 14))
        bc = QColor(base); bc.setAlpha(0)
        band.setColorAt(0, bc)
        bc.setAlpha(_a(45 * strength))
        band.setColorAt(0.5, bc)
        bc.setAlpha(0)
        band.setColorAt(1, bc)
        p.fillRect(QRectF(cx - s * 0.85, band_y - 14, s * 1.7, 28), QBrush(band))

