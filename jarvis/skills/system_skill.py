"""System: volume, lock / sleep / restart / shutdown (with confirmation), screenshot, PC status."""
from __future__ import annotations

import datetime as _dt
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .. import platform_utils as pu
from ..planner import words_to_digits
from .core import pct, units
from .base import Confirm, Skill, ToolResult, fn, s

log = logging.getLogger(__name__)

VOL_SET_RE = re.compile(r"\b(?:громкость|звук)\b.*?\b(?:на|в)?\s*(\d{1,3})\s*(?:процент\w*|%)?")
VOL_UP_RE = re.compile(r"^(?:сделай |сделай звук |звук |еще |ещё |чуть |немного |можно )*(?:по)?громче(?: сделай| звук| еще| ещё)?$"
                       r"|^(?:прибавь|подними|увеличь|подбавь|добавь|повысь)(?: звук| громкость)?(?: немного| чуть)?$")
VOL_DOWN_RE = re.compile(r"^(?:сделай |сделай звук |звук |еще |ещё |чуть |немного |можно )*(?:по)?тише(?: сделай| звук| еще| ещё)?$"
                         r"|^(?:убавь|опусти|уменьши|понизь|приглуши)(?: звук| громкость)?(?: немного| чуть)?$")
MUTE_RE = re.compile(r"^(выключи звук|без звука|отключи звук|замьють|мьют|убери звук)$")
UNMUTE_RE = re.compile(r"^(включи звук|верни звук|размьють)$")
LOCK_RE = re.compile(r"\b(заблокируй|блокируй|залочь)\b.*\b(компьютер|пк|экран|комп)\b|^блокировка$")
SLEEP_RE = re.compile(r"\b(спящий режим|в сон|усыпи|отправь в сон|режим сна)\b")
REBOOT_RE = re.compile(r"\b(перезагрузи|перезагрузка|ребут)\b")
SHUTDOWN_RE = re.compile(r"\b(выключи|выруби|заверши работу)\b.*\b(компьютер|пк|комп|систему)\b")
SHUT_TIMER_RE = re.compile(r"\b(выключи|выруби)\b.*\b(компьютер|пк|комп)\b\s+(через|в)\s+(.+)$")
ABORT_RE = re.compile(r"\bотмени (выключение|перезагрузку)\b")
SHOT_RE = re.compile(r"\b(скриншот|снимок экрана|скрин)\b")
STATUS_RE = re.compile(r"\b(загрузка|нагрузка|состояние|статус)\b.*\b(процессор\w*|пк|компьютер\w*|системы|памят\w*|видеокарт\w*|цп)\b|\bтемпература видеокарт\w*\b|\bсколько (свободной )?памяти\b")


def pictures_dir() -> Path:
    if pu.IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 39, None, 0, buf) == 0 and buf.value:  # CSIDL_MYPICTURES
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Pictures"


