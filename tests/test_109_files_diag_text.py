"""1.0.9: «открой файл/папку X» fast path (translit, digits, top-3 + pick), «собери диагностику» zip,
mixed-script junk before TTS, Whisper sound-tag hallucinations."""
from __future__ import annotations

import json
import os
import types
import zipfile

import pytest

from jarvis import diagbundle, noname
from jarvis import windows as wn
from jarvis.launcher import phonetic
from jarvis.llm import ABILITIES, clean_for_speech
from jarvis.skills import files_skill as fsk
from jarvis.stt import accept, strip_sound_tags


@pytest.fixture
def fs(tmp_path, monkeypatch):
    home = tmp_path / "User"
    files = ["Desktop/hello.txt", "Documents/1111.docx", "Downloads/я.png", "Documents/Отчёт за март.xlsx",
             "Documents/отчёт за апрель.xlsx", "Documents/отчёт за май.xlsx", "Music/hello world.mp3",
             "D/archive/old/hello.txt", "Documents/Проекты/readme.md"]
    dirs = ["Documents/Проекты", "Desktop/Фото с моря"]
    for d in dirs:
        (home / d).mkdir(parents=True, exist_ok=True)
    for f in files:
        p = home / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    opened = []
    monkeypatch.setenv("USERPROFILE", str(home))          # live scan of Desktop/Documents/Downloads = this tree
    monkeypatch.setattr(fsk.pu, "open_path", lambda p: opened.append(str(p)))
    monkeypatch.setattr(fsk, "windows_search", lambda q, limit=10: [])
    monkeypatch.setattr(fsk.FilesSkill, "_load_cache", lambda self: None)
    monkeypatch.setattr(fsk.threading, "Thread", lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    ctx = types.SimpleNamespace(cfg={"files": {"roots": [str(home)]}}, extra={}, llm=None)
    sk = fsk.FilesSkill(ctx)
    sk.build_index()
    return sk, opened, home


def say(sk, phrase):
    return sk.match(phrase, phrase)


@pytest.mark.parametrize("phrase,name", [("открой файл хэллоу", "hello.txt"), ("открой файл hello", "hello.txt"),
                                         ("открой файл 1111", "1111.docx"), ("открой файл я", "я.png"),
                                         ("открой файл отчёт за март", "Отчёт за март.xlsx"),
                                         ("открой документ отчет за март", "Отчёт за март.xlsx"),
                                         ("открой папку проекты", "Проекты"),
                                         ("открой папку фото с моря", "Фото с моря")])
def test_open_file_fast_path(fs, phrase, name):
    sk, opened, _ = fs
    r = say(sk, phrase)
    assert r is not None and opened and opened[-1].endswith(name), (r, opened)
    assert r.reply == f"Открываю {name}."


def test_file_saved_after_index_build_is_found(fs):
    """Wine 1.0.9: «отчёт за март.txt» saved on the Desktop a moment ago -> «Не нашёл» (index is hours old)."""
    sk, opened, home = fs
    (home / "Desktop" / "смета ремонта.txt").write_text("x", encoding="utf-8")
    sk._live_cache = None
    r = say(sk, "открой файл смета ремонта")
    assert r is not None and opened and opened[-1].endswith("смета ремонта.txt"), (r, opened)
    assert not any(p.endswith("смета ремонта.txt") for p, _ in sk.index)      # came from the live scan


def test_live_scan_is_cached_capped_and_deduplicated(fs, monkeypatch):
    sk, _, home = fs
    sk._live_cache = None
    a = sk._live_items([])
    assert a and sk._live_items([]) is a                                      # 5 s cache: one listing per phrase
    sk._live_cache = None
    assert len(sk._live_items([], cap=2)) == 2
    sk._live_cache = None
    hits = sk.find_items("hello", limit=5)
    assert len([p for _, p in hits if p.endswith(os.path.join("Desktop", "hello.txt"))]) == 1


def test_preferred_dir_wins_over_deep_duplicate(fs):
    sk, opened, home = fs
    say(sk, "открой файл hello")
    assert opened[-1] == str(home / "Desktop" / "hello.txt")


def test_several_matches_top3_and_pick(fs):
    sk, opened, _ = fs
    r = say(sk, "открой файл отчёт")
    assert opened == [] and r.expect is not None and "1 —" in r.reply and "в документах" in r.reply
    assert len(sk.results) == 3
    r2 = r.expect("второй")
    assert opened == [sk.results[1]] and r2.reply.startswith("Открываю")
    assert r.expect("включи музыку") is None                       # anything else = a new command
    assert r.expect("нет").reply == "Ок, не открываю."


def test_explorer_current_folder_preferred(fs):
    sk, opened, home = fs
    (home / "Documents" / "Проекты" / "hello.txt").write_text("x")
    sk.build_index()

    class B:
        def windows(self):
            return [wn.Window(5, "Проекты", 500, "explorer.exe", "CabinetWClass")]
    sk.ctx.extra["wm"] = types.SimpleNamespace(b=B(), candidates=lambda: B().windows())
    say(sk, "открой файл hello")
    assert opened[-1] == str(home / "Documents" / "Проекты" / "hello.txt")


def test_not_found_and_known_folders_deferred(fs):
    sk, opened, _ = fs
    assert "Не нашёл файл «абракадабра»" in say(sk, "открой файл абракадабра").reply
    assert say(sk, "открой папку загрузки") is None and opened == []
    sk.ctx.extra["noname"] = True
    assert say(sk, "открой файл абракадабра").reply == ""


def test_noname_only_when_file_exists(fs):
    sk, _, _ = fs
    router = types.SimpleNamespace(by_name={"files": sk})
    assert noname.classify("открой файл хэллоу", router) == "apps"
    assert noname.classify("открой файл абракадабра", router) is None
    assert noname.classify("открой папку загрузки", router) == "apps"


def test_short_query_never_substring_matches(fs):
    sk, _, _ = fs
    assert phonetic("я") == "ia"
    items = sk.find_items("я")
    assert [p.split("/")[-1].split("\\")[-1] for _, p in items] == ["я.png"]


def test_llm_prompt_knows_files():
    assert "открой файл" in ABILITIES and "нет доступа" in ABILITIES


# ---------------------------------------------------------------- diagnostics zip
def test_collect_diagnostics_zip(tmp_path, monkeypatch):
    log = tmp_path / "logs" / "jarvis.log"
    log.parent.mkdir()
    log.write_text("hello log", encoding="utf-8")
    (tmp_path / "logs" / "jarvis.log.1").write_text("older", encoding="utf-8")
    la = tmp_path / "LocalAppData" / "Ollama"
    la.mkdir(parents=True)
    (la / "server.log").write_text("no usable GPU found", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setattr(diagbundle, "resolve_path", lambda f: log)
    settings = tmp_path / "settings.local.yaml"
    settings.write_text("telegram:\n  api_hash: SECRET123\n  api_id: 42\nllm:\n  placement: gpu\n", encoding="utf-8")
    monkeypatch.setattr(diagbundle, "LOCAL_SETTINGS", settings)
    run = lambda args, **kw: types.SimpleNamespace(stdout="GTX 1660 SUPER 6144MiB", stderr="")   # noqa: E731
    http = types.SimpleNamespace(get=lambda url, timeout=5: types.SimpleNamespace(
        json=lambda: {"models": [{"name": "qwen2.5:7b", "size_vram": 0}]}))
    out = diagbundle.collect({"logging": {"file": "logs/jarvis.log"}}, dest=tmp_path / "Desk", run=run, http=http)
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"jarvis/jarvis.log", "jarvis/jarvis.log.1", "ollama/server.log", "nvidia-smi.txt",
                "ollama/api-ps.json", "system.json", "settings.local.yaml"} <= names
        st = z.read("settings.local.yaml").decode()
        assert "SECRET123" not in st and "***" in st and "placement: gpu" in st
        assert "1660" in z.read("nvidia-smi.txt").decode()
        assert json.loads(z.read("system.json"))["jarvis"]
    assert out.name.startswith("Jarvis-диагностика-") and out.parent == tmp_path / "Desk"


@pytest.mark.parametrize("phrase", ["собери диагностику", "Джарвис собери диагностику", "сохрани логи",
                                    "собери диагностику на рабочий стол"])
def test_collect_phrase_routes(phrase, monkeypatch):
    from jarvis.skills.diag_skill import COLLECT_RE, DiagSkill
    assert COLLECT_RE.match(phrase.lower())
    started = []
    monkeypatch.setattr("jarvis.skills.diag_skill.threading.Thread",
                        lambda target=None, **k: types.SimpleNamespace(start=lambda: started.append(target)))
    sk = DiagSkill(types.SimpleNamespace(cfg={}, extra={}, llm=None))
    r = sk.match(phrase, phrase.lower())
    assert r is not None and "архив" in r.reply and started


def test_self_test_phrase_still_self_test():
    from jarvis.skills.diag_skill import COLLECT_RE, DIAG_RE
    assert not COLLECT_RE.match("запусти диагностику") and DIAG_RE.search("запусти диагностику")


# ---------------------------------------------------------------- text cleanup
@pytest.mark.parametrize("src,dst", [
    ("У меня нет доступа к твоему файловомуsystemsystsem.", "У меня нет доступа к твоему файловому."),
    ("Это фaйл на диске.", "Это файл на диске."),
    ("Скачай systemsystsem сейчас", "Скачай сейчас"),
    ("Открой YouTube и Steam, включи Wi-Fi.", "Открой YouTube и Steam, включи Wi-Fi."),
    ("Hello world, this is English.", "Hello world, this is English."),
    ("Модель qwen2.5:7b работает на GPU.", "Модель qwen2.5:7b работает на GPU."),
])
def test_mixed_script_junk(src, dst):
    assert clean_for_speech(src) == dst


@pytest.mark.parametrize("src,dst", [
    ("ДИНАМИЧНАЯ МУЗЫКА", ""), ("[МУЗЫКА]", ""), ("(смех)", ""), ("АПЛОДИСМЕНТЫ", ""), ("Аплодисменты.", ""),
    ("динамичная музыка", ""), ("Смех в зале", ""), ("Джарвис, стоп [смех]", "Джарвис, стоп"),
    ("включи музыку", "включи музыку"), ("музыка", "музыка"), ("ЮТУБ", "ЮТУБ"), ("ОК", "ОК"),
    ("Открой ютуб.", "Открой ютуб."),
])
def test_sound_tags(src, dst):
    assert strip_sound_tags(src) == dst


def test_accept_drops_user_log_hallucination():
    seg = types.SimpleNamespace(text="ДИНАМИЧНАЯ МУЗЫКА", no_speech_prob=0.43, avg_logprob=-0.5)
    assert accept([seg]) == ""
