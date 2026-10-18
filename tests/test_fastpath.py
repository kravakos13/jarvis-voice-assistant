"""Fast path: common commands are answered by the rule router (no LLM), incl. mat, filler and Whisper typos."""
import os
import subprocess
import types
import webbrowser
from pathlib import Path

import pytest

from jarvis.config import load_config
from jarvis.router import Router, clean_command
from jarvis.skills.base import SkillContext


class FakeLLM:
    model = "fake"
    keep_alive = "30m"
    history: list = []
    system_prompt = ""

    def complete(self, *a, **k):
        return "ок"

    def embed(self, *a, **k):
        raise RuntimeError("no embeddings in tests")

    def unload_all(self):
        pass


@pytest.fixture(scope="module")
def router(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)          # notes/tasks go to a temp folder
    real = (subprocess.Popen, subprocess.run, webbrowser.open)

    class NoProc:
        def __init__(self, *a, **k):
            self.pid, self.returncode = 1, 0

        def poll(self):
            return 0

        def wait(self, *a, **k):
            return 0

        def communicate(self, *a, **k):
            return b"", b""
    subprocess.Popen = NoProc                                           # nothing is launched / opened
    subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr="")
    webbrowser.open = lambda *a, **k: True
    cfg = load_config(use_local=False)
    cfg["alerts"]["enabled"] = True
    cfg["monitor"]["interval_s"] = 3600
    ctx = SkillContext(cfg=cfg, llm=FakeLLM(), say=lambda *a, **k: None, notify=lambda *a: None, project_root=Path("."))
    r = Router(ctx)
    yield r
    planner = r.by_name.get("planner")
    if planner:
        planner.planner.stop()
    subprocess.Popen, subprocess.run, webbrowser.open = real
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


FAST = {
    "media": ["открой ютуб", "ну давай открой ютуб", "включи музыку", "включи imagine dragons",
              "поставь на ютубе обзор rtx 5070", "пауза", "следующий трек", "следующую песню", "врубай музыку",
              "а включи музыку", "Джарвис, поставь на паузу"],
    "windows": ["закрой ютуб", "ска закрой ютуб нахрен", "закрой хром", "сверни окно", "блин закрой стим",
                "закрои ютюб", "разверни окно"],
    "system": ["сделай погромче", "сделай тише", "громкость 30", "громкость на 50 процентов", "выключи звук",
               "сделай погромче пожалуйста", "ещё громче", "прибавь звук", "убавь громкость немного",
               "сделай звук тише", "блин сделай по громче", "по тише", "выключи компьютер через час",
               "перезагрузи компьютер"],
    "core": ["который час", "сколько времени", "скока щас время", "который сейчас час", "джарвис скажи время",
             "какое сегодня число", "какой сегодня день", "сколько щас время"],
    "weather": ["какая погода", "какая погода завтра", "погода в киеве", "какая погода блин", "какая щас погода"],
    "alerts": ["есть ли тревога", "тревога есть", "ну че там с тревогой", "что там с тревогой"],
    "notes": ["запиши заметку купить хлеб", "прочитай заметки"],
    "planner": ["напомни через 10 минут выключить чайник", "напомни завтра в 9 позвонить маме",
                "поставь напоминание на 18:00 тренировка", "разбуди меня в 7", "поставь будильник на 6:30",
                "поставь будильник", "напомни мне через час проверить духовку"],
    "timers": ["поставь таймер на 5 минут", "таймер 10 минут", "слушай поставь таймер на 3 минуты",
               "поставь таймер на 3 минуты пожалуйста"],
    "modes": ["игровой режим", "включи ночной режим", "обычный режим", "игровой режим включи"],
    "calc": ["сколько будет 2 плюс 2"],
    "web": ["найди в интернете курс доллара"],
}


@pytest.mark.parametrize("phrase,skill", [(p, k) for k, v in FAST.items() for p in v])
def test_common_commands_skip_the_llm(router, phrase, skill):
    r = router.handle(phrase)
    router.pending_confirm = router.pending_expect = None
    assert r is not None, f"«{phrase}» ушло бы в ИИ"
    assert router.last_skill == skill, (phrase, router.last_skill, r.reply)


@pytest.mark.parametrize("phrase", ["как дела", "расскажи анекдот", "почему небо голубое",
                                    "ну блин как же так", "давай поговорим о космосе", "что ты думаешь о фильме дюна"])
def test_conversation_still_goes_to_the_llm(router, phrase):
    assert router.handle(phrase) is None and router.last_skill == ""


def test_alarm_is_not_music(router):
    r = router.handle("поставь будильник на 7 утра")
    assert router.last_skill == "planner" and "Будильник поставил" in r.reply and "07:00" in r.reply
    assert "06:30" in router.handle("поставь будильник на 6:30").reply           # normalize() ate the colon
    assert "На какое время" in router.handle("поставь будильник").reply


def test_cleaning_only_after_a_miss():
    assert clean_command("ну блин сделай по громче нахрен") == "сделай погромче"
    assert clean_command("джарвис скока щас время") == "сколько сейчас время"
    assert clean_command("а ладно открой ютуб") == "открой ютуб"
    assert clean_command("блин") == ""


def test_router_speed(router):
    import time
    t0 = time.perf_counter()
    for _ in range(20):
        router.handle("сделай погромче")
        router.handle("расскажи анекдот")             # miss -> cleaned retry -> miss
    per = (time.perf_counter() - t0) / 40
    assert per < 0.02, f"{per * 1000:.1f} мс на фразу"
