"""Ollama local HTTP API client with streaming and capped history."""
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


class OllamaChat:
    def __init__(self, cfg: dict, session: Optional[requests.Session] = None):
        self.host = str(cfg.get("host", "http://localhost:11434")).rstrip("/")
        self.model = cfg.get("model", "qwen2.5:7b")
        self.system_prompt = cfg.get("system_prompt", "")
        self.temperature = float(cfg.get("temperature", 0.8))
        self.num_ctx = int(cfg.get("num_ctx", 4096))
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
            return False, (f"Не могу подключиться к Ollama по адресу {self.host}. "
                           "Запусти Ollama (значок в трее или команда `ollama serve`).")
        names = [m.get("name", "") for m in r.json().get("models", [])]
        wanted = self.model if ":" in self.model else self.model + ":latest"
        if wanted not in names and self.model not in names:
            return False, (f"Модель '{self.model}' не скачана. Выполни: ollama pull {self.model}"
                           + (f" (сейчас есть: {', '.join(names)})" if names else ""))
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
        return {"role": "system", "content": content}

    def _trim(self) -> None:
        if self.max_history > 0 and len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
            # never start the history with an assistant reply
            while self.history and self.history[0]["role"] != "user":
                self.history.pop(0)

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
        payload = {
            "model": self.model,
            "messages": [self._system_message()] + self.history,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
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

    def complete(self, messages: list[dict], model: Optional[str] = None, fmt: Optional[str] = None,
                 temperature: Optional[float] = None, num_ctx: Optional[int] = None,
                 timeout: Optional[float] = None) -> str:
        """One-off, non-streaming request that does not touch the conversation history.
        fmt="json" asks Ollama for strict JSON output."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive if (model or self.model) == self.model else self.aux_keep_alive,
            "options": {"temperature": self.temperature if temperature is None else temperature,
                        "num_ctx": num_ctx or self.num_ctx},
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
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    """Accumulates streamed tokens and emits complete sentences for TTS."""

    _END_RE = re.compile(r"([.!?…]+[\"»)]?)(\s+|$)")

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
                if m.end(1) >= self.min_chars and m.group(2):  # need whitespace after: sentence finished
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
