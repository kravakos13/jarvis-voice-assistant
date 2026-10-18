"""Data-driven hotkey commands (jarvis/commands.yaml + %LOCALAPPDATA%/Jarvis/commands.yaml), fast path, no LLM."""
from __future__ import annotations

import logging
import re
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from .. import keyinput
from ..windows import BROWSERS
from ..ytpick import CARDINALS, _ordinal
from .base import Skill, ToolResult

log = logging.getLogger(__name__)
BUILTIN = Path(__file__).resolve().parent.parent / "commands.yaml"
LEAD = r"(?:(?:ну|давай|пожалуйста|джарвис|а|слушай)\s+)*"
TAIL = r"(?:\s+(?:пожалуйста|плиз|быстро))?"


def _user_file() -> Optional[Path]:
    """Next to settings.local.yaml (the install folder, %LOCALAPPDATA%\\Jarvis)."""
    try:
        from ..config import LOCAL_SETTINGS
        return LOCAL_SETTINGS.parent / "commands.yaml"
    except Exception:                                    # noqa: BLE001
        return None


def load_commands(extra: Optional[Path] = None) -> list[dict]:
    """Built-in table, then the user's file: same id replaces, new ids are added. Broken entries are skipped."""
    out: dict[str, dict] = {}
    for f in (BUILTIN, extra if extra is not None else _user_file()):
        if not f or not Path(f).exists():
            continue
        try:
            data = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or []
        except Exception as e:                           # noqa: BLE001
            log.error("commands.yaml (%s): %s", f, e)
            continue
        for c in data if isinstance(data, list) else []:
            try:
                cid = str(c["id"])
                rx = [re.compile(rf"^{LEAD}(?:{p}){TAIL}$") for p in c.get("phrases") or []]
                if not rx or not (c.get("keys") or c.get("action")):
                    raise ValueError("нужны phrases и keys/action")
                out[cid] = dict(c, _rx=rx, scope=str(c.get("scope") or "any"))
            except Exception as e:                       # noqa: BLE001
                log.error("commands.yaml (%s): команда %r пропущена: %s", f, c, e)
    return list(out.values())


def ordinal(word: str) -> Optional[int]:
    w = (word or "").strip()
    if w in CARDINALS:
        return CARDINALS[w]
    if w in ("последнюю", "последняя", "последний", "последнее"):
        return -1
    return _ordinal(w)


def site_url(q: str) -> str:
    q = q.strip().rstrip(".")
    words = q.split()
    tld = {"ru", "com", "org", "net", "io", "рф", "ua", "by", "su", "me", "dev", "info", "tv", "kz", "app"}
    if len(words) >= 2 and words[-1] in tld and all(re.fullmatch(r"[\w-]+", w) for w in words):
        q = ".".join(words)                                # normalize() turned «ru.wikipedia.org» into words
    if re.fullmatch(r"[\w.-]+\.[a-zа-я]{2,}(?:/\S*)?", q):
        return q if q.startswith("http") else f"https://{q}"
    # «хабр» -> first result («I'm feeling lucky» via DuckDuckGo, no API key)
    return "https://duckduckgo.com/?q=" + urllib.parse.quote(f"!ducky {q}")


class HotkeysSkill(Skill):
    name = "hotkeys"

    def __init__(self, ctx, commands: Optional[list] = None, press: Callable[[str], bool] = keyinput.press,
                 opener: Callable[[str], Any] = webbrowser.open, links: Optional[Callable[[int], list]] = None,
                 invoke: Optional[Callable[[Any], bool]] = None):
        super().__init__(ctx)
        self.commands = commands if commands is not None else load_commands()
        self.press, self.opener = press, opener
        if links is None or invoke is None:
            from .. import uia
            links = links or (lambda h: uia.result_links(uia.page_links(h)))
            invoke = invoke or uia.invoke
        self.links, self.invoke = links, invoke

    def _target(self):
        wm = self.ctx.extra.get("wm")
        return wm.active() if wm is not None and getattr(wm, "b", None) is not None else None

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        t = t.replace("ё", "е")
        for c in self.commands:
            m = next((m for rx in c["_rx"] if (m := rx.match(t))), None)
            if m is None:
                continue
            w = self._target()
            browser = w is not None and w.exe in BROWSERS
            if c["scope"] == "browser" and not browser:
                continue                                  # «назад»/«обнови» elsewhere: other skills / LLM
            if c.get("action") != "open_site" and w is None:
                continue                                  # unknown target: never type into Jarvis itself
            res = self.run(c, m.groupdict(), w)
            if res is not None:
                return res
        return None

    def run(self, c: dict, g: dict, w) -> Optional[ToolResult]:
        action = c.get("action")
        reply = str(c.get("reply") or "")
        if action == "open_site":
            q = (g.get("q") or "").strip()
            self.opener(site_url(q))
            return ToolResult(reply.format(q=q) if reply else "")
        if action in ("tab_n", "result_link"):
            n = ordinal(g.get("n") or "")
            if n is None or n == 0:
                return None
            if action == "tab_n":
                if not (n == -1 or 1 <= n <= 8):
                    return ToolResult("Могу переключить только вкладки с первой по восьмую и последнюю.")
                return self._keys(w, "ctrl+9" if n == -1 else f"ctrl+{n}", reply)
            return ToolResult("", then=lambda: self._result_link(w, n), background=True)
        keys = c.get("keys_firefox") if (w is not None and w.exe == "firefox.exe" and c.get("keys_firefox")) \
            else c.get("keys")
        return self._keys(w, str(keys), reply, int(c.get("times") or 1))

    def _keys(self, w, combo: str, reply: str, times: int = 1) -> ToolResult:
        wm = self.ctx.extra.get("wm")
        if keyinput.focus_target(wm) is None and wm is not None and getattr(wm, "b", None) is not None:
            return ToolResult("Не вижу окна, куда нажать.")
        ok = all(self.press(combo) for _ in range(max(1, times)))
        return ToolResult(reply if ok else "Не получилось нажать клавиши (только в Windows).")

    def _result_link(self, w, n: int) -> str:
        items = self.links(w.hwnd)
        if not items:
            return "Не вижу ссылок на этой странице."
        it = items[n] if n < 0 and -n <= len(items) else (items[n - 1] if 0 < n <= len(items) else None)
        if it is None:
            return f"На экране всего {len(items)} ссылок."
        title = it["title"][:80]
        return f"Открываю: {title}." if self.invoke(it["el"]) else f"Не получилось нажать на «{title}»."
