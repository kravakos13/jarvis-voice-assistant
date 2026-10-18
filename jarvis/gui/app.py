"""Main window (frameless, translucent, draggable), chat log, tray icon, hotkeys.

All heavy work runs in worker threads (jarvis.assistant); the UI only receives
events through a Qt signal, so it never freezes.
"""
from __future__ import annotations

import html
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSystemTrayIcon, QTextBrowser, QVBoxLayout, QWidget)

from .. import platform_utils as pu
from ..config import PROJECT_ROOT, save_local_settings
from .head3d import face_signature, make_face, prepare_app

log = logging.getLogger(__name__)

STYLE = """
QWidget#root { background: transparent; }
QWidget#panel { background: rgba(4, 14, 30, 225); border: 1px solid rgba(70, 220, 255, 90); border-radius: 18px; }
QLabel#title { color: rgb(120, 230, 255); font: 600 13px 'Segoe UI'; letter-spacing: 4px; }
QPushButton { color: rgb(150, 235, 255); background: rgba(20, 70, 110, 120); border: 1px solid rgba(70, 220, 255, 110);
              border-radius: 10px; padding: 6px 10px; font: 12px 'Segoe UI'; }
QPushButton:hover { background: rgba(40, 120, 170, 160); }
QPushButton:checked { background: rgba(200, 60, 60, 150); border-color: rgba(255, 120, 120, 160); }
QPushButton#flat { background: transparent; border: none; font-size: 15px; padding: 2px 6px; }
QLineEdit { color: white; background: rgba(10, 40, 70, 170); border: 1px solid rgba(70, 220, 255, 100);
            border-radius: 10px; padding: 7px; font: 13px 'Segoe UI'; selection-background-color: rgb(30,120,200); }
QTextBrowser { color: rgb(215, 245, 255); background: rgba(0, 20, 40, 120); border: 1px solid rgba(70,220,255,50);
               border-radius: 12px; padding: 6px; font: 13px 'Segoe UI'; }
QProgressBar { background: rgba(10,40,70,150); border: none; border-radius: 3px; height: 6px; color: transparent; }
QProgressBar::chunk { background: rgb(70, 220, 255); border-radius: 3px; }
QMenu { background: rgb(8, 24, 44); color: rgb(190, 240, 255); border: 1px solid rgb(40, 120, 170); }
QMenu::item:selected { background: rgb(30, 90, 140); }
"""


def make_icon(muted: bool = False) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(255, 110, 110) if muted else QColor(70, 220, 255)
    for w, a in ((12, 40), (7, 90), (3, 255)):
        c.setAlpha(a)
        p.setPen(QPen(c, w))
        p.drawEllipse(10, 10, 44, 44)
    c.setAlpha(255)
    p.setBrush(c)
    p.setPen(Qt.NoPen)
    p.drawEllipse(26, 26, 12, 12)
    p.end()
    return QIcon(pm)


class Bridge(QObject):
    event = Signal(str, dict)


