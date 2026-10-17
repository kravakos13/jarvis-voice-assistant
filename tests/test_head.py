"""Procedural hologram head: anatomy sanity checks + offscreen render."""
import numpy as np

from jarvis.gui import headmesh as hm


def test_head_geometry():
    h = hm.build_head()
    v = h.verts
    assert v[..., 1].min() > -1.0 and v[..., 1].max() <= 1.0          # floating head: nothing below the chin
    bottom = v[-1]                                                     # skull closes under the jaw (no neck)
    assert np.ptp(bottom[:, 0]) < 0.05 and np.ptp(bottom[:, 2]) < 0.05
    head_w = np.abs(v[..., 0]).max()
    ears = [e for e in h.ears]
    assert len(ears) == 8 and max(np.abs(e[:, 0]).max() for e in ears) > head_w + 0.05   # ears stick out
    assert {e[0, 0] > 0 for e in ears} == {True, False}                                  # both sides
    nose_tip = hm.front_point(np.array(0.0), np.array(-0.27))
    cheek = hm.front_point(np.array(0.3), np.array(-0.27))
    assert nose_tip[2] - cheek[2] > 0.15                                                 # real nose
    moved = np.abs(h.jaw).sum(-1) > 1e-6
    assert moved.any() and v[moved][:, 1].max() < hm.MOUTH_Y + 0.02                     # only the jaw moves
    assert h.hair == []                                                                    # bald by default (art direction)
    hh = hm.build_head(hair=200)
    assert len(hh.hair) > 100 and all(s[:, 1].min() > -0.45 for s in hh.hair)            # optional hair stays on the scalp


def test_face_renders_offscreen():
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from jarvis.gui.face import HologramFace
    f = HologramFace()
    f.resize(300, 300)
    f.set_color("#ff8040")
    assert f.cyan == QColor("#ff8040")
    for st in ("idle", "listening", "thinking", "muted"):
        f.set_state(st)
        f.grab()
    f.speak([0.9] * 50, [0.5] * 50)
    img = f.grab().toImage().convertToFormat(QImage.Format_ARGB32)
    assert not img.isNull() and img.width() >= 300
    f.set_animation("lite")
    f.grab()
