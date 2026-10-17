"""Procedural 3D human head for the hologram (pure numpy, no assets, no licences to worry about).

Model space: y up (chin ≈ -0.97, top of the skull = 1), x to the head's left/right, z towards the viewer.
The head floats: no neck, the skull closes under the jaw. Contents:
  * ``verts``  (n_lat, n_lon, 3) surface grid, ``normals`` the same shape
  * ``jaw``    per-vertex displacement for a fully open mouth (lip sync blends it in)
  * ``ears``   list of 3D polylines (helix, antihelix, concha, lobe) on both sides
  * ``hair``   short strands over the scalp (neat short haircut hint)
  * feature curves (brows, eyelids, nose, lips) that lie on the surface, used for crisp glowing lines
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# half-width / depth profiles along the height (y)
_Y = np.array([-1.0, -0.97, -0.9, -0.75, -0.55, -0.3, 0.0, 0.25, 0.5, 0.75, 0.9, 0.97, 1.0])
_HW = np.array([0.0, 0.17, 0.31, 0.53, 0.64, 0.68, 0.72, 0.76, 0.73, 0.60, 0.42, 0.24, 0.0])
_DF = np.array([0.0, 0.30, 0.56, 0.67, 0.72, 0.78, 0.82, 0.84, 0.80, 0.66, 0.46, 0.24, 0.0])
_DB = np.array([0.0, 0.20, 0.38, 0.58, 0.74, 0.88, 0.96, 0.99, 0.93, 0.76, 0.52, 0.26, 0.0])

MOUTH_Y = -0.545


def _g(v, s):
    return np.exp(-(np.asarray(v, float) / s) ** 2)


def _smooth(a, b, v):
    t = np.clip((np.asarray(v, float) - a) / (b - a), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def features(x, y):
    """Facial relief: displacement towards the viewer for a front point (x, y)."""
    ax = np.abs(x)
    d = 0.05 * _g(y - 0.14, 0.045) * _g(ax - 0.25, 0.25)                       # brow ridge
    d += 0.02 * _g(y - 0.10, 0.06) * _g(x, 0.07)                               # glabella
    d -= 0.10 * _g(ax - 0.29, 0.11) * _g(y - 0.02, 0.075)                      # eye sockets
    d += 0.035 * _g(ax - 0.29, 0.07) * _g(y - 0.02, 0.04)                      # eyeballs under the lids
    d += 0.05 * _g(ax - 0.45, 0.13) * _g(y + 0.08, 0.08)                       # cheekbones
    d -= 0.025 * _g(ax - 0.45, 0.12) * _g(y + 0.38, 0.10)                      # cheek hollows
    # nose: bridge growing towards the tip, sharp underside
    t = np.clip((0.12 - y) / 0.39, 0.0, 1.0)
    amp = np.where(y > -0.27, 0.02 + 0.19 * t ** 1.4, 0.21 * np.clip(1 - (-0.27 - y) / 0.09, 0, 1) ** 1.5)
    amp = np.where(y > 0.12, 0.0, amp)
    sx = 0.045 + 0.035 * t
    d += amp * _g(x, sx)
    d += 0.10 * _g(ax - 0.085, 0.04) * _g(y + 0.31, 0.035)                     # nostril wings (alae)
    d -= 0.03 * _g(ax - 0.05, 0.02) * _g(y + 0.345, 0.012)                     # nostrils
    d -= 0.012 * _g(x, 0.015) * _g(y + 0.42, 0.04)                             # philtrum
    d += 0.055 * _g(x, 0.17) * _g(y + 0.50, 0.032)                             # upper lip
    d += 0.06 * _g(x, 0.14) * _g(y + 0.60, 0.035)                              # lower lip
    d -= 0.02 * _g(y - MOUTH_Y, 0.008) * _g(x, 0.17)                           # mouth line
    d -= 0.025 * _g(x, 0.12) * _g(y + 0.68, 0.025)                             # under-lip fold
    d += 0.06 * _g(x, 0.13) * _g(y + 0.80, 0.07)                               # chin
    return d


def _profiles(y):
    return np.interp(y, _Y, _HW), np.interp(y, _Y, _DF), np.interp(y, _Y, _DB)


def _jaw_weight(y, z):
    return _smooth(MOUTH_Y + 0.015, MOUTH_Y - 0.07, y) * np.clip(z / 0.45, 0.0, 1.0)


def surface(theta, y):
    """Surface point(s) for longitude theta (0 = front) and height y."""
    hw, df, db = _profiles(y)
    s, c = np.sin(theta), np.cos(theta)
    x = hw * np.sign(s) * np.abs(s) ** 0.85
    depth = np.where(c >= 0, df, db)
    z = depth * c
    front = np.clip(c, 0.0, 1.0) ** 1.5
    z = z + features(x, y) * front
    return np.stack([x, np.broadcast_to(y, x.shape), z], -1)


def front_point(x, y):
    """Point on the face surface at (x, y) (inverts the front parametrisation)."""
    hw, df, _ = _profiles(y)
    s = np.clip(np.asarray(x, float) / np.maximum(hw, 1e-6), -1, 1)
    s = np.sign(s) * np.abs(s) ** (1 / 0.85)
    th = np.arcsin(s)
    return surface(th, np.asarray(y, float))


@dataclass
class Head:
    verts: np.ndarray
    normals: np.ndarray
    jaw: np.ndarray
    ears: list = field(default_factory=list)
    hair: list = field(default_factory=list)
    curves: dict = field(default_factory=dict)
    eyes: tuple = ()


def _normals(v):
    du = np.roll(v, -1, 1) - np.roll(v, 1, 1)
    dv = np.gradient(v, axis=0)
    n = np.cross(du, dv)
    n /= np.linalg.norm(n, axis=-1, keepdims=True) + 1e-9
    centre = v - np.array([0, 0.05, 0.05])
    flip = np.sum(n * centre, -1) < 0          # make them point outwards
    n[flip] *= -1
    return n


def _ears(side: int) -> list:
    out = []
    yc, zc = -0.08, -0.10
    hw = float(np.interp(yc, _Y, _HW))

    def ring(scale, stick, n=40, open_gap=0.0):
        pts = []
        for i in range(n + 1):
            t = open_gap / 2 + (2 * math.pi - open_gap) * i / n
            a = math.cos(t)                     # forward(-)/back(+)
            b = math.sin(t)
            rz = 0.095 * scale * (1.0 if b > -0.2 else 0.8)      # narrower lobe
            ry = 0.22 * scale
            y = yc + ry * b
            z = zc - rz * a
            back = max(0.0, a)                  # rear edge sticks out more
            x = side * (float(np.interp(y, _Y, _HW)) * 0.97 + stick * (0.25 + 0.75 * back))
            pts.append((x, y, z))
        return np.array(pts)
    out.append(ring(1.0, 0.17))                                  # helix (outer rim)
    out.append(ring(0.66, 0.10, open_gap=1.1))                   # antihelix
    out.append(ring(0.33, 0.04))                                 # concha
    lobe = np.array([(side * (hw * 0.97 + 0.06), yc - 0.20 + 0.03 * math.cos(t), zc + 0.03 * math.sin(t))
                     for t in np.linspace(0, math.pi, 10)])
    out.append(lobe)
    return out


def _hair(rng: np.random.Generator, n: int) -> list:
    """Short neat haircut: sparse strands combed back (top) and down (sides/back), floating just above the scalp."""
    strands: list[np.ndarray] = []
    hl_t = np.array([0.0, 0.9, 1.3, 1.7, 2.4, math.pi])
    hl_y = np.array([0.56, 0.47, 0.26, 0.18, -0.15, -0.28])
    centre = np.array([0.0, 0.05, -0.05])
    comb = np.array([0.0, -0.35, -1.0])
    tries = 0
    while len(strands) < n and tries < n * 20:
        tries += 1
        th = rng.uniform(-math.pi, math.pi)
        y = rng.uniform(-0.3, 0.995)
        if y < np.interp(abs(th), hl_t, hl_y):
            continue
        p = surface(np.array(th), np.array(y))
        L = rng.uniform(0.06, 0.11)
        pts = []
        for k in range(4):
            r = p - centre
            r /= np.linalg.norm(r) + 1e-9
            d = comb - np.dot(comb, r) * r            # tangent direction of the comb
            d /= np.linalg.norm(d) + 1e-9
            pts.append(p + r * (0.016 + 0.005 * k))
            p = p + d * L / 3
        strands.append(np.array(pts))
    return strands


def build_head(n_lat: int = 46, n_lon: int = 72, hair: int = 0, seed: int = 3) -> Head:
    ys = np.cos(np.linspace(0.02, math.pi - 0.02, n_lat))[:, None] * np.ones((1, n_lon))
    ths = np.linspace(-math.pi, math.pi, n_lon, endpoint=False)[None, :] * np.ones((n_lat, 1))
    v = surface(ths, ys)
    w = _jaw_weight(v[..., 1], v[..., 2])
    jaw = np.zeros_like(v)
    jaw[..., 1] = -0.15 * w
    jaw[..., 2] = -0.035 * w
    curves = {}
    xs = np.linspace(0.12, 0.45, 14)
    for side, nm in ((1, "brow_r"), (-1, "brow_l")):
        curves[nm] = front_point(side * xs, 0.165 + 0.03 * np.sin((xs - 0.12) / 0.33 * math.pi) - (xs - 0.12) * 0.06)
    ex = np.linspace(0.19, 0.40, 14)
    lid_up = 0.02 + 0.036 * np.sin((ex - 0.19) / 0.21 * math.pi)
    lid_lo = 0.02 - 0.022 * np.sin((ex - 0.19) / 0.21 * math.pi)
    for side, nm in ((1, "r"), (-1, "l")):
        curves["lid_up_" + nm] = front_point(side * ex, lid_up)
        curves["lid_lo_" + nm] = front_point(side * ex, lid_lo)
        a = np.linspace(-0.2, math.pi + 0.6, 12)
        curves["ala_" + nm] = front_point(side * (0.085 + 0.045 * np.cos(a)), -0.31 + 0.04 * np.sin(a))
    lx = np.linspace(-0.17, 0.17, 25)
    curves["lip_top"] = front_point(lx, -0.50 + 0.012 * np.abs(np.sin(lx / 0.17 * math.pi)) - 0.008 * _g(lx, 0.03)
                                    - 0.03 * (np.abs(lx) / 0.17) ** 3)
    curves["lip_mid"] = front_point(lx, MOUTH_Y - 0.004 * (1 - (lx / 0.17) ** 2))
    curves["lip_bot"] = front_point(lx * 0.85, -0.625 + 0.035 * (lx / 0.17) ** 2)
    nb = np.linspace(0.10, -0.29, 12)
    curves["nose_bridge"] = front_point(0.0 * nb + 0.035, nb)
    curves["nose_base"] = front_point(np.linspace(-0.07, 0.07, 9), -0.345 + 0.006 * np.cos(np.linspace(-1.5, 1.5, 9)))
    eyes = tuple(front_point(np.array(s * 0.295), np.array(0.02)) for s in (-1, 1))
    rng = np.random.default_rng(seed)
    return Head(verts=v, normals=_normals(v), jaw=jaw, ears=_ears(1) + _ears(-1), hair=_hair(rng, hair),
                curves=curves, eyes=eyes)


def jaw_curves(curves: dict) -> set:
    """Curves that follow the jaw when the mouth opens."""
    return {"lip_bot"}


def rotation(yaw: float, pitch: float) -> np.ndarray:
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    return rx @ ry


def project(p: np.ndarray, R: np.ndarray, cx: float, cy: float, s: float, f: float = 4.0):
    """-> screen x, screen y, rotated z (towards viewer)."""
    q = p @ R.T
    k = f / (f - q[..., 2])
    return cx + q[..., 0] * s * k, cy - q[..., 1] * s * k, q[..., 2]
