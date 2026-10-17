"""Small reusable widgets: hotkey recorder, key/value table, diagnostics report dialog."""
from __future__ import annotations

import threading
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

MODS = {Qt.Key_Control: "ctrl", Qt.Key_Alt: "alt", Qt.Key_Shift: "shift", Qt.Key_Meta: "windows"}
NAMED = {Qt.Key_Space: "space", Qt.Key_Return: "enter", Qt.Key_Tab: "tab", Qt.Key_Backspace: "backspace",
         Qt.Key_Home: "home", Qt.Key_End: "end", Qt.Key_Insert: "insert", Qt.Key_Delete: "delete",
         Qt.Key_PageUp: "page up", Qt.Key_PageDown: "page down", Qt.Key_Pause: "pause",
         Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right"}

# shortcuts Windows/apps already use — warn if the user picks one of them
RESERVED = {"ctrl+alt+delete", "ctrl+shift+esc", "alt+tab", "alt+f4", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z",
            "ctrl+a", "ctrl+s", "windows+l", "windows+d", "windows+e", "windows+r", "ctrl+alt+tab", "windows+tab"}

# Qt key -> layout-independent latin name (Russian layout gives Cyrillic text otherwise)
def key_name(key: int, text: str) -> Optional[str]:
    if key in NAMED:
        return NAMED[key]
    if Qt.Key_F1 <= key <= Qt.Key_F24:
        return f"f{key - Qt.Key_F1 + 1}"
    if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
        return chr(key).lower()
    return text.lower() if text and text.isascii() and text.isprintable() else None


def normalize_hotkey(h: str) -> str:
    parts = [p.strip().lower() for p in (h or "").replace(" + ", "+").split("+") if p.strip()]
    mods = [m for m in ("ctrl", "alt", "shift", "windows") if m in parts]
    rest = [p for p in parts if p not in mods]
    return "+".join(mods + rest)


class HotkeyEdit(QLineEdit):
    """Click, press the combination — it is recorded as e.g. 'ctrl+alt+m'."""
    changed = Signal(str)

    def __init__(self, value: str = "", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("нажми сочетание клавиш...")
        self.setText(value)
        self.setToolTip("Щёлкни и нажми сочетание. Backspace/Delete — очистить.")

    def keyPressEvent(self, e):  # noqa: N802
        k = e.key()
        if k in (Qt.Key_Backspace, Qt.Key_Delete) and not e.modifiers():
            self.setText("")
            self.changed.emit("")
            return
        if k in MODS or k == Qt.Key_unknown:
            return
        mods = []
        m = e.modifiers()
        if m & Qt.ControlModifier:
            mods.append("ctrl")
        if m & Qt.AltModifier:
            mods.append("alt")
        if m & Qt.ShiftModifier:
            mods.append("shift")
        if m & Qt.MetaModifier:
            mods.append("windows")
        name = key_name(k, e.text())
        if not name:
            return
        if not mods and not name.startswith("f"):
            self.setToolTip("Нужен модификатор (Ctrl/Alt/Shift/Win) или F-клавиша")
            return
        hk = "+".join(mods + [name])
        self.setText(hk)
        self.changed.emit(hk)


def hotkey_conflicts(hotkeys: dict) -> list[str]:
    """{label: hotkey} -> list of problems (duplicates, reserved, invalid)."""
    errs, seen = [], {}
    for label, hk in hotkeys.items():
        n = normalize_hotkey(hk)
        if not n:
            continue
        if n in RESERVED:
            errs.append(f"«{label}»: {n} занято Windows/программами")
        if n in seen:
            errs.append(f"«{label}» и «{seen[n]}»: одинаковое сочетание {n}")
        seen[n] = label
        try:
            import keyboard
            keyboard.parse_hotkey(n)
        except ImportError:
            pass
        except Exception:
            errs.append(f"«{label}»: не понимаю сочетание {n}")
    return errs


class KeyValueTable(QWidget):
    """Editable two-column table (alias -> target) with add/remove buttons."""

    def __init__(self, left: str, right: str, data: Optional[dict] = None, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([left, right])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        add = QPushButton("Добавить")
        rm = QPushButton("Удалить")
        add.clicked.connect(lambda: self.add_row("", ""))
        rm.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()) if self.table.currentRow() >= 0 else None)
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)
        lay.addLayout(row)
        self.set_data(data or {})

    def add_row(self, k: str, v: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(str(k)))
        self.table.setItem(r, 1, QTableWidgetItem(str(v)))

    def set_data(self, data: dict) -> None:
        self.table.setRowCount(0)
        for k, v in (data or {}).items():
            self.add_row(k, v)

    def data(self) -> dict:
        out = {}
        for r in range(self.table.rowCount()):
            k = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            v = (self.table.item(r, 1).text() if self.table.item(r, 1) else "").strip()
            if k and v:
                out[k] = v
        return out


class DiagnosticsDialog(QDialog):
    """Russian report with fix-it buttons. checks: list of dicts (diagnostics.Check.__dict__)."""
    _done = Signal(str)

    def __init__(self, checks: list, cfg: dict, rerun: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Самодиагностика")
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        bl = QVBoxLayout(body)
        for c in checks:
            row = QHBoxLayout()
            icon = {True: "✅", False: "❌", None: "⚠️"}[c.get("ok")]
            lb = QLabel(f"{icon}  <b>{c['title']}</b><br><span style='color:#9cc'>{c['detail']}</span>")
            lb.setWordWrap(True)
            row.addWidget(lb, 1)
            for fix_id, label in c.get("fixes", []):
                b = QPushButton(label)
                b.clicked.connect(lambda _=False, f=fix_id: self._fix(f))
                row.addWidget(b)
            bl.addLayout(row)
        bl.addStretch(1)
        area.setWidget(body)
        lay.addWidget(area)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bottom = QHBoxLayout()
        if rerun:
            again = QPushButton("Проверить снова")
            again.clicked.connect(lambda: (self.accept(), rerun()))
            bottom.addWidget(again)
        bottom.addStretch(1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        lay.addLayout(bottom)
        self._done.connect(self.status.setText)

    def _fix(self, fix_id: str) -> None:
        if fix_id.startswith("settings:"):
            self.status.setText("Открой Настройки → Интеграции.")
            return
        self.status.setText("Выполняю...")

        def work():
            try:
                from .. import diagnostics
                msg = diagnostics.fix(fix_id, self.cfg)
            except Exception as e:
                msg = f"Не получилось: {e}"
            self._done.emit(msg)
        threading.Thread(target=work, daemon=True).start()
