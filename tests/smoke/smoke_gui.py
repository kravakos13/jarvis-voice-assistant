"""GUI smoke test (offscreen or xvfb). Usage: python tests/smoke/smoke_gui.py q1.wav q2.wav
Real Assistant (Whisper, Piper, router, Ollama) with mocked mic/speaker.
Saves frames to /tmp/frames for the demo video and a screenshot."""
import os, sys, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_NO_FIRSTRUN", "1")  # no first-run face dialog in the smoke run
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(_P(__file__).parent))
os.chdir(ROOT)
import fakeaudio
fakeaudio.FILES[:] = sys.argv[1:]
fakeaudio.START_DELAY = int(os.environ.get("SMOKE_START_DELAY", "15"))
fakeaudio.install()
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtGui import QPainter, QColor, QImage
from PySide6.QtWidgets import QApplication
from jarvis.config import load_config
from jarvis.main import setup_logging
cfg = load_config(); cfg["llm"]["model"] = "qwen2.5:1.5b"; cfg["stt"]["device"] = "cpu"
cfg["gui"]["animations"] = os.environ.get("SMOKE_ANIM", cfg["gui"].get("animations", "full"))  # software GL is slow
setup_logging(cfg["logging"], False)
import jarvis.gui.app as G
G.prepare_app(cfg)  # 3D head: scheme registration before QApplication
app = QApplication(sys.argv)
ctl = G.Controller(app, cfg, demo=False, speak=True)
ctl.apply_ui(); ctl.window.show(); ctl.start_assistant()
out = Path("/tmp/frames"); out.mkdir(exist_ok=True)
for f in out.glob("*.png"): f.unlink()
n = [0]; t0 = time.time(); events = []
ctl.bridge.event.connect(lambda ev, data: events.append((ev, data)))
def grab():
    pm = ctl.window.grab()
    img = QImage(pm.size(), QImage.Format_RGB32); img.fill(QColor(8, 12, 20))
    p = QPainter(img); p.drawPixmap(0, 0, pm); p.end()
    img.save(str(out / f"f{n[0]:05d}.png")); n[0] += 1
    users = [d for e, d in events if e == "user_text"]
    if time.time() - t0 > float(os.environ.get("SMOKE_TIMEOUT", "110")) or (len(users) >= len(fakeaudio.FILES) and time.time() - users_t[0] > 9):
        img.save("/tmp/gui_final.png")
        print("USER", [d.get("text") for d in users], flush=True); print("ASSIST", [d.get("text") for e, d in events if e == "assistant_text"], flush=True)
        print("FRAMES", n[0], flush=True); ctl.quit(0) if hasattr(ctl, "quit") else app.quit()
    if len(users) < len(fakeaudio.FILES):
        users_t[0] = time.time()
users_t = [time.time()]
tm = QTimer(); tm.timeout.connect(grab); tm.start(100)
app.exec()
print("GUI_SMOKE_DONE")
os._exit(0)
