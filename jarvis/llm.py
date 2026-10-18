"""Ollama local HTTP API client with streaming, a token budget for the prompt and a Russian-language guard.

qwen2.5 drifts into Chinese when it loses its instructions or samples too hot. 1.0.4 defends in depth:
  * the Russian-only rule is ALWAYS appended to the system prompt (also to custom prompts and one-off calls);
  * num_ctx 8192 + a token budget: history is trimmed so system prompt + tool schemas + history + the answer
    (num_predict) always fit — Ollama never silently truncates/shifts the context and drops the system prompt;
  * sane sampling (temperature 0.6, top_p 0.9, top_k 40, repeat_penalty 1.1);
  * replies are checked: CJK characters (or a mostly non-Cyrillic answer) -> regenerate once with a
    corrective instruction, then strip CJK as the last resort; clean_for_speech never passes CJK to Piper.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Iterator, Optional

import requests

log = logging.getLogger(__name__)

_RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                  "сентября", "октября", "ноября", "декабря"]


class LLMError(RuntimeError):
    pass


RU_RULE = ("ЯЗЫК: отвечай только на русском языке, русскими буквами. Никогда не пиши по-китайски, "
           "по-японски, по-корейски и не используй иероглифы; не переходи на английский, даже если вопрос, "
           "найденные данные или результаты инструментов на другом языке. Названия и имена можно оставить как есть.")
CORRECTION = ("Твой предыдущий ответ был не на русском языке. Ответь на последнее сообщение пользователя "
              "заново, коротко и только по-русски.")
# CJK ideographs (+ext A, compat), kana, hangul, CJK/fullwidth punctuation
_CJK_RE = re.compile(r"[\u2E80-\u2FDF\u3000-\u303F\u3040-\u30FF\u3100-\u312F\u3130-\u318F\u31A0-\u31FF"
                     r"\u3200-\u32FF\u3400-\u4DBF\u4E00-\u9FFF\uA960-\uA97F\uAC00-\uD7AF\uF900-\uFAFF"
                     r"\uFE30-\uFE4F\uFF00-\uFF65]")
_CYR_RE = re.compile(r"[а-яёА-ЯЁ]")
_LETTER_RE = re.compile(r"[^\W\d_]")


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def strip_cjk(text: str) -> str:
    out = _CJK_RE.sub(" ", text or "")
    out = re.sub(r"\s+([,.!?…;:])", r"\1", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def cyrillic_ratio(text: str) -> float:
    letters = _LETTER_RE.findall(text or "")
    return (len(_CYR_RE.findall(text or "")) / len(letters)) if letters else 1.0


def not_russian(text: str, min_letters: int = 40) -> bool:
    """CJK anywhere, or a long reply that is mostly not Cyrillic (English drift). Short Latin names
    («Включаю Imagine Dragons») are fine."""
    if has_cjk(text):
        return True
    letters = len(_LETTER_RE.findall(text or ""))
    return letters >= min_letters and cyrillic_ratio(text) < 0.25


def est_tokens(obj) -> int:
    """Conservative token estimate for budgeting (qwen2.5: ~3 chars/token for Russian, ~4 for JSON)."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return len(text) // 3 + 4


