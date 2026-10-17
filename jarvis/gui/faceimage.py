"""«Своё лицо»: any PNG/JPG portrait animated in WebGL (jarvis/gui/web/face.html).

The picked image is copied ONLY into the local app folder (``<app>/faces``, never synced,
never uploaded) and face landmarks are detected once:

1. mediapipe FaceLandmarker (optional ``requirements-face.txt``); stylised/glowing faces are
   detected after grayscale + autocontrast + padding,
2. OpenCV Haar cascades (if ``cv2`` is installed),
3. a proportion heuristic on the bright face region (``needs_calibration``) -> the user drags
   the markers in the calibration dialog.

Landmarks are stored next to the image as JSON, normalised to the image (0..1, y down)::

    {"eyes": [[x, y, hw, hh], [x, y, hw, hh]], "mouth": [x, y, hw, hh, curve], "chin": y,
     "source": "mediapipe|opencv|heuristic|manual", "version": 1}
"""
from __future__ import annotations

import json
import logging
import shutil
import urllib.request
from pathlib import Path

from ..config import PROJECT_ROOT

log = logging.getLogger(__name__)

WEB_FACES = Path(__file__).resolve().parent / "web" / "faces"
DEFAULT_IMAGE = WEB_FACES / "default.jpg"
DEFAULT_LM = WEB_FACES / "default.json"
MAX_SIDE = 1024
LANDMARKER_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/"
                  "float16/1/face_landmarker.task")
ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def user_dir() -> Path:
    d = PROJECT_ROOT / "faces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def custom_paths() -> tuple[Path, Path]:
    d = user_dir()
    return d / "custom.png", d / "custom.json"


# ------------------------------------------------------------------ landmark helpers
def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def normalize(lm: dict) -> dict:
    """Validate/clamp a landmark dict (raises ValueError when unusable)."""
    try:
        eyes = [[_clamp(v) for v in e[:4]] for e in lm["eyes"][:2]]
        m = [_clamp(v) for v in lm["mouth"][:4]]
        curve = float(lm["mouth"][4]) if len(lm["mouth"]) > 4 else 0.0
        chin = _clamp(lm["chin"])
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise ValueError(f"bad landmarks: {e}") from e
    if len(eyes) != 2 or len(m) != 4:
        raise ValueError("bad landmarks: need 2 eyes and a mouth")
    eyes.sort(key=lambda e: e[0])                       # left (in the image) first
    for ey in eyes:
        ey[2], ey[3] = max(ey[2], 0.01), max(ey[3], 0.006)
    m[2], m[3] = max(m[2], 0.02), max(m[3], 0.005)
    if chin <= m[1]:
        chin = min(1.0, m[1] + 4 * m[3] + 0.05)
    eyes = [[round(v, 4) for v in e] for e in eyes]
    out = {"eyes": eyes, "mouth": [round(v, 4) for v in m + [max(-0.05, min(0.05, curve))]], "chin": round(chin, 4),
           "source": str(lm.get("source", "manual")), "version": 1}
    if isinstance(lm.get("hue"), (int, float)):          # dominant hue of the picture (optional)
        out["hue"] = round(float(lm["hue"]) % 1.0, 4)
    return out


def from_points(eye_l, eye_r, mouth_l, mouth_r, chin, source: str = "manual") -> dict:
    """Landmarks from 5 dragged points (normalised x, y): eye centres, mouth corners, chin."""
    d = abs(eye_r[0] - eye_l[0]) or 0.2
    mw = abs(mouth_r[0] - mouth_l[0]) / 2 or d * 0.35
    my = (mouth_l[1] + mouth_r[1]) / 2
    return normalize({"eyes": [[eye_l[0], eye_l[1], d * 0.24, d * 0.09], [eye_r[0], eye_r[1], d * 0.24, d * 0.09]],
                      "mouth": [(mouth_l[0] + mouth_r[0]) / 2, my, mw, mw * 0.16, 0.0],
                      "chin": chin[1], "source": source})


def to_points(lm: dict) -> dict:
    m = lm["mouth"]
    return {"eye_l": lm["eyes"][0][:2], "eye_r": lm["eyes"][1][:2], "mouth_l": [m[0] - m[2], m[1]],
            "mouth_r": [m[0] + m[2], m[1]], "chin": [m[0], lm["chin"]]}


