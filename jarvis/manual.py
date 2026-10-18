"""User manual (1.0.8): docs/ИНСТРУКЦИЯ.md. The command tables are GENERATED from COMMANDS below + commands.yaml
(`python -m jarvis.manual --write`), and tests/test_manual.py routes every example phrase to its documented module,
so the manual cannot drift from the real behaviour. `python -m jarvis.manual --open` renders HTML and opens it
(tray «Инструкция», Start menu shortcut)."""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
MD = ROOT / "docs" / "ИНСТРУКЦИЯ.md"
BEGIN, END = "<!-- КОМАНДЫ:НАЧАЛО (генерируется: python -m jarvis.manual --write) -->", "<!-- КОМАНДЫ:КОНЕЦ -->"

# (section, phrase said AFTER «Джарвис, », module that handles it ("llm" = the AI), no-name?, note)
# module names = Skill.name (router.last_skill). Windows-only actions still route on Linux (reply «только в Windows»).
COMMANDS: list[tuple[str, str, str, bool, str]] = [
    # --- приложения и окна
    ("Приложения и окна", "открой стим", "apps", True, "любая установленная программа или игра (Steam, Epic)"),
    ("Приложения и окна", "открой ютуб", "media", True, "ютуб и ютуб музыка открываются своими окнами"),
    ("Приложения и окна", "закрой ютуб", "windows", True, "«Закрыл» — только после проверки, что окно исчезло"),
    ("Приложения и окна", "закрой хром", "windows", True, ""),
    ("Приложения и окна", "закрой", "windows", True, "окно, которое было впереди, когда ты начал говорить; "
     "также «закрой это / окно / текущее». В браузере — всё окно. Игры не закрывает никогда"),
    ("Приложения и окна", "закрой весь проводник", "windows", True, "все окна программы сразу: «закрой все окна "
     "хрома», «закрой весь ютуб» (все вкладки ютуба), «закрой все окна» — все окна активной программы"),
    ("Приложения и окна", "закрой калькулятор", "windows", True, "и другие приложения Windows: Параметры, Фото, "
     "Медиаплеер, Microsoft Store, Блокнот, Терминал"),
    ("Приложения и окна", "закрой стим", "windows", True, "программы, которые живут у часов, выходят полностью: "
     "Steam, Discord, Telegram, Epic, WiFi Mouse («закрой вайфай маус»). Если идёт игра из Steam — сначала спросит"),
    ("Приложения и окна", "закрой киберпанк", "windows", False, "игру по названию — только с именем и после «да»; "
     "без имени Джарвис подскажет, как сказать"),
    ("Приложения и окна", "закрой игру", "windows", False, "игру, что сейчас открыта — с именем и после «да»"),
    ("Приложения и окна", "сверни", "windows", True, "активное окно"),
    ("Приложения и окна", "сверни хром", "windows", True, ""),
    ("Приложения и окна", "разверни ютуб", "windows", True, ""),
    ("Приложения и окна", "сверни все окна", "hotkeys", True, "то же: «покажи рабочий стол»"),
    ("Приложения и окна", "какие игры установлены", "apps", False, ""),
    # --- переключение
    ("Переключись / вернись", "переключись на хром", "windows", True, "окно; восстанавливается, если свёрнуто"),
    ("Переключись / вернись", "переключись на телеграм", "windows", True, ""),
    ("Переключись / вернись", "вернись на ютуб", "windows", True, "окно или фоновая вкладка браузера; если ютуба нет — откроет его"),
    ("Переключись / вернись", "вернись в хром", "windows", True, "то же: «верни на», «вернёмся в», «давай обратно на»"),
    ("Переключись / вернись", "вернись", "windows", True, "в предыдущее окно"),
    # --- браузер
    ("Браузер", "новая вкладка", "hotkeys", True, "Chrome, Edge, Firefox, Яндекс — окно браузера впереди"),
    ("Браузер", "закрой вкладку", "windows", True, ""),
    ("Браузер", "верни вкладку", "hotkeys", True, "открыть закрытую вкладку"),
    ("Браузер", "вторая вкладка", "hotkeys", True, "любая по номеру: «на третью вкладку», «вкладка три», "
     "«десятая вкладка», «последняя вкладка»"),
    ("Браузер", "перейди на пятую вкладку", "hotkeys", True, "и «открой пятую вкладку», «третью вкладку открой»"),
    ("Браузер", "третья", "hotkeys", True, "просто номер, если впереди браузер; «третье» — видео или файл"),
    ("Браузер", "следующая вкладка", "hotkeys", True, "и «предыдущая вкладка»"),
    ("Браузер", "назад", "nav", True, "в браузере, проводнике, YouTube Music, Discord и других Chromium-приложениях"),
    ("Браузер", "вперёд", "nav", True, ""),
    ("Браузер", "назад три раза", "nav", True, "«дважды назад», «назад 2 раза»"),
    ("Браузер", "обнови", "hotkeys", True, ""),
    ("Браузер", "листай вниз", "hotkeys", True, "и «листай вверх», «в начало», «в конец»"),
    ("Браузер", "увеличь масштаб", "hotkeys", True, "«уменьши масштаб», «обычный масштаб»"),
    ("Браузер", "на весь экран", "view", True, "браузер на весь экран (F11); на ютубе — видео на весь экран"),
    ("Браузер", "инкогнито", "hotkeys", True, ""),
    ("Браузер", "открой сайт wikipedia.org", "core", True, "по адресу; «открой сайт хабр» — по названию"),
    ("Браузер", "открой вторую ссылку", "hotkeys", True, "по порядку результатов поиска на странице"),
    # --- ютуб / музыка
    ("YouTube и музыка", "включи Imagine Dragons", "media", True, "YouTube Music; без имени — коротко и чётко"),
    ("YouTube и музыка", "включи на ютубе обзор RTX 5070", "media", True, ""),
    ("YouTube и музыка", "найди на ютубе рецепт пиццы", "media", True, ""),
    ("YouTube и музыка", "включи второе видео", "media", True, "из открытого списка, канала, плейлиста или ленты"),
    ("YouTube и музыка", "включи последнее видео", "media", True, "и «номер 3», «следующее видео»"),
    ("YouTube и музыка", "полный экран", "view", True, "видео на весь экран; «разверни видео»"),
    ("YouTube и музыка", "обычный экран", "view", True, "выйти из полного экрана: «малый экран», «сверни видео»"),
    ("YouTube и музыка", "кинотеатр", "view", True, "широкий плеер; «мини-плеер», «субтитры»"),
    ("YouTube и музыка", "пауза", "media", True, "«поставь на паузу»"),
    ("YouTube и музыка", "продолжи", "media", True, "«воспроизведи»"),
    ("YouTube и музыка", "следующий трек", "media", True, "«предыдущий трек»"),
    ("YouTube и музыка", "выключи музыку", "windows", True, "если играет — пауза, иначе закрывает YouTube Music"),
    ("YouTube и музыка", "стоп", "core", True, "если что-то играет — пауза; иначе (с именем) Джарвис закрывается. "
     "Пока Джарвис думает, «стоп» отменяет ответ"),
    # --- перемотка
    ("Перемотка", "перемотай назад", "nav", True, "на 10 секунд (YouTube и YouTube Music)"),
    ("Перемотка", "вперёд на 30 секунд", "nav", True, ""),
    ("Перемотка", "назад на минуту", "nav", True, ""),
    # --- проводник
    ("Проводник", "открой второй файл", "explorer", True, "в открытом окне проводника, по видимому порядку"),
    ("Проводник", "открой третью папку", "explorer", True, ""),
    ("Проводник", "открой второе", "explorer", True, "любой элемент; если впереди проводник"),
    ("Проводник", "вверх", "explorer", True, "на папку выше; «назад» — Alt+←"),
    # --- клавиши
    ("Клавиши «нажми»", "нажми энтер", "keys", True, "также «нажал», «жми», «жмакни», «клацни», «тыцни»"),
    ("Клавиши «нажми»", "нажми пять", "keys", True, "цифры 0–9 словами"),
    ("Клавиши «нажми»", "нажми эй", "keys", True, "латинские буквы: эй, би, цэ, дэ…; «нажми я» печатает «я»"),
    ("Клавиши «нажми»", "нажми эф пять", "keys", True, "F1–F12"),
    ("Клавиши «нажми»", "нажми контрол цэ", "keys", True, "сочетания: «ctrl c», «контрол шифт эскейп»"),
    ("Клавиши «нажми»", "альт таб", "keys", True, "сочетание с Alt/Shift/Win можно без «нажми»"),
    ("Клавиши «нажми»", "нажми вниз три раза", "keys", True, "повтор до 20 раз"),
    ("Клавиши «нажми»", "нажми пробел", "keys", True, "эскейп, таб, бэкспейс, делит, хоум, энд, пейдж ап/даун"),
    # --- текст
    ("Набор текста", "напиши привет, как дела?", "dictate", True, "печатает в активное окно, с пунктуацией"),
    ("Набор текста", "пиши", "dictate", True, "диктовка: всё сказанное печатается"),
    ("Набор текста", "копируй", "hotkeys", True, "«вставь», «выдели всё», «отмени», «сохрани»"),
    # --- громкость и система
    ("Громкость и система", "громче", "system", True, "«тише», «без звука»"),
    ("Громкость и система", "громкость 40", "system", True, ""),
    ("Громкость и система", "скриншот", "system", True, ""),
    ("Громкость и система", "заблокируй компьютер", "system", False, ""),
    ("Громкость и система", "выключи компьютер", "system", False, "только после «да»"),
    ("Громкость и система", "перезагрузи компьютер", "system", False, "только после «да»"),
    ("Громкость и система", "какая загрузка процессора", "system", False, ""),
    ("Громкость и система", "который час", "core", False, ""),
    ("Громкость и система", "сколько будет 15 умножить на 7", "calc", False, ""),
    ("Громкость и система", "посчитай 15 процентов от 200", "calc", False, "и «посчитай 2 плюс 2»"),
    # --- файлы
    ("Файлы", "открой файл отчёт за март", "files", False, "ищет по имени на компьютере, сначала на рабочем столе, в "
     "документах и загрузках; нашлось несколько — назовёт три, скажи «второй». Без имени тоже можно, но только "
     "если такой файл точно есть"),
    ("Файлы", "открой папку проекты", "files", False, "без имени — тоже, если папка есть"),
    ("Файлы", "найди файл резюме", "files", False, "список найденного; «открой первый»"),
    # --- заметки, задачи, таймеры
    ("Заметки, задачи, таймеры", "запиши заметку купить подарок маме", "notes", False, "файлы в Документы\\Jarvis\\Notes"),
    ("Заметки, задачи, таймеры", "прочитай последнюю заметку", "notes", False, ""),
    ("Заметки, задачи, таймеры", "напомни через 20 минут проверить духовку", "planner", False, ""),
    ("Заметки, задачи, таймеры", "какие задачи на завтра", "planner", False, ""),
    ("Заметки, задачи, таймеры", "поставь таймер на 5 минут", "timers", False, "«сколько осталось», «отмени таймер»"),
    # --- погода и тревоги
    ("Погода и тревоги", "какая погода", "weather", False, "«погода завтра», «погода в Киеве»"),
    ("Погода и тревоги", "есть ли тревога", "alerts", False, "о тревоге и отбое Джарвис предупреждает сам"),
    # --- интернет
    ("Интернет", "найди в интернете когда выходит GTA 6", "web", True, "с именем — ответ вслух со ссылками; без "
     "имени — откроет поиск в браузере"),
    ("Интернет", "найди в хроме рецепт борща", "core", True, "поиск в браузере; «загугли …», «найди в гугле …»"),
    ("Интернет", "найди на ютубе обзор RTX 5070", "media", True, "поиск на ютубе"),
    ("Интернет", "последние новости про SpaceX", "web", False, ""),
    # --- код и 3D
    ("Код и 3D", "напиши скетч для ESP32 мигать светодиодом", "code", False, ""),
    ("Код и 3D", "сделай 3D модель коробки 50 на 30 на 20", "cad", False, "нужен CadQuery"),
    # --- умный дом
    ("Умный дом", "включи свет", "smarthome", True, "только устройства из «Настройки → Интеграции → Умный дом»"),
    # --- Люмо
    ("Передать Люмо (письмом)", "передай Люмо найди билеты в Киев на субботу", "lumo", False,
     "зачитает и спросит «Отправить?»"),
    ("Передать Люмо (письмом)", "отправь это Люмо с экраном", "lumo", False, "со снимком экрана"),
    # --- режимы
    ("Режимы", "игровой режим", "modes", False, "«рабочий режим», «ночной режим»"),
    ("Режимы", "проверь себя", "diag", False, "самодиагностика"),
    ("Режимы", "собери диагностику", "diag", False, "архив с логами на рабочий стол — пришли его разработчику"),
    ("Режимы", "что ты умеешь", "core", False, "краткий список вслух; полный — «Настройки → Что я умею»"),
    # --- беседа
    ("Беседа", "давай поболтаем", "llm", False, "дальше можно без имени; выход: «хватит болтать», «пока», «тихо»"),
    ("Беседа", "расскажи интересный факт", "llm", False, "и «расскажи анекдот», «расскажи историю», «как дела»"),
]
NEEDS = {"telegram": "Telegram включается в «Настройки → Модули» и «Интеграции → Telegram → Войти»"}
TELEGRAM = [("напиши маме в телеграм что буду через час", "зачитает и спросит подтверждение"),
            ("прочитай последние сообщения от Саши", "")]