class OllamaChat:
    def __init__(self, cfg: dict, session: Optional[requests.Session] = None):
        self.host = str(cfg.get("host", "http://localhost:11434")).rstrip("/")
        self.model = cfg.get("model", "qwen2.5:7b")
        self.system_prompt = cfg.get("system_prompt", "")
        self.temperature = float(cfg.get("temperature", 0.6))
        self.num_ctx = max(2048, int(cfg.get("num_ctx", 8192)))
        self.num_predict = int(cfg.get("num_predict", 512))          # voice answers are short
        self.top_p = float(cfg.get("top_p", 0.9))
        self.top_k = int(cfg.get("top_k", 40))
        self.repeat_penalty = float(cfg.get("repeat_penalty", 1.1))
        self.max_history = int(cfg.get("max_history_messages", 16))
        self.timeout = float(cfg.get("timeout_s", 120))
        self.keep_alive = cfg.get("keep_alive", "30m")
        # extra models (coder, vision) are unloaded quickly to fit into 6 GB VRAM
        self.aux_keep_alive = cfg.get("aux_keep_alive", "1m")
        self.http = session or requests.Session()
        self.history: list[dict] = []
        # optional hook: fn(user_text) -> extra context for the system prompt (long-term memory)
        self.context_provider = None
        self._last_user_text = ""

    # ---------------------------------------------------------------- service
    def check(self) -> tuple[bool, str]:
        """Returns (ok, message in Russian)."""
        try:
            r = self.http.get(f"{self.host}/api/tags", timeout=5)
            r.raise_for_status()
        except requests.RequestException:
            from .ollama_setup import friendly_error
            return False, friendly_error({"llm": {"host": self.host}}, session=self.http)
        names = [m.get("name", "") for m in r.json().get("models", [])]
        wanted = self.model if ":" in self.model else self.model + ":latest"
        if wanted not in names and self.model not in names:
            return False, (f"Модель ИИ «{self.model}» ещё не скачана. Джарвис предложит скачать её в окне, или: "
                           "Пуск → Джарвис → «Докачать компоненты»"
                           + (f" (сейчас есть: {', '.join(names)})" if names else "") + ".")
        return True, "ok"

    def reset(self) -> None:
        self.history.clear()

    def _system_message(self) -> dict:
        now = _dt.datetime.now()
        stamp = (f"Сейчас {_RU_WEEKDAYS[now.weekday()]}, {now.day} {_RU_MONTHS_GEN[now.month - 1]} "
                 f"{now.year} года, {now:%H:%M}.")
        extra = ""
        if self.context_provider and self._last_user_text:
            try:
                extra = self.context_provider(self._last_user_text) or ""
            except Exception as e:  # memory must never break the chat
                log.debug("context_provider: %s", e)
        content = f"{self.system_prompt}\n\n{stamp}"
        if extra:
            content += f"\n\n{extra}"
        content += f"\n\n{RU_RULE}"            # always last: survives custom prompts, strongest position
        return {"role": "system", "content": content}

    def options(self, temperature: Optional[float] = None, num_ctx: Optional[int] = None,
                num_predict: Optional[int] = None) -> dict:
        o = {"temperature": self.temperature if temperature is None else temperature,
             "top_p": self.top_p, "top_k": self.top_k, "repeat_penalty": self.repeat_penalty,
             "num_ctx": num_ctx or self.num_ctx}
        if num_predict:
            o["num_predict"] = num_predict
        return o

    def _trim(self) -> None:
        if self.max_history > 0 and len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        # never start the history with an assistant reply
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)

    def _fit(self, system: dict, tools: Optional[list]) -> list[dict]:
        """Drop the oldest turns until system + tools + history + answer fit into num_ctx (with margin), so
        Ollama never truncates the prompt itself (which loses the system prompt / Russian rule)."""
        budget = self.num_ctx - self.num_predict - 256 - est_tokens(system["content"]) - (est_tokens(tools) if tools else 0)
        hist = list(self.history)
        dropped = 0
        while len(hist) > 1 and sum(est_tokens(m.get("content", "")) for m in hist) > budget:
            hist.pop(0)
            dropped += 1
            while len(hist) > 1 and hist[0]["role"] != "user":
                hist.pop(0)
                dropped += 1
        if hist and est_tokens(hist[-1].get("content", "")) > budget > 200:     # one giant message: cut its head
            m = dict(hist[-1])
            m["content"] = m["content"][-budget * 3:]
            hist[-1] = m
        if dropped:
            log.info("LLM: история урезана на %d сообщений, чтобы влезть в num_ctx=%d", dropped, self.num_ctx)
            self.history = hist
        return hist

    # ---------------------------------------------------------------- chat
    def _post_stream(self, payload: dict) -> Iterator[dict]:
        with self.http.post(f"{self.host}/api/chat", json=payload, stream=True, timeout=self.timeout) as r:
            if r.status_code != 200:
                raise LLMError(f"Ollama вернула {r.status_code}: {r.text[:300]}")
            for line in r.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise LLMError(str(data["error"]))
                yield data
                if data.get("done"):
                    break

    def stream(self, user_text: str, tools: Optional[list] = None,
               tool_calls_out: Optional[list] = None) -> Iterator[str]:
        """Yield response text pieces as they arrive. History is updated at the end.

        If `tools` (Ollama/OpenAI style function schemas) are given and the model
        decides to call one, the calls are appended to `tool_calls_out` as
        {"name": str, "arguments": dict}; the caller executes them and should
        then call `add_assistant_message()` with the spoken result.
        """
        self.history.append({"role": "user", "content": user_text})
        self._trim()
        self._last_user_text = user_text
        system = self._system_message()
        payload = {
            "model": self.model,
            "messages": [system] + self._fit(system, tools),
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": self.options(num_predict=self.num_predict),
        }
        if tools:
            payload["tools"] = tools
        parts: list[str] = []
        calls: list[dict] = []
        try:
            for data in self._post_stream(payload):
                msg = data.get("message") or {}
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    calls.append({"name": fn.get("name", ""), "arguments": args})
                piece = msg.get("content", "")
                if piece:
                    parts.append(piece)
                    yield piece
        except requests.RequestException as e:
            self._drop_last_user()
            raise LLMError(f"Ошибка связи с Ollama: {e}") from e
        except LLMError:
            self._drop_last_user()
            raise
        if tool_calls_out is not None:
            tool_calls_out.extend(calls)
        answer = strip_think("".join(parts)).strip()
        if answer:
            self.history.append({"role": "assistant", "content": answer})
            self._trim()
        elif not calls:
            self._drop_last_user()

    def _drop_last_user(self) -> None:
        if self.history and self.history[-1]["role"] == "user":
            self.history.pop()

    def add_assistant_message(self, text: str) -> None:
        if text:
            self.history.append({"role": "assistant", "content": text})
            self._trim()

    def ask(self, user_text: str) -> str:
        return strip_think("".join(self.stream(user_text))).strip()

    def regenerate_russian(self) -> str:
        """The streamed answer drifted (CJK / not Russian): ask again with a corrective instruction.
        The history ends with the user's message (the drifted answer was not stored). Stores the result."""
        if self.history and self.history[-1]["role"] == "assistant" and (
                has_cjk(self.history[-1]["content"]) or not_russian(self.history[-1]["content"])):
            self.history.pop()                       # never keep (or learn from) the drifted answer
        system = self._system_message()
        msgs = [system] + self._fit(system, None) + [{"role": "user", "content": CORRECTION}]
        out = self.complete(msgs, temperature=0.3, lang="ru", _ru_rule=False)
        out = out or "Извини, сбился. Повтори, пожалуйста."
        self.history.append({"role": "assistant", "content": out})
        self._trim()
        return out

    @staticmethod
    def _with_ru_rule(messages: list[dict]) -> list[dict]:
        msgs = [dict(m) for m in messages]
        if msgs and msgs[0].get("role") == "system":
            if RU_RULE not in msgs[0].get("content", ""):
                msgs[0]["content"] = f"{msgs[0].get('content', '')}\n\n{RU_RULE}"
        else:
            msgs.insert(0, {"role": "system", "content": RU_RULE})
        return msgs

    def complete(self, messages: list[dict], model: Optional[str] = None, fmt: Optional[str] = None,
                 temperature: Optional[float] = None, num_ctx: Optional[int] = None,
                 timeout: Optional[float] = None, lang: str = "ru", _ru_rule: bool = True) -> str:
        """One-off, non-streaming request that does not touch the conversation history.
        fmt="json" asks Ollama for strict JSON output.
        lang: "ru" (default; Russian rule + check, retry once, strip CJK) | "no_cjk" (code/JSON: only CJK is
        checked) | "any" (translator: target language decided by the caller)."""
        if lang == "ru" and _ru_rule:
            messages = self._with_ru_rule(messages)
        out = self._complete_once(messages, model, fmt, temperature, num_ctx, timeout)
        bad = (lang == "ru" and not_russian(out)) or (lang == "no_cjk" and has_cjk(out))
        if bad:
            log.warning("LLM: ответ не на русском (%s…) — переспрашиваю", out[:60].replace("\n", " "))
            fix = CORRECTION if lang == "ru" else "Не используй китайские/японские иероглифы. Повтори ответ без них."
            retry = self._complete_once(messages + [{"role": "assistant", "content": out},
                                                    {"role": "user", "content": fix}],
                                        model, fmt, 0.3, num_ctx, timeout)
            if retry and not (lang == "ru" and not_russian(retry)) and not has_cjk(retry):
                return retry
            out = retry or out                      # the corrected attempt is the better base to clean
        return strip_cjk(out) if lang != "any" and has_cjk(out) else out

    def _complete_once(self, messages: list[dict], model: Optional[str], fmt: Optional[str],
                       temperature: Optional[float], num_ctx: Optional[int], timeout: Optional[float]) -> str:
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive if (model or self.model) == self.model else self.aux_keep_alive,
            "options": self.options(temperature, num_ctx),
        }
        if fmt:
            payload["format"] = fmt
        try:
            r = self.http.post(f"{self.host}/api/chat", json=payload, timeout=timeout or max(self.timeout, 600))
        except requests.RequestException as e:
            raise LLMError(f"Ошибка связи с Ollama: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama вернула {r.status_code}: {r.text[:300]}")
        return strip_think((r.json().get("message") or {}).get("content", "")).strip()

    def embed(self, texts: list[str], model: str = "nomic-embed-text") -> list[list[float]]:
        r = self.http.post(f"{self.host}/api/embed", json={"model": model, "input": texts, "keep_alive": "5m"}, timeout=60)
        if r.status_code != 200:
            raise LLMError(f"embed {r.status_code}: {r.text[:200]}")
        return r.json().get("embeddings") or []

    def complete_with_images(self, prompt: str, images_b64: list[str], model: str, timeout: float = 300) -> str:
        """Vision request (qwen2.5vl / moondream / minicpm-v). keep_alive=0 unloads it right after (6 GB VRAM)."""
        payload = {"model": model, "stream": False, "keep_alive": 0,
                   "messages": [{"role": "user", "content": prompt, "images": images_b64}]}
        try:
            r = self.http.post(f"{self.host}/api/chat", json=payload, timeout=timeout)
        except requests.RequestException as e:
            raise LLMError(f"Ошибка связи с Ollama: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama вернула {r.status_code}: {r.text[:300]}")
        return strip_think((r.json().get("message") or {}).get("content", "")).strip()

    def unload_all(self) -> None:
        """Unload every model Ollama currently holds in memory (keep_alive=0)."""
        try:
            ps = self.http.get(f"{self.host}/api/ps", timeout=5).json()
            for m in ps.get("models", []):
                self.http.post(f"{self.host}/api/generate", json={"model": m.get("name"), "keep_alive": 0}, timeout=15)
        except Exception as e:
            log.debug("unload: %s", e)

    def has_model(self, model: str) -> bool:
        try:
            names = [m.get("name", "") for m in self.http.get(f"{self.host}/api/tags", timeout=5).json().get("models", [])]
        except Exception:
            return False
        return model in names or (":" not in model and model + ":latest" in names)


