"""«Что я умею»: the phrase list shown in Settings and summarised by voice. Hotkey commands come from
commands.yaml, so user-added commands appear here automatically."""
from __future__ import annotations

from typing import Optional

STATIC: list[tuple[str, list[str]]] = [
    ("Музыка и видео", [
        "включи <песню / исполнителя> — YouTube Music", "найди на ютубе <запрос>",
        "включи второе / третье / последнее видео, номер 2, следующее видео — из открытого списка или ленты YouTube",
        "пауза, поставь на паузу, продолжи, воспроизведи", "стоп (если играет музыка — пауза, иначе выход)",
        "выключи музыку / видео, выключи (если что-то играет)", "следующее, предыдущее, дальше",
        "громче, тише, без звука, громкость 40%", "сделай погромче / потише"]),
    ("Окна", [
        "открой <программу или сайт>", "закрой <программу / ютуб / вкладку / это> — с проверкой",
        "сверни, разверни, сверни всё", "переключись на <хром / телеграм / …>"]),
    ("Проводник", [
        "открой второй файл, открой третью папку, открой второе (если проводник впереди)",
        "открой файл номер 12, открой последнюю папку", "назад, вверх, на уровень выше"]),
    ("Текст", [
        "напиши <текст> — напечатаю в активное окно", "пиши / диктовка — режим диктовки без имени",
        "в диктовке: новая строка, абзац, удали слово, отправь, хватит / стоп диктовка (сама выключится через 30 с тишины)"]),
    ("Система", [
        "скриншот", "заблокируй компьютер", "выключи / перезагрузи компьютер — спрошу подтверждение",
        "микрофон: «Перезапустить прослушку» в меню трея, если я перестал реагировать"]),
    ("Программы", [
        "открой / закрой телеграм, дискорд, стим (по списку установленных программ)",
        "Spotify, VLC и другие плееры — пауза / следующий / громкость через медиаклавиши"]),
    ("Остальное", [
        "который час, погода, таймер на 5 минут, напомни …, запиши заметку …",
        "что нового в телеграме, напиши <кому> в телеграм <текст> (с подтверждением)",
        "что ты умеешь — этот список"]),
]


def sections(commands: Optional[list] = None) -> list[tuple[str, list[str]]]:
    if commands is None:
        try:
            from .skills.hotkeys_skill import load_commands
            commands = load_commands()
        except Exception:                                # noqa: BLE001
            commands = []
    out = [(t, list(p)) for t, p in STATIC]
    extra: dict[str, list[str]] = {}
    for c in commands:
        ex = str(c.get("example") or "").strip()
        if ex:
            extra.setdefault(str(c.get("section") or "Ещё"), []).append(ex)
    for title, items in extra.items():
        for t, lst in out:
            if t == title:
                lst.extend(items)
                break
        else:
            out.append((title, items))
    return out


def as_html(commands: Optional[list] = None) -> str:
    parts = ["<p>Говорите «Джарвис, …». Все команды ниже работают сразу, без ИИ. Свои горячие клавиши можно "
             "добавить в <b>commands.yaml</b> рядом с settings.local.yaml.</p>"]
    for title, items in sections(commands):
        parts.append(f"<h3>{title}</h3><ul>" + "".join(f"<li>{i.replace('<', '&lt;')}</li>" for i in items) + "</ul>")
    return "".join(parts)


def spoken_summary() -> str:
    return ("Управляю музыкой и YouTube, окнами, проводником и браузером, печатаю текст, ставлю таймеры и "
            "напоминания, отвечаю на вопросы. Полный список — в настройках, вкладка «Что я умею».")
