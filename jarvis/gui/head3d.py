"""WebGL faces inside QtWebEngine with automatic fallback to the QPainter head.

Face styles (``gui.face_style``):

* ``image`` (default) - «Своё лицо»: a portrait animated in WebGL (``web/face.html``): the bundled
  original face or the user's own picture (``jarvis.gui.faceimage``),
* ``3d``  - Three.js hologram head on a 3D scan (``web/head.html``),
* ``2d``  - procedural QPainter head (``face.HologramFace``), no WebGL at all.

Pages are served from the custom ``jarvis://`` scheme: read-only, only files of ``jarvis/gui/web``
plus ``/userface/*`` from the local faces folder, no network.  When QtWebEngine is missing, WebGL
fails or the page does not report "ready" in time, the widget swaps itself for ``HologramFace``
- the public API is identical, so the window does not care.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QStackedLayout, QWidget

from .face import HologramFace

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
SCHEME = b"jarvis"
READY_TIMEOUT_MS = 20000
MIC_MIN_INTERVAL = 1 / 15          # throttle mic level updates to ~15 Hz
_MIME = {".js": "text/javascript", ".html": "text/html", ".glb": "model/gltf-binary", ".json": "application/json",
         ".txt": "text/plain", ".png": "image/png"}


def webengine_available() -> bool:
    try:
        import PySide6.QtWebEngineCore  # noqa: F401
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except Exception:
        return False
    return (WEB_DIR / "head.html").is_file()


def _confined(root: Path, rel: str) -> Path | None:
    root = root.resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None


def resolve_web_path(url_path: str, user_faces: Path | None = None) -> Path | None:
    """Map a jarvis:// URL path onto a file inside WEB_DIR or the user faces folder (None = outside/missing)."""
    rel = url_path.lstrip("/") or "face.html"
    if rel.startswith("userface/"):
        if user_faces is None:
            from .faceimage import user_dir
            user_faces = user_dir()
        return _confined(user_faces, rel[len("userface/"):])
    return _confined(WEB_DIR, rel)


def face_style(cfg: dict) -> str:
    st = str(cfg.get("gui", {}).get("face_style", "image")).lower()
    return st if st in ("image", "3d", "2d") else "image"


def face_signature(cfg: dict) -> tuple:
    """Changes when the face widget has to be rebuilt (style, picture, landmarks)."""
    from . import faceimage
    g = cfg.get("gui", {})
    img, lm = faceimage.custom_paths()
    stamp = tuple(int(p.stat().st_mtime_ns) if p.is_file() else 0 for p in (img, lm))
    return face_style(cfg), str(g.get("face_image", "default")), stamp


def want_3d(cfg: dict) -> bool:
    """True when a WebGL face (image or 3d) should be tried."""
    return face_style(cfg) in ("image", "3d") and webengine_available()


def prepare_app(cfg: dict) -> None:
    """Must run BEFORE QApplication is created (scheme registration + shared GL contexts)."""
    if not want_3d(cfg):
        return
    try:
        from PySide6.QtCore import QCoreApplication, Qt
        from PySide6.QtWebEngineCore import QWebEngineUrlScheme
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        if not QWebEngineUrlScheme.schemeByName(QByteArray(SCHEME)).name():
            s = QWebEngineUrlScheme(QByteArray(SCHEME))
            s.setSyntax(QWebEngineUrlScheme.Syntax.Path)
            F = QWebEngineUrlScheme.Flag
            flags = F.SecureScheme | F.LocalScheme | F.LocalAccessAllowed | F.CorsEnabled
            if hasattr(F, "FetchApiAllowed"):      # Qt >= 6.6: GLTFLoader uses fetch()
                flags |= F.FetchApiAllowed
            s.setFlags(flags)
            QWebEngineUrlScheme.registerScheme(s)
    except Exception as e:
        log.warning("3D head disabled: %s", e)


