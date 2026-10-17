"""Two simulated PCs syncing through one shared folder."""
import time
from pathlib import Path

import yaml

from jarvis.memory import Memory
from jarvis.planner import Planner
from jarvis.sync import SyncEngine


class PC:
    def __init__(self, tmp: Path, name: str, shared: Path):
        self.data = tmp / name / "data"
        self.proj = tmp / name / "proj"
        self.data.mkdir(parents=True)
        self.proj.mkdir(parents=True)
        self.settings = self.proj / "settings.local.yaml"
        self.cfg = {"sync": {"folder": str(shared), "device_name": name}, "planner": {"dir": str(self.data)},
                    "memory": {"dir": str(self.data), "use_embeddings": False}, "notes": {"dir": str(self.data / "Notes")}}
        self.planner = Planner(self.cfg["planner"])
        self.memory = Memory(self.cfg["memory"])
        self.engine = SyncEngine(self.cfg, data_root=self.data, project_root=self.proj, local_settings=self.settings)
        self.notes = self.data / "Notes"
        self.notes.mkdir(exist_ok=True)

    def sync(self):
        r = self.engine.sync(make_backup=False)
        assert r.ok, r.message
        return r

    def tasks(self):
        return sorted(t.text for t in self.planner.list(include_done=True))


def test_two_pcs(tmp_path):
    shared = tmp_path / "GoogleDrive"
    shared.mkdir()
    a, b = PC(tmp_path, "Desktop", shared), PC(tmp_path, "Laptop", shared)

    # --- records created on both sides merge both ways
    a.planner.add("купить молоко", None)
    b.planner.add("позвонить маме", None)
    a.memory.add("Любимая игра — Dota 2")
    a.sync(); b.sync(); a.sync()
    assert a.tasks() == b.tasks() == ["купить молоко", "позвонить маме"]
    assert [m.text for m in b.memory.all()] == ["Любимая игра — Dota 2"]

    # --- newer edit wins; a reminder fired on A is not re-fired on B
    tb = next(t for t in b.planner.list() if t.text == "позвонить маме")
    time.sleep(0.01)
    b.planner.conn.execute("UPDATE tasks SET text=? WHERE id=?", ("позвонить маме вечером", tb.id))
    b.planner.conn.commit()
    ta = next(t for t in a.planner.list() if t.text == "купить молоко")
    a.planner.mark_reminded(ta.id)
    b.sync(); a.sync(); b.sync()
    assert "позвонить маме вечером" in a.tasks()
    assert next(t for t in b.planner.list() if t.text == "купить молоко").reminded

    # --- deletion travels as a tombstone, but never over a later edit
    a.planner.delete(ta.id)
    a.sync(); b.sync()
    assert "купить молоко" not in b.tasks()
    m = a.memory.all()[0]
    a.memory.delete(m.id)
    time.sleep(0.01)
    bm = b.memory.all()[0]
    b.memory.update(bm.id, "Любимая игра — Dota 2 и CS2")
    a.sync(); b.sync(); a.sync()
    assert [x.text for x in a.memory.all()] == ["Любимая игра — Dota 2 и CS2"]
    assert [x.text for x in b.memory.all()] == ["Любимая игра — Dota 2 и CS2"]

    # --- notes: push/pull, one-sided edit, conflict copy on both PCs
    (a.notes / "идея.txt").write_text("v1", encoding="utf-8")
    a.sync(); b.sync()
    assert (b.notes / "идея.txt").read_text(encoding="utf-8") == "v1"
    (b.notes / "идея.txt").write_text("v2 от ноутбука", encoding="utf-8")
    b.sync(); a.sync()
    assert (a.notes / "идея.txt").read_text(encoding="utf-8") == "v2 от ноутбука"
    (a.notes / "идея.txt").write_text("A edit", encoding="utf-8")
    (b.notes / "идея.txt").write_text("B edit", encoding="utf-8")
    b.sync()
    r = a.sync()
    assert r.conflicts == ["идея (конфликт Laptop).txt"]
    assert (a.notes / "идея (конфликт Laptop).txt").read_text(encoding="utf-8") == "B edit"
    assert (a.notes / "идея.txt").read_text(encoding="utf-8") == "A edit"
    b.sync()
    assert (b.notes / "идея (конфликт Laptop).txt").read_text(encoding="utf-8") == "B edit"
    assert (b.notes / "идея.txt").read_text(encoding="utf-8") == "A edit"     # both versions kept everywhere

    # --- settings: shareable values travel; per-machine values and secrets never do
    a.settings.write_text(yaml.safe_dump({"gui": {"color": "#ff0000", "hotkey_mic": "ctrl+alt+x"},
                                          "audio": {"input_device": "USB Mic"},
                                          "telegram": {"api_hash": "SECRET"},
                                          "llm": {"system_prompt": "Ты — {name}, весёлый."}}, allow_unicode=True),
                          encoding="utf-8")
    (a.proj / "aliases.yaml").write_text("кс: Counter-Strike 2\n", encoding="utf-8")
    a.sync(); b.sync()
    bs = yaml.safe_load(b.settings.read_text(encoding="utf-8"))
    assert bs["gui"]["color"] == "#ff0000" and bs["llm"]["system_prompt"] == "Ты — {name}, весёлый."
    assert "audio" not in bs and "telegram" not in bs and "hotkey_mic" not in bs["gui"]
    assert (b.proj / "aliases.yaml").read_text(encoding="utf-8") == "кс: Counter-Strike 2\n"
    blob = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in shared.rglob("*") if p.is_file())
    assert "SECRET" not in blob and "USB Mic" not in blob


def test_not_configured(tmp_path):
    e = SyncEngine({"sync": {}}, data_root=tmp_path, project_root=tmp_path, local_settings=tmp_path / "s.yaml")
    r = e.sync(make_backup=False)
    assert not r.ok and "не выбрана" in r.message