# ---------------------------------------------------------------------------- text helpers
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks of reasoning models (qwen3, deepseek-r1)."""
    return _THINK_RE.sub("", text)


def clean_for_speech(text: str) -> str:
    """Remove markdown / emoji so TTS doesn't read symbols aloud."""
    text = strip_think(text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)      # [text](url)
    text = re.sub(r"https?://\S+", " ссылка ", text)
    text = re.sub(r"[*_#>|~]+", " ", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", text)  # emoji
    text = strip_cjk(text)                     # Piper cannot (and must not) speak hieroglyphs
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    """Accumulates streamed tokens and emits complete sentences for TTS."""

    _END_RE = re.compile(r"([.!?…。！？]+[\"»)」]?)(\s+|$)?")

    def __init__(self, min_chars: int = 12, max_chars: int = 220):
        self.buf = ""
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._in_think = False

    def feed(self, piece: str) -> list[str]:
        self.buf += piece
        # hide reasoning blocks while streaming
        if "<think>" in self.buf:
            if "</think>" not in self.buf:
                return []
            self.buf = strip_think(self.buf)
        out = []
        while True:
            cut = None
            for m in self._END_RE.finditer(self.buf):
                # need whitespace after (sentence finished); CJK enders never get a space -> cut right away
                if m.end(1) >= self.min_chars and (m.group(2) or m.group(1)[-1] in "。！？」"):
                    cut = m.end(1)
                    break
            if cut is None and len(self.buf) > self.max_chars:
                idx = max(self.buf.rfind(",", 0, self.max_chars), self.buf.rfind(" ", 0, self.max_chars))
                cut = idx + 1 if idx > 0 else self.max_chars
            if cut is None:
                break
            sentence, self.buf = self.buf[:cut].strip(), self.buf[cut:]
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> list[str]:
        rest = strip_think(self.buf).strip()
        self.buf = ""
        return [rest] if rest else []