class MainWindow(QWidget):
    def __init__(self, cfg: dict, controller: "Controller"):
        super().__init__()
        self.cfg = cfg
        self.ctl = controller
        self.setObjectName("root")
        self.setWindowTitle(cfg["assistant"].get("name", "Джарвис"))
        flags = Qt.FramelessWindowHint | Qt.Window
        if cfg["gui"].get("always_on_top"):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.resize(460, 780)
        self.setStyleSheet(STYLE)
        self._drag: Optional[QPoint] = None
        self._partial = ""

        panel = QWidget(self)
        panel.setObjectName("panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addWidget(panel)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 10, 14, 14)
        lay.setSpacing(8)

        top = QHBoxLayout()
        self.title = QLabel(cfg["assistant"].get("name", "Джарвис").upper())
        self.title.setObjectName("title")
        top.addWidget(self.title)
        top.addStretch(1)
        self.btn_menu = QPushButton("⋯")
        self.btn_menu.setObjectName("flat")
        self.btn_menu.clicked.connect(lambda: self.ctl.tray_menu.exec(self.btn_menu.mapToGlobal(QPoint(0, 24))))
        btn_min = QPushButton("—")
        btn_min.setObjectName("flat")
        btn_min.clicked.connect(self.hide)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("flat")
        btn_close.clicked.connect(self.close)
        for b in (self.btn_menu, btn_min, btn_close):
            top.addWidget(b)
        lay.addLayout(top)

        status = QHBoxLayout()
        self.lbl_profile = QLabel("● рабочий режим")
        self.lbl_alert = QLabel("")
        self.lbl_stats = QLabel("")
        for lb in (self.lbl_profile, self.lbl_alert, self.lbl_stats):
            lb.setStyleSheet("color: rgb(110,190,220); font: 11px 'Segoe UI';")
        status.addWidget(self.lbl_profile)
        status.addWidget(self.lbl_alert)
        self.lbl_mic = QLabel("")
        self.lbl_mic.setStyleSheet("color: rgb(255,120,110); font: bold 11px 'Segoe UI';")
        status.addWidget(self.lbl_mic)
        status.addStretch(1)
        status.addWidget(self.lbl_stats)
        lay.addLayout(status)

        self._face_layout = lay
        self.face = make_face(self, cfg)
        self.face.setMinimumHeight(380)
        self.face_sig = face_signature(cfg)
        lay.addWidget(self.face, 3)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: rgb(120,200,230); font: 11px 'Segoe UI';")
        self.progress_label.hide()
        lay.addWidget(self.progress_label)
        lay.addWidget(self.progress)

        self.chat = QTextBrowser()
        self.chat.setOpenLinks(False)
        self.chat.anchorClicked.connect(QDesktopServices.openUrl)
        self.chat.setMinimumHeight(150)
        lay.addWidget(self.chat, 2)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Напиши или скажи «%s»..." % cfg["assistant"].get("name", "Джарвис"))
        self.input.returnPressed.connect(self._send)
        self.btn_talk = QPushButton("🎤")
        self.btn_talk.setToolTip("Удерживай, чтобы говорить")
        self.btn_talk.pressed.connect(self.ctl.ptt_press)
        self.btn_talk.released.connect(self.ctl.ptt_release)
        self.btn_mute = QPushButton("🔇")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setToolTip("Выключить микрофон")
        self.btn_mute.toggled.connect(self.ctl.set_muted)
        send = QPushButton("➤")
        send.clicked.connect(self._send)
        self.btn_lumo = QPushButton("☁")
        self.btn_lumo.setToolTip("Передать Люмо (облачный ассистент)")
        self.btn_lumo.clicked.connect(self.ctl.lumo_dialog)
        row.addWidget(self.input, 1)
        for b in (send, self.btn_lumo, self.btn_talk, self.btn_mute):
            b.setFixedWidth(44)
            row.addWidget(b)
        lay.addLayout(row)

    # ------------------------------------------------------------ chat

    def rebuild_face(self, cfg: dict) -> None:
        """Swap the face widget (style / picture changed) keeping its place, colour and state."""
        old = self.face
        state = getattr(old, "state", "idle")
        new = make_face(self, cfg)
        new.setMinimumHeight(380)
        i = self._face_layout.indexOf(old)
        self._face_layout.insertWidget(i if i >= 0 else 2, new, 3)
        self._face_layout.removeWidget(old)
        old.hide()
        old.deleteLater()
        self.face = new
        self.face_sig = face_signature(cfg)
        new.set_color(cfg["gui"].get("color", "#46dcff"))
        new.set_animation(cfg["gui"].get("animations", "full"))
        new.set_state("idle" if state == "speaking" else state)
    def add_message(self, who: str, text: str, links: Optional[list] = None) -> None:
        color = "rgb(120,230,255)" if who != "Ты" else "rgb(255,210,140)"
        body = html.escape(text).replace("\n", "<br>")
        s = f'<p style="margin:4px 0"><b style="color:{color}">{html.escape(who)}:</b> {body}</p>'
        if links:
            s += "<p style='margin:0 0 6px 12px; font-size:11px'>" + " · ".join(
                f'<a style="color:rgb(90,190,255)" href="{html.escape(u)}">{html.escape(t[:50] or u)}</a>'
                for t, u in links) + "</p>"
        self.chat.append(s)
        self.chat.verticalScrollBar().setValue(self.chat.verticalScrollBar().maximum())

    def _send(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.ctl.submit_text(text)

    # ------------------------------------------------------------ window behaviour
    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._drag = None

    def closeEvent(self, e):  # noqa: N802
        if self.cfg["gui"].get("close_to_tray", True) and not self.ctl.quitting and QSystemTrayIcon.isSystemTrayAvailable():
            e.ignore()
            self.hide()
            self.ctl.tray_message("Я тут, в трее. Слушаю дальше.")
        else:
            e.accept()

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.ctl.set_dropped_files(files)


class Controller(QObject):
    """Owns the assistant, window and tray; translates assistant events into UI updates."""

    def __init__(self, app: QApplication, cfg: dict, demo: bool = False, speak: bool = True):
        super().__init__()
        self.app = app
        self.cfg = cfg
        self.demo = demo
        self.speak = speak
        self.quitting = False
        self.assistant = None
        self.dropped_files: list[str] = []
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.window = MainWindow(cfg, self)
        self._build_tray()
        self._hotkey_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ tray
    def _build_tray(self) -> None:
        name = self.cfg["assistant"].get("name", "Джарвис")
        self.tray = QSystemTrayIcon(make_icon(), self.app)
        self.tray.setToolTip(f"{name} — голосовой ассистент")
        m = QMenu()
        m.addAction("Показать / скрыть", self.toggle_window)
        self.act_mute = QAction("Микрофон выключен", m, checkable=True)
        self.act_mute.toggled.connect(self.set_muted)
        m.addAction(self.act_mute)
        modes = QActionGroup(m)
        self.act_wake = QAction(f"Режим: по имени «{name}»", m, checkable=True)
        self.act_ptt = QAction("Режим: push-to-talk (клавиша)", m, checkable=True)
        for a in (self.act_wake, self.act_ptt):
            modes.addAction(a)
            m.addAction(a)
        (self.act_ptt if self.cfg["assistant"].get("mode") == "ptt" else self.act_wake).setChecked(True)
        self.act_wake.triggered.connect(lambda: self.set_mode("wake"))
        self.act_ptt.triggered.connect(lambda: self.set_mode("ptt"))
        m.addSeparator()
        prof = m.addMenu("Режим работы")
        pg = QActionGroup(prof)
        self.act_profiles = {}
        for key, title in (("work", "Рабочий"), ("game", "Игровой"), ("night", "Ночной")):
            a = QAction(title, prof, checkable=True)
            a.triggered.connect(lambda _=False, k=key: self.set_profile(k))
            pg.addAction(a)
            prof.addAction(a)
            self.act_profiles[key] = a
        self.act_profiles["work"].setChecked(True)
        m.addSeparator()
        self.act_autostart = QAction("Запускать вместе с Windows", m, checkable=True)
        self.act_autostart.setChecked(pu.autostart_enabled() if pu.IS_WIN else bool(self.cfg["autostart"].get("enabled")))
        self.act_autostart.toggled.connect(self.set_autostart)
        m.addAction(self.act_autostart)
        self.act_top = QAction("Поверх всех окон", m, checkable=True)
        self.act_top.setChecked(bool(self.cfg["gui"].get("always_on_top")))
        self.act_top.toggled.connect(self.set_on_top)
        m.addAction(self.act_top)
        m.addSeparator()
        m.addAction("Открыть папку Jarvis", lambda: pu.open_path(pu.data_dir("")))
        m.addAction("Настройки...", self.open_settings)
        m.addAction("Остановить (замолчать)", self.stop_now)
        m.addAction("Перезапустить прослушку", self.restart_listening)
        m.addAction("Синхронизировать", self.sync_now)
        m.addAction("Самодиагностика", self.run_diagnostics)
        m.addAction("Собрать диагностику (архив)", self.collect_diagnostics)
        m.addAction("Проверить обновления", lambda: self.check_updates(manual=True))
        m.addAction("Инструкция", self.open_manual)
        m.addSeparator()
        m.addAction("Выход", self.quit)
        self.tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self.toggle_window() if r == QSystemTrayIcon.Trigger else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def tray_message(self, text: str) -> None:
        if self.tray.isVisible():
            self.tray.showMessage(self.cfg["assistant"].get("name", "Джарвис"), text, make_icon(), 3000)

    def toggle_window(self) -> None:
        if self.window.isVisible() and not self.window.isMinimized():
            self.window.hide()
        else:
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()

    # ------------------------------------------------------------ actions
    def set_muted(self, muted: bool) -> None:
        for w in (self.act_mute, self.window.btn_mute):
            w.blockSignals(True)
            w.setChecked(muted)
            w.blockSignals(False)
        self.tray.setIcon(make_icon(muted))
        name = self.cfg["assistant"].get("name", "Джарвис")
        self.tray.setToolTip(f"{name} — микрофон на паузе" if muted else f"{name} — голосовой ассистент")
        self.window.lbl_mic.setText("🔇 микрофон выкл." if muted else "")
        if self.assistant and self.assistant.mic_muted != muted:
            threading.Thread(target=self.assistant.set_mic_muted, args=(muted,), daemon=True).start()
        self.window.face.set_state("muted" if muted else self._rest_state())

    def _rest_state(self) -> str:
        """What the face shows between requests: asleep (waits for the name), PTT idle, or mic off."""
        if self.assistant and self.assistant.mic_muted:
            return "muted"
        return "sleeping" if self.cfg["assistant"].get("mode", "wake") == "wake" else "idle"

    TRAY_STATES = {"sleeping": "спит — скажи «{name}»", "listening": "слушает", "thinking": "думает",
                   "speaking": "говорит", "muted": "микрофон на паузе", "idle": "ждёт (push-to-talk)",
                   "loading": "загружается"}

    def _tray_state(self, st: str) -> None:
        name = self.cfg["assistant"].get("name", "Джарвис")
        label = self.TRAY_STATES.get(st)
        if label and getattr(self, "tray", None) is not None and getattr(self, "_tray_st", None) != st:
            self._tray_st = st
            self.tray.setToolTip(f"{name} — {label.format(name=name)}")

    def toggle_mic(self) -> None:
        self.set_muted(not (self.assistant.mic_muted if self.assistant else self.act_mute.isChecked()))

    def set_mode(self, mode: str) -> None:
        self.cfg["assistant"]["mode"] = mode
        if self.assistant:
            self.assistant.mode = mode
        save_local_settings({"assistant": {"mode": mode}})

    def set_profile(self, name: str) -> None:
        if self.assistant:
            threading.Thread(target=lambda: self.assistant.set_profile(name), daemon=True).start()

    def set_autostart(self, on: bool) -> None:
        save_local_settings({"autostart": {"enabled": on}})
        if pu.IS_WIN and not pu.set_autostart(on, PROJECT_ROOT):
            QMessageBox.warning(self.window, "Автозагрузка", "Не получилось изменить автозагрузку.")

    def set_on_top(self, on: bool) -> None:
        save_local_settings({"gui": {"always_on_top": on}})
        self.window.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.window.show()

    def submit_text(self, text: str) -> None:
        if self.assistant:
            self.assistant.submit(text, "text")
        else:
            self.window.add_message("Система", "Ещё загружаюсь...")

    def ptt_press(self) -> None:
        if self.assistant:
            self.assistant.ptt_press()
            if self.assistant.mode != "ptt":
                self.assistant._ptt_override = True
                self.assistant.mode = "ptt"

    def ptt_release(self) -> None:
        if self.assistant:
            self.assistant.ptt_release()
            if getattr(self.assistant, "_ptt_override", False):
                self.assistant._ptt_override = False
                QTimer.singleShot(300, lambda: setattr(self.assistant, "mode", self.cfg["assistant"].get("mode", "wake")))

    def set_dropped_files(self, files: list[str]) -> None:
        self.dropped_files = files
        names = ", ".join(Path(f).name for f in files[:3])
        self.window.add_message("Система", f"Файлы приняты: {names}.")
        if self.assistant:
            self.assistant.router.ctx.extra["dropped_files"] = files

    def open_settings(self, tab: Optional[str] = None) -> None:
        from .settings import SettingsDialog
        memory = None
        if self.assistant and "memory" in self.assistant.router.by_name:
            memory = self.assistant.router.by_name["memory"].memory
        if getattr(self, "_settings_open", False):
            return
        self._settings_open = True
        try:
            dlg = SettingsDialog(self.cfg, memory, self.window, controller=self)
            if tab:
                dlg.select_tab(tab)
            if not dlg.exec():
                return
            if getattr(dlg, "changes", None):
                self.apply_settings(dlg.changes)
            if getattr(dlg, "_force_restart", False):
                self.restart()
                return
            if dlg.restart_needed:
                if QMessageBox.question(self.window, "Настройки", "Настройки сохранены. Часть изменений вступит в силу "
                                        "после перезапуска. Перезапустить сейчас?") == QMessageBox.Yes:
                    self.restart()
                    return
            self.window.add_message("Система", "Настройки сохранены и применены.")
        except Exception as e:
            log.exception("settings: %s", e)
            QMessageBox.critical(self.window, "Настройки", f"Ошибка окна настроек: {e}")
        finally:
            self._settings_open = False

    def lumo_dialog(self) -> None:
        """Explicit 'send to Lumo' from the GUI: text + optional screenshot/file, then confirm."""
        from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QPlainTextEdit
        from .. import lumo
        if not lumo.configured(self.cfg):
            from .. import google_auth as ga
            if ga.client_available() and not ga.connected() and lumo.method(self.cfg) is None and \
                    str(self.cfg.get("lumo", {}).get("method", "auto")) in ("auto", "gmail"):
                if QMessageBox.question(self.window, "Люмо", "Чтобы передавать задачи Люмо по почте, подключи Google "
                                        "(разрешение только на отправку писем и на файлы Джарвиса в Drive).\n\n"
                                        "Подключить Google сейчас?") == QMessageBox.StandardButton.Yes:
                    self.connect_google()
                return
            QMessageBox.information(self.window, "Люмо", lumo.not_configured_message(self.cfg))
            self.open_settings(tab="integrations")
            return
        dlg = QDialog(self.window)
        dlg.setWindowTitle("Передать Люмо")
        lay = QVBoxLayout(dlg)
        text = QPlainTextEdit(self.window.input.text())
        text.setPlaceholderText("Что передать Люмо…")
        cb = QCheckBox("Приложить снимок экрана")
        files: list = list(getattr(self, "dropped_files", [])[:1])
        lbl = QLabel(f"Файл: {Path(files[0]).name}" if files else "Файл: нет")
        pick = QPushButton(f"Прикрепить файл (до {lumo.max_total(self.cfg) // 1024 // 1024} МБ)…")

        def choose():
            p, _ = QFileDialog.getOpenFileName(dlg, "Файл для Люмо")
            if p:
                if Path(p).stat().st_size > lumo.max_total(self.cfg):
                    QMessageBox.warning(dlg, "Люмо", f"Файл больше {lumo.max_total(self.cfg) // 1024 // 1024} МБ.")
                    return
                files[:] = [p]
                lbl.setText(f"Файл: {Path(p).name}")
        pick.clicked.connect(choose)
        bb = QDialogButtonBox()
        bb.addButton("Отправить", QDialogButtonBox.AcceptRole)
        bb.addButton("Отмена", QDialogButtonBox.RejectRole)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        for w in (QLabel("Задача для Люмо:"), text, cb, lbl, pick, bb):
            lay.addWidget(w)
        dlg.resize(460, 300)
        if not dlg.exec():
            return
        task = text.toPlainText().strip()
        if not task and not files and not cb.isChecked():
            return
        hist = list(getattr(getattr(self.assistant, "llm", None), "history", []) or [])
        self.window.input.clear()
        self.window.add_message("Ты", f"→ Люмо: {task or '(вложения)'}")
        if cb.isChecked():
            self.window.hide()          # don't capture our own window
        def work():
            try:
                import time as _t
                atts = []
                if cb.isChecked():
                    _t.sleep(0.4)
                    atts.append(lumo.screenshot_attachment())
                    self.emit_from_thread("show")
                for f in files:
                    atts.append(lumo.file_attachment(f))
                turns = int(self.cfg.get("lumo", {}).get("context_turns", 6))
                lumo.send(self.cfg, lumo.build_payload(task or "(см. вложения)", hist, ("", ""), atts, turns))
                msg = "Передал Люмо."
            except Exception as e:
                msg = f"Не получилось передать Люмо: {e}"
            if self.assistant:
                self.assistant.say(msg)
            else:
                self.emit_from_thread("notice", text=msg)
        threading.Thread(target=work, daemon=True).start()

    def ollama_flow(self, state: str, models: Optional[list] = None, ask: bool = True) -> None:
        """Ollama is missing / stopped / lacks models: offer to fix it right here, with progress in the window."""
        from .. import ollama_setup as osu
        if self.demo or getattr(self, "_ollama_busy", False):
            return
        oc = self.cfg.get("ollama") or {}
        models = models or []
        if state == "missing":
            if not oc.get("offer_install", True) and ask:
                return
            q = ("Ollama — локальный ИИ, без которого Джарвис не может отвечать, — не установлена.\n\n"
                 "Установить её сейчас? Скачается около 1,6 ГБ, права администратора не нужны, 5–20 минут. "
                 "Потом докачается модель ИИ (около 4,7 ГБ).")
        elif state == "running" and models:
            gb = sum(osu.MODEL_SIZES_GB.get(m, 1.0) for m in models)
            size = f"около {gb:.1f} ГБ".replace(".", ",")
            if ask and oc.get("auto_pull", True):
                ask = False             # Jarvis cannot answer without its model: just start (resumable) pulling
                self.window.add_message("Система", f"Не скачана модель ИИ: {', '.join(models)} ({size}, один раз). "
                                        "Начинаю загрузку — прогресс над полем ввода, при обрыве докачается.")
            q = f"Не скачаны модели ИИ: {', '.join(models)} ({size}).\n\nСкачать сейчас?"
        elif state == "stopped":
            q = "Ollama установлена, но не запустилась.\n\nПопробовать запустить её ещё раз?"
        else:
            return
        if ask and QMessageBox.question(self.window, "Ollama", q) != QMessageBox.StandardButton.Yes:
            self.window.add_message("Система", osu.friendly_error(self.cfg, state) if state != "running"
                                    else "Модели можно скачать позже: «проверь себя» → «Скачать».")
            return
        self._ollama_busy = True
        if state != "running":
            self.window.add_message("Система", "Готовлю Ollama… Прогресс — над полем ввода.")
        self._ollama_cancel = threading.Event()

        def prog(label: str, pct: Optional[int]) -> None:
            self.emit_from_thread("progress", value=pct if pct is not None else 0, label=label)

        def work():
            try:
                ok, msg = osu.ensure(self.cfg, progress=prog, allow_install=(state == "missing"),
                                     cancel=self._ollama_cancel)
                if ok and self.assistant is not None:
                    self.assistant.recheck_llm()
                text = (f"Готово: {msg} Можно говорить." if ok else
                        msg + " Повторить: «проверь себя» → «Скачать» (продолжит с места обрыва).")
            except Exception as e:
                log.exception("ollama flow: %s", e)
                text = "Не получилось подготовить Ollama (подробности в журнале). Попробуй «проверь себя»."
            finally:
                self._ollama_busy = False
            self.emit_from_thread("progress", value=100)
            self.emit_from_thread("notice", text=text)
        threading.Thread(target=work, name="jarvis-ollama-setup", daemon=True).start()

    def connect_google(self) -> None:
        """Interactive Google sign-in in the browser (in a thread); result goes to the chat."""
        from .. import google_auth as ga
        self.window.add_message("Система", "Открываю вход в Google в браузере…")

        def work():
            try:
                ga.authorize()
                self.emit_from_thread("notice", text="Google подключён ✓ Теперь можно говорить «передай Люмо …».")
            except Exception as e:
                self.emit_from_thread("notice", text=f"Google не подключён: {e}")
        threading.Thread(target=work, name="jarvis-google-auth", daemon=True).start()

    # ------------------------------------------------------------ live settings
    def preview_ui(self, g: dict) -> None:
        w = self.window
        if "opacity" in g:
            w.setWindowOpacity(max(0.3, min(1.0, float(g.get("opacity") or 1.0))))
        if g.get("color"):
            w.face.set_color(g["color"])

    def apply_ui(self) -> None:
        g = self.cfg["gui"]
        w = self.window
        if face_signature(self.cfg) != getattr(w, "face_sig", None):
            try:
                w.rebuild_face(self.cfg)
            except Exception as e:
                log.error("face rebuild: %s", e)
        if hasattr(w.face, "set_color_mode"):
            from . import faceimage
            w.face.set_color_mode(faceimage.color_mode(self.cfg))
        self.preview_ui(g)
        w.face.set_animation(g.get("animations", "full"))
        w.chat.setVisible(bool(g.get("subtitles", True)))
        try:
            w.resize(int(g.get("width", 460)), int(g.get("height", 780)))
        except Exception:
            pass

    def apply_settings(self, changes: dict) -> None:
        from ..config import render_prompt, update_inplace
        update_inplace(self.cfg, changes)
        self.cfg["llm"]["system_prompt_raw"] = changes.get("llm", {}).get("system_prompt", self.cfg["llm"].get("system_prompt_raw"))
        self.cfg["llm"]["system_prompt"] = render_prompt(self.cfg)
        self.apply_ui()
        g = self.cfg["gui"]
        self.window.setWindowFlag(Qt.WindowStaysOnTopHint, bool(g.get("always_on_top")))
        self.window.show() if self.window.isVisible() or not self.window.isHidden() else None
        self.act_top.blockSignals(True)
        self.act_top.setChecked(bool(g.get("always_on_top")))
        self.act_top.blockSignals(False)
        name = self.cfg["assistant"].get("name", "Джарвис")
        self.window.title.setText(name.upper())
        self.tray.setToolTip(f"{name} — голосовой ассистент")
        on = bool(self.cfg["autostart"].get("enabled"))
        self.act_autostart.blockSignals(True)
        self.act_autostart.setChecked(on)
        self.act_autostart.blockSignals(False)
        if pu.IS_WIN:
            pu.set_autostart(on, PROJECT_ROOT)
        mode = self.cfg["assistant"].get("mode", "wake")
        (self.act_ptt if mode == "ptt" else self.act_wake).setChecked(True)
        if self.assistant:
            try:
                self.assistant.apply_settings()
            except Exception as e:
                log.error("apply: %s", e)
        self._register_hotkeys()

    def restart(self) -> None:
        import os
        import subprocess
        if os.environ.get("JARVIS_WATCHDOG"):        # the watchdog restarts us (exit code 75)
            self.quit(75)
            return
        argv = [sys.executable, "-m", "jarvis"] + [a for a in sys.argv[1:] if a != "--minimized"]
        try:
            subprocess.Popen(argv, cwd=str(PROJECT_ROOT), close_fds=True)
        except Exception as e:
            QMessageBox.warning(self.window, "Перезапуск", f"Не удалось перезапустить: {e}. Запусти вручную.")
        self.quit()

    # ------------------------------------------------------------ diagnostics / updates
    def sync_now(self) -> None:
        if not self.assistant:
            self.window.add_message("Система", "Ещё загружаюсь...")
            return
        self.window.add_message("Система", "Синхронизация...")
        threading.Thread(target=self.assistant.sync_now, daemon=True).start()

    def run_diagnostics(self) -> None:
        self.window.add_message("Система", "Самодиагностика...")

        def work():
            from .. import diagnostics
            checks = diagnostics.run(self.cfg, self.assistant, quick=False)
            self.emit_from_thread("diagnostics", checks=[c.__dict__ for c in checks])
        threading.Thread(target=work, daemon=True).start()

    def show_diagnostics(self, checks: list, startup: bool = False) -> None:
        from .widgets import DiagnosticsDialog
        bad = [c for c in checks if c.get("ok") is False]
        if startup:
            self.window.add_message("Система", "При запуске найдены проблемы: " + "; ".join(
                f"{c['title']} — {c['detail']}" for c in bad) + ". Меню ⋯ → Самодиагностика.")
            return
        dlg = DiagnosticsDialog(checks, self.cfg, rerun=self.run_diagnostics, parent=self.window,
                                actions={"install_ollama": lambda: self.ollama_flow("missing", ask=False),
                                         "pull:*": lambda fid: self.ollama_flow(
                                             "running", [fid.split(":", 1)[1]], ask=False)})
        dlg.show()
        self._diag_dlg = dlg

    def check_updates(self, manual: bool = False) -> None:
        def work():
            from .. import updater
            try:
                info = updater.check(self.cfg) if manual else updater.auto_check(self.cfg)
                if manual:
                    updater.mark_checked()
            except Exception as e:
                info = {"status": "error", "message": str(e)}
            if info:
                self.emit_from_thread("update", info=info, manual=manual)
        threading.Thread(target=work, name="jarvis-update", daemon=True).start()

    def show_update(self, info: dict, manual: bool) -> None:
        if info.get("status") != "available":
            if manual:
                QMessageBox.information(self.window, "Обновления", info.get("message", ""))
            return
        if not manual:
            pu.notify("Джарвис: доступно обновление", info["message"])
        box = QMessageBox(self.window)
        box.setWindowTitle("Доступно обновление")
        box.setIcon(QMessageBox.Information)
        box.setText(f"<b>{info['message']}</b>")
        box.setInformativeText("Что нового:\n\n" + (info.get("notes") or "—"))
        b_now = box.addButton("Установить сейчас", QMessageBox.AcceptRole)
        box.addButton("Позже", QMessageBox.RejectRole)
        b_skip = box.addButton("Пропустить эту версию", QMessageBox.DestructiveRole)
        box.exec()
        if box.clickedButton() is b_skip:
            save_local_settings({"update": {"skip_version": info["version"]}})
            self.cfg.setdefault("update", {})["skip_version"] = info["version"]
        elif box.clickedButton() is b_now:
            self._install_update(info)

    def _install_update(self, info: dict) -> None:
        def work():
            from .. import updater
            try:
                self.emit_from_thread("progress", value=0, label=f"Скачиваю обновление {info['version']}...")
                path = updater.download(info["url"], lambda p: self.emit_from_thread(
                    "progress", value=p, label=f"Скачиваю обновление {info['version']}... {p}%"))
                self.emit_from_thread("progress", value=100)
                updater.run_installer(path)
                self.emit_from_thread("exit")
            except Exception as e:
                self.emit_from_thread("progress", value=100)
                self.emit_from_thread("notice", text=f"Обновление не удалось: {e}")
        threading.Thread(target=work, daemon=True).start()

    def update_models(self) -> None:
        def work():
            from .. import updater
            self.emit_from_thread("notice", text="Обновляю модели Ollama...")
            errs = updater.pull_models(self.cfg, lambda m, p: self.emit_from_thread(
                "progress", value=p, label=f"ollama pull {m}: {p}%"))
            self.emit_from_thread("progress", value=100)
            self.emit_from_thread("notice", text=("Ошибки: " + "; ".join(errs)) if errs else "Модели обновлены.")
        threading.Thread(target=work, daemon=True).start()

    def quit(self, code: int = 0) -> None:
        self.quitting = True
        try:
            if self.assistant:
                self.assistant.close()
        except Exception as e:
            log.error("close: %s", e)
        self.tray.hide()
        self.app.exit(code)

    # ------------------------------------------------------------ assistant events (GUI thread)
    def emit_from_thread(self, event: str, **data) -> None:
        self.bridge.event.emit(event, data)

    def _on_event(self, event: str, data: dict) -> None:
        w = self.window
        name = self.cfg["assistant"].get("name", "Джарвис")
        if event == "state":
            st = data.get("state", "idle")
            if self.assistant and self.assistant.mic_muted and st in ("idle", "sleeping"):
                st = "muted"
            if not (w.face.state == "speaking" and w.face.env and st in ("idle", "sleeping", "thinking")):
                w.face.set_state(st)
            self._tray_state(st)
        elif event == "mic_level":
            w.face.set_mic_level(data.get("level", 0.0))
            if w.face.state == "idle" and data.get("level", 0) > 0.15 and not (self.assistant and self.assistant.mic_muted):
                w.face.set_state("listening")
        elif event == "speech":
            w.face.speak(data.get("env", []), data.get("wid", []), data.get("dt", 0.02))
        elif event == "speech_end":
            QTimer.singleShot(200, lambda: w.face.set_state(self._rest_state()) if not w.face.env else None)
        elif event == "user_text":
            w.add_message("Ты", data.get("text", ""))
        elif event == "assistant_text":
            w.add_message(name, data.get("text", ""))
        elif event == "assistant_done":
            w.add_message(name, data.get("text", ""))
        elif event == "links":
            w.add_message("Источники", "", data.get("links", []))
        elif event == "notice":
            w.add_message("Система", data.get("text", ""))
        elif event == "ollama_problem":
            QTimer.singleShot(500, lambda: self.ollama_flow(data.get("state", ""), data.get("models") or []))
        elif event == "open_settings":
            tab = data.get("tab")
            QTimer.singleShot(300, lambda: self.open_settings(tab=tab))
        elif event == "progress":
            val = data.get("value")
            if val is None or val >= 100:
                w.progress.hide()
                w.progress_label.hide()
            else:
                w.progress.show()
                w.progress_label.show()
                w.progress.setValue(int(val))
                w.progress_label.setText(data.get("label", ""))
        elif event == "profile":
            p = data.get("profile", "work")
            names = {"work": "рабочий", "game": "игровой", "night": "ночной"}
            colors = {"work": "rgb(110,190,220)", "game": "rgb(150,230,140)", "night": "rgb(150,150,230)"}
            w.lbl_profile.setText(f"● {names.get(p, p)} режим" + (" (авто)" if data.get("auto") else ""))
            w.lbl_profile.setStyleSheet(f"color: {colors.get(p)}; font: 11px 'Segoe UI';")
            if p in getattr(self, "act_profiles", {}):
                self.act_profiles[p].setChecked(True)
        elif event == "alert":
            if data.get("active"):
                w.lbl_alert.setText("⚠ ТРЕВОГА")
                w.lbl_alert.setStyleSheet("color: rgb(255,90,80); font: bold 11px 'Segoe UI';")
                w.lbl_alert.setToolTip(", ".join(data.get("places", [])))
            else:
                w.lbl_alert.setText("✓ тревоги нет")
                w.lbl_alert.setStyleSheet("color: rgb(110,200,150); font: 11px 'Segoe UI';")
        elif event == "pcstats":
            parts = []
            if "cpu_temp" in data:
                parts.append(f"CPU {data['cpu_temp']:.0f}°")
            elif "cpu_load" in data:
                parts.append(f"CPU {data['cpu_load']:.0f}%")
            if "gpu_temp" in data:
                parts.append(f"GPU {data['gpu_temp']:.0f}°")
            w.lbl_stats.setText("  ".join(parts))
        elif event == "mic_muted":
            if self.act_mute.isChecked() != data.get("muted"):
                self.set_muted(bool(data.get("muted")))
        elif event == "toggle_mic":
            self.toggle_mic()
        elif event == "sync":
            msg = data.get("message", "")
            if data.get("conflicts"):
                msg += " Конфликтные копии: " + ", ".join(data["conflicts"][:5])
            w.add_message("Синхронизация", msg)
        elif event == "diagnostics":
            self.show_diagnostics(data.get("checks", []), bool(data.get("startup")))
        elif event == "update":
            self.show_update(data.get("info", {}), bool(data.get("manual")))
        elif event == "show":
            self.toggle_window() if not w.isVisible() else None
        elif event == "exit":
            self.quit()

    # ------------------------------------------------------------ startup
    def maybe_first_run(self) -> None:
        """One-time offer to pick an own face picture (gui.face_prompted remembers the answer)."""
        from . import firstrun
        try:
            if self.demo or not firstrun.should_prompt(self.cfg):
                return
        except Exception as e:
            log.warning("first run check: %s", e)
            return
        dlg = firstrun.FirstRunFaceDialog(self.cfg, save_local_settings, self.window)

        def chosen(lm: dict) -> None:
            self.apply_ui()
            if lm.get("source") == "heuristic":
                self._first_run_calibrate()

        dlg.chosen.connect(chosen)
        dlg.show()
        self._first_run_dlg = dlg

    def _first_run_calibrate(self) -> None:
        import json

        from . import faceimage
        from .facecalib import FaceCalibDialog
        img, lmp = faceimage.custom_paths()
        try:
            lm = faceimage.normalize(json.loads(lmp.read_text(encoding="utf-8")))
        except Exception:
            lm = faceimage.detect_heuristic(faceimage.load_image(img))
        QMessageBox.information(self.window, "Своё лицо", "Лицо не распозналось автоматически. "
                                "Отметь глаза, рот и подбородок вручную, это займёт полминуты.")
        dlg = FaceCalibDialog(img, lm, self.window)
        if dlg.exec():
            faceimage.save_landmarks(dlg.result_landmarks())
            self.apply_ui()

    def _voice_on_cpu(self) -> bool:
        """A CUDA/cuBLAS/cuDNN error must never cost the voice mode: restart it with Whisper on the CPU."""
        try:
            from ..assistant import Assistant
            from ..stt import SpeechToText
            self.cfg["stt"].update(device="cpu", compute_type="int8")
            self.emit_from_thread("notice", text="Видеокарта для распознавания не запустилась — распознаю на процессоре.")
            if self.assistant is None:
                self.assistant = Assistant(self.cfg, speak=self.speak, need_stt=True, on_event=self.emit_from_thread)
                self.assistant.router.ctx.extra["open_settings"] = \
                    lambda tab: self.emit_from_thread("open_settings", tab=tab)
            else:                                   # reuse the running assistant, only swap Whisper
                self.assistant.stt = SpeechToText(self.cfg["stt"])
            self.assistant.run_voice()
            return True
        except Exception as e:
            log.exception("Голосовой режим на процессоре тоже не запустился: %s", e)
            return False

    def open_manual(self):
        """Tray «Инструкция»: docs/ИНСТРУКЦИЯ.md rendered to HTML in the default browser (no network)."""
        try:
            from ..manual import open_manual
            open_manual()
        except Exception as e:                           # noqa: BLE001
            log.warning("Инструкция не открылась: %s", e)

    def restart_listening(self) -> None:
        """Tray: re-open the microphone and reset the voice state machine; revive the loop if it ended."""
        a = self.assistant
        if a is None or getattr(a, "stt", None) is None:
            self.tray_message("Голосовой режим не запущен (работаю в текстовом режиме).")
            return
        if a.mic_muted:
            a.set_mic_muted(False, cue=False)
        if not a.restart_listening():
            log.warning("Голосовой цикл не работал — запускаю заново")
            threading.Thread(target=a.run_voice, name="jarvis-voice", daemon=True).start()
        self.tray_message("Прослушка перезапущена. Скажи «Джарвис».")

    def start_assistant(self) -> None:
        def worker():
            try:
                from ..assistant import Assistant
                self.emit_from_thread("state", state="loading")
                self.assistant = Assistant(self.cfg, speak=self.speak, need_stt=True, on_event=self.emit_from_thread)
                self.assistant.router.ctx.extra["open_settings"] = \
                    lambda tab: self.emit_from_thread("open_settings", tab=tab)
                self.assistant.run_voice()
            except Exception as e:
                log.exception("Ассистент не запустился: %s", e)
                from ..cuda_libs import is_gpu_lib_error
                if is_gpu_lib_error(e) and self._voice_on_cpu():
                    return
                self.emit_from_thread("notice", text=f"Ошибка запуска: {e}. Работаю в текстовом режиме.")
                try:
                    if self.assistant is None:
                        from ..assistant import Assistant
                        self.assistant = Assistant(self.cfg, speak=self.speak, need_stt=False, on_event=self.emit_from_thread)
                        self.assistant.router.ctx.extra["open_settings"] = \
                            lambda tab: self.emit_from_thread("open_settings", tab=tab)
                        self.assistant.greet()
                        self.emit_from_thread("state", state="idle")
                except Exception as e2:
                    self.emit_from_thread("notice", text=f"Не удалось запустить даже текстовый режим: {e2}")
        threading.Thread(target=worker, name="jarvis-voice", daemon=True).start()
        self._start_hotkeys()
        QTimer.singleShot(20000, lambda: self.check_updates(manual=False))

    def _register_hotkeys(self) -> bool:
        """(Re)register global hotkeys from the live config. Each one isolated."""
        try:
            import keyboard
        except Exception as e:
            log.info("Глобальные горячие клавиши недоступны: %s", e)
            return False
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        g = self.cfg["gui"]
        for hk, fn in ((g.get("hotkey_show", "ctrl+alt+j"), lambda: self.emit_from_thread("show")),
                       (g.get("hotkey_mic", "ctrl+alt+m"), lambda: self.emit_from_thread("toggle_mic")),
                       (g.get("hotkey_stop", "ctrl+alt+x"), self.stop_now),
                       (self.cfg.get("dictate", {}).get("hotkey", "ctrl+alt+d"), self._toggle_dictation)):
            if not hk:
                continue
            try:
                keyboard.add_hotkey(hk, fn)
            except Exception as e:
                log.warning("Горячая клавиша %s не зарегистрирована: %s", hk, e)
        return True

    def collect_diagnostics(self) -> None:
        def work():
            from .. import diagbundle
            try:
                path = diagbundle.collect(self.cfg, self.assistant)
                self.emit_from_thread("notice", text=f"Архив диагностики: {path}")
                pu.open_path(path.parent)
            except Exception as e:                       # noqa: BLE001
                self.emit_from_thread("notice", text=f"Не получилось собрать диагностику: {e}")
        threading.Thread(target=work, name="jarvis-diag-zip", daemon=True).start()

    def stop_now(self) -> None:
        """1.0.9: hotkey / tray «Остановить» — stop speaking and thinking immediately (works while Jarvis speaks,
        when the mic is deaf to its own voice)."""
        a = self.assistant
        if a is None:
            return
        threading.Thread(target=lambda: a.cancel_current(reply="", source="клавиша/трей"), daemon=True).start()

    def _toggle_dictation(self) -> None:
        a = self.assistant
        if a and "dictate" in a.router.by_name:
            a.say(a.router.by_name["dictate"].toggle())

    def _start_hotkeys(self) -> None:
        g = self.cfg["gui"]
        if not self._register_hotkeys():
            return
        import keyboard

        def poll():
            ptt_down = game_down = False
            while not self.quitting:
                try:
                    p = keyboard.is_pressed(g.get("hotkey_ptt", "ctrl+alt+space"))
                    gm = keyboard.is_pressed(g.get("hotkey_game", "ctrl+alt+g"))
                except Exception:
                    return
                if gm and not game_down and self.assistant and "game" in self.assistant.router.by_name:
                    self.assistant.router.by_name["game"].forced = True
                if (p or gm) and not (ptt_down or game_down):
                    self.ptt_press()
                if not (p or gm) and (ptt_down or game_down):
                    self.ptt_release()
                ptt_down, game_down = p, gm
                time.sleep(0.03)
        self._hotkey_thread = threading.Thread(target=poll, name="jarvis-hotkeys", daemon=True)
        self._hotkey_thread.start()


def run_gui(cfg: dict, minimized: bool = False, demo: bool = False, speak: bool = True) -> int:
    if sys.platform == "win32":
        try:  # own taskbar icon/group instead of python.exe
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Jarvis.Assistant")
        except Exception:
            pass
    if QApplication.instance() is None:
        prepare_app(cfg)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Jarvis")
    app.setWindowIcon(make_icon())
    ctl = Controller(app, cfg, demo=demo, speak=speak)
    try:
        ctl.apply_ui()
    except Exception as e:
        log.error("ui settings: %s", e)
    if not minimized:
        ctl.window.show()
    if demo:
        from .demo import start_demo
        start_demo(ctl)
    else:
        ctl.start_assistant()
        QTimer.singleShot(1500, ctl.maybe_first_run)
    return app.exec()
