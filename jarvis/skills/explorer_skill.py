"""«открой второй файл / третью папку / второе», «назад», «вверх» in Windows Explorer (fast path, no LLM)."""
from __future__ import annotations

from typing import Optional

from ..explorer import Explorer, ItemPick, parse
from .base import Skill, ToolResult


class ExplorerSkill(Skill):
    name = "explorer"

    def __init__(self, ctx, explorer: Optional[Explorer] = None):
        super().__init__(ctx)
        self._ex = explorer

    @property
    def ex(self) -> Explorer:
        if self._ex is None:
            self._ex = Explorer(self.ctx.extra.get("wm"))
        return self._ex

    def _run(self, w, p: ItemPick) -> ToolResult:
        return ToolResult("", then=lambda: self.ex.open(w, p), background=True)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        p = parse(t)
        if p is None:
            return None
        if p.kind == "back":
            return None                                   # 1.0.8: generic «назад» (nav skill: Alt+Left everywhere)
        if p.kind in ("back", "up"):
            w = self.ex.active()                          # only when Explorer is the window in front
            return self._run(w, p) if w is not None else None
        if p.explicit:
            w = self.ex.active() or next(iter(self.ex.windows()), None)
            if w is None:
                return ToolResult("Не вижу открытого проводника.") if p.kind in ("file", "folder") else None
            return self._run(w, p)
        w = self.ex.target_for_bare()                     # bare «открой второе»: Explorer vs YouTube by context
        return self._run(w, p) if w is not None else None
