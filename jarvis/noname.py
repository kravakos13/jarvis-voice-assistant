"""Commands without the wake word (1.0.8, «Команды без имени»).

The mic also hears YouTube, TV and people talking, so a phrase without «Джарвис» is executed only if ALL hold:
* the WHOLE utterance (after fillers «ну», «давай», «пожалуйста»…) is one known command — classify() below,
  built from the skills' own pure parsers, so it never guesses;
* short: <= MAX_WORDS words («напиши …» up to MAX_WORDS_TYPE);
* Whisper was confident: avg_logprob >= MIN_LOGPROB and no_speech_prob <= MAX_NOSPEECH;
* Jarvis is not speaking and finished speaking >= TTS_GAP_S ago;
* its group is enabled in settings (assistant.noname.groups).
Executed silently (no voice reply). Never without the name: LLM chat, web search, notes, Lumo, shutdown/reboot,
lock, «заблокируй», closing Jarvis («стоп» only pauses playing media), unresolved «открой/закрой X».
"""
from __future__ import annotations

import re
from typing import Any, Optional

MAX_WORDS = 6
MAX_WORDS_TYPE = 10
MIN_LOGPROB = -0.6
MAX_NOSPEECH = 0.35
TTS_GAP_S = 0.5

GROUPS: list[tuple[str, str]] = [
    ("media", "Музыка и видео: пауза, продолжи, стоп, громче, тише, без звука, следующее, предыдущее"),
    ("youtube", "Видео по номеру: «включи второе видео», «следующее видео»"),
    ("nav", "Назад / вперёд (в т.ч. «назад три раза»), перемотка «вперёд на 30 секунд»"),
    ("browser", "Вкладки и страницы: новая/закрой/верни вкладку, вторая вкладка, обнови, листай, масштаб, вторая ссылка"),
    ("windows", "Окна: сверни, разверни, сверни всё, переключись/вернись на X, вернись; копируй, вставь, отмени, сохрани, скриншот, громкость N%"),
    ("explorer", "Проводник: открой второй файл / третью папку, вверх"),
    ("keys", "Клавиши: нажми энтер, нажми контрол цэ, альт таб, нажми вниз три раза"),
    ("dictation", "Набор текста: напиши <текст>, пиши (диктовка)"),
    ("apps", "Открой / закрой / включи / выключи X — только известные программы, окна и сайты"),
    ("music", "Включи <песня> — коротко и чётко"),
    ("smarthome", "Умный дом — только с названием устройства («включи свет на кухне»)"),
    ("search", "Поиск: «найди в гугле / в хроме / в интернете / на ютубе …», «загугли …» — открывает поиск"),
]
MAX_WORDS_SEARCH = 15
SEARCH_VERB = r"(?:найди|найти|нашел|нашли|найдешь|ищи|поищи|поищем|поиск|погугли|загугли|гугли|загуглим)"
SEARCH_WHERE = (r"(?P<where>гугле|гугл|google|яндексе|яндекс|хроме|хром|chrome|браузере|интернете|инете|сети|"
                r"ютубе|ютуб|youtube|ютюбе)")
SEARCH_NN_RE = re.compile(rf"^(?P<verb>{SEARCH_VERB})(?:\s+мне)?(?:\s+(?:в|во|на|через)\s+{SEARCH_WHERE})?"
                          rf"\s+(?P<q>.+)$")


def parse_search(t: str) -> Optional[tuple[str, str]]:
    """1.0.9 no-name search: ('web'|'youtube', query) — ONLY with an explicit marker («в гугле / в хроме /
    в интернете / на ютубе», or the verb «загугли») and a real query, so speech or video audio can't trigger it."""
    m = SEARCH_NN_RE.match(t)
    if not m:
        return None
    where, verb = m.group("where") or "", m.group("verb")
    q = re.sub(r"^(?:про|о|об)\s+", "", m.group("q").strip()).strip(" .,!?")   # «что такое X» is the query
    if not where and not re.match(r"(?:за|по)?гугли", verb):
        return None                                    # «найди ключи» — no marker: not a search command
    if where:
        q2 = re.sub(rf"^{SEARCH_WHERE}\s+", "", q)       # «найди в гугле в гугле …» stutter
        q = q2 if q2 else q
    if not q or len(q.split()) > MAX_WORDS_SEARCH or len(q) < 2:
        return None
    return ("youtube" if where.startswith(("ют", "you")) else "web"), q


