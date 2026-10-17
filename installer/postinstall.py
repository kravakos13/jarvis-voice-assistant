"""Run by JarvisSetup.exe after copying files (with the bundled embeddable Python):

  1. pip (get-pip.py) + Python packages from requirements.txt (or the bundled wheels\\ folder)
  2. Piper voice + Whisper model (scripts/download_models.py)
  3. Ollama (silent install of OllamaSetup.exe if missing) + `ollama pull` of the configured models

Every step is logged to logs\\install.log; a failed step does not abort the others —
the app's self-diagnostics ("проверь себя") offers to fix whatever is missing later.
Usage: python installer\\postinstall.py [--core-only] [--no-ollama] [--no-models]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)
LOG = ROOT / "logs" / "install.log"
OLLAMA_URL = "https://ollama.com/download/OllamaSetup.exe"
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


def ollama_exe() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(cand) if cand.exists() else None


def ensure_ollama() -> str | None:
    exe = ollama_exe()
    if exe:
        log(f"Ollama найдена: {exe}")
        return exe
    if sys.platform != "win32":
        log("Ollama не найдена (не Windows) — пропускаю установку.")
        return None
    dst = ROOT / "cache" / "OllamaSetup.exe"
    dst.parent.mkdir(parents=True, exist_ok=True)
    log(f"Скачиваю Ollama: {OLLAMA_URL}")
    try:
        urllib.request.urlretrieve(OLLAMA_URL, dst)
    except Exception as e:
        log(f"  не удалось скачать Ollama: {e}")
        return None
    run([dst, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"])
    try:
        dst.unlink()
    except OSError:
        pass
    return ollama_exe()


def wait_ollama(exe: str) -> bool:
    import requests
    for i in range(40):
        try:
            requests.get("http://localhost:11434/api/tags", timeout=2)
            return True
        except Exception:
            if i == 0:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                try:
                    subprocess.Popen([exe, "serve"], creationflags=flags,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass
            time.sleep(1.5)
    return False


def pull_models(exe: str) -> None:
    sys.path.insert(0, str(ROOT))
    from jarvis.config import load_config
    cfg = load_config()
    models = []
    for m in (cfg["llm"].get("model"), (cfg.get("code") or {}).get("model"),
              (cfg.get("memory") or {}).get("embed_model")):
        if m and m not in models:
            models.append(m)
    for m in models:
        log(f"Скачиваю модель {m} (может занять долго)...")
        if run([exe, "pull", m]) != 0:
            log(f"  модель {m} не скачалась — можно повторить: Настройки → О программе → Обновить модели")


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
        exe = ensure_ollama()
        if exe and wait_ollama(exe):
            pull_models(exe)
        else:
            ok = False
            log("[!] Ollama не готова — модели можно скачать позже (Настройки → О программе).")
    log("=== Готово ===" if ok else "=== Готово с предупреждениями (см. выше) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
