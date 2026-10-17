"""3D head: bundled assets, licence notes, safe jarvis:// path mapping, 2D fallback selection."""
import re

import pytest

from jarvis.gui import head3d


def test_web_assets_and_licences():
    w = head3d.WEB_DIR
    for rel in ("head.html", "head.js", "face.html", "face.js", "faces/default.jpg", "faces/default.json",
                "models/LeePerrySmith.glb", "models/LICENSE.txt",
                "vendor/three/three.module.min.js", "vendor/three/LICENSE",
                "vendor/three/jsm/loaders/GLTFLoader.js", "vendor/three/jsm/postprocessing/UnrealBloomPass.js"):
        assert (w / rel).is_file(), rel
    lic = (w / "models/LICENSE.txt").read_text(encoding="utf-8")
    assert "Lee Perry-Smith" in lic and "Attribution 3.0" in lic
    readme = (head3d.WEB_DIR.parents[2] / "README.md").read_text(encoding="utf-8")
    assert "Lee Perry-Smith" in readme and "CC BY 3.0" in readme


def test_page_has_no_remote_urls():
    for name in ("head.html", "head.js", "face.html", "face.js"):
        txt = (head3d.WEB_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://", txt.replace("http://www.triplegangers.com", "")), name


def test_eyes_are_symmetric_around_face_midline():
    js = (head3d.WEB_DIR / "head.js").read_text(encoding="utf-8")
    l = [float(v) for v in re.search(r"EYE_L = vec3\(([^)]*)\)", js).group(1).split(",")]
    r = [float(v) for v in re.search(r"EYE_R = vec3\(([^)]*)\)", js).group(1).split(",")]
    mid = (l[0] + r[0]) / 2
    assert -0.2 < mid < 0.0            # scan midline (nose ridge) is at x ~ -0.11
    assert 0.5 < r[0] - l[0] < 1.5 and l[1] == r[1] and l[2] == r[2]


def test_resolve_web_path_is_confined():
    assert head3d.resolve_web_path("/head.html") == (head3d.WEB_DIR / "head.html").resolve()
    assert head3d.resolve_web_path("/") is not None
    assert head3d.resolve_web_path("/../head3d.py") is None
    assert head3d.resolve_web_path("/../../config.py") is None
    assert head3d.resolve_web_path("/nope.js") is None


def test_userface_route_is_confined(tmp_path):
    (tmp_path / "custom.png").write_bytes(b"x")
    (tmp_path.parent / "secret.txt").write_text("no")
    assert head3d.resolve_web_path("/userface/custom.png", tmp_path) == (tmp_path / "custom.png").resolve()
    assert head3d.resolve_web_path("/userface/../secret.txt", tmp_path) is None
    assert head3d.resolve_web_path("/userface/nope.png", tmp_path) is None


def test_face_style_values():
    assert head3d.face_style({}) == "image"
    assert head3d.face_style({"gui": {"face_style": "3D"}}) == "3d"
    assert head3d.face_style({"gui": {"face_style": "weird"}}) == "image"


def test_make_face_falls_back_to_2d(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from jarvis.gui.face import HologramFace
    app = QApplication.instance() or QApplication([])  # noqa: F841
    f = head3d.make_face(None, {"gui": {"face_style": "2d", "fps": 30}})
    assert isinstance(f, HologramFace)
    monkeypatch.setattr(head3d, "webengine_available", lambda: False)
    f2 = head3d.make_face(None, {"gui": {"face_style": "image"}})
    assert isinstance(f2, HologramFace)
    for x in (f, f2):
        x.set_color("#ff8800"); x.set_state("speaking"); x.speak([0.5] * 5, [], 0.02); x.speech_end()
        x.deleteLater()
