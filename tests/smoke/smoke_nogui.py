"""Smoke test with mocked microphone/speaker. Usage: python tests/smoke/smoke_nogui.py q1.wav q2.wav"""
import os, sys, threading, time
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(_P(__file__).parent))
os.chdir(ROOT)
import fakeaudio, faulthandler; faulthandler.dump_traceback_later(120, exit=True)
fakeaudio.FILES[:] = sys.argv[1:]   # WAVs, e.g. made with: python -m jarvis --say "Джарвис, который час?" --out q1.wav
fakeaudio.install()
from jarvis.config import load_config
from jarvis.main import setup_logging
cfg = load_config()
cfg["llm"]["model"] = "qwen2.5:1.5b"; cfg["stt"]["device"] = "cpu"
setup_logging(cfg["logging"], False)
events = []
from jarvis.assistant import Assistant
a = Assistant(cfg, speak=True, need_stt=True, on_event=lambda ev, **d: events.append((ev, d)))
t = threading.Thread(target=a.run_voice, daemon=True); t.start()
deadline = time.time() + 150
while time.time() < deadline:
    texts = [d.get("text") for e, d in events if e == "assistant_text"]
    if sum(1 for e, d in events if e == "user_text") >= 2 and len(texts) >= 2:
        time.sleep(2); break
    time.sleep(0.5)
for e, d in events:
    if e in ("user_text", "assistant_text", "profile", "notice"):
        print("EVENT", e, d)
print("PLAYED", [round(x, 1) for x in fakeaudio.PLAYED])
a.stop() if hasattr(a, "stop") else None
a.close() if hasattr(a, "close") else None
print("SMOKE_DONE")