def search_command(t: str) -> Optional[str]:
    p = parse_search(clean(t))
    if p is None:
        return None
    return f"найди на ютубе {p[1]}" if p[0] == "youtube" else f"загугли {p[1]}"


GROUP_IDS = [g for g, _ in GROUPS]

FILLER_RE = re.compile(r"^(?:(?:ну|давай|а|так|пожалуйста|слушай|эй|ка)\s+)+")
TAIL_RE = re.compile(r"\s+(?:пожалуйста|плиз|ка)$")
# Whisper often returns another verb form: «открыл» for «открой», «нажал» for «нажми»
VERB_FORMS = {
    "открыл": "открой", "открыла": "открой", "откройте": "открой", "открою": "открой", "открывай": "открой",
    "открои": "открой", "закрыл": "закрой", "закрыла": "закрой", "закройте": "закрой", "закрою": "закрой",
    "закрои": "закрой", "включил": "включи", "включила": "включи", "включите": "включи", "включай": "включи",
    "врубил": "вруби", "врубай": "вруби", "врубите": "вруби", "выключил": "выключи", "выключила": "выключи",
    "выключите": "выключи", "выключай": "выключи", "вырубил": "выруби", "вырубай": "выруби", "свернул": "сверни",
    "сверните": "сверни", "развернул": "разверни", "разверните": "разверни", "переключил": "переключись",
    "переключился": "переключись", "переключить": "переключись", "переключи": "переключись",
    "переключитесь": "переключись", "пеши": "пиши", "вернулся": "вернись", "вернул": "верни", "напиши-ка": "напиши",
    "написал": "напиши", "напишите": "напиши",
}
VOL_RE = re.compile(r"^(?:(?:сделай|еще|ещё|чуть|немного)\s+)*(?:по)?(?:громче|тише)(?:\s+(?:звук|еще|ещё|немного|чуть))*$"
                    r"|^(?:без звука|выключи звук|отключи звук|включи звук|верни звук|убери звук|"
                    r"(?:прибавь|убавь|подними|опусти)(?: звук| громкость)?(?: немного| чуть)?)$")
CLOSE_VERBS = ("закрой", "выключи", "выруби")
OPEN_VERBS = ("открой", "запусти", "включи", "вруби")
NEVER = re.compile(r"\b(компьютер\w*|комп|пк|систем\w*|перезагру\w*|заблокир\w*|блокир\w*|залочь|джарвис\w*|себя|"
                   r"ассистент\w*|игру|игра|игры|игрушк\w*|игрух\w*)\b")             # «закрой игру» — only with the name


DICT_NOT = re.compile(r"\b(скетч|код|кода|программ\w*|скрипт\w*|функци\w*|телеграм\w*|телег[уе]|люмо|"
                      r"заметк\w*|письм\w*|напоминан\w*|в чат)\b")


def canonical(t: str) -> str:
    """Fix the first verb's form only («я закрыл дверь» mid-sentence is left alone)."""
    toks = (t or "").split()
    if toks and toks[0] in VERB_FORMS:
        toks[0] = VERB_FORMS[toks[0]]
    return " ".join(toks)


def clean(t: str) -> str:
    from .router import normalize
    t = " ".join(normalize(t or "").lower().replace("ё", "е").split())
    t = FILLER_RE.sub("", t)
    t = TAIL_RE.sub("", t)
    from .skills.windows_skill import SPLIT_RE
    t = SPLIT_RE.sub(r"\1 \2\3", t)                  # «закройки берпанк» -> «закрой киберпанк»
    return canonical(t)


