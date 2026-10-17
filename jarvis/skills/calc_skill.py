"""Calculator, unit and currency conversion."""
from __future__ import annotations

import ast
import logging
import math
import operator
import re
from typing import Optional

import requests

from ..planner import words_to_digits
from .base import Skill, ToolResult, fn, s

log = logging.getLogger(__name__)

CALC_RE = re.compile(r"^(?:сколько будет|посчитай|вычисли|подсчитай|сколько)\s+(.+)$")
CONV_RE = re.compile(r"(?:переведи|сколько|конвертируй)?\s*(\d+(?:[.,]\d+)?)\s+([а-яa-z.$€]+(?:\s[а-я]+)?)\s+(?:в|во)\s+([а-яa-z.$€]+)")

WORD_OPS = [(r"\bумножить на\b|\bумножь на\b|\bпомножить на\b|\bраз\b|\bх\b|\bx\b", "*"),
            (r"\bразделить на\b|\bподелить на\b|\bделить на\b|\bподели на\b", "/"),
            (r"\bплюс\b|\bприбавить\b|\bи\b", "+"), (r"\bминус\b|\bотнять\b", "-"),
            (r"\bв степени\b", "**"), (r"\bв квадрате\b", "**2"), (r"\bв кубе\b", "**3")]
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}


def safe_eval(expr: str) -> float:
    """Arithmetic only: numbers, + - * / ** %, parentheses, sqrt()."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 100:
                raise ValueError("слишком большая степень")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "sqrt" and len(n.args) == 1:
            return math.sqrt(ev(n.args[0]))
        raise ValueError("недопустимое выражение")
    return ev(ast.parse(expr, mode="eval"))


def spoken_to_expr(text: str) -> Optional[str]:
    t = words_to_digits(text.lower()).replace(",", ".")
    t = re.sub(r"(\d+(?:\.\d+)?)\s*процент\w* от\s*(\d+(?:\.\d+)?)", r"(\1/100*\2)", t)
    t = re.sub(r"(?:квадратный )?корень (?:квадратный )?из\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", t)
    for rx, op in WORD_OPS:
        t = re.sub(rx, f" {op} ", t)
    t = t.replace("×", "*").replace("÷", "/").replace("^", "**")
    t = re.sub(r"[^\d.+\-*/()% sqrt]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not re.search(r"\d", t) or not re.search(r"[+\-*/%]|sqrt", t):
        return None
    return t


def fmt_num(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.4g}".replace(".", ",") if abs(x) < 1e6 else f"{x:,.0f}".replace(",", " ")


UNITS = {  # name -> (dimension, factor to base)
    "км": ("len", 1000), "километр": ("len", 1000), "м": ("len", 1), "метр": ("len", 1), "см": ("len", .01),
    "сантиметр": ("len", .01), "мм": ("len", .001), "миллиметр": ("len", .001), "миля": ("len", 1609.344),
    "мил": ("len", 1609.344), "фут": ("len", .3048), "дюйм": ("len", .0254), "ярд": ("len", .9144),
    "кг": ("mass", 1), "килограмм": ("mass", 1), "г": ("mass", .001), "грамм": ("mass", .001),
    "фунт": ("mass", .45359237), "унци": ("mass", .028349523), "тонн": ("mass", 1000),
    "л": ("vol", 1), "литр": ("vol", 1), "мл": ("vol", .001), "миллилитр": ("vol", .001), "галлон": ("vol", 3.785411784),
    "кмч": ("speed", 1 / 3.6), "мс": ("speed", 1), "миль в час": ("speed", .44704),
}
CURRENCY = {"доллар": "USD", "бакс": "USD", "usd": "USD", "$": "USD", "евро": "EUR", "eur": "EUR", "€": "EUR",
            "гривн": "UAH", "грн": "UAH", "uah": "UAH", "рубл": "RUB", "rub": "RUB", "злот": "PLN",
            "фунт стерлинг": "GBP", "фунтов стерлингов": "GBP", "юан": "CNY", "биткоин": "BTC"}


def _unit(word: str):
    w = word.lower().strip(".")
    for k in sorted(UNITS, key=len, reverse=True):
        if w == k or (len(k) > 2 and w.startswith(k)):
            return UNITS[k]
    return None


def _cur(word: str):
    w = word.lower()
    for k in sorted(CURRENCY, key=len, reverse=True):
        if w.startswith(k):
            return CURRENCY[k]
    return None


def convert(amount: float, src: str, dst: str) -> Optional[str]:
    if _unit(src) and _unit(dst):
        (d1, f1), (d2, f2) = _unit(src), _unit(dst)
        if d1 != d2:
            return "Эти единицы нельзя перевести друг в друга."
        return f"{fmt_num(amount)} {src} это {fmt_num(amount * f1 / f2)} {dst}."
    c1, c2 = _cur(src), _cur(dst)
    if c1 and c2:
        try:
            rates = requests.get(f"https://open.er-api.com/v6/latest/{c1}", timeout=10).json()["rates"]
            return f"{fmt_num(amount)} {src} это примерно {fmt_num(amount * rates[c2])} {dst} по текущему курсу."
        except Exception as e:
            log.warning("Курс валют: %s", e)
            return "Не могу получить курс валют, нужен интернет."
    if src.startswith(("цельси", "градус")) and dst.startswith("фаренг"):
        return f"{fmt_num(amount)} по Цельсию это {fmt_num(amount * 9 / 5 + 32)} по Фаренгейту."
    if src.startswith("фаренг") and dst.startswith(("цельси", "градус")):
        return f"{fmt_num(amount)} по Фаренгейту это {fmt_num((amount - 32) * 5 / 9)} по Цельсию."
    return None


class CalcSkill(Skill):
    name = "calc"

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        t2 = words_to_digits(t).replace("км ч", "кмч").replace("м с", "мс").replace("километров в час", "кмч")
        m = CONV_RE.search(t2)
        if m:
            r = convert(float(m.group(1).replace(",", ".")), m.group(2), m.group(3))
            if r:
                return ToolResult(r)
        m = CALC_RE.match(t)
        expr = spoken_to_expr(m.group(1)) if m else None
        if expr:
            try:
                return ToolResult(f"{fmt_num(safe_eval(expr))}.")
            except ZeroDivisionError:
                return ToolResult("На ноль делить нельзя, даже мне.")
            except Exception:
                return None
        return None

    def tools(self):
        return [fn("calculate", "Посчитать арифметическое выражение.", {"expression": s("например (2+3)*4")}, ["expression"])]

    def call(self, name, args):
        if name == "calculate":
            try:
                return ToolResult(f"{fmt_num(safe_eval(str(args.get('expression', ''))))}.")
            except Exception:
                return ToolResult("Не смог посчитать.")
        return None
