"""«Своё лицо»: import (local copy only), landmark normalisation, detectors' fallback chain, calibration points."""
import json

import pytest
from PIL import Image, ImageDraw

from jarvis.gui import faceimage as fi


@pytest.fixture()
def faces_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fi, "PROJECT_ROOT", tmp_path)
    return tmp_path / "faces"


def _glow_face(path, size=(400, 480)):
    """Synthetic glowing face: bright ellipse with darker eyes and mouth on black."""
    im = Image.new("RGB", size, (0, 0, 0))
    d = ImageDraw.Draw(im)
    W, H = size
    d.ellipse((W * 0.2, H * 0.1, W * 0.8, H * 0.9), fill=(40, 120, 220))
    for cx in (0.38, 0.62):
        d.ellipse((W * (cx - 0.05), H * 0.44, W * (cx + 0.05), H * 0.48), fill=(200, 240, 255))
    d.line((W * 0.42, H * 0.73, W * 0.58, H * 0.73), fill=(200, 240, 255), width=3)
    im.save(path)
    return path


def test_default_face_landmarks_are_valid():
    lm = fi.normalize(json.loads(fi.DEFAULT_LM.read_text(encoding="utf-8")))
    (l, r), m = lm["eyes"], lm["mouth"]
    assert l[0] < 0.5 < r[0] and abs(l[1] - r[1]) < 0.03          # two level eyes left/right of centre
    assert l[1] < m[1] < lm["chin"]                                # eyes above mouth above chin
    assert abs(m[0] - 0.5) < 0.05 and 0 < lm["hue"] < 1


def test_normalize_rejects_garbage_and_orders_eyes():
    with pytest.raises(ValueError):
        fi.normalize({"eyes": [[0.3, 0.4, 0.05, 0.02]], "mouth": [0.5, 0.7, 0.1, 0.02], "chin": 0.8})
    with pytest.raises(ValueError):
        fi.normalize({"mouth": [0.5]})
    lm = fi.normalize({"eyes": [[0.7, 0.4, 0.05, 0.02], [0.3, 0.4, 0.05, 0.02]], "mouth": [0.5, 0.7, 0.1, 0.02],
                       "chin": 0.5, "hue": 1.25})
    assert lm["eyes"][0][0] == 0.3 and lm["chin"] > lm["mouth"][1] and lm["hue"] == 0.25


def test_points_roundtrip():
    lm = fi.from_points((0.4, 0.45), (0.6, 0.45), (0.45, 0.7), (0.55, 0.7), (0.5, 0.85))
    p = fi.to_points(lm)
    assert p["eye_l"] == [0.4, 0.45] and p["chin"][1] == 0.85
    assert abs(p["mouth_l"][0] - 0.45) < 1e-3 and abs(p["mouth_r"][0] - 0.55) < 1e-3


def test_import_copies_locally_and_falls_back_to_heuristic(tmp_path, faces_dir):
    src = _glow_face(tmp_path / "me.png")
    lm = fi.import_face(src, detector=fi.detect_heuristic)        # no ML needed for the test
    img, lmp = fi.custom_paths()
    assert img.parent == faces_dir and img.is_file() and lmp.is_file()
    assert lm["source"] == "heuristic" and fi.has_custom()
    l, r = lm["eyes"]
    assert 0.25 < l[0] < 0.5 < r[0] < 0.75 and l[1] < lm["mouth"][1] < lm["chin"]
    url, f, lm2 = fi.current({"gui": {"face_image": "custom"}})
    assert url.startswith("jarvis:///userface/custom.png") and f == img and lm2["eyes"] == lm["eyes"]
    url, f, _ = fi.current({"gui": {"face_image": "default"}})
    assert f == fi.DEFAULT_IMAGE and url == "jarvis:///faces/default.jpg"
    fi.remove_custom()
    assert not fi.has_custom()
    assert fi.current({"gui": {"face_image": "custom"}})[1] == fi.DEFAULT_IMAGE   # missing -> default


def test_import_rejects_non_images(tmp_path, faces_dir):
    bad = tmp_path / "x.txt"
    bad.write_text("hi")
    with pytest.raises(ValueError):
        fi.import_face(bad)
    big = tmp_path / "big.png"
    Image.new("RGBA", (3000, 1500), (10, 20, 30, 0)).save(big)
    fi.import_face(big, detector=fi.detect_heuristic)
    out = Image.open(fi.custom_paths()[0])
    assert max(out.size) == fi.MAX_SIDE and out.mode == "RGB"


def test_detect_chain_survives_broken_detectors(tmp_path, monkeypatch):
    img = fi.load_image(_glow_face(tmp_path / "f.png"))
    monkeypatch.setattr(fi, "detect_mediapipe", lambda im: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(fi, "detect_opencv", lambda im: None)
    assert fi.detect(img)["source"] == "heuristic"


def test_calibration_dialog(tmp_path, faces_dir):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from jarvis.gui.facecalib import FaceCalibDialog
    app = QApplication.instance() or QApplication([])  # noqa: F841
    lm = fi.import_face(_glow_face(tmp_path / "me.png"), detector=fi.detect_heuristic)
    dlg = FaceCalibDialog(fi.custom_paths()[0], lm)
    m = dlg.markers["mouth_r"]
    m.setPos(m.pos().x() + 20, m.pos().y())
    out = dlg.result_landmarks()
    assert out["source"] == "manual" and out["mouth"][2] > 0.0
    assert abs(out["eyes"][0][0] - lm["eyes"][0][0]) < 0.01
    dlg.deleteLater()