ACTIONS = {"tab_n": "`Ctrl+1…8`, последняя — `Ctrl+9`, дальше — `Ctrl+1` и `Ctrl+Tab`", "open_site": "открывает сайт в браузере",
           "result_link": "открывает N-ю ссылку результатов поиска"}


def tables_md() -> str:
    out: list[str] = []
    sec = None
    for s, phrase, _skill, nn, note in COMMANDS:
        if s != sec:
            sec = s
            out += ["", f"### {s}", "", "| Скажи «Джарвис, …» | Без имени | Что будет |", "|---|---|---|"]
        out.append(f"| {phrase} | {'да' if nn else '—'} | {note} |")
    out += ["", "### Telegram", "", f"_{NEEDS['telegram']}._", "", "| Скажи «Джарвис, …» | Без имени | Что будет |",
            "|---|---|---|"] + [f"| {p} | — | {n} |" for p, n in TELEGRAM]
    try:
        from .skills.hotkeys_skill import load_commands
        extra = [c for c in load_commands() if c.get("example")]
    except Exception:                                    # noqa: BLE001
        extra = []
    if extra:
        out += ["", "### Все горячие клавиши из commands.yaml", "",
                "| Пример | Где работает | Клавиши |", "|---|---|---|"]
        for c in extra:
            where = "браузер впереди" if c.get("scope") == "browser" else "любое окно"
            keys = ACTIONS.get(c.get("action") or "", "") or "`" + str(c.get("keys", "")) + "`"
            out.append(f"| {c['example']} | {where} | {keys} |")
    return "\n".join(out).strip() + "\n"


