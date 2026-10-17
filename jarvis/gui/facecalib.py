"""Calibration dialog for «Своё лицо»: drag the markers onto the eyes, mouth corners and chin."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGraphicsEllipseItem, QGraphicsPixmapItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView, QLabel, QVBoxLayout)

from . import faceimage

MARKERS = (("eye_l", "глаз"), ("eye_r", "глаз"), ("mouth_l", "рот"), ("mouth_r", "рот"), ("chin", "подбородок"))


class _Marker(QGraphicsEllipseItem):
    def __init__(self, x: float, y: float, r: float, text: str):
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.setPos(x, y)
        self.setPen(QPen(QColor(255, 210, 60), max(1.5, r / 4)))
        self.setBrush(QBrush(QColor(255, 210, 60, 70)))
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        label = QGraphicsSimpleTextItem(text, self)
        label.setBrush(QBrush(QColor(255, 230, 140)))
        f = label.font()
        f.setPointSizeF(max(8.0, r * 0.9))
        label.setFont(f)
        label.setPos(r * 1.2, -r * 1.2)


class FaceCalibDialog(QDialog):
    def __init__(self, image: Path, landmarks: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Калибровка лица")
        self.landmarks = landmarks
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Перетащи жёлтые метки на центры глаз, уголки рта и низ подбородка. "
                           "По ним анимируются моргание и рот."))
        pm = QPixmap(str(image))
        self.size_ = (max(1, pm.width()), max(1, pm.height()))
        self.scene = QGraphicsScene(self)
        self.scene.addItem(QGraphicsPixmapItem(pm))
        self.scene.setSceneRect(QRectF(0, 0, *self.size_))
        r = max(5.0, min(self.size_) / 60)
        pts = faceimage.to_points(landmarks)
        self.markers = {}
        for key, text in MARKERS:
            x, y = pts[key]
            m = _Marker(x * self.size_[0], y * self.size_[1], r, text)
            self.scene.addItem(m)
            self.markers[key] = m
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setMinimumSize(520, 520)
        v.addWidget(self.view, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def showEvent(self, ev):  # noqa: N802
        super().showEvent(ev)
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def points(self) -> dict:
        W, H = self.size_
        out = {}
        for k, m in self.markers.items():
            p: QPointF = m.pos()
            out[k] = (min(1.0, max(0.0, p.x() / W)), min(1.0, max(0.0, p.y() / H)))
        return out

    def result_landmarks(self) -> dict:
        p = self.points()
        lm = faceimage.from_points(p["eye_l"], p["eye_r"], p["mouth_l"], p["mouth_r"], p["chin"], "manual")
        if "hue" in self.landmarks:
            lm["hue"] = self.landmarks["hue"]
        return lm