def classify(t: str, router: Any = None) -> Optional[str]:
    """Group of a whole-utterance command, or None (= not a no-name command; ignore silently)."""
    from .skills import hotkeys_skill, keys_skill, nav_skill, windows_skill as ws
    from .skills.dictate_skill import ONCE_RE, START_RE
    from .skills.media_skill import BARE_STOP_RE, MEDIA_CMDS, NOT_MUSIC, STOP_MEDIA_RE, VOLUME_RE
    from . import explorer, keyspeech, windows as wn
    from .ytpick import parse_pick

    t = clean(t)
    if not t:
        return None
    n = len(t.split())
    if parse_search(t) is not None:                   # own word limit (queries are longer than commands)
        return "search"
    if ONCE_RE.match(t):                               # «напиши скетч / маме в телеграм» belong to code/telegram
        return "dictation" if n <= MAX_WORDS_TYPE and not DICT_NOT.search(t) else None
    if n > MAX_WORDS or NEVER.search(t):
        return None
    if START_RE.match(t):
        return "dictation"
    kp = keyspeech.parse(t, need_verb=False)
    if kp is not None:
        return None if kp.combo in keys_skill.FORBIDDEN else "keys"
    if nav_skill.parse_seek(t) or nav_skill.parse_nav(t):
        return "nav"
    from .skills.view_skill import parse_view
    if parse_view(t):
        return "media"                                  # 1.0.9 «полный экран», «субтитры»
    if ws.BACK_TO_PREV.match(t):
        return "windows"
    if parse_pick(t):
        return "youtube"
    p = explorer.parse(t)
    if p is not None and p.kind != "back":
        return "explorer"
    if any(rx.match(t) for rx, _ in MEDIA_CMDS) or any(rx.match(t) for rx, _ in VOLUME_RE) \
            or STOP_MEDIA_RE.match(t) or BARE_STOP_RE.match(t) or t in ("стоп", "хватит", "пауза") or VOL_RE.match(t):
        return "media"
    for c in _commands(router, hotkeys_skill):
        if any(rx.match(t) for rx in c["_rx"]):
            return "browser" if c["scope"] == "browser" or c.get("section") == "Браузер" else "windows"
    if re.match(r"^(?:(?:поставь|сделай|установи)\s+)?(?:громкость|звук)\s+(?:на\s+)?\d{1,3}\s*(?:процент\w*|%)?$", t) \
            or re.match(r"^(?:сделай\s+)?(?:скриншот|скрин|снимок экрана)$", t):
        return "windows"
    m = ws.VERB_RE.match(t)
    if m:
        verb, rest = m.group(1), m.group(2).strip()
        target = ws._target_words(rest)
        if ws.ALL_MOD.search(rest):                     # «закрой все окна (хрома)»
            target = " ".join(w for w in target.split() if w not in ws.ALL_NOISE and not ws.ALL_MOD.fullmatch(w))
        if re.fullmatch(ws.ACT_V, verb):
            return "windows" if target and _known_target(target, router, wn) else None
        if re.match(ws.MIN_V, verb) or re.match(ws.MAX_V, verb):
            return "windows" if not target or ws.ALL_RE.match(rest) or _known_target(target, router, wn) else None
        if verb.startswith("закр") and not target and not ws.TAB_RE.search(rest):
            return "windows"                              # 1.0.9: bare «закрой (это/окно)» = the active window
        if verb.startswith(CLOSE_VERBS) and target and _known_target(target, router, wn):
            return "apps"                                 # «закрой ютуб» — always a named target
    from .skills.files_skill import OPEN_FILE_RE
    from .skills.core import FOLDERS
    fm = OPEN_FILE_RE.match(t)
    if fm and fm.group("q").strip() in FOLDERS:
        fm = None                                      # «открой папку загрузки» — known folders (old path)
    if fm:                                             # 1.0.9: «открой файл hello» — only when the file exists
        fs = getattr(router, "by_name", {}).get("files") if router is not None else None
        ok = fs is not None and hasattr(fs, "has_match") and fs.has_match(fm.group("q").strip(),
                                                                          fm.group("kind").startswith("папк"))
        return "apps" if ok else None
    m = re.match(r"^(открой|запусти|включи|вруби|выключи|выруби)\s+(.+)$", t)
    if m:
        verb, x = m.group(1), m.group(2)
        if _device(x, router):
            return "smarthome"
        if verb in OPEN_VERBS and (_known_target(x, router, wn) or _app(x, router)):
            return "apps"
        if verb in ("включи", "вруби") and not NOT_MUSIC.match(x) and n >= 2:
            return "music"
    m = re.match(r"^(?:поставь|сыграй)\s+(?:песню\s+|трек\s+)?(.+)$", t)
    if m and not NOT_MUSIC.match(m.group(1)):
        return "music"
    return None


