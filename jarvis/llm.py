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
import threading
import time
from typing import Iterator, Optional

import requests

log = logging.getLogger(__name__)

_RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                  "сентября", "октября", "ноября", "декабря"]


RETRY_TIMEOUT_S = 25.0        # the «answer in Russian» retry: hard cap (was 600 s)
RETRY_MAX_TOKENS = 160        # a spoken answer; a runaway degenerate generation must not run to num_ctx
FALLBACK_RU = "Не расслышал, повтори, пожалуйста."

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


NOTE_PREFIX = "[Справка, не зачитывай: "
_NOTE_RE = re.compile(r"^\[Справка, не зачитывай: .*?\]\n", re.S)


def strip_note(text: str) -> str:
    """User message without the context note (for summaries / display)."""
    return _NOTE_RE.sub("", text or "", count=1)


def _stats(data: dict) -> dict:
    """Ollama's own timings (nanoseconds) -> seconds; tok_s = generation speed (low = model spilled to CPU)."""
    ns = 1e9
    ev, evd = int(data.get("eval_count") or 0), float(data.get("eval_duration") or 0)
    return {"load_s": round(float(data.get("load_duration") or 0) / ns, 2),
            "prompt_tokens": int(data.get("prompt_eval_count") or 0),
            "prompt_s": round(float(data.get("prompt_eval_duration") or 0) / ns, 2),
            "eval_tokens": ev, "eval_s": round(evd / ns, 2),
            "tok_s": round(ev / (evd / ns), 1) if evd else 0.0,
            "total_s": round(float(data.get("total_duration") or 0) / ns, 2)}


