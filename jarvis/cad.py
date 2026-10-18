"""3D modelling helper: description -> LLM -> OpenSCAD (.scad/.stl/.png) or CadQuery (.step/.stl).

Generated code is validated by actually running it; on failure the error is fed
back to the LLM once. CadQuery code is Python, so it is AST-checked against a
strict whitelist and executed in a separate process with a timeout.
"""
from __future__ import annotations

import ast
import datetime as _dt
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import platform_utils as pu
from .coding import extract_code

log = logging.getLogger(__name__)

SCAD_SYSTEM = (
    "Ты эксперт по OpenSCAD. По описанию пользователя пишешь параметрическую 3D-модель для 3D-печати. "
    "Все размеры в миллиметрах, ключевые параметры — переменные в начале файла с комментариями на русском. "
    "Используй только встроенные примитивы и модули OpenSCAD, без import(), include и use внешних файлов. "
    "Обязательно должен быть видимый объект верхнего уровня. Поставь $fn = 64; в начале. "
    "Формат ответа: одна фраза по-русски о модели, затем ОДИН блок кода ```openscad ... ```."
)
CQ_SYSTEM = (
    "Ты эксперт по CadQuery (Python). По описанию пишешь параметрическую модель. "
    "Правила: только `import cadquery as cq` и `import math`, никаких других импортов, файлов, print и exec. "
    "Размеры в мм, параметры — переменные в начале с комментариями на русском. "
    "Итоговое тело присвой переменной `result` (cq.Workplane или cq.Shape). Экспорт не делай. "
    "Формат ответа: одна фраза по-русски о модели, затем ОДИН блок кода ```python ... ```."
)


def find_openscad(override: str = "") -> Optional[str]:
    if override and Path(override).exists():
        return override
    if pu.IS_WIN:
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env)
            if base:
                for name in ("openscad.com", "openscad.exe"):  # .com prints errors to the console
                    p = Path(base) / "OpenSCAD" / name
                    if p.exists():
                        return str(p)
    return shutil.which("openscad")


# ---------------------------------------------------------------- CadQuery safety
_ALLOWED_IMPORTS = {"cadquery", "math"}
_BANNED_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "getattr", "setattr",
                 "delattr", "vars", "input", "breakpoint", "help", "exit", "quit", "memoryview", "dir", "type",
                 "object", "super", "classmethod", "staticmethod", "property"}


class UnsafeCode(ValueError):
    pass


def check_cadquery_code(code: str) -> None:
    """Raise UnsafeCode unless the script only uses cadquery/math and no dunder/IO tricks."""
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in _ALLOWED_IMPORTS:
                    raise UnsafeCode(f"import {a.name} запрещён")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in _ALLOWED_IMPORTS or node.level:
                raise UnsafeCode(f"from {node.module} import запрещён")
        elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            raise UnsafeCode(f"имя {node.id} запрещено")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            raise UnsafeCode(f"имя {node.id} запрещено")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise UnsafeCode(f"атрибут {node.attr} запрещён")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await)):
            raise UnsafeCode("конструкция запрещена")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                "export", "exportStep", "exportStl", "save", "write", "exportBrep", "importStep", "importStl"):
            raise UnsafeCode(f"вызов .{node.func.attr} запрещён (экспорт делает ассистент)")


_CQ_RUNNER = r'''
import math, sys, builtins
import cadquery as cq
code_path, out_dir = sys.argv[1], sys.argv[2]
code = open(code_path, encoding="utf-8").read()
safe_builtins = {k: getattr(builtins, k) for k in (
    "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "len", "list", "max", "min",
    "range", "reversed", "round", "set", "sorted", "sum", "tuple", "zip", "map", "filter", "isinstance",
    "ValueError", "Exception", "True", "False", "None", "print") if hasattr(builtins, k)}
def _imp(name, *a, **k):
    if name.split(".")[0] not in ("cadquery", "math"):
        raise ImportError(name)
    return __import__(name, *a, **k)
safe_builtins["__import__"] = _imp
ns = {"__builtins__": safe_builtins, "cq": cq, "math": math, "__name__": "jarvis_model"}
exec(compile(code, "model.py", "exec"), ns)
result = ns.get("result")
if result is None:
    raise SystemExit("ERROR: переменная result не задана")
import os
cq.exporters.export(result, os.path.join(out_dir, "model.step"))
cq.exporters.export(result, os.path.join(out_dir, "model.stl"))
bb = (result.val() if hasattr(result, "val") else result).BoundingBox()
print(f"OK {bb.xlen:.1f} x {bb.ylen:.1f} x {bb.zlen:.1f}")
'''


@dataclass
class ModelResult:
    ok: bool
    folder: Path
    files: dict = field(default_factory=dict)
    summary: str = ""
    error: str = ""
    attempts: int = 0


