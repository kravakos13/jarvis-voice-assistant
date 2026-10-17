"""One-time "choose your face picture" offer on the first start.

Shown only when the «Своё лицо» style is active, no custom picture exists and the user has not answered yet
(gui.face_prompted). The picture is copied into the local faces folder only - never synced or uploaded."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from . import faceimage

log = logging.getLogger("jarvis.gui.firstrun")

PICK_FILTER = "Картинки (*.png *.jpg *.jpeg *.webp *.bmp)"


def should_prompt(cfg: dict) -> bool:
    if os.environ.get("JARVIS_NO_FIRSTRUN"):
        return False
    g = cfg.get("gui", {})
    if g.get("face_prompted"):
        return False
    if str(g.get("face_style", "image")) != "image":
        return False
    return not faceimage.has_custom()


def accept_image(cfg: dict, path: str | Path, save: Callable[[dict], None], detector=None) -> dict:
    """Copy + detect the picture, remember the choice. Returns the landmarks (raises on a bad image)."""
    lm = faceimage.import_face(path) if detector is None else faceimage.import_face(path, detector=detector)
    changes = {"gui": {"face_image": "custom", "face_style": "image", "face_prompted": True,
                       "face_image_path": str(Path(path))}}
    save(changes)
    cfg.setdefault("gui", {}).update(changes["gui"])
    return lm


def skip(cfg: dict, save: Callable[[dict], None]) -> None:
    save({"gui": {"face_prompted": True}})
    cfg.setdefault("gui", {})["face_prompted"] = True


class FirstRunFaceDialog(QDialog):
    """Non-modal. Emits `chosen(landmarks)` after a picture was imported, `skipped()` otherwise."""
    chosen = Signal(dict)
    skipped = Signal()

    def __init__(self, cfg: dict, save: Callable[[dict], None], parent=None):
        super().__init__(parent)
        self.cfg, self._save = cfg, save
        self._answered = False
        self.setWindowTitle("Джарвис — лицо")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        t = QLabel("Хочешь, чтобы Джарвис выглядел как твоя картинка?\n\n"
                   "Выбери изображение лица (PNG/JPG, лицо анфас). Оно скопируется только в папку "
                   "программы на этом ПК и никуда не отправляется. Поменять можно потом: "
                   "Настройки → Интерфейс.")
        t.setWordWrap(True)
        lay.addWidget(t)
        row = QHBoxLayout()
        self.pick_btn = QPushButton("Выбрать своё изображение лица…")
        self.skip_btn = QPushButton("Использовать встроенное лицо")
        self.pick_btn.clicked.connect(self._pick)
        self.skip_btn.clicked.connect(self._skip)
        row.addWidget(self.pick_btn)
        row.addWidget(self.skip_btn)
        lay.addLayout(row)

    def _pick(self, path: Optional[str] = None) -> None:
        p = path or QFileDialog.getOpenFileName(self, "Изображение лица", str(Path.home()), PICK_FILTER)[0]
        if not p:
            return
        try:
            lm = accept_image(self.cfg, p, self._save)
        except Exception as e:
            log.warning("first-run face import failed: %s", e)
            QMessageBox.warning(self, "Своё лицо", f"Не получилось открыть картинку: {e}")
            return
        self._answered = True
        self.chosen.emit(lm)
        self.close()

    def _skip(self) -> None:
        self._answered = True
        skip(self.cfg, self._save)
        self.skipped.emit()
        self.close()

    def closeEvent(self, e):  # closing the window = "use the built-in face" (don't nag every start)
        if not self._answered:
            self._answered = True
            skip(self.cfg, self._save)
            self.skipped.emit()
        super().closeEvent(e)
