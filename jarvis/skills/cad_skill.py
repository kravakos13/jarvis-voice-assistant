"""3D models: OpenSCAD / CadQuery."""
from __future__ import annotations

import re
from typing import Optional

from ..cad import CadHelper
from .base import Skill, ToolResult, fn, s

CAD_RE = re.compile(r"\b(смоделируй|замоделируй|сделай|создай|нарисуй|спроектируй|сгенерируй)\b.*\b(3д|3d|три д|трехмерн\w*|модел\w*|деталь|детали|корпус\w*|stl|степ|step|чертеж\w*)\b")


class CadSkill(Skill):
    name = "cad"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.helper = CadHelper(ctx.cfg.get("cad", {}), ctx.llm)

    def _make(self, description: str, kind: str = "auto") -> ToolResult:
        def work():
            return CadHelper.spoken(self.helper.make_model(description, kind))
        return ToolResult("Принял, моделирую. Это может занять пару минут.", then=work)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if CAD_RE.search(t) and not re.search(r"\b(скетч|прошивк)", t):
            return self._make(text)
        return None

    def tools(self):
        return [fn("make_3d_model", "Создать 3D-модель по описанию (OpenSCAD -> STL, или CadQuery -> STEP для SolidWorks).",
                   {"description": s("описание детали с размерами"), "format": s("формат", ["scad", "cadquery"])},
                   ["description"])]

    def call(self, name, args):
        if name == "make_3d_model":
            return self._make(str(args.get("description", "")), str(args.get("format") or "auto"))
        return None