class OllamaChat:
    def __init__(self, cfg: dict, session: Optional[requests.Session] = None):
        self.host = str(cfg.get("host", "http://localhost:11434")).rstrip("/")
        self.model = cfg.get("model", "qwen2.5:7b")
        self.system_prompt = cfg.get("system_prompt", "")
        self.temperature = float(cfg.get("temperature", 0.5))
        self.num_ctx = max(2048, int(cfg.get("num_ctx", 6144)))
        self.num_predict = int(cfg.get("num_predict", 512))          # voice answers are short
        self.top_p = float(cfg.get("top_p", 0.9))
        self.top_k = int(cfg.get("top_k", 40))
        self.repeat_penalty = float(cfg.get("repeat_penalty", 1.1))
        self.max_history = int(cfg.get("max_history_messages", 16))
        self.timeout = float(cfg.get("timeout_s", 120))
        self.turn_timeout = float(cfg.get("turn_timeout_s", 90))   # whole streamed answer, incl. slow CPU prompt
        self.keep_alive = cfg.get("keep_alive", "30m")
        # extra models (coder, vision) are unloaded quickly to fit into 6 GB VRAM
        self.aux_keep_alive = cfg.get("aux_keep_alive", "1m")
        # where the model runs: "gpu" (fastest), "cpu" (num_gpu=0: frees VRAM, ~3-5x slower), "auto" (GPU; CPU in games)
        self.placement = str(cfg.get("placement", "gpu") or "gpu").lower()
        self.on_cpu = self.placement == "cpu"
        self.last_stats: dict = {}                 # Ollama timings of the last request (load / prompt / eval)
        self.warm_tools: Optional[list] = None     # tool schemas used for warm-ups (set by the assistant)
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
        """STATIC system prompt (changes at most once a day). Ollama/llama.cpp reuse the evaluated prompt
        prefix (system + tool schemas, ~2-2.5k tokens) between requests only while it stays byte-identical,
        so the clock and memory hits go into the last user message instead (see _turn_context)."""
        now = _dt.datetime.now()
        stamp = f"Сегодня {_RU_WEEKDAYS[now.weekday()]}, {now.day} {_RU_MONTHS_GEN[now.month - 1]} {now.year} года."
        content = f"{self.system_prompt}\n\n{stamp}\n\n{RU_RULE}"   # RU_RULE last: survives custom prompts
        return {"role": "system", "content": content}

    def _turn_context(self) -> str:
        now = _dt.datetime.now()
        parts = [f"Сейчас {now:%H:%M}."]
        if self.context_provider and self._last_user_text:
            try:
                extra = self.context_provider(self._last_user_text) or ""
            except Exception as e:  # memory must never break the chat
                log.debug("context_provider: %s", e)
                extra = ""
            if extra:
                parts.append(extra)
        return " ".join(parts)

    def _messages(self, system: dict, tools: Optional[list]) -> list[dict]:
        """system + fitted history. The per-turn context note is attached to the newest user message
        IN the stored history, so every later request repeats the exact same tokens and Ollama only has
        to evaluate what is new (the reply it generated itself is already in its cache)."""
        for m in reversed(self.history):
            if m.get("role") == "user":
                if not m["content"].startswith(NOTE_PREFIX):
                    m["content"] = f"{NOTE_PREFIX}{self._turn_context()}]\n{m['content']}"
                break
        return [system] + self._fit(system, tools)

    def options(self, temperature: Optional[float] = None, num_ctx: Optional[int] = None,
                num_predict: Optional[int] = None) -> dict:
        o = {"temperature": self.temperature if temperature is None else temperature,
             "top_p": self.top_p, "top_k": self.top_k, "repeat_penalty": self.repeat_penalty,
             "num_ctx": num_ctx or self.num_ctx}
        if num_predict:
            o["num_predict"] = num_predict
        if self.on_cpu:
            o["num_gpu"] = 0                        # all layers in RAM: VRAM stays free (games), slower
        return o

    def set_on_cpu(self, on_cpu: bool, warm_tools: Optional[list] = None) -> bool:
        """Move the model between GPU and RAM. Ollama reloads it when num_gpu changes; warm it right away
        (in the background) so the next command does not pay the reload. Returns True if it changed."""
        if bool(on_cpu) == self.on_cpu:
            return False
        self.on_cpu = bool(on_cpu)
        log.info("ИИ теперь работает на %s", "процессоре (оперативка)" if self.on_cpu else "видеокарте")
        threading.Thread(target=self.warm, args=(warm_tools or self.warm_tools,), daemon=True, name="llm-warm").start()
        return True

    def warm(self, tools: Optional[list] = None) -> Optional[float]:
        """Load the model and pre-evaluate the static prefix (system + tools) so the first real command
        is fast. Returns seconds, None on failure (never raises)."""
        t0 = time.monotonic()
        try:
            system = self._system_message()
            payload: dict = {"model": self.model, "stream": False, "keep_alive": self.keep_alive,
                             "messages": [system, {"role": "user", "content": "Привет"}],
                             "options": self.options(num_predict=1)}
            if tools:
                payload["tools"] = tools
            r = self.http.post(f"{self.host}/api/chat", json=payload, timeout=max(self.timeout, 300))
            if r.status_code != 200:
                log.info("Прогрев модели: Ollama %s %s", r.status_code, r.text[:120])
                return None
            self.last_stats = _stats(r.json())
            dt = time.monotonic() - t0
            log.info("Модель %s прогрета за %.1f с (загрузка %.1f с, промпт %d ток. за %.1f с)", self.model, dt,
                     self.last_stats.get("load_s", 0), self.last_stats.get("prompt_tokens", 0),
                     self.last_stats.get("prompt_s", 0))
            return dt
        except Exception as e:
            log.info("Прогрев модели не удался: %s", e)
            return None

    def placement_info(self) -> Optional[dict]:
        """What Ollama really did: {'model', 'size_gb', 'vram_gb', 'gpu_share'} from /api/ps (None if unknown).
        gpu_share < 1 means part of the model spilled to RAM/CPU (much slower)."""
        try:
            ps = self.http.get(f"{self.host}/api/ps", timeout=3).json()
        except Exception:
            return None
        for m in ps.get("models", []) or []:
            if (m.get("name") or m.get("model") or "").split(":")[0] == self.model.split(":")[0]:
                size, vram = float(m.get("size") or 0), float(m.get("size_vram") or 0)
                return {"model": m.get("name"), "size_gb": round(size / 1e9, 2), "vram_gb": round(vram / 1e9, 2),
                        "gpu_share": round(vram / size, 2) if size else 0.0}
        return None

    def _trim(self) -> None:
        if self.max_history > 0 and len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        # never start the history with an assistant reply
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)

    def _fit(self, system: dict, tools: Optional[list], reserve: int = 0) -> list[dict]:
        """Drop the oldest turns until system + tools + history + answer fit into num_ctx (with margin), so
        Ollama never truncates the prompt itself (which loses the system prompt / Russian rule)."""
        budget = (self.num_ctx - self.num_predict - 256 - reserve - est_tokens(system["content"])
                  - (est_tokens(tools) if tools else 0))
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
        with self.http.post(f"{self.host}/api/chat", json=payload, stream=True,
                            timeout=(10, min(self.timeout, self.turn_timeout))) as r:
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
            "messages": self._messages(system, tools),
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": self.options(num_predict=self.num_predict),
        }
        if tools:
            payload["tools"] = tools
        parts: list[str] = []
        calls: list[dict] = []
        deadline = time.monotonic() + self.turn_timeout
        try:
            for data in self._post_stream(payload):
                if time.monotonic() > deadline:
                    raise LLMError(f"timeout: ответ дольше {self.turn_timeout:.0f} с — прерываю")
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
                if data.get("done"):
                    self.last_stats = _stats(data)
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

    def regenerate_russian(self, timeout: float = RETRY_TIMEOUT_S) -> str:
        """The streamed answer drifted (CJK / not Russian): ask again ONCE with a corrective instruction.
        Hard limits (1.0.7): RETRY_TIMEOUT_S and RETRY_MAX_TOKENS, no nested retry — in 1.0.6 this request had a
        600 s timeout and no token cap, so a degenerate generation on CPU blocked Jarvis for many minutes
        (the «falls asleep» bug). Any failure -> a short Russian fallback. Stores the result."""
        if self.history and self.history[-1]["role"] == "assistant" and (
                has_cjk(self.history[-1]["content"]) or not_russian(self.history[-1]["content"])):
            self.history.pop()                       # never keep (or learn from) the drifted answer
        system = self._system_message()
        msgs = self._with_ru_rule(self._messages(system, None) + [{"role": "user", "content": CORRECTION}])
        try:
            out = self._complete_once(msgs, None, None, 0.3, None, timeout, num_predict=RETRY_MAX_TOKENS)
        except LLMError as e:
            log.warning("LLM: повторный ответ по-русски не получен: %s", e)
            out = ""
        if has_cjk(out) or not_russian(out):
            log.warning("LLM: и повтор не по-русски (%r) — короткий ответ", out[:60])
            out = ""
        out = out or FALLBACK_RU
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
                       temperature: Optional[float], num_ctx: Optional[int], timeout: Optional[float],
                       num_predict: Optional[int] = None) -> str:
        main = (model or self.model) == self.model
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive if main else self.aux_keep_alive,
            # a different num_ctx would make Ollama RELOAD the main model (seconds) -> always the same one
            "options": self.options(temperature, None if main else num_ctx),
        }
        if num_predict:
            payload["options"]["num_predict"] = int(num_predict)
        if fmt:
            payload["format"] = fmt
        try:
            r = self.http.post(f"{self.host}/api/chat", json=payload, timeout=timeout or max(self.timeout, 600))
        except requests.RequestException as e:
            raise LLMError(f"Ошибка связи с Ollama: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama вернула {r.status_code}: {r.text[:300]}")
        if not main and not self.on_cpu:
            # coder / other model on a 6 GB card pushed the main model out: bring it back in the background
            threading.Thread(target=self.warm, args=(self.warm_tools,), daemon=True, name="llm-rewarm").start()
        return strip_think((r.json().get("message") or {}).get("content", "")).strip()

    def embed(self, texts: list[str], model: str = "nomic-embed-text") -> list[list[float]]:
        # num_gpu=0: the small embedding model runs on the CPU (~0.1 s) and never pushes the main model out
        # of 6 GB VRAM (Ollama evicts a loaded model when a new one does not fit -> multi-second reload)
        r = self.http.post(f"{self.host}/api/embed", json={"model": model, "input": texts, "keep_alive": self.keep_alive,
                                                           "options": {"num_gpu": 0}}, timeout=60)
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

    def __init__(self, min_chars: int = 12, max_chars: int = 220, first_clause: int = 70):
        self.buf = ""
        self.min_chars = min_chars
        self.max_chars = max_chars
        # latency: a long FIRST sentence is cut at a comma after ~40 chars once 70 have arrived,
        # so the voice starts while the model is still writing the rest (0 = off)
        self.first_clause = first_clause
        self._emitted = 0
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
            if cut is None and not self._emitted and self.first_clause and len(self.buf) > self.first_clause:
                idx = self.buf.find(", ", 40)
                if 0 < idx < self.first_clause:
                    cut = idx + 1
            if cut is None and len(self.buf) > self.max_chars:
                idx = max(self.buf.rfind(",", 0, self.max_chars), self.buf.rfind(" ", 0, self.max_chars))
                cut = idx + 1 if idx > 0 else self.max_chars
            if cut is None:
                break
            sentence, self.buf = self.buf[:cut].strip(), self.buf[cut:]
            if sentence:
                out.append(sentence)
                self._emitted += 1
        return out

    def flush(self) -> list[str]:
        rest = strip_think(self.buf).strip()
        self.buf = ""
        return [rest] if rest else []