def _commands(router, hotkeys_skill) -> list:
    sk = getattr(router, "by_name", {}).get("hotkeys") if router is not None else None
    return getattr(sk, "commands", None) or hotkeys_skill.load_commands()


def _known_target(x: str, router, wn) -> bool:
    if wn.resolve_alias(x) is not None:
        return True
    from . import procs
    if procs.app_key(x):                               # tray apps: «вайфай маус», «стим», «дискорд»
        return True
    sk = getattr(router, "by_name", {}).get("windows") if router is not None else None
    wm = getattr(sk, "wm", None)
    try:
        if sk is not None and wm is not None and wm.b is not None and hasattr(sk, "_windows_for") \
                and sk._windows_for(x)[0]:
            return True                                 # incl. game windows by launcher name («киберпанк»)
        return bool(wm is not None and wm.b is not None and wm.find_by_name(x))
    except Exception:                                   # noqa: BLE001
        return False


def _app(x: str, router) -> bool:
    sk = getattr(router, "by_name", {}).get("apps") if router is not None else None
    if sk is None:
        return False
    try:
        e, score = sk.launcher.find(x)
        return e is not None and (score is None or score >= 0.85)
    except Exception:                                   # noqa: BLE001
        return False


def _device(x: str, router) -> bool:
    sk = getattr(router, "by_name", {}).get("smarthome") if router is not None else None
    if sk is None or not getattr(sk.home, "devices", None):
        return False
    try:
        return sk.home.find(x) is not None
    except Exception:                                   # noqa: BLE001
        return False


def settings(cfg: dict) -> tuple[bool, dict]:
    nn = (cfg or {}).get("noname") or {}
    if not isinstance(nn, dict):
        nn = {"enabled": bool(nn)}
    groups = nn.get("groups") or {}
    return bool(nn.get("enabled", True)), {g: bool(groups.get(g, True)) for g in GROUP_IDS}


def confident(conf: Optional[tuple]) -> bool:
    """conf = (avg_logprob, no_speech_prob) of the utterance; unknown -> not confident."""
    if not conf:
        return False
    lp, ns = conf
    return lp >= MIN_LOGPROB and ns <= MAX_NOSPEECH


def decide(text: str, cfg: dict, conf: Optional[tuple], tts_quiet: bool, router: Any = None) -> Optional[str]:
    """-> the cleaned command to run silently, or None. Pure (tests)."""
    on, groups = settings(cfg)
    if not on or not tts_quiet or not confident(conf):
        return None
    g = classify(text, router)
    if g is None:
        from . import fuzzy
        for v in fuzzy.variants(clean(text)):
            if (g := classify(v, router)) is not None:
                text = v
                break
    if g is None or not groups.get(g, True):
        return None
    if g == "dictation":
        return FILLER_RE.sub("", text.strip())          # keep Whisper's case/punctuation for typing
    if g == "search":
        return search_command(text)                     # canonical: «загугли X» / «найди на ютубе X» (never the LLM)
    return clean(text)


# ------------------------------------------------------------------ 1.0.9: cancel while thinking
CANCEL_RE = re.compile(r"^(?:(?:джарвис|джервис|jarvis)\s+)?(?:стоп|отмена|отмени|отменяй|хватит|забей|не надо)"
                       r"(?:\s+(?:джарвис|пожалуйста))?$")


def is_cancel(text: str, conf: Optional[tuple] = None, min_logprob: float = -0.7, max_nospeech: float = 0.5) -> bool:
    """Only an exact short cancel phrase with a confident transcription (the user may be talking to someone)."""
    t = re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е"))
    t = re.sub(r"\s+", " ", t).strip()
    if not CANCEL_RE.match(t):
        return False
    if conf:
        try:
            lp, ns = float(conf[0]), float(conf[1])
        except (TypeError, ValueError, IndexError):
            return True
        if lp < min_logprob or ns > max_nospeech:
            return False
    return True