# ---------------------------------------------------------------- volume
def _endpoint():
    from pycaw.pycaw import AudioUtilities
    dev = AudioUtilities.GetSpeakers()
    ev = getattr(dev, "EndpointVolume", None)          # pycaw >= 20240316
    if ev is not None:
        return ev
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def set_volume(percent: int) -> bool:
    percent = max(0, min(100, int(percent)))
    try:
        ep = _endpoint()
        ep.SetMute(0, None)
        ep.SetMasterVolumeLevelScalar(percent / 100.0, None)
        return True
    except Exception as e:
        log.debug("pycaw: %s - использую медиаклавиши", e)
    if not pu.IS_WIN:
        return False
    pu.press_media_key("volume_down", 50)       # each key = 2%
    return pu.press_media_key("volume_up", percent // 2)


def set_mute(mute: bool) -> bool:
    try:
        _endpoint().SetMute(1 if mute else 0, None)
        return True
    except Exception:
        return pu.press_media_key("mute")


# ---------------------------------------------------------------- power
def _power(action: str) -> str:
    if not pu.IS_WIN:
        return "Управление питанием доступно только в Windows."
    if action == "lock":
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return "Заблокировал."
    if action == "sleep":
        import ctypes
        ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
        return "Спокойной ночи."
    if action == "restart":
        subprocess.run(["shutdown", "/r", "/t", "10"], check=False)
        return "Перезагружаю через 10 секунд. Скажи «отмени перезагрузку», если передумал."
    if action == "shutdown":
        subprocess.run(["shutdown", "/s", "/t", "10"], check=False)
        return "Выключаю через 10 секунд. Скажи «отмени выключение», если передумал."
    if action == "abort":
        subprocess.run(["shutdown", "/a"], check=False)
        return "Отменил."
    return "Не понял."


def screenshot() -> Optional[Path]:
    out = pictures_dir() / "Jarvis Screenshots"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"screenshot_{_dt.datetime.now():%Y-%m-%d_%H-%M-%S}.png"
    try:
        from PIL import ImageGrab
        ImageGrab.grab(all_screens=True).save(path)
        return path
    except Exception as e:
        log.error("Скриншот: %s", e)
        return None


def pc_status() -> str:
    parts = []
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        parts.append(f"Процессор загружен на {pct(cpu)}")
        parts.append(f"память занята на {pct(mem.percent)}, свободно {mem.available / 2**30:.1f} гигабайта")
    except Exception as e:
        log.debug("psutil: %s", e)
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            r = subprocess.run([smi, "--query-gpu=name,temperature.gpu,utilization.gpu",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
            name, temp, util = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            parts.append(f"видеокарта {name}: {units(temp, 'градус', 'градуса', 'градусов')}, загрузка {pct(util)}")
        except Exception as e:
            log.debug("nvidia-smi: %s", e)
    return (". ".join(parts) + ".") if parts else "Не получилось получить данные о системе."


class SystemSkill(Skill):
    name = "system"

    def shutdown_timer(self, kind: str, spec: str) -> ToolResult:
        from .timers_skill import parse_duration, say_duration
        if kind == "через":
            secs = parse_duration(spec)
        else:
            mm = re.match(r"(\d{1,2})(?:[:. ](\d{2}))?", spec)
            if not mm:
                return ToolResult("Не понял время выключения.")
            now = _dt.datetime.now()
            when = now.replace(hour=int(mm.group(1)) % 24, minute=int(mm.group(2) or 0), second=0, microsecond=0)
            if when <= now:
                when += _dt.timedelta(days=1)
            secs = int((when - now).total_seconds())
        if secs < 30 or secs > 86400 * 3:
            return ToolResult("Не понял, через сколько выключить.")
        human = say_duration(secs) if kind == "через" else spec

        def go():
            if not pu.IS_WIN:
                return ToolResult("Таймер выключения работает только в Windows.")
            subprocess.run(["shutdown", "/s", "/t", str(secs)], check=False)
            import threading
            if secs > 70:     # spoken warning one minute before
                def warn():
                    self.ctx.notify("Джарвис", "Компьютер выключится через минуту")
                    self.ctx.say("Через минуту выключаю компьютер. Скажи «отмени выключение», если передумал.", "critical")
                t = threading.Timer(secs - 60, warn)
                t.daemon = True
                t.start()
                self._shutdown_warn = t
            return ToolResult(f"Окей, выключу компьютер {('через ' + human) if kind == 'через' else 'в ' + human}.")
        return ToolResult("", confirm=Confirm(f"Выключить компьютер {('через ' + human) if kind == 'через' else 'в ' + human}? Да или нет?", go))

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        t2 = words_to_digits(t)
        m = VOL_SET_RE.search(t2)
        if m and re.search(r"\b(поставь|сделай|установи|громкость на|звук на|громкость|убавь|прибавь)\b", t2):
            n = int(m.group(1))
            return ToolResult(f"Громкость {pct(min(n, 100))}." if set_volume(n) else "Не получилось поменять громкость.")
        if VOL_UP_RE.match(t):
            return ToolResult("Громче." if pu.press_media_key("volume_up", 5) else "Не могу поменять громкость.")
        if VOL_DOWN_RE.match(t):
            return ToolResult("Тише." if pu.press_media_key("volume_down", 5) else "Не могу поменять громкость.")
        if MUTE_RE.match(t):
            return ToolResult("Звук выключен." if set_mute(True) else "Не получилось.")
        if UNMUTE_RE.match(t):
            return ToolResult("Звук включен." if set_mute(False) else "Не получилось.")
        if ABORT_RE.search(t):
            w = getattr(self, "_shutdown_warn", None)
            if w:
                w.cancel()
            return ToolResult(_power("abort"))
        if LOCK_RE.search(t):
            return ToolResult(_power("lock"))
        if SLEEP_RE.search(t):
            return ToolResult("", confirm=Confirm("Точно отправить компьютер в сон? Да или нет?", lambda: ToolResult(_power("sleep"))))
        if REBOOT_RE.search(t) and re.search(r"компьютер|пк|комп|систему|^перезагрузи$|^перезагрузка$", t):
            return ToolResult("", confirm=Confirm("Точно перезагрузить компьютер? Да или нет?", lambda: ToolResult(_power("restart"))))
        m = SHUT_TIMER_RE.search(words_to_digits(t))
        if m:
            return self.shutdown_timer(m.group(3), m.group(4))
        if SHUTDOWN_RE.search(t):
            return ToolResult("", confirm=Confirm("Точно выключить компьютер? Да или нет?", lambda: ToolResult(_power("shutdown"))))
        if SHOT_RE.search(t) and re.search(r"\b(сделай|сними|скрин|скриншот)\b", t):
            p = screenshot()
            return ToolResult("Скриншот сохранил в картинки." if p else "Не получилось сделать скриншот.")
        if STATUS_RE.search(t):
            return ToolResult(pc_status())
        return None

    def tools(self):
        return [
            fn("set_volume", "Установить системную громкость в процентах.", {"percent": {"type": "integer"}}, ["percent"]),
            fn("power", "Блокировка, сон, перезагрузка или выключение ПК (спросит подтверждение).",
               {"action": s("действие", ["lock", "sleep", "restart", "shutdown"])}, ["action"]),
            fn("pc_status", "Загрузка процессора, памяти и температура видеокарты.", {}),
            fn("screenshot", "Сделать скриншот экрана.", {}),
        ]

    def call(self, name, args):
        if name == "set_volume":
            n = int(args.get("percent", 50))
            return ToolResult(f"Громкость {pct(n)}." if set_volume(n) else "Не получилось.")
        if name == "power":
            a = str(args.get("action", ""))
            if a == "lock":
                return ToolResult(_power("lock"))
            if a in ("sleep", "restart", "shutdown"):
                q = {"sleep": "отправить компьютер в сон", "restart": "перезагрузить компьютер", "shutdown": "выключить компьютер"}[a]
                return ToolResult("", confirm=Confirm(f"Точно {q}? Да или нет?", lambda: ToolResult(_power(a))))
        if name == "pc_status":
            return ToolResult(pc_status())
        if name == "screenshot":
            p = screenshot()
            return ToolResult("Скриншот сохранил." if p else "Не получилось сделать скриншот.")
        return None
