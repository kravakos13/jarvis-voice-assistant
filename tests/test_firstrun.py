"""First-run «своё лицо» offer: shown once, choice stored, picture copied only into the local faces folder."""
import os

import pytest
from PIL import Image

from jarvis.gui import faceimage as fi
from jarvis.gui import firstrun as fr


@pytest.fixture()
def faces_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fi, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("JARVIS_NO_FIRSTRUN", raising=False)
    return tmp_path / "faces"


def _img(path):
    Image.new("RGB", (300, 360), (30, 90, 200)).save(path)
    return path


def test_should_prompt_rules(faces_dir, monkeypatch):
    assert fr.should_prompt({"gui": {"face_style": "image"}})
    assert not fr.should_prompt({"gui": {"face_style": "image", "face_prompted": True}})
    assert not fr.should_prompt({"gui": {"face_style": "3d"}})
    monkeypatch.setenv("JARVIS_NO_FIRSTRUN", "1")
    assert not fr.should_prompt({"gui": {"face_style": "image"}})


def test_accept_and_skip_store_choice(faces_dir, tmp_path):
    saved = []
    cfg = {"gui": {"face_style": "image"}}
    src = _img(tmp_path / "me.png")
    lm = fr.accept_image(cfg, src, saved.append, detector=fi.detect_heuristic)
    assert lm["eyes"] and fi.has_custom() and fi.custom_paths()[0].parent == faces_dir
    g = saved[-1]["gui"]
    assert g["face_image"] == "custom" and g["face_prompted"] is True and g["face_image_path"] == str(src)
    assert cfg["gui"]["face_image"] == "custom"
    assert not fr.should_prompt(cfg)                       # never asks again
    cfg2: dict = {"gui": {}}
    fr.skip(cfg2, saved.append)
    assert saved[-1] == {"gui": {"face_prompted": True}} and cfg2["gui"]["face_prompted"]


def test_color_mode_auto(faces_dir, tmp_path):
    assert fi.color_mode({"gui": {}}) == "hologram"                          # built-in face: tinted
    fi.import_face(_img(tmp_path / "me.png"), detector=fi.detect_heuristic)
    assert fi.color_mode({"gui": {"face_image": "custom"}}) == "original"    # own picture: as-is
    assert fi.color_mode({"gui": {"face_image": "custom", "face_colors": "hologram"}}) == "hologram"
    assert fi.color_mode({"gui": {"face_colors": "original"}}) == "original"


def test_dialog_close_counts_as_skip(faces_dir, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    saved = []
    cfg: dict = {"gui": {"face_style": "image"}}
    dlg = fr.FirstRunFaceDialog(cfg, saved.append)
    got = []
    dlg.skipped.connect(lambda: got.append("skip"))
    dlg.show()
    dlg.close()
    app.processEvents()
    assert got == ["skip"] and saved == [{"gui": {"face_prompted": True}}]
