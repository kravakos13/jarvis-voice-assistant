"""--demo: animate the GUI without microphone/LLM (states + lip sync from Piper if available)."""
from __future__ import annotations

import threading
import time


def start_demo(ctl) -> None:
    def run():
        emit = ctl.emit_from_thread
        cfg = ctl.cfg
        lines = ["Добрый день, мистер Флоки, чего желаете?", "Сейчас в Сумах плюс двенадцать, без осадков."]
        try:
            from ..tts import PiperTTS, speech_envelope
            from ..audio import Player
            tts, player = PiperTTS(cfg["tts"]), Player(cfg["audio"].get("output_device"))
        except Exception:
            tts = player = None
        while not ctl.quitting:
            for i, text in enumerate(lines):
                emit("state", state="listening")
                for k in range(40):
                    emit("mic_level", level=abs(((k * 7) % 10) / 10 - 0.3))
                    time.sleep(0.03)
                if i:
                    emit("user_text", text="Джарвис, какая погода?")
                emit("state", state="thinking")
                time.sleep(1.5)
                emit("assistant_text", text=text)
                if tts:
                    audio, rate = tts.synthesize(text)
                    env, wid = speech_envelope(audio, rate)
                    emit("speech", env=env, wid=wid, dt=0.02)
                    try:
                        player.play(audio, rate)
                    except Exception:
                        time.sleep(len(audio) / rate)
                else:
                    import math
                    env = [abs(math.sin(j / 3)) for j in range(150)]
                    emit("speech", env=env, wid=[0.5] * 150, dt=0.02)
                    time.sleep(3)
                emit("speech_end")
                emit("state", state="idle")
                time.sleep(2)
    threading.Thread(target=run, daemon=True).start()
