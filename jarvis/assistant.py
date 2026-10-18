"""Assistant core: voice loop, command worker, LLM + tools, events for the GUI.

Threads:
  - voice loop  (microphone -> VAD -> Whisper -> wake word)   [jarvis-voice]
  - brain       (commands queue -> skills / LLM -> TTS)         [jarvis-brain]
  - TTS worker, planner scheduler, Telegram, background jobs
The GUI only receives events (thread-safe via Qt signals) and never blocks.
"""
from __future__ import annotations

import datetime as _dt
import logging
import queue
import threading
import time
from typing import Callable, Optional

import numpy as np

from .config import PROJECT_ROOT
from .llm import LLMError, OllamaChat, SentenceSplitter
from .router import Router
from .skills.base import SkillContext, ToolResult
from .wake import WakeWordDetector

log = logging.getLogger("jarvis")

PROFILE_HINTS = {
    "work": "",
    "game": "\n\nСейчас ИГРОВОЙ режим: пользователь играет. Отвечай максимально коротко, одной фразой, без болтовни.",
    "night": "\n\nСейчас НОЧНОЙ режим: отвечай очень коротко и спокойно.",
}
LENGTH_HINTS = {
    "short": "",
    "normal": "\n\nМожно отвечать чуть подробнее: до четырёх-пяти предложений.",
    "long": "\n\nОтвечай развёрнуто и подробно, если вопрос того требует.",
}
PROFILE_NAMES = {"work": "рабочий", "game": "игровой", "night": "ночной"}

TOOL_HINT = ("\n\nУ тебя есть инструменты (функции). Вызывай их только когда пользователь явно просит что-то сделать "
             "на компьютере, или нужна свежая информация из интернета. Для обычной болтовни инструменты не нужны.")