class CadHelper:
    def __init__(self, cfg: dict, llm):
        self.cfg = cfg or {}
        self.llm = llm
        self.model = self.cfg.get("model", "qwen2.5-coder:7b")
        self.dir = pu.data_dir("Models", self.cfg.get("dir", ""))
        self.openscad = find_openscad(self.cfg.get("openscad_path", ""))
        self.timeout = int(self.cfg.get("timeout_s", 180))
        self.open_after = bool(self.cfg.get("open_preview", True))

    def _model(self) -> str:
        return self.model if self.llm.has_model(self.model) else self.llm.model

    @staticmethod
    def cadquery_available() -> bool:
        try:
            import importlib.util
            return importlib.util.find_spec("cadquery") is not None
        except Exception:
            return False

    def _folder(self, description: str) -> Path:
        folder = self.dir / f"{_dt.datetime.now():%Y%m%d_%H%M%S}_{pu.slugify(description, 28)}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # ------------------------------------------------------------ runners
    def _openscad_cmd(self, args: list[str]) -> list[str]:
        cmd = [self.openscad] + args
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a"] + cmd   # headless PNG rendering on Linux
        return cmd

    def run_openscad(self, scad: Path) -> tuple[bool, str]:
        stl = scad.with_suffix(".stl")
        try:
            r = subprocess.run(self._openscad_cmd(["-o", str(stl), str(scad)]), capture_output=True, text=True,
                               timeout=self.timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return False, "OpenSCAD: превышено время рендера"
        out = (r.stdout or "") + (r.stderr or "")
        errors = [l for l in out.splitlines() if "ERROR" in l or "empty" in l.lower() and "top level" in l.lower()]
        if r.returncode != 0 or errors or not stl.exists() or stl.stat().st_size < 100:
            return False, "\n".join(errors) or out[-1500:] or "OpenSCAD не создал STL"
        return True, out

    def render_png(self, scad: Path, png: Path) -> bool:
        try:
            r = subprocess.run(self._openscad_cmd(["-o", str(png), "--imgsize=1024,768", "--autocenter", "--viewall",
                                                   "--colorscheme=Tomorrow Night", "--render", str(scad)]),
                               capture_output=True, text=True, timeout=self.timeout, encoding="utf-8", errors="replace")
            return r.returncode == 0 and png.exists()
        except Exception as e:
            log.warning("PNG превью: %s", e)
            return False

    def run_cadquery(self, code_path: Path, folder: Path) -> tuple[bool, str]:
        try:
            check_cadquery_code(code_path.read_text(encoding="utf-8"))
        except (UnsafeCode, SyntaxError) as e:
            return False, f"Код отклонён проверкой безопасности/синтаксиса: {e}"
        runner = folder / "_jarvis_cq_runner.py"
        runner.write_text(_CQ_RUNNER, encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, str(runner), str(code_path), str(folder)], capture_output=True,
                               text=True, timeout=self.timeout, cwd=str(folder), encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return False, "CadQuery: превышено время выполнения"
        finally:
            runner.unlink(missing_ok=True)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or not (folder / "model.step").exists():
            tail = out.strip().splitlines()[-6:]
            return False, "\n".join(tail) or "CadQuery завершился с ошибкой"
        return True, out

    # ------------------------------------------------------------ main entry
    def make_model(self, description: str, kind: str = "auto") -> ModelResult:
        """kind: 'scad' | 'cadquery' | 'auto' (CadQuery if user mentions SolidWorks/STEP)."""
        low = description.lower()
        if kind == "auto":
            kind = "cadquery" if re.search(r"солид|solid|step|степ|стэп|cadquery|кадквери", low) else "scad"
        if kind == "cadquery" and not self.cadquery_available():
            kind = "scad"
            log.warning("CadQuery не установлен (pip install -r requirements-cad.txt), делаю OpenSCAD")
        folder = self._folder(description)
        system = CQ_SYSTEM if kind == "cadquery" else SCAD_SYSTEM
        messages = [{"role": "system", "content": system}, {"role": "user", "content": description}]
        res = ModelResult(False, folder)
        for attempt in range(2):            # first try + one retry with the error message
            res.attempts = attempt + 1
            answer = self.llm.complete(messages, model=self._model(), temperature=0.2, num_ctx=8192, lang="no_cjk")
            summary, code = extract_code(answer)
            res.summary = res.summary or summary
            if kind == "cadquery":
                src = folder / "model.py"
                src.write_text(code, encoding="utf-8")
                ok, out = self.run_cadquery(src, folder)
                if ok:
                    res.files = {"py": src, "step": folder / "model.step", "stl": folder / "model.stl"}
            else:
                src = folder / "model.scad"
                src.write_text(code, encoding="utf-8")
                if not self.openscad:
                    res.files = {"scad": src}
                    res.ok, res.error = True, "OpenSCAD не установлен: STL и превью не построены"
                    return res
                ok, out = self.run_openscad(src)
                if ok:
                    res.files = {"scad": src, "stl": src.with_suffix(".stl")}
            if ok:
                res.ok, res.error = True, ""
                break
            res.error = out
            log.warning("Модель не собралась (попытка %d): %s", attempt + 1, out[:300])
            messages += [{"role": "assistant", "content": answer},
                         {"role": "user", "content": f"Код не работает, ошибка:\n{out[:1500]}\nИсправь и пришли полный код."}]
        if res.ok and self.openscad and "stl" in res.files:
            png = folder / "preview.png"
            src = res.files.get("scad")
            if src is None:  # CadQuery: preview the STL through OpenSCAD
                src = folder / "_preview.scad"
                src.write_text('color("DeepSkyBlue") import("model.stl");\n', encoding="utf-8")
            if self.render_png(src, png):
                res.files["png"] = png
            if src.name == "_preview.scad":
                src.unlink(missing_ok=True)
        if res.ok and self.open_after:
            pu.open_path(res.files.get("png") or folder)
        return res

    @staticmethod
    def spoken(res: ModelResult) -> str:
        if not res.ok:
            return "Не получилось собрать модель даже со второй попытки. Код и ошибку сохранил в папке модели."
        kinds = []
        if "step" in res.files:
            kinds.append("STEP для СолидВоркс")
        if "stl" in res.files:
            kinds.append("STL")
        if "scad" in res.files:
            kinds.append("исходник OpenSCAD")
        s = (res.summary.split(".")[0].strip() + ". ") if res.summary else ""
        tail = " " + res.error if res.error else ""
        return f"{s}Готово: {', '.join(kinds)}." + (" Открываю превью." if "png" in res.files else "") + tail
