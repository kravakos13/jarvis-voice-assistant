"""Voice key emulation (1.0.8): «нажми энтер», «нажми пять», «нажми а», «нажми контрол цэ», «альт таб»,
«нажми эф пять», «нажми вниз три раза». Keys go to the user's window (never Jarvis's own) via SendInput."""
from __future__ import annotations

from typing import Callable, Optional

from .. import keyinput, keyspeech
from .base import Skill, ToolResult

# combos that are never sent by voice: they log off / lock / kill the session or wipe things without undo
FORBIDDEN = {"ctrl+alt+delete", "win+l"}


class KeysSkill(Skill):
    name = "keys"

    def __init__(self, ctx, press: Callable[..., bool] = keyinput.press,
                 type_text: Callable[[str], bool] = keyinput.type_text):
        super().__init__(ctx)
        self.press, self.type_text = press, type_text

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        kp = keyspeech.parse(t, need_verb=False)
        if kp is None:
            return None
        if kp.combo in FORBIDDEN:
            return ToolResult("Эту комбинацию голосом не нажимаю.")
        wm = self.ctx.extra.get("wm")
        if wm is None or getattr(wm, "b", None) is None:
            return ToolResult("Нажимать клавиши я умею только в Windows.")
        if keyinput.focus_target(wm) is None:
            return ToolResult("Не вижу окна, куда нажать.")
        if kp.text:
            ok = all(self.type_text(kp.text) for _ in range(kp.times))
        else:
            ok = self.press(kp.combo, kp.times)
        return ToolResult("" if ok else "Не получилось нажать клавиши.")