def greeting_text(a: dict, now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now()
    hello = "Добрый день"
    if a.get("greeting_time_of_day"):
        h = now.hour
        hello = ("Доброе утро" if 5 <= h < 12 else "Добрый день" if 12 <= h < 17 else
                 "Добрый вечер" if 17 <= h < 23 else "Доброй ночи")
    tpl = a.get("greeting", "{hello}, мистер {user_name}, чего желаете?")
    return tpl.replace("{hello}", hello).replace("{user_name}", a.get("user_name", "Флоки")).replace(
        "{name}", a.get("name", "Джарвис"))


class Assistant:
    def __init__(self, cfg: dict, speak: bool = True, need_stt: bool = True,
                 on_event: Optional[Callable[..., None]] = None):
        self.cfg = cfg
        self.on_event = on_event or (lambda *a, **k: None)
        self.stop_event = threading.Event()
        a = cfg["assistant"]
        self.name = a.get("name", "Джарвис")
        self.mode = a.get("mode", "wake")
        self.mic_muted = False
        self.wake = WakeWordDetector.from_name(self.name, a.get("extra_wake_words") or [])
        log.debug("Варианты имени: %s", self.wake.wake_words)

        self.llm = OllamaChat(cfg["llm"])
        self.tool_calling = bool(cfg["llm"].get("tool_calling", True))
        self._compose_prompt()
        self.profile = "work"
        self.profile_auto = False
        self.llm_ok, msg = self._ensure_ollama()

        self.speaker = None
        if speak:
            from .tts import Speaker, create_engine
            engine = create_engine(cfg["tts"])
            self.speaker = Speaker(engine, cfg["audio"].get("output_device"), on_event=self._on_tts)

        ctx = SkillContext(cfg=cfg, llm=self.llm, say=self.say_async, notify=self._notify, project_root=PROJECT_ROOT)
        ctx.extra["set_profile"] = self.set_profile
        ctx.extra["get_profile"] = lambda: self.profile
        ctx.extra["on_sources"] = lambda links: self.emit("links", links=links)
        ctx.extra["assistant"] = self
        self.router = Router(ctx)
        self.router.start_background()
        try:
            from .backup import AutoBackup
            self.autobackup = AutoBackup(cfg).start()
        except Exception as e:
            log.warning("Автобэкап недоступен: %s", e)
        try:
            from .sync import AutoSync, SyncEngine
            self.sync_engine = SyncEngine(cfg)
            self.autosync = AutoSync(self.sync_engine, self._sync_done).start()
        except Exception as e:
            self.sync_engine = self.autosync = None  # type: ignore[assignment]
            log.warning("Синхронизация недоступна: %s", e)
        if cfg.get("diagnostics", {}).get("check_on_start", True):
            threading.Thread(target=self._startup_check, name="jarvis-diag", daemon=True).start()
        self.set_profile(cfg.get("profiles", {}).get("default", "work"), announce=False)

        self.stt = None
        if need_stt:
            from .stt import SpeechToText
            self.emit("notice", text="Загружаю распознавание речи...")
            self.stt = SpeechToText(cfg["stt"])

        self._commands: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._idle = threading.Event()
        self._idle.set()
        self._brain = threading.Thread(target=self._brain_loop, name="jarvis-brain", daemon=True)
        self._brain.start()
        self._ptt_active = threading.Event()
        self._turns = 0

    def _ensure_ollama(self) -> tuple[bool, str]:
        """Start an installed-but-stopped Ollama (hidden, waits up to ollama.start_timeout_s); report the rest.
        The GUI gets an 'ollama_problem' event and offers to install Ollama / download the models."""
        from . import ollama_setup as osu
        oc = self.cfg.get("ollama") or {}
        host = osu.host_of(self.cfg)
        if oc.get("auto_start", True) and not osu.is_running(host) and osu.is_local(host) and osu.find_exe():
            self.emit("notice", text="Запускаю Ollama (локальный ИИ)…")
            osu.start(self.cfg, wait=float(oc.get("start_timeout_s", 30)))
        ok, msg = self.llm.check()
        if not ok:
            log.warning(msg)
            self.emit("notice", text=msg)
            state = osu.status(self.cfg)
            models = osu.missing_models(self.cfg) if state == "running" else []
            self.emit("ollama_problem", state=state, models=models, text=msg)
        return ok, msg

    def recheck_llm(self) -> bool:
        self.llm_ok, _ = self.llm.check()
        return self.llm_ok

    # ------------------------------------------------------------------ events / output
    def emit(self, event: str, **data) -> None:
        try:
            self.on_event(event, **data)
        except Exception as e:
            log.debug("event %s: %s", event, e)

    def _on_tts(self, event: str, **data) -> None:
        if event == "speech_start":
            self.emit("state", state="speaking")
            self.emit("speech", **data)
        elif event == "speech_end":
            self.emit("speech_end")

    def _notify(self, title: str, text: str) -> None:
        from .platform_utils import notify
        notify(title, text)
        self._last_notice = (text, time.monotonic())
        self.emit("notice", text=f"{title}: {text}")

    def _shown_as_notice(self, text: str) -> bool:
        """Same text was just shown as a notice (alert/reminder toast) — don't duplicate it in the chat."""
        last = getattr(self, "_last_notice", None)
        return bool(last and last[0].strip() == text.strip() and time.monotonic() - last[1] < 10)

    def say(self, text: str, wait: bool = False, voice: Optional[str] = None) -> None:
        if not text:
            return
        print(f"{self.name}: {text}")
        if not self._shown_as_notice(text):
            self.emit("assistant_text", text=text)
        if self.speaker:
            self.speaker.say(text, voice=voice)
            if wait:
                self.speaker.wait()

    def say_async(self, text: str, priority: str = "normal") -> None:
        """For reminders/timers/alerts from other threads.
        priority: critical (air raid, overheating — always spoken at full volume),
                  normal (reminders, timers), low (chatty notifications)."""
        if priority == "critical":
            print(f"{self.name}: {text}")
            if not self._shown_as_notice(text):
                self.emit("assistant_text", text=text)
            if self.speaker:
                self.speaker.say(text, volume=1.0)
            return
        pc = self.cfg.get("profiles", {})
        if priority == "low" and ((self.profile == "game" and not pc.get("game_notifications", False))
                                  or (self.profile == "night" and not pc.get("night_notifications", False))):
            return
        if priority == "normal" and ((self.profile == "night" and not pc.get("night_reminders_voice", False))
                                     or (self.profile == "game" and not pc.get("game_reminders_voice", True))):
            self._notify(self.name, text)          # toast only
            return
        self.say(text)

    def sync_now(self):
        if not getattr(self, "sync_engine", None):
            return None
        r = self.sync_engine.sync()
        self._sync_done(r, quiet=True)
        return r

    def _sync_done(self, r, quiet: bool = False) -> None:
        self.emit("sync", ok=r.ok, message=r.message, conflicts=r.conflicts)
        eng = getattr(self, "sync_engine", None)
        if eng is not None:
            st = eng.state()
            if st.pop("settings_applied", False):
                from .sync import _write_json
                _write_json(eng.state_file, st)
                self.emit("notice", text="Получены настройки с другого ПК — применятся после перезапуска.")
        if not quiet and not r.ok:
            self._notify(self.name, r.message)

    def _startup_check(self) -> None:
        """Quick silent check: report only problems (GUI notice + toast)."""
        time.sleep(8)
        try:
            from . import diagnostics
            checks = diagnostics.run(self.cfg, self, quick=True)
            bad = [c for c in checks if c.ok is False]
            if bad:
                self.emit("diagnostics", checks=[c.__dict__ for c in checks], startup=True)
                from .platform_utils import notify      # toast only: the GUI already shows the report line
                notify(self.name, "Проблемы: " + "; ".join(c.title for c in bad))
        except Exception as e:
            log.debug("startup check: %s", e)

    def _compose_prompt(self) -> None:
        base = self.cfg["llm"].get("system_prompt") or ""
        length = self.cfg["llm"].get("answer_length", "short")
        base += LENGTH_HINTS.get(length, "")
        if self.tool_calling:
            base += TOOL_HINT
        self._base_prompt = base
        self.llm.system_prompt = base + PROFILE_HINTS.get(getattr(self, "profile", "work"), "")

    def apply_settings(self) -> None:
        """Re-read live-changeable values from self.cfg (after the Settings window saved)."""
        a, l, t = self.cfg["assistant"], self.cfg["llm"], self.cfg["tts"]
        self.name = a.get("name", self.name)
        try:
            self.wake = WakeWordDetector.from_name(self.name, a.get("extra_wake_words") or [])
        except Exception as e:
            log.error("wake words: %s", e)
        self.mode = a.get("mode", self.mode)
        self.llm.model = l.get("model", self.llm.model)
        self.llm.temperature = float(l.get("temperature", 0.8))
        self._compose_prompt()
        eng = self.speaker.engine if self.speaker else None
        if eng is not None and hasattr(eng, "length_scale"):
            eng.length_scale = float(t.get("length_scale", 1.0))
            eng.volume = float(t.get("volume", 1.0))
            eng.pitch = float(t.get("pitch_semitones", 0.0))
            fx = t.get("fx") or {}
            eng.fx_enabled = bool(fx.get("enabled", False))
            eng.fx_intensity = float(fx.get("intensity", 0.35))

    # ------------------------------------------------------------ privacy / VRAM
    def set_mic_muted(self, muted: bool, cue: bool = True) -> None:
        """Privacy pause: the input stream is really closed while muted (nothing is captured)."""
        if muted == self.mic_muted:
            return
        self.mic_muted = muted
        mic = getattr(self, "_mic", None)
        try:
            if mic is not None:
                mic.stop() if muted else (mic.start(), mic.clear())
        except Exception as e:
            log.error("Микрофон: %s", e)
        if cue:
            self._cue(muted)
        self.emit("mic_muted", muted=muted)
        self.emit("state", state="muted" if muted else "idle")
        log.info("Микрофон %s", "на паузе" if muted else "включён")

    def _cue(self, down: bool) -> None:
        """Short two-tone sound: falling = mic off, rising = mic on."""
        try:
            sp = self.speaker
            if not sp or not sp.player:
                return
            rate = 22050
            t = np.arange(int(rate * 0.09)) / rate
            f1, f2 = (880, 587) if down else (587, 880)
            tone = np.concatenate([np.sin(2 * np.pi * f1 * t), np.sin(2 * np.pi * f2 * t)]).astype(np.float32)
            tone *= np.concatenate([np.hanning(len(t))] * 2).astype(np.float32) * 0.25
            threading.Thread(target=sp.player.play, args=(tone, rate), daemon=True).start()
        except Exception as e:
            log.debug("cue: %s", e)

    def release_gpu(self) -> None:
        """Game mode: free VRAM — unload Ollama models now, keep them unloaded after each answer,
        move Whisper to CPU (or unload it; reloaded on demand)."""
        vc = self.cfg.get("vram", {})
        if not vc.get("release_in_game", True):
            return
        self._saved_keep_alive = getattr(self, "_saved_keep_alive", None) or self.llm.keep_alive
        self.llm.keep_alive = 0
        threading.Thread(target=self.llm.unload_all, daemon=True).start()
        if self.stt is not None and getattr(self.stt, "device", "cpu") == "cuda":
            mode = vc.get("whisper_in_game", "cpu")
            threading.Thread(target=self.stt.relocate, args=("cpu" if mode == "cpu" else None,), daemon=True).start()
        log.info("VRAM освобождена для игры")

    def restore_gpu(self) -> None:
        if getattr(self, "_saved_keep_alive", None) is None:
            return
        self.llm.keep_alive = self._saved_keep_alive
        self._saved_keep_alive = None
        if self.stt is not None and getattr(self.stt, "relocated", False):
            threading.Thread(target=self.stt.relocate, args=("restore",), daemon=True).start()
        log.info("Игра закрыта — модели снова на GPU (по запросу)")

    def set_profile(self, name: str, auto: bool = False, announce: bool = True) -> str:
        name = name if name in PROFILE_HINTS else "work"
        self.profile, self.profile_auto = name, auto
        self.llm.system_prompt = self._base_prompt + PROFILE_HINTS[name]
        self.cfg.setdefault("profiles", {})["current"] = name
        try:
            self.release_gpu() if name == "game" else self.restore_gpu()
        except Exception as e:
            log.error("VRAM: %s", e)
        if self.speaker:
            self.speaker.volume = float(self.cfg.get("profiles", {}).get("night_volume", 0.45)) if name == "night" else 1.0
        self.emit("profile", profile=name, auto=auto)
        log.info("Режим: %s%s", name, " (авто)" if auto else "")
        return f"Включил {PROFILE_NAMES[name]} режим."

    def greet(self) -> None:
        if self.cfg["assistant"].get("greet_on_start", True) and self.profile != "night":
            self.say(greeting_text(self.cfg["assistant"]), wait=True)

    # ------------------------------------------------------------------ command processing
    def submit(self, text: str, source: str = "text") -> None:
        """Queue a user command (from the GUI text box, voice loop, or console)."""
        if text and text.strip():
            self._idle.clear()
            self._commands.put((text.strip(), source))

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)

    def _brain_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                text, source = self._commands.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self.emit("user_text", text=text, source=source)
                if self.respond(text) == "exit":
                    self.emit("exit")
                    self.stop_event.set()
            except Exception as e:
                log.exception("Ошибка обработки команды: %s", e)
                self.say("Что-то пошло не так, глянь лог.")
            finally:
                if self.speaker:
                    self.speaker.wait()
                if self._commands.empty():
                    self.emit("state", state="idle")
                    self._idle.set()

    def _run_result(self, r: ToolResult) -> Optional[str]:
        if r.action == "reset":
            self._summarize_async(list(self.llm.history))
            self.llm.reset()
        if r.links:
            self.emit("links", links=r.links)
        if r.reply:
            self.say(r.reply, voice=r.voice)
        if r.then:
            def job():
                try:
                    extra = r.then()
                except Exception as e:
                    log.exception("action: %s", e)
                    extra = f"Не получилось: {e}"
                if isinstance(extra, ToolResult):
                    return self._run_result(extra)
                if extra:
                    self.say(extra)
                    self.llm.add_assistant_message(extra)
            if r.background:
                threading.Thread(target=job, daemon=True).start()
            else:
                self.emit("state", state="thinking")
                if self.speaker:
                    self.speaker.wait()
                job()
        return r.action

    def respond(self, text: str) -> Optional[str]:
        """Handle one user command. Returns 'exit' when the assistant should stop."""
        text = (text or "").strip()
        if not text:
            return None
        self.emit("state", state="thinking")
        r = self.router.handle(text)
        if r is not None:
            return self._run_result(r)
        return self._chat(text)

    def _chat(self, text: str) -> Optional[str]:
        splitter = SentenceSplitter()
        tools = self.router.tool_schemas() if self.tool_calling else None
        calls: list = []
        print(f"{self.name}: ", end="", flush=True)
        full = []
        try:
            for piece in self.llm.stream(text, tools=tools, tool_calls_out=calls):
                full.append(piece)
                print(piece, end="", flush=True)
                self.emit("assistant_partial", text=piece)
                if self.speaker:
                    for sentence in splitter.feed(piece):
                        self.speaker.say(sentence)
                if self.stop_event.is_set():
                    break
            print()
            if self.speaker:
                for sentence in splitter.flush():
                    self.speaker.say(sentence)
            if full:
                self.emit("assistant_done", text="".join(full))
        except LLMError as e:
            print()
            log.error("%s", e)
            msg = str(e).lower()
            if "repeat" in msg or "aborted" in msg:
                self.say("Что-то я зациклился. Спроси ещё раз, пожалуйста, по-другому.")
            elif "not found" in msg and "model" in msg:
                self.say(f"Модель {self.llm.model} не скачана. Открой Настройки, вкладка ИИ, и нажми «Скачать модель».")
            else:
                self.say("Языковая модель не отвечает. Проверь, что Ollama запущена.")
            return None
        action = None
        for c in calls:
            log.info("tool call: %s %s", c["name"], c["arguments"])
            r = self.router.call_tool(c["name"], c["arguments"])
            if r is not None:
                self.llm.add_assistant_message(r.reply or f"[{c['name']}]")
                action = self._run_result(r) or action
        self._turns += 1
        cap = int(self.cfg["llm"].get("max_history_messages", 16))
        if len(self.llm.history) >= cap and self._turns % max(2, cap // 4) == 0:
            self._summarize_async(self.llm.history[: cap // 2])
        return action

    def _summarize_async(self, messages: list) -> None:
        mem = self.router.by_name.get("memory")
        if mem and messages and self.cfg.get("memory", {}).get("summaries", True):
            threading.Thread(target=mem.memory.summarize, args=(list(messages),), daemon=True).start()

    # ------------------------------------------------------------------ text mode
    def run_text(self) -> None:
        print(f"Текстовый режим. Пиши сообщения для {self.name} (\"выход\" или Ctrl+C - завершить).")
        while not self.stop_event.is_set():
            try:
                line = input("Ты: ")
            except EOFError:
                break
            wr = self.wake.detect(line)
            self.submit(wr.command if wr.detected and wr.command else line)
            self.wait_idle()
        self.wait_idle(5)

    # ------------------------------------------------------------------ voice
    def _new_mic(self):
        from .audio import Microphone
        a = self.cfg["audio"]
        return Microphone(a.get("input_device"), int(a.get("sample_rate", 16000)), int(a.get("frame_ms", 30)))

    def _frames(self, mic):
        """Mic frames with level events; while muted, frames are dropped."""
        k = 0
        for frame in mic.frames(self.stop_event):
            if self.mic_muted:
                continue
            k += 1
            if k % 3 == 0:
                self.emit("mic_level", level=min(1.0, float(np.sqrt(np.mean(frame * frame))) * 8))
            yield frame

    def _listen(self, mic, vad, timeout: Optional[float] = None, abort: Optional[Callable[[], bool]] = None):
        from .audio import segment_utterance
        a = self.cfg["audio"]
        deadline = time.monotonic() + timeout if timeout else None

        def should_stop():
            return (self.stop_event.is_set() or (deadline is not None and time.monotonic() > deadline)
                    or (abort is not None and abort()))

        return segment_utterance(
            self._frames(mic), vad, mic.rate,
            frame_ms=int(a.get("frame_ms", 30)), silence_ms=int(a.get("silence_ms", 800)),
            min_speech_ms=int(a.get("min_speech_ms", 250)), max_utterance_s=float(a.get("max_utterance_s", 15)),
            pre_roll_ms=int(a.get("pre_roll_ms", 300)), should_stop=should_stop)

    def _execute_voice(self, mic, command: str) -> None:
        print(f"Ты: {command}")
        self.submit(command, "voice")
        self.wait_idle()
        mic.clear()

    def ptt_press(self) -> None:
        self._ptt_active.set()

    def ptt_release(self) -> None:
        self._ptt_active.clear()

    def run_voice(self) -> None:
        """Wake word / push-to-talk loop (mode can be switched at runtime)."""
        from .audio import make_vad
        a = self.cfg["assistant"]
        follow_up = float(a.get("follow_up_seconds", 0) or 0)
        vad = make_vad(self.cfg["audio"])
        with self._new_mic() as mic:
            self._mic = mic
            if self.mic_muted:
                mic.stop()
            self.greet()
            mic.clear()
            follow_until = 0.0
            print(f"Слушаю... Скажи «{self.name}» и команду. Ctrl+C - выход.")
            while not self.stop_event.is_set():
                self.emit("state", state="muted" if self.mic_muted else "idle")
                if self.mode == "ptt":
                    if not self._ptt_active.wait(0.2):
                        mic.clear()
                        continue
                    self.emit("state", state="listening")
                    mic.clear()
                    chunks = []
                    for frame in self._frames(mic):
                        chunks.append(frame)
                        if not self._ptt_active.is_set() or len(chunks) * mic.frame_ms > 30000:
                            break
                    if not chunks:
                        continue
                    self.emit("state", state="thinking")
                    text = self.stt.transcribe(np.concatenate(chunks), language=self.router.stt_language())
                    if text:
                        wr = self.wake.detect(text)
                        self._execute_voice(mic, wr.command if wr.detected and wr.command else text)
                    continue

                awaiting = self.router.awaiting_input
                in_follow_up = awaiting or time.monotonic() < follow_until
                audio = self._listen(mic, vad, timeout=None if awaiting else (
                    (follow_until - time.monotonic()) if in_follow_up else None),
                    abort=lambda: self.mode != "wake")
                if audio is None:
                    follow_until = 0.0
                    continue
                self.emit("state", state="thinking")
                text = self.stt.transcribe(audio, language=self.router.stt_language())
                if not text:
                    continue
                wr = self.wake.detect(text)
                if wr.detected:
                    command = wr.command
                    if not command:
                        print(f"Ты: {text}")
                        self.emit("user_text", text=text, source="voice")
                        self.say(a.get("wake_reply", "Слушаю"), wait=True)
                        mic.clear()
                        self.emit("state", state="listening")
                        audio = self._listen(mic, vad, timeout=8)
                        command = self.stt.transcribe(audio) if audio is not None else ""
                        if not command:
                            continue
                elif in_follow_up:
                    command = text
                else:
                    log.debug("(без имени, пропускаю): %s", text)
                    continue
                self._execute_voice(mic, command)
                follow_until = time.monotonic() + follow_up if follow_up > 0 and self.profile != "game" else 0.0

    def run_wav(self, path: str) -> None:
        from .audio import read_wav
        audio = read_wav(path, 16000)
        t0 = time.time()
        text = self.stt.transcribe(audio)
        print(f"[распознано за {time.time() - t0:.1f} с] Ты: {text}")
        wr = self.wake.detect(text)
        print(f"[имя «{self.name}» {'найдено' if wr.detected else 'не найдено'}]")
        if wr.detected and not wr.command:
            self.say(self.cfg["assistant"].get("wake_reply", "Слушаю"), wait=True)
            return
        self.submit(wr.command if wr.detected and wr.command else text, "voice")
        self.wait_idle()

    def close(self) -> None:
        self.stop_event.set()
        try:
            if getattr(self, "autosync", None):
                self.autosync.on_exit()
        except Exception as e:
            log.error("sync on exit: %s", e)
        planner = self.router.by_name.get("planner")
        if planner:
            planner.planner.stop()
        if self.speaker:
            self.speaker.close()