# ------------------------------------------------------------------ detectors
def _landmarker_model() -> Path | None:
    p = PROJECT_ROOT / "models" / "face_landmarker.task"
    if p.is_file():
        return p
    try:                       # one-time download (Apache-2.0 model from Google), only when the user imports a face
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        urllib.request.urlretrieve(LANDMARKER_URL, tmp)
        tmp.replace(p)
        return p
    except Exception as e:
        log.info("face_landmarker model unavailable: %s", e)
        return None


def _variants(img):
    from PIL import Image, ImageOps
    gray = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=2)
    for f, im in ((1.0, img), (1.6, gray.convert("RGB")), (1.6, img.split()[2].convert("RGB")),
                  (1.6, ImageOps.equalize(ImageOps.grayscale(img)).convert("RGB"))):
        if f == 1.0:
            yield im, 0, 0, 1.0
            continue
        W, H = im.size
        c = Image.new("RGB", (int(W * f), int(H * f)))
        ox, oy = (c.width - W) // 2, (c.height - H) // 2
        c.paste(im, (ox, oy))
        yield c, ox, oy, f


def detect_mediapipe(img) -> dict | None:
    try:
        import mediapipe as mp
        import numpy as np
        from mediapipe.tasks.python import BaseOptions, vision
    except Exception:
        return None
    model = _landmarker_model()
    if model is None:
        return None
    opt = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(model)), num_faces=1,
                                       min_face_detection_confidence=0.1, min_face_presence_confidence=0.1)
    det = vision.FaceLandmarker.create_from_options(opt)
    try:
        W, H = img.size
        for im, ox, oy, _f in _variants(img):
            r = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(np.asarray(im))))
            if not r.face_landmarks:
                continue
            pts = [((p.x * im.width - ox) / W, (p.y * im.height - oy) / H) for p in r.face_landmarks[0]]

            def eye(o, i, t, b, pts=pts):
                cx, cy = (pts[o][0] + pts[i][0]) / 2, (pts[t][1] + pts[b][1] + pts[o][1] + pts[i][1]) / 4
                hw = abs(pts[o][0] - pts[i][0]) / 2
                return [cx, cy, hw * 1.1, max(abs(pts[b][1] - pts[t][1]) / 2 * 1.3, hw * 0.3)]
            ml, mr, top, bot = pts[61], pts[291], pts[13], pts[14]
            my = (top[1] + bot[1]) / 2
            hh = max(abs(pts[17][1] - pts[0][1]) / 4, 0.004)
            return normalize({"eyes": [eye(33, 133, 159, 145), eye(362, 263, 386, 374)],
                              "mouth": [(ml[0] + mr[0]) / 2, my, abs(mr[0] - ml[0]) / 2, hh,
                                        (ml[1] + mr[1]) / 2 - my],
                              "chin": pts[152][1], "source": "mediapipe"})
        return None
    finally:
        try:
            det.close()
        except Exception:
            pass