def make_face(parent, cfg: dict):
    fps = min(30, int(cfg.get("gui", {}).get("fps") or 30))         # 1.0.8: capped at 30
    if want_3d(cfg):
        try:
            if face_style(cfg) == "3d":
                return WebFace(parent, fps=fps, page="head.html")
            from . import faceimage
            url, _file, lm = faceimage.current(cfg)
            w = WebFace(parent, fps=fps, page="face.html", face=(url, lm))
            w.set_color_mode(faceimage.color_mode(cfg))
            return w
        except Exception as e:
            log.warning("WebGL face failed, using 2D: %s", e)
    return HologramFace(parent, fps=fps)


class WebFace(QWidget):
    """Same API as HologramFace: state/env attributes, set_state, set_color, set_animation,
    set_mic_level, speak, speech_end (+ set_face / set_color_mode for the image face)."""

    def __init__(self, parent=None, fps: int = 30, page: str = "face.html", face: tuple | None = None):
        super().__init__(parent)
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEngineUrlSchemeHandler
        from PySide6.QtWebEngineWidgets import QWebEngineView

        from .faceimage import user_dir
        faces_dir = user_dir()

        class Handler(QWebEngineUrlSchemeHandler):
            def requestStarted(self, job):  # noqa: N802
                p = resolve_web_path(job.requestUrl().path(), faces_dir)
                if p is None:
                    job.fail(job.Error.UrlNotFound)
                    return
                buf = QBuffer(job)
                buf.setData(p.read_bytes())
                buf.open(QIODevice.ReadOnly)
                mime = _MIME.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                job.reply(QByteArray(mime.encode()), buf)

        self.setMinimumSize(320, 320)
        self.state = "loading"
        self._env: list = []
        self._env_end = 0.0
        self.base_fps = fps
        self._quality = "full"
        self._color = "#46dcff"
        self._ready = False
        self._failed = False
        self._fallback: HologramFace | None = None
        self._last_mic = 0.0
        self.page_name = page
        self._face = face
        self._color_mode = "hologram"
        self._lay = QStackedLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.view.page().setBackgroundColor(QColor(4, 7, 13))
        prof = self.view.page().profile()
        self._handler = Handler(self)
        if prof.urlSchemeHandler(QByteArray(SCHEME)) is None:
            prof.installUrlSchemeHandler(QByteArray(SCHEME), self._handler)
        st = self.view.settings()
        st.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        st.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        st.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
        self.view.titleChanged.connect(self._on_title)
        self.view.loadFinished.connect(self._on_loaded)
        self.view.page().renderProcessTerminated.connect(lambda *_: self._fail("render process terminated"))
        self._lay.addWidget(self.view)
        from PySide6.QtCore import QUrl, QUrlQuery
        url = QUrl(f"jarvis:///{page}")
        q = QUrlQuery()
        q.addQueryItem("state", "loading")
        if face is not None:                       # image face: picture + landmarks come with the URL
            q.addQueryItem("img", face[0])
            q.addQueryItem("lm", json.dumps(face[1]))
        url.setQuery(q)
        self.view.setUrl(url)
        QTimer.singleShot(READY_TIMEOUT_MS, lambda: None if self._ready else self._fail("timeout"))

    # ------------------------------------------------------------ lifecycle
    def _on_title(self, title: str) -> None:
        if title == "ready" and not self._ready:
            self._ready = True
            log.info("WebGL face ready (%s)", self.page_name)
            self._js(f"window.jarvis.setColor({json.dumps(self._color)})")
            if self.page_name == "face.html":
                self._js(f"window.jarvis.setColorMode({json.dumps(self._color_mode)})")
            self._js(f"window.jarvis.setMaxFps({int(min(30, self.base_fps))})")
            self._js(f"window.jarvis.setQuality({json.dumps(self._quality)})")
            self._js(f"window.jarvis.setPaused({json.dumps(not self.isVisible())})")
            self._js(f"window.jarvis.setState({json.dumps(self.state)})")
        elif title == "error":
            self._fail("page error")

    def _on_loaded(self, ok: bool) -> None:
        if not ok:
            self._fail("load failed")

    def _fail(self, why: str) -> None:
        if self._failed:
            return
        self._failed = True
        log.warning("WebGL face unavailable (%s) - switching to 2D hologram", why)
        fb = HologramFace(self, fps=self.base_fps)
        fb.set_color(self._color)
        fb.set_animation(self._quality)
        fb.set_state(self.state)
        self._fallback = fb
        self._lay.addWidget(fb)
        self._lay.setCurrentWidget(fb)
        from PySide6.QtCore import QUrl
        self.view.setUrl(QUrl("about:blank"))

    @property
    def using_fallback(self) -> bool:
        return self._fallback is not None

    def _js(self, code: str) -> None:
        if self._ready and not self._failed:
            self.view.page().runJavaScript(code)

    # ------------------------------------------------------------ API
    @property
    def env(self) -> list:
        if self._fallback is not None:
            return self._fallback.env
        if self._env and time.monotonic() > self._env_end:
            self._env = []
        return self._env

    @env.setter
    def env(self, value: list) -> None:
        self._env = value or []

    def set_color(self, color) -> None:
        c = QColor(color)
        if not c.isValid():
            return
        self._color = c.name()
        if self._fallback is not None:
            self._fallback.set_color(c)
        self._js(f"window.jarvis.setColor({json.dumps(self._color)})")

    # 1.0.8: no rendering while the window is hidden (tray) or minimized
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        if self._ready:
            self._js("window.jarvis.setPaused(false)")

    def hideEvent(self, e):  # noqa: N802
        super().hideEvent(e)
        if self._ready:
            self._js("window.jarvis.setPaused(true)")

    def set_animation(self, level: str) -> None:
        self._quality = level if level in ("full", "lite", "off") else "full"
        if self._fallback is not None:
            self._fallback.set_animation(self._quality)
        self._js(f"window.jarvis.setQuality({json.dumps(self._quality)})")

    def set_state(self, state: str) -> None:
        self.state = state
        if self._fallback is not None:
            self._fallback.set_state(state)
        self._js(f"window.jarvis.setState({json.dumps(state)})")

    def set_mic_level(self, level: float) -> None:
        level = max(0.0, min(1.0, float(level)))
        if self._fallback is not None:
            self._fallback.set_mic_level(level)
            return
        now = time.monotonic()
        if now - self._last_mic >= MIC_MIN_INTERVAL:
            self._last_mic = now
            self._js(f"window.jarvis.setMic({level:.3f})")

    def speak(self, env: list, wid: list, dt: float = 0.02, start: float | None = None) -> None:
        self.state = "speaking"
        if self._fallback is not None:
            self._fallback.speak(env, wid, dt, start)
            return
        self._env = [round(float(v), 3) for v in (env or [])]
        w = [round(float(v), 3) for v in (wid or [])]
        self._env_end = time.monotonic() + len(self._env) * dt
        self._js(f"window.jarvis.speak({json.dumps(self._env)}, {float(dt)}, {json.dumps(w)})")

    def speech_end(self) -> None:
        self._env = []
        if self._fallback is not None:
            self._fallback.speech_end()
        self._js("window.jarvis.stopSpeech()")

    def set_face(self, url: str, landmarks: dict) -> None:
        """Image face: show another picture (live, no reload)."""
        self._face = (url, landmarks)
        self._js(f"window.jarvis.setFace({json.dumps(url)}, {json.dumps(landmarks)})")

    def set_color_mode(self, mode: str) -> None:
        """Image face: 'hologram' (recolour to the hologram colour) or 'original'."""
        self._color_mode = "original" if mode == "original" else "hologram"
        self._js(f"window.jarvis.setColorMode({json.dumps(self._color_mode)})")


Head3D = WebFace          # backwards-compatible name
