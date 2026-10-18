"""Assistant core: voice loop, command worker, LLM + tools, events for the GUI.

Threads:
  - voice loop  (microphone -> VAD -> Whisper -> wake word)   [jarvis-voice]
  - brain       (commands queue -> skills / LLM -> TTS)         [jarvis-brain]
  - TTS worker, planner scheduler, Telegram, background jobs
The GUI only receives events (thread-safe via Qt signals) and never blocks.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import logging
import queue
import re
import threading
import time
from typing import Any, Callable, Optional

import numpy as np

from .config import PROJECT_ROOT
from .llm import FALLBACK_RU, LLMError, OllamaChat, SentenceSplitter, has_cjk, not_russian, strip_cjk, strip_note
from .router import Router
from .skills.base import SkillContext, ToolResult
from .wake import WakeWordDetector
from .wakeflow import WakeFlow
from .timing import Timings, Trace

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
             "на компьютере, или нужна свежая информация из интернета. Для обычной болтовни инструменты не нужны. "
             "НИКОГДА не говори, что что-то сделал (закрыл, открыл, включил, выключил, отправил, удалил), если не "
             "вызвал для этого инструмент — результат сообщит сам инструмент. Если подходящего инструмента нет, "
             "честно скажи, что этого пока не умеешь.")
# "I did it" narration: only allowed when a tool really ran
CLAIM_RE = re.compile(r"^\W*(?:(?:хорошо|окей|ок|готово|сделано|есть|принято|понял)[,.!\s—-]*)?(?:сейчас\s+|уже\s+)?"
                      r"(закрыл\w*|закрываю|закрыто|открыл\w*|открываю|запустил\w*|запускаю|включил\w*|включаю|"
                      r"выключил\w*|выключаю|свернул\w*|сворачиваю|развернул\w*|отправил\w*|отправляю|удалил\w*|"
                      r"удаляю|поставил\w*|ставлю|остановил\w*|переключил\w*|сделал\w*|готово|сделано)\b", re.I)
HONEST_NO_TOOL = "Это я сделать не смог — такой команды у меня пока нет. Скажи по-другому или сделай вручную."


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
    stt: Any = None                      # set after set_profile() in __init__
    _saved_keep_alive: Any = None        # game mode (GPU placement): keep_alive before unloading
    _llm_moved: bool = False             # game mode (auto placement): model moved to RAM
    def __init__(self, cfg: dict, speak: bool = True, need_stt: bool = True,
                 on_event: Optional[Callable[..., None]] = None):
        self.cfg = cfg
        self.on_event = on_event or (lambda *a, **k: None)
        self.stop_event = threading.Event()
        a = cfg["assistant"]
        self.name = a.get("name", "Джарвис")
        self.mode = a.get("mode", "wake")
        self.mic_muted = False
        self._said = []                                   # (text, time) of what Jarvis said: echo rejection
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
            self.emit("notice", text="Загружаю распознавание речи...")
            self.stt = self._load_stt(cfg["stt"])

        self.timings = Timings()
        self._trace: Optional[Trace] = None
        self._stt_last: Optional[tuple[float, float]] = None     # (start, seconds) of the last transcription
        self._ack_text = ""
        self.llm.warm_tools = self.router.tool_schemas() if self.tool_calling else None
        self.router.ctx.extra["stt_conf"] = lambda: getattr(self.stt, "last_conf", None)
        if self.llm.placement == "cpu":
            self.llm.on_cpu = True
        # after Whisper took its VRAM: load the LLM and pre-evaluate system prompt + tools, so the first
        # command is not a cold start (several seconds on a 6 GB card)
        if self.llm_ok and cfg["llm"].get("warm_up", True):
            threading.Thread(target=self._warm_and_check, name="llm-warm", daemon=True).start()
        from .resources import Monitor
        self.monitor = Monitor(self.llm, float((cfg.get("load") or {}).get("log_every_s", 300) or 300))
        threading.Thread(target=self.monitor.run, args=(self.stop_event,), name="jarvis-res", daemon=True).start()

        self._commands: "queue.Queue[tuple]" = queue.Queue()
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
        if event == "state":
            st = data.get("state", "")
            if getattr(self, "_vstate", ("",))[0] != st:
                self._vstate = (st, time.monotonic())
        try:
            self.on_event(event, **data)
        except Exception as e:
            log.debug("event %s: %s", event, e)

    def _on_tts(self, event: str, **data) -> None:
        if event == "speech_start":
            self._said.append((str(data.get("text") or ""), time.monotonic()))
            del self._said[:-5]
            tr = getattr(self, "_trace", None)
            if tr is not None:
                if self._ack_text and data.get("text") == self._ack_text:
                    self._ack_text = ""                       # «Секунду» is not the answer
                else:
                    tr.mark("first_audio")
            self.emit("state", state="speaking")
            self.emit("speech", **data)
        elif event == "speech_end":
            self._tts_end_at = time.monotonic()
            self.emit("speech_end")
            idle = getattr(self, "_idle", None)
            sp = self.speaker
            more = sp is not None and getattr(getattr(sp, "_q", None), "unfinished_tasks", 0) > 1
            if idle is not None and not idle.is_set() and not more:
                self.emit("state", state="thinking")          # LLM still generating: not «speaking» (watchdog label)

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
        old_model = self.llm.model
        self.llm.model = l.get("model", self.llm.model)
        self.llm.temperature = float(l.get("temperature", 0.6))
        self._compose_prompt()
        moved = self.apply_placement(str(l.get("placement", "gpu") or "gpu").lower())
        if self.llm.model != old_model and not moved and self.llm_ok:
            threading.Thread(target=self.llm.warm, args=(self.llm.warm_tools,), daemon=True).start()
        eng = self.speaker.engine if self.speaker else None
        if eng is not None and hasattr(eng, "length_scale"):
            eng.length_scale = float(t.get("length_scale", 1.0))
            eng.volume = float(t.get("volume", 1.0))
            eng.pitch = float(t.get("pitch_semitones", 0.0))
            fx = t.get("fx") or {}
            eng.fx_enabled = bool(fx.get("enabled", False))
            eng.fx_intensity = float(fx.get("intensity", 0.35))

    def apply_placement(self, placement: str) -> bool:
        """Settings → «Где работает ИИ» changed: move the model now (no restart). Returns True if it moved."""
        if placement not in ("gpu", "cpu", "auto"):
            placement = "gpu"
        if placement == self.llm.placement:
            return False
        self.llm.placement = placement
        in_game = self.profile == "game" and self.cfg.get("vram", {}).get("release_in_game", True)
        if getattr(self, "_saved_keep_alive", None) is not None and placement != "gpu":
            self.llm.keep_alive = self._saved_keep_alive       # GPU-unload of the game mode is not needed now
            self._saved_keep_alive = None
        want_cpu = placement == "cpu" or (placement == "auto" and in_game)
        self._llm_moved = placement == "auto" and in_game
        if placement == "gpu" and in_game:                     # behave like the GPU game mode: free VRAM
            self.llm.on_cpu = False
            self.release_gpu()
            return True
        return self.llm.set_on_cpu(want_cpu)

    # ------------------------------------------------------------ privacy / VRAM
    def set_mic_muted(self, muted: bool, cue: bool = True) -> None:
        """Privacy pause: the input stream is really closed while muted (nothing is captured)."""
        if muted == self.mic_muted:
            return
        self.mic_muted = muted
        mic = getattr(self, "_mic", None)
        try:
            if mic is not None:
                (mic.pause() if hasattr(mic, "pause") else mic.stop()) if muted else (mic.start(), mic.clear())
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
        placement = self.llm.placement
        if placement == "auto":
            # keep answering in games: the model moves to RAM (num_gpu=0) instead of being unloaded
            self._llm_moved = self.llm.set_on_cpu(True)
        elif placement == "gpu":
            self._saved_keep_alive = getattr(self, "_saved_keep_alive", None) or self.llm.keep_alive
            self.llm.keep_alive = 0
            threading.Thread(target=self.llm.unload_all, daemon=True).start()
        # placement "cpu": the model is in RAM already, nothing to free
        if self.stt is not None and getattr(self.stt, "device", "cpu") == "cuda":
            mode = vc.get("whisper_in_game", "cpu")
            threading.Thread(target=self.stt.relocate, args=("cpu" if mode == "cpu" else None,), daemon=True).start()
        log.info("VRAM освобождена для игры")

    def restore_gpu(self) -> None:
        if getattr(self, "_llm_moved", False):
            self._llm_moved = False
            self.llm.set_on_cpu(False)                # back to the GPU (re-warmed in the background)
        if getattr(self, "_saved_keep_alive", None) is None:
            if self.stt is not None and getattr(self.stt, "relocated", False):
                threading.Thread(target=self.stt.relocate, args=("restore",), daemon=True).start()
            return
        self.llm.keep_alive = self._saved_keep_alive
        self._saved_keep_alive = None
        threading.Thread(target=self.llm.warm, args=(self.llm.warm_tools,), daemon=True).start()
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
    def submit(self, text: str, source: str = "text", trace: Optional[Trace] = None) -> None:
        """Queue a user command (from the GUI text box, voice loop, or console)."""
        if text and text.strip():
            self._idle.clear()
            self._commands.put((text.strip(), source, trace))

    def _load_stt(self, scfg: dict):
        """Whisper as configured; if that fails for any reason, CPU/int8: never lose voice mode to the STT device."""
        from .stt import SpeechToText

        def notice(t: str) -> None:
            self.emit("notice", text=t)
        try:
            return SpeechToText(scfg, on_notice=notice)
        except Exception as e:
            if str(scfg.get("device", "")).lower() == "cpu" and scfg.get("compute_type") in (None, "auto", "int8"):
                raise                               # already the safest setting: a real problem (model files...)
            log.warning("Whisper (%s) не загрузился: %s — пробую процессор/int8.", scfg.get("device"), e)
            return SpeechToText(dict(scfg, device="cpu", compute_type="int8"), on_notice=notice)

    def _transcribe(self, audio, language=None) -> str:
        """Whisper with timing (the trace of the command starts when the user stopped speaking)."""
        t0 = time.monotonic()
        try:
            text = self.stt.transcribe(audio, language=language) if self.stt is not None else ""
        except Exception as e:                  # never let one failed phrase kill the voice loop
            log.exception("Распознавание не удалось: %s", e)
            text = ""
        self._stt_last = (t0, time.monotonic() - t0)
        if text and self._is_echo(text):
            log.debug("STT отброшено (это моя собственная фраза): %r", text)
            return ""
        return text

    _said: list = []

    def _is_echo(self, heard: str, window_s: float = 20.0) -> bool:
        """The mic caught Jarvis's own voice (speakers, room echo after the mute tail)."""
        from .stt import _plain
        h = _plain(heard)
        if not h:
            return False
        now = time.monotonic()
        for text, at in self._said:
            if now - at > window_s:
                continue
            said = _plain(text)
            if not said:
                continue
            if h == said or (len(h.split()) >= 2 and h in said):
                return True
            if difflib.SequenceMatcher(None, h, said).ratio() >= 0.8:
                return True
        return False

    def _voice_trace(self, command: str) -> Trace:
        if self._stt_last:
            t0, dur = self._stt_last
            tr = Trace(command, "voice", t0=t0)
            tr.add("stt", dur)
            return tr
        return Trace(command, "voice")

    def _start_ack(self, tr: Trace, source: str) -> Optional[threading.Timer]:
        """Slow answer (web search, long LLM turn): after ack_after_s of silence say a short «Секунду»."""
        try:
            after = float(self.cfg["assistant"].get("ack_after_s", 2.5) or 0)
        except (TypeError, ValueError):
            after = 0.0
        if after <= 0 or source != "voice" or not self.speaker:
            return None

        def ack():
            if self._trace is tr and tr.get("first_audio") is None and "ack" not in tr.marks:
                tr.mark("ack")
                self._ack_text = "Секунду."
                self.speaker.say(self._ack_text)
        t = threading.Timer(after, ack)
        t.daemon = True
        t.start()
        return t

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)

    def _brain_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                text, source, trace = self._commands.get(timeout=0.3)
            except queue.Empty:
                continue
            tr = trace or Trace(text, source)
            self._trace = tr
            ack = self._start_ack(tr, source)
            try:
                if source == "noname":
                    self._respond_silent(text)
                    continue
                self.emit("user_text", text=text, source=source)
                if self.respond(text) == "exit":
                    self.emit("exit")
                    self.stop_event.set()
            except Exception as e:
                log.exception("Ошибка обработки команды: %s", e)
                self.say("Что-то пошло не так, глянь лог.")
            finally:
                if ack is not None:
                    ack.cancel()
                if self.speaker:
                    self.speaker.wait()
                self._trace = None
                try:
                    self.timings.finish(tr)
                except Exception as e:
                    log.debug("timing: %s", e)
                try:
                    self.router.after_turn()          # conversation mode: silence is counted after the answer
                except Exception as e:                # noqa: BLE001
                    log.debug("after_turn: %s", e)
                if self._commands.empty():
                    self.emit("state", state="idle")
                    self._idle.set()

    def _respond_silent(self, text: str) -> None:
        """No-name command: fast path only (never the LLM), no voice reply, never «exit»."""
        r = self.router.handle(text)
        if r is None:
            log.debug("Без имени: никто не взял %r — молчу", text)
            return
        self.emit("user_text", text=text, source="noname")     # GUI shows it briefly
        self.emit("noname", text=text)                          # face flash
        if r.reply:
            log.info("Без имени выполнено: %s → %s", text, r.reply)
        if r.then:
            def job():
                try:
                    extra = r.then()
                    if isinstance(extra, ToolResult) and extra.then:
                        extra.then()
                except Exception as e:                           # noqa: BLE001
                    log.exception("action: %s", e)
            threading.Thread(target=job, daemon=True).start() if r.background else job()

    def _warm_and_check(self) -> None:
        """1.0.8: warm the model, then a tiny self-test. Garbage (CJK / no Cyrillic, seen on GTX 1660 Super with
        flash attention + q8 KV) -> restart Ollama with safe settings and test again; still bad -> tell the user."""
        from . import ollama_setup
        if self.llm.warm(self.llm.warm_tools) is None:
            return
        if not (self.cfg.get("ollama") or {}).get("selftest", True):
            return
        if self.llm.selftest() != "garbage":
            return
        log.warning("ИИ отвечает мусором — перезапускаю Ollama без flash attention и с KV f16")
        self.emit("notice", text="ИИ отвечал мусором — перезапускаю Ollama с безопасными настройками…")
        if ollama_setup.is_local(ollama_setup.host_of(self.cfg)) and ollama_setup.restart_safe(self.cfg):
            self.llm.warm(self.llm.warm_tools)
            res = self.llm.selftest()
            if res == "ok":
                log.info("После перезапуска Ollama ИИ отвечает нормально")
                self.emit("notice", text="ИИ снова в порядке.")
                return
        log.error("ИИ всё ещё отвечает мусором: попробуй «Нагрузка: Лёгкая» (qwen2.5:3b) или обнови Ollama и "
                  "драйвер видеокарты")
        self.emit("notice", text="ИИ отвечает мусором даже после перезапуска Ollama. Команды работают; для разговора "
                                 "попробуй в настройках «Нагрузка: Лёгкая» или обнови Ollama и драйвер видеокарты.")

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
        tr = getattr(self, "_trace", None)
        t0 = time.monotonic()
        r = self.router.handle(text)
        if tr is not None:
            tr.add("route", time.monotonic() - t0)
            tr.path = (getattr(self.router, "last_skill", "") or "?") if r is not None else "llm"
        if r is not None:
            return self._run_result(r)
        return self._chat(text)

    def _chat(self, text: str) -> Optional[str]:
        """Stream the LLM answer sentence by sentence to TTS, with two guards:
        * language: a sentence with CJK (or an English first sentence) is never spoken — the stream is
          abandoned and the answer regenerated once in Russian (llm.regenerate_russian), CJK stripped last;
        * false success: an "I closed/opened/…" sentence is held back while tools are offered; if no tool was
          actually called it is replaced with an honest "не смог", if a tool ran its own (verified) reply is used."""
        splitter = SentenceSplitter()
        tools = self.router.tool_schemas() if self.tool_calling else None
        calls: list = []
        print(f"{self.name}: ", end="", flush=True)
        spoken: list[str] = []
        held: list[str] = []
        drift = False

        def out(sentence: str) -> bool:
            """False = drift detected (stop the stream)."""
            nonlocal drift
            if has_cjk(sentence) or (not spoken and not held and not_russian(sentence)):
                drift = True
                return False
            if tools and not spoken and (held or CLAIM_RE.match(sentence)):
                held.append(sentence)                  # wait: did a tool really run?
                return True
            spoken.append(sentence)
            print(sentence, end=" ", flush=True)
            self.emit("assistant_partial", text=sentence + " ")
            if self.speaker:
                self.speaker.say(sentence)
            return True
        try:
            stream = self.llm.stream(text, tools=tools, tool_calls_out=calls)
            tr = getattr(self, "_trace", None)
            for piece in stream:
                if tr is not None:
                    tr.mark("llm_first")
                if any(not out(sn) for sn in splitter.feed(piece)) or self.stop_event.is_set():
                    break
            else:
                for sn in splitter.flush():
                    if not out(sn):
                        break
            if tr is not None:
                tr.mark("llm_first")                       # tool-call-only answers have no text pieces
                tr.mark("llm_done")
                tr.llm_stats = dict(getattr(self.llm, "last_stats", None) or {})
            if drift:
                close = getattr(stream, "close", None)
                if close:
                    close()                                # abandon the drifting stream (history keeps the user msg)
                log.warning("LLM: ответ уходит не на русский язык — переспрашиваю по-русски")
                calls.clear()
                held.clear()
                try:
                    fixed = strip_cjk(self.llm.regenerate_russian())
                except Exception as e:                 # noqa: BLE001 — never hang/die here (1.0.6 «falls asleep»)
                    log.exception("LLM: повтор по-русски упал: %s", e)
                    fixed = FALLBACK_RU
                if spoken and fixed.startswith(" ".join(spoken)[:20]):
                    fixed = fixed[len(" ".join(spoken)):].strip()
                if fixed:
                    spoken.append(fixed)
                    print(fixed, end="", flush=True)
                    self.emit("assistant_partial", text=fixed)
                    if self.speaker:
                        self.speaker.say(fixed)
            print()
        except LLMError as e:
            print()
            log.error("%s", e)
            msg = str(e).lower()
            if msg.startswith("cancelled"):
                return None                               # «Перезапустить прослушку» — silently
            if msg.startswith("memory:"):
                self.say(str(e).split(":", 1)[1].strip())
            elif "timeout" in msg or "timed out" in msg:
                self.say("Слишком долго думаю. Повтори, пожалуйста, покороче.")
            elif "repeat" in msg or "aborted" in msg:
                self.say("Что-то я зациклился. Спроси ещё раз, пожалуйста, по-другому.")
            elif "not found" in msg and "model" in msg:
                self.say(f"Модель {self.llm.model} не скачана. Открой Настройки, вкладка ИИ, и нажми «Скачать модель».")
            else:
                self.say("Языковая модель не отвечает. Проверь, что Ollama запущена.")
            return None
        if held and not calls:
            log.warning("LLM заявил о действии без вызова инструмента: %r", " ".join(held))
            self._replace_last_answer(" ".join(held), HONEST_NO_TOOL)
            spoken.append(HONEST_NO_TOOL)
            self.say(HONEST_NO_TOOL)
        elif held:                                     # a tool ran: its verified reply replaces the narration
            self._replace_last_answer(" ".join(held), "")
        if spoken:
            self.emit("assistant_done", text=" ".join(spoken))
        action = None
        for c in calls:
            log.info("tool call: %s %s", c["name"], c["arguments"])
            r = self.router.call_tool(c["name"], c["arguments"])
            if r is not None:
                self.llm.add_assistant_message(r.reply or f"[{c['name']}]")
                action = self._run_result(r) or action
            else:
                self.say("Не получилось: такой команды у меня нет.")
        self._turns += 1
        cap = int(self.cfg["llm"].get("max_history_messages", 16))
        if len(self.llm.history) >= cap and self._turns % max(2, cap // 4) == 0:
            self._summarize_async(self.llm.history[: cap // 2])
        return action

    def _replace_last_answer(self, old: str, new: str) -> None:
        """Fix the stored assistant turn so the model does not learn from its own false claim."""
        h = self.llm.history
        if h and h[-1].get("role") == "assistant" and old[:20] in h[-1].get("content", ""):
            if new:
                h[-1] = {"role": "assistant", "content": new}
            else:
                h.pop()

    def _summarize_async(self, messages: list) -> None:
        mem = self.router.by_name.get("memory")
        if mem and messages and self.cfg.get("memory", {}).get("summaries", True):
            plain = [dict(m, content=strip_note(m.get("content", ""))) for m in messages]
            threading.Thread(target=mem.memory.summarize, args=(plain,), daemon=True).start()

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
        """Mic frames with level events; while muted, frames are dropped.
        While Jarvis speaks (plus a short tail for echo) the mic hears silence, so its own
        voice can never wake it or become a command; timeouts keep running on the silence."""
        k = 0
        tail = float(self.cfg["assistant"].get("tts_tail_ms", 500) or 0) / 1000.0
        for frame in mic.frames(self.stop_event):
            self._hb = time.monotonic()
            rl = getattr(self, "_relisten", None)
            if rl is not None and rl.is_set():
                return
            if self.mic_muted:
                continue
            if self.speaker is not None and self.speaker.active(tail):
                yield np.zeros_like(frame)
                continue
            k += 1
            if k % 3 == 0:
                self.emit("mic_level", level=min(1.0, float(np.sqrt(np.mean(frame * frame))) * 8))
            yield frame

    def _listen(self, mic, vad, timeout: Optional[float] = None, abort: Optional[Callable[[], bool]] = None,
                silence_ms: Optional[int] = None, max_s: Optional[float] = None):
        from .audio import segment_utterance
        a = self.cfg["audio"]
        deadline = time.monotonic() + timeout if timeout else None

        def should_stop():
            return (self.stop_event.is_set() or (deadline is not None and time.monotonic() > deadline)
                    or (abort is not None and abort()))

        return segment_utterance(
            self._frames(mic), vad, mic.rate,
            frame_ms=int(a.get("frame_ms", 30)), silence_ms=int(silence_ms or a.get("silence_ms", 800)),
            min_speech_ms=int(a.get("min_speech_ms", 250)), max_utterance_s=float(max_s or a.get("max_utterance_s", 15)),
            pre_roll_ms=int(a.get("pre_roll_ms", 300)), should_stop=should_stop)

    def _execute_voice(self, mic, command: str) -> None:
        print(f"Ты: {command}")
        self.submit(command, "voice", self._voice_trace(command))
        if not self.wait_idle(self.STUCK_S * 2):
            log.warning("Команда «%s» выполняется > %.0f с — продолжаю слушать", command[:60], self.STUCK_S * 2)
        mic.clear()

    def ptt_press(self) -> None:
        self._ptt_active.set()

    def ptt_release(self) -> None:
        self._ptt_active.clear()

    def _wake_flow(self, mic, vad) -> WakeFlow:
        def say(text: str) -> None:
            self.say(text, wait=True)
            mic.clear()

        return WakeFlow(
            detect=lambda t: self.wake.detect(t),
            listen=lambda timeout=None, silence_ms=None, max_s=None: self._listen(
                mic, vad, timeout=timeout, silence_ms=silence_ms, max_s=max_s, abort=lambda: self.mode != "wake"),
            transcribe=lambda audio: self._transcribe(audio, language=self.router.stt_language()),
            say=say,
            execute=lambda command: self._execute_voice(mic, command),
            emit=lambda st: self.emit("state", state="muted" if self.mic_muted else st),
            awaiting=lambda: bool(self.router.awaiting_input),
            awaiting_timeout=lambda: self.router.awaiting_timeout,
            cfg=self.cfg["assistant"],
            follow_allowed=lambda: self.profile != "game",
            noname=lambda text: self._noname(mic, text))

    _tts_end_at = 0.0

    def _tts_quiet(self) -> bool:
        from .noname import TTS_GAP_S
        sp = self.speaker
        busy = False
        if sp is not None:
            q = getattr(sp, "_q", None)
            busy = bool(getattr(sp, "speaking", False) or getattr(sp, "busy", False)
                        or (q is not None and getattr(q, "unfinished_tasks", 0)))
        return not busy and time.monotonic() - self._tts_end_at >= TTS_GAP_S

    def _noname(self, mic, text: str) -> bool:
        """1.0.8: a phrase without «Джарвис» -> run silently if it is exactly a safe, known command."""
        from . import noname
        conf = getattr(self.stt, "last_conf", None)
        cmd = noname.decide(text, self.cfg["assistant"], conf, self._tts_quiet(), self.router)
        if cmd is None:
            return False
        log.info("Без имени: %r (conf=%s)", cmd, conf)
        self.submit(cmd, "noname")
        self.wait_idle(self.STUCK_S)
        mic.clear()
        return True

    # ---------------------------------------------------------------- voice loop supervision (1.0.7)
    STUCK_S = 60.0          # listening / thinking / speaking longer than this = stuck -> reset
    DEAF_S = 15.0           # no mic frames consumed while waiting for the wake word -> restart the stream
    MIC_GRACE_S = 5.0       # after a restart the mic gets this long before it can be declared dead

    def _stuck_limit(self, st: str) -> float:
        """THINKING may legitimately last a whole LLM turn + one bounded retry; the others STUCK_S."""
        if st == "thinking":
            llm = getattr(self, "llm", None)
            from .llm import RETRY_TIMEOUT_S
            return max(self.STUCK_S, float(getattr(llm, "turn_timeout", 90) or 90) + RETRY_TIMEOUT_S + 10)
        return self.STUCK_S

    def run_voice(self) -> None:
        """Supervisor: the listening session is re-opened after ANY error or a restart request, so one bad phrase,
        a vanished microphone or a stuck state can never leave Jarvis deaf until the app is restarted."""
        self._relisten = threading.Event()
        self._voice_alive = True
        self._hb = time.monotonic()
        if not getattr(self, "_watchdog_started", False):
            self._watchdog_started = True
            threading.Thread(target=self._watchdog, name="jarvis-voice-watchdog", daemon=True).start()
        greet = True
        try:
            while not self.stop_event.is_set():
                self._relisten.clear()
                try:
                    self._voice_session(greet)
                except Exception as e:                    # noqa: BLE001 — full traceback to jarvis.log
                    log.exception("Голосовая сессия упала: %s — перезапускаю прослушку", e)
                    self.stop_event.wait(2.0)
                finally:
                    greet = False
                    self._reset_voice_state()
                if self._relisten.is_set():
                    log.warning("Прослушка перезапущена")
        finally:
            self._voice_alive = False

    def _reset_voice_state(self) -> None:
        try:
            self._ptt_active.clear()
            self.emit("state", state="muted" if self.mic_muted else ("sleeping" if self.mode == "wake" else "idle"))
        except Exception as e:                            # noqa: BLE001
            log.debug("reset state: %s", e)

    def restart_listening(self, reason: str = "по запросу") -> bool:
        """Tray «Перезапустить прослушку» / watchdog. Returns False if the voice loop is not running at all."""
        log.warning("Перезапуск прослушки: %s", reason)
        sp = self.speaker
        if sp is not None and (getattr(sp, "stuck", lambda: False)() or reason == "по запросу"):
            try:
                sp.interrupt()
            except Exception as e:                        # noqa: BLE001
                log.debug("interrupt: %s", e)
        if reason == "по запросу" or reason.startswith("завис"):
            try:
                self.llm.cancel()                         # 1.0.8: never keep waiting on a stuck LLM answer
            except Exception as e:                        # noqa: BLE001
                log.debug("llm cancel: %s", e)
        self._hb = time.monotonic() + self.MIC_GRACE_S    # a reopened stream needs a few seconds for frames
        ev = getattr(self, "_relisten", None)
        if ev is not None:
            ev.set()
        return bool(getattr(self, "_voice_alive", False))

    def _watchdog(self) -> None:
        while not self.stop_event.wait(2.0):
            try:
                self._watch_once(time.monotonic())
            except Exception as e:                        # noqa: BLE001
                log.exception("watchdog: %s", e)

    def _watch_once(self, now: float) -> Optional[str]:
        """One watchdog check; returns what it did (tests)."""
        if not getattr(self, "_voice_alive", False) or self.mic_muted:
            return None
        sp = self.speaker
        if sp is not None and getattr(sp, "stuck", lambda: False)():
            log.warning("Речь зависла (> %.0f с) — сбрасываю", self.STUCK_S)
            sp.interrupt()
            return "tts"
        st, since = getattr(self, "_vstate", ("", now))
        if st in ("listening", "thinking", "speaking") and now - since > self._stuck_limit(st):
            log.warning("Состояние «%s» держится %.0f с — сбрасываю прослушку", st, now - since)
            self._vstate = ("", now)
            self.restart_listening(f"завис в состоянии {st}")
            return "stuck"
        if st in ("sleeping", "idle", "") and self.mode == "wake" and now - getattr(self, "_hb", now) > self.DEAF_S:
            self._hb = now
            self.restart_listening("нет кадров с микрофона")
            return "deaf"
        return None

    def _voice_session(self, greet: bool) -> None:
        """Wake word / push-to-talk loop (mode can be switched at runtime)."""
        from .audio import make_vad
        vad = make_vad(self.cfg["audio"])
        with self._new_mic() as mic:
            self._mic = mic
            if self.mic_muted:
                mic.pause() if hasattr(mic, "pause") else mic.stop()
            if greet:
                self.greet()
            mic.clear()
            flow = self._wake_flow(mic, vad)
            print(f"Сплю. Скажи «{self.name}» (или «{self.name}, <команда>»). Ctrl+C - выход.")
            while not self.stop_event.is_set() and not self._relisten.is_set():
                self.emit("state", state="muted" if self.mic_muted else ("sleeping" if self.mode == "wake" else "idle"))
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
                    text = self._transcribe(np.concatenate(chunks), language=self.router.stt_language())
                    if text:
                        wr = self.wake.detect(text)
                        self._execute_voice(mic, wr.command if wr.detected and wr.command else text)
                    continue

                flow.cfg = self.cfg["assistant"]          # live: Settings changes apply at once
                try:
                    flow.step()
                except Exception as e:                    # keep listening: one bad step must not drop to text mode
                    log.exception("Голосовой цикл: %s", e)
                    self.stop_event.wait(0.5)
                finally:
                    flow.state = ""                       # next step always starts from «waiting for the name»

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