def write(path: Path = MD) -> None:
    s = path.read_text(encoding="utf-8")
    a, b = s.index(BEGIN) + len(BEGIN), s.index(END)
    path.write_text(s[:a] + "\n" + tables_md() + "\n" + s[b:], encoding="utf-8")


# ------------------------------------------------------------------ tiny markdown -> HTML (no extra packages)
def _inline(t: str) -> str:
    t = html.escape(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])_([^_]+)_(?!\w)", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|#[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


def _anchor(title: str) -> str:
    return re.sub(r"[^\w-]+", "-", title.lower()).strip("-")


def to_html(md: str) -> str:
    lines, out, i = md.splitlines(), [], 0
    para: list[str] = []

    def flush():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("<!--"):
            i += 1
            continue
        if ln.startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            flush()
            n, title = len(m.group(1)), m.group(2)
            out.append(f'<h{n} id="{_anchor(title)}">{_inline(title)}</h{n}>')
        elif ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[-| :]+\|$", lines[i + 1]):
            flush()
            head = [c.strip() for c in ln.strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            out.append("<table><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in head) + "</tr>" + "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows) + "</table>")
            continue
        elif re.match(r"^\s*([*-]|\d+\.)\s+", ln):
            flush()
            tag = "ol" if re.match(r"^\s*\d+\.", ln) else "ul"
            items = []
            while i < len(lines) and re.match(r"^\s*([*-]|\d+\.)\s+", lines[i]):
                item = re.sub(r"^\s*([*-]|\d+\.)\s+", "", lines[i])
                i += 1
                while i < len(lines) and lines[i].startswith("  ") and lines[i].strip() and \
                        not re.match(r"^\s*([*-]|\d+\.)\s+", lines[i]):
                    item += " " + lines[i].strip()
                    i += 1
                items.append(f"<li>{_inline(item)}</li>")
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        elif ln.startswith(">"):
            flush()
            out.append("<blockquote>" + _inline(ln.lstrip("> ")) + "</blockquote>")
        elif ln.strip() == "---":
            flush()
            out.append("<hr>")
        elif not ln.strip():
            flush()
        else:
            para.append(ln.strip())
        i += 1
    flush()
    css = ("body{font-family:Segoe UI,Arial,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;line-height:1.5;"
           "color:#e8f4ff;background:#0b1220}a{color:#46dcff}h1,h2,h3{color:#46dcff}table{border-collapse:collapse;"
           "width:100%;margin:8px 0 18px}td,th{border:1px solid #24405a;padding:6px 8px;vertical-align:top}"
           "th{background:#12253a}code{background:#12253a;padding:1px 4px;border-radius:3px}"
           "pre{background:#12253a;padding:10px;overflow:auto}blockquote{border-left:3px solid #46dcff;margin:8px 0;padding:4px 12px;background:#0f1b2c}")
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Джарвис — инструкция</title>'
            f"<style>{css}</style></head><body>" + "\n".join(out) + "</body></html>")


def html_path() -> Path:
    from .config import LOCAL_SETTINGS                  # %LOCALAPPDATA%\Jarvis (not the user's Documents)
    return LOCAL_SETTINGS.parent / "cache" / "ИНСТРУКЦИЯ.html"


def build_html(md_path: Path = MD, dest: Optional[Path] = None) -> Path:
    dest = dest or html_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(to_html(md_path.read_text(encoding="utf-8")), encoding="utf-8")
    return dest


def open_manual() -> bool:
    try:
        p = build_html()
    except Exception:                                    # noqa: BLE001 — read-only data dir etc.
        import tempfile
        p = build_html(dest=Path(tempfile.gettempdir()) / "Jarvis-ИНСТРУКЦИЯ.html")
    import webbrowser
    return bool(webbrowser.open(p.as_uri()))


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
        print("tables updated:", MD)
    if "--html" in sys.argv:
        print(build_html(dest=ROOT / "docs" / "ИНСТРУКЦИЯ.html"))
    if "--open" in sys.argv:
        open_manual()