def detect_opencv(img) -> dict | None:
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    from PIL import ImageOps
    g = np.asarray(ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=2))
    W, H = img.size
    base = Path(getattr(getattr(cv2, "data", None), "haarcascades", "") or "")
    fx, ex = base / "haarcascade_frontalface_default.xml", base / "haarcascade_eye.xml"
    if not (fx.is_file() and ex.is_file()):           # some OpenCV wheels ship without cascades
        return None
    face_c, eye_c = cv2.CascadeClassifier(str(fx)), cv2.CascadeClassifier(str(ex))
    faces = face_c.detectMultiScale(g, 1.1, 4, minSize=(W // 6, H // 6))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    eyes = eye_c.detectMultiScale(g[y:y + h // 2 + h // 8, x:x + w], 1.1, 4)
    if len(eyes) >= 2:
        e = sorted(sorted(eyes, key=lambda e: -e[2] * e[3])[:2], key=lambda e: e[0])
        el = ((x + e[0][0] + e[0][2] / 2) / W, (y + e[0][1] + e[0][3] / 2) / H)
        er = ((x + e[1][0] + e[1][2] / 2) / W, (y + e[1][1] + e[1][3] / 2) / H)
    else:
        el, er = ((x + 0.3 * w) / W, (y + 0.4 * h) / H), ((x + 0.7 * w) / W, (y + 0.4 * h) / H)
    cx = (el[0] + er[0]) / 2
    my = (y + 0.78 * h) / H
    lm = from_points(el, er, (cx - 0.17 * w / W, my), (cx + 0.17 * w / W, my), (cx, (y + 1.02 * h) / H), "opencv")
    return lm


def detect_heuristic(img) -> dict:
    """Proportions on the bright (glowing) face region; always returns something."""
    import numpy as np
    from PIL import ImageFilter, ImageOps
    g = np.asarray(ImageOps.grayscale(img).filter(ImageFilter.GaussianBlur(4)), dtype=np.float32)
    W, H = img.size
    mask = g > max(25.0, float(g.max()) * 0.18)
    ys, xs = np.nonzero(mask)
    if len(xs) < 50:
        x0, x1, y0, y1 = 0.25 * W, 0.75 * W, 0.15 * H, 0.9 * H
    else:
        x0, x1 = np.percentile(xs, 2), np.percentile(xs, 98)
        y0, y1 = np.percentile(ys, 1), np.percentile(ys, 99)
    w, h = (x1 - x0) / W, (y1 - y0) / H
    cx, top = (x0 + x1) / 2 / W, y0 / H
    lm = from_points((cx - 0.2 * w, top + 0.46 * h), (cx + 0.2 * w, top + 0.46 * h),
                     (cx - 0.12 * w, top + 0.79 * h), (cx + 0.12 * w, top + 0.79 * h), (cx, top + 0.97 * h),
                     "heuristic")
    return lm


def detect(img) -> dict:
    for fn in (detect_mediapipe, detect_opencv):
        try:
            lm = fn(img)
        except Exception as e:
            log.warning("face detector %s failed: %s", fn.__name__, e)
            lm = None
        if lm:
            return lm
    return detect_heuristic(img)


# ------------------------------------------------------------------ import / load
def load_image(path: str | Path):
    from PIL import Image
    p = Path(path)
    if p.suffix.lower() not in ALLOWED:
        raise ValueError("Нужна картинка PNG, JPG, WebP или BMP")
    img: Image.Image = Image.open(p)
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        img = Image.alpha_composite(bg, rgba)
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


def import_face(path: str | Path, detector=detect) -> dict:
    """Copy the picture into <app>/faces/custom.png and store detected landmarks. Returns them."""
    img = load_image(path)
    dst, lmp = custom_paths()
    tmp = dst.with_suffix(".tmp.png")
    img.save(tmp, "PNG")
    lm = detector(img)
    tmp.replace(dst)
    save_landmarks(lm)
    log.info("custom face imported (%dx%d, landmarks: %s)", img.width, img.height, lm.get("source"))
    return lm


def save_landmarks(lm: dict, path: Path | None = None) -> dict:
    lm = normalize(lm)
    p = path or custom_paths()[1]
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(lm, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return lm


def remove_custom() -> None:
    for p in custom_paths():
        p.unlink(missing_ok=True)


def has_custom() -> bool:
    img, lm = custom_paths()
    return img.is_file() and lm.is_file()


def current(cfg: dict) -> tuple[str, Path, dict]:
    """(url, file, landmarks) of the face to show: the user's picture if chosen, else the bundled one."""
    use_custom = str(cfg.get("gui", {}).get("face_image", "default")) == "custom" and has_custom()
    if use_custom:
        img, lmp = custom_paths()
        try:
            lm = normalize(json.loads(lmp.read_text(encoding="utf-8")))
            return f"jarvis:///userface/custom.png?v={int(img.stat().st_mtime)}", img, lm
        except Exception as e:
            log.warning("custom face landmarks broken (%s) - using the default face", e)
    lm = normalize(json.loads(DEFAULT_LM.read_text(encoding="utf-8")))
    return "jarvis:///faces/default.jpg", DEFAULT_IMAGE, lm


def color_mode(cfg: dict) -> str:
    """'original' / 'hologram'. 'auto' (default): the user's own picture keeps its colours as-is,
    the built-in face is tinted to the hologram colour."""
    mode = str(cfg.get("gui", {}).get("face_colors", "auto"))
    if mode in ("original", "hologram"):
        return mode
    custom = str(cfg.get("gui", {}).get("face_image", "default")) == "custom" and has_custom()
    return "original" if custom else "hologram"


def copy_out(dst: str | Path) -> None:
    """Export the current custom picture (for backups)."""
    img, _ = custom_paths()
    if img.is_file():
        shutil.copy2(img, dst)
