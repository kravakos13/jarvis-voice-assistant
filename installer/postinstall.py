"""Run by JarvisSetup.exe after copying files (with the bundled embeddable Python):

  1. pip (get-pip.py) + Python packages from requirements.txt (or the bundled wheels\\ folder)
  2. Piper voice + Whisper model (scripts/download_models.py)
  3. Ollama via jarvis/ollama_setup.py: download OllamaSetup.exe (resume/retries) + silent install if missing,
     start it hidden, pull missing models over the HTTP API (no PATH needed)

Every step is logged to logs\\install.log; a failed step does not abort the others —
the app's self-diagnostics ("проверь себя") offers to fix whatever is missing later.
Usage: python installer\\postinstall.py [--core-only] [--no-ollama] [--no-models]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)
LOG = ROOT / "logs" / "install.log"
CORE = ["PyYAML>=6.0", "requests>=2.31", "numpy>=1.24", "psutil>=5.9", "keyring>=24", "dateparser>=1.2"]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode(), flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(argv: list, **kw) -> int:
    log("> " + " ".join(str(a) for a in argv))
    try:
        return subprocess.call([str(a) for a in argv], cwd=str(ROOT), **kw)
    except OSError as e:
        log(f"  ошибка запуска: {e}")
        return 1


def ensure_pip() -> bool:
    if run([PY, "-m", "pip", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        return True
    gp = ROOT / "installer" / "get-pip.py"
    wheels = ROOT / "wheels"
    extra = ["--no-index", "--find-links", wheels] if wheels.is_dir() else []
    return run([PY, gp, "--no-warn-script-location", *extra]) == 0


def install_packages(core_only: bool) -> bool:
    base = [PY, "-m", "pip", "install", "--no-warn-script-location", "--disable-pip-version-check"]
    wheels = ROOT / "wheels"
    if wheels.is_dir():                      # offline install from bundled wheels
        base += ["--no-index", "--find-links", wheels]
    if core_only:
        return run(base + CORE) == 0
    if run(base + ["-r", ROOT / "requirements.txt"]) == 0:
        face = ROOT / "requirements-face.txt"      # optional (auto face landmarks); failure is not an error
        if face.is_file() and run(base + ["-r", face]) != 0:
            log("Автоопределение лица (mediapipe) не установилось — метки лица можно поставить вручную.")
        return True
    log("Полная установка пакетов не удалась — ставлю базовые, остальное можно докачать позже.")
    run(base + CORE)
    return False


def _progress():
    """Console progress (the installer shows this window); the log gets every 10%."""
    state = {"pct": -1, "label": ""}

    def cb(label: str, pct) -> None:
        if pct is None:
            log(label)
            return
        try:
            print(f"\r  {label} ({pct}%)   ", end="", flush=True)
        except UnicodeEncodeError:
            pass
        if pct // 10 != state["pct"] // 10 or pct == 100:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}]   {label} ({pct}%)\n")
        state["pct"] = pct
    return cb


def _log_to_file() -> None:
    """jarvis.* log records (HTTP status / Ollama error bodies of failed pulls) also go to install.log."""
    import logging
    lg = logging.getLogger("jarvis")
    for h in list(lg.handlers):
        if getattr(h, "_install_log", False):
            if getattr(h, "baseFilename", "") == str(Path(LOG).resolve()):
                return
            lg.removeHandler(h)
            h.close()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(LOG, encoding="utf-8")
    h.setFormatter(logging.Formatter("[%(asctime)s]   %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    h._install_log = True  # type: ignore[attr-defined]
    lg.addHandler(h)
    lg.setLevel(logging.INFO)


def setup_ollama(no_install: bool = False, no_models: bool = False, sleep=time.sleep) -> bool:
    """Install Ollama if missing (download with resume + silent setup), start it hidden, pull missing models.
    1.0.3: waits until the API really answers (a fresh install's tray app needs time), retries each pull 3x with
    backoff (Ollama resumes partial layers), requires Ollama's final "success" and re-checks /api/tags."""
    sys.path.insert(0, str(ROOT))
    _log_to_file()
    try:
        from jarvis import ollama_setup as osu
        from jarvis.config import load_config
    except Exception as e:                       # packages not installed - the app will offer this later
        log(f"[!] Модуль Ollama недоступен ({e}) — Джарвис предложит установить Ollama при запуске.")
        return False
    cfg = load_config()
    prog = _progress()
    exe = osu.find_exe()
    if exe:
        log(f"Ollama найдена: {exe}")
    elif no_install:
        log("Ollama не установлена (--no-ollama-install).")
        return False
    else:
        log(f"Ollama не найдена — скачиваю {osu.OLLAMA_URL} (~1,6 ГБ, докачивается при обрыве)")
        try:
            exe = osu.install(cfg, prog)
            print()
            log(f"Ollama установлена: {exe}")
        except osu.OllamaSetupError as e:
            print()
            log(f"[!] {e}")
            log("    Джарвис предложит установить Ollama при запуске, или: Пуск → Джарвис → «Докачать компоненты».")
            return False
    started = False
    for n in range(1, 4):
        if osu.start(cfg, wait=60, progress=prog) and osu.wait_ready(cfg, 30, sleep=sleep):
            started = True
            break
        log(f"  Ollama пока не отвечает (попытка {n} из 3), жду...")
        sleep(10 * n)
    if not started:
        log("[!] " + osu.friendly_error(cfg))
        log("    Модели ИИ Джарвис предложит скачать при запуске (окно с прогрессом).")
        return False
    log(f"Ollama отвечает ({osu.host_of(cfg)}).")
    if no_models:
        return True
    missing = osu.missing_models(cfg)
    if not missing:
        log("Модели ИИ уже скачаны.")
        return True
    log("Скачиваю модели ИИ: " + ", ".join(missing) + " (основная ~4,7 ГБ, может занять долго; "
        "при обрыве докачивается)")
    errs = osu.pull_missing(cfg, prog, sleep=sleep, starter=lambda: osu.start(cfg, wait=60))
    print()
    for err in errs:
        log(f"  [!] модель не скачалась: {err} — Джарвис сам начнёт докачку при запуске")
    left = osu.missing_models(cfg)
    log("Модели ИИ на месте: " + ", ".join(osu.wanted_models(cfg)) if not left
        else "  Не хватает моделей: " + ", ".join(left))
    return not left


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-only", action="store_true", help="только базовые пакеты (тест)")
    ap.add_argument("--no-ollama", action="store_true")
    ap.add_argument("--no-models", action="store_true")
    a = ap.parse_args()
    log(f"=== Установка Джарвиса в {ROOT} (Python {sys.version.split()[0]}) ===")
    ok = True
    if not ensure_pip():
        log("[!] pip не установился — нужен интернет. Запусти установщик ещё раз.")
        return 1
    ok &= install_packages(a.core_only)
    if not a.no_models and not a.core_only:
        ok &= run([PY, ROOT / "scripts" / "download_models.py"]) == 0
    if not a.no_ollama and not a.core_only:
        try:
            ok &= setup_ollama(no_models=a.no_models)
        except Exception as e:                   # never abort the installer because of Ollama
            ok = False
            log(f"[!] Ollama: {e}")
    log("=== Готово ===" if ok else "=== Готово с предупреждениями (см. выше) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
