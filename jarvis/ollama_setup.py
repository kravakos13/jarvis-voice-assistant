"""Ollama lifecycle: find it, start it (hidden), install it (download + silent setup), pull missing models.

Used by the app at startup (auto-start an installed but stopped Ollama, offer to install a missing one) and by
installer/postinstall.py. Robust against the usual fresh-PC problems:
  * PATH is not refreshed in the same session after OllamaSetup.exe -> known install paths + the user's PATH
    from the registry are searched, never just `shutil.which`.
  * Ollama's own tray app ("ollama app.exe") autostarts with Windows and may still be starting when Jarvis
    starts -> wait for it instead of spawning a second server; start the tray app first, `ollama serve`
    (hidden) only as a fallback.
  * OllamaSetup.exe is ~1.6 GB -> streamed download with timeouts, resume, retries, size check, progress.
All side effects (HTTP, processes, sleep, clock) are injectable for tests."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"
OLLAMA_URL = "https://ollama.com/download/OllamaSetup.exe"
DOWNLOAD_PAGE = "https://ollama.com/download"
DEFAULT_HOST = "http://localhost:11434"
MIN_SETUP_BYTES = 50 * 1024 * 1024        # a real OllamaSetup.exe is >1 GB; anything tiny is an error page
NEED_FREE_BYTES = 6 * 1024 ** 3           # setup (~1.6 GB) + installed app (~4 GB)
SETUP_FLAGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
APP_NAME = "ollama app.exe"
EXE_NAME = "ollama.exe" if IS_WIN else "ollama"

Progress = Callable[[str, Optional[int]], None]     # (label, percent or None)


class OllamaSetupError(Exception):
    pass


# ------------------------------------------------------------------ discovery
def host_of(cfg: dict) -> str:
    return str((cfg.get("llm") or {}).get("host") or DEFAULT_HOST).rstrip("/")


def is_local(host: str) -> bool:
    h = (urllib.parse.urlparse(host).hostname or "").lower()
    return h in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _registry_path_dirs() -> list[str]:
    """User + machine PATH as stored in the registry (fresh even if this process started before the install)."""
    if sys.platform != "win32":
        return []
    out: list[str] = []
    try:
        import winreg
        for root, key in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(root, key) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    out += [os.path.expandvars(p) for p in str(val).split(";") if p]
            except OSError:
                pass
    except Exception:
        pass
    return out


def candidates(env: Optional[dict] = None, registry_dirs: Optional[list] = None) -> list[Path]:
    env = dict(os.environ if env is None else env)
    c: list[Path] = []
    if env.get("LOCALAPPDATA"):
        c.append(Path(env["LOCALAPPDATA"]) / "Programs" / "Ollama" / EXE_NAME)      # default per-user install
    for var in ("ProgramFiles", "ProgramW6432"):
        if env.get(var):
            c.append(Path(env[var]) / "Ollama" / EXE_NAME)
    dirs = (registry_dirs if registry_dirs is not None else _registry_path_dirs()) + env.get("PATH", "").split(os.pathsep)
    c += [Path(d) / EXE_NAME for d in dirs if d and "ollama" in d.lower()]
    if not IS_WIN:
        c += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    seen, out = set(), []
    for p in c:
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def find_exe(env: Optional[dict] = None, which: Callable[[str], Optional[str]] = shutil.which,
             registry_dirs: Optional[list] = None) -> Optional[Path]:
    for p in candidates(env, registry_dirs):
        if p.is_file():
            return p
    w = which("ollama")
    return Path(w) if w else None


def find_app(exe: Optional[Path]) -> Optional[Path]:
    if exe is None:
        return None
    app = exe.parent / APP_NAME
    return app if app.is_file() else None


def is_running(host: str = DEFAULT_HOST, session=requests, timeout: float = 2.0) -> bool:
    try:
        r = session.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def ollama_processes() -> list[str]:
    """Names of running Ollama processes (tray app autostarting at login, a server being started...)."""
    try:
        import psutil
    except Exception:
        return []
    names = []
    for p in psutil.process_iter(["name"]):
        n = (p.info.get("name") or "").lower()
        if n in (APP_NAME, "ollama.exe", "ollama"):
            names.append(n)
    return names


def status(cfg: dict, session=requests, find: Optional[Callable[[], Optional[Path]]] = None) -> str:
    """'running' / 'stopped' (installed, not answering) / 'missing' (not installed) / 'remote' (other host down)."""
    find = find or find_exe
    host = host_of(cfg)
    if is_running(host, session):
        return "running"
    if not is_local(host):
        return "remote"
    return "stopped" if find() else "missing"


def friendly_error(cfg: dict, state: Optional[str] = None, find: Optional[Callable[[], Optional[Path]]] = None,
                   session=requests) -> str:
    """Actionable Russian message for the UI / log."""
    find = find or find_exe
    host = host_of(cfg)
    state = state or status(cfg, session=session, find=find)
    if state == "missing":
        return ("Ollama (локальный ИИ Джарвиса) не установлена. Джарвис может установить её сам: нажми "
                "«Установить Ollama» в окне Джарвиса (скачается около 1,6 ГБ), или Пуск → Джарвис → «Докачать "
                f"компоненты». Вручную: {DOWNLOAD_PAGE} → OllamaSetup.exe, затем перезапусти Джарвиса.")
    if state == "stopped":
        exe = find()
        return (f"Ollama установлена, но не запустилась. Запусти «Ollama» из меню Пуск или выполни в PowerShell: "
                f"& \"{exe}\" serve. Если не помогло — перезагрузи компьютер.")
    if state == "remote":
        return f"Не могу подключиться к Ollama по адресу {host}. Проверь, что сервер Ollama там запущен."
    return "Ollama запущена, но не ответила вовремя — повтори, пожалуйста, через пару секунд."


# ------------------------------------------------------------------ start
def _spawn_hidden(argv: list, env: Optional[dict] = None) -> subprocess.Popen:
    flags = 0
    if IS_WIN:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen([str(a) for a in argv], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags, env=env)


def _serve_env(host: str) -> dict:
    env = dict(os.environ)
    port = urllib.parse.urlparse(host).port
    if port and port != 11434:
        env["OLLAMA_HOST"] = f"127.0.0.1:{port}"
    return env


def start(cfg: dict, wait: float = 30.0, session=requests, popen=_spawn_hidden, sleep=time.sleep,
          clock=time.monotonic, find: Optional[Callable[[], Optional[Path]]] = None,
          procs: Optional[Callable[[], list]] = None, progress: Optional[Progress] = None) -> bool:
    """Make sure the local Ollama server answers. Returns True when it does (within `wait` seconds)."""
    host = host_of(cfg)
    if is_running(host, session):
        return True
    if not is_local(host):
        return False
    exe = (find or find_exe)()
    if not exe:
        return False
    procs = procs or ollama_processes
    oc = cfg.get("ollama") or {}
    end = clock() + wait

    def wait_until(deadline: float) -> bool:
        while clock() < deadline:
            if is_running(host, session):
                return True
            sleep(1.0)
        return is_running(host, session)

    if progress:
        progress("Запускаю Ollama…", None)
    if procs():                        # tray app / server already starting (e.g. Windows autostart at login)
        log.info("Ollama уже запускается — жду")
        if wait_until(min(end, clock() + wait * 0.5)):
            return True
    app = find_app(exe) if oc.get("start_tray_app", True) else None
    if app:
        try:
            popen([app])
            log.info("Запущено приложение Ollama: %s", app)
            if wait_until(min(end, clock() + 12)):
                return True
        except OSError as e:
            log.warning("Приложение Ollama не запустилось: %s", e)
    try:
        popen([exe, "serve"], env=_serve_env(host))
        log.info("Запущен скрытый сервер: %s serve", exe)
    except OSError as e:
        log.warning("ollama serve не запустился: %s", e)
        return False
    return wait_until(end)


# ------------------------------------------------------------------ install
def download(url: str, dest: Path, progress: Optional[Progress] = None, session=requests, attempts: int = 4,
             sleep=time.sleep) -> Path:
    """Streamed download with resume (HTTP Range), retries and a size check."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last_err: Optional[Exception] = None
    total = 0
    for attempt in range(attempts):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with session.get(url, stream=True, timeout=(15, 60), headers=headers, allow_redirects=True) as r:
                if r.status_code == 416:            # already complete
                    break
                if r.status_code not in (200, 206):
                    raise OllamaSetupError(f"сервер ответил {r.status_code}")
                if r.status_code == 200:
                    have = 0                        # server ignored Range -> start over
                total = have + int(r.headers.get("content-length") or 0)
                mode = "ab" if have else "wb"
                done, shown = have, -1
                with open(part, mode) as f:
                    for chunk in r.iter_content(1 << 20):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        pct = int(done * 100 / total) if total else None
                        if progress and pct is not None and pct != shown:
                            shown = pct
                            progress(f"Скачиваю Ollama: {done // 2**20} из {total // 2**20} МБ", pct)
                if total and done < total:
                    raise OllamaSetupError(f"загрузка прервалась ({done // 2**20} из {total // 2**20} МБ)")
                break
        except (requests.RequestException, OSError, OllamaSetupError) as e:
            last_err = e
            log.warning("Загрузка Ollama, попытка %d: %s", attempt + 1, e)
            sleep(min(30, 3 * (attempt + 1)))
    else:
        raise OllamaSetupError(f"Не удалось скачать Ollama: {last_err}. Проверь интернет и попробуй ещё раз.")
    size = part.stat().st_size if part.exists() else 0
    with open(part, "rb") as f:
        magic = f.read(2)
    if size < MIN_SETUP_BYTES or magic != b"MZ":
        part.unlink(missing_ok=True)
        raise OllamaSetupError("Скачанный файл не похож на установщик Ollama — попробуй позже.")
    part.replace(dest)
    return dest


def install(cfg: dict, progress: Optional[Progress] = None, session=requests, run=subprocess.run,
            cache_dir: Optional[Path] = None, find: Optional[Callable[[], Optional[Path]]] = None, disk_free: Optional[Callable[[Path], int]] = None,
            platform_ok: bool = IS_WIN) -> Path:
    """Download OllamaSetup.exe and install it silently (per user, no admin). Returns the path to ollama.exe."""
    if not platform_ok:
        raise OllamaSetupError(f"Автоустановка Ollama есть только в Windows. Установи вручную: {DOWNLOAD_PAGE}")
    if cache_dir is None:
        from . import platform_utils as pu
        cache_dir = pu.data_dir("cache")
    target_drive = Path(os.environ.get("LOCALAPPDATA") or cache_dir)
    free = (disk_free or (lambda p: shutil.disk_usage(p).free))(target_drive)
    if free < NEED_FREE_BYTES:
        raise OllamaSetupError(f"Мало места на диске: свободно {free / 1024 ** 3:.1f} ГБ, для Ollama нужно около "
                               f"{NEED_FREE_BYTES // 1024 ** 3} ГБ (плюс 5 ГБ на модель).")
    url = str((cfg.get("ollama") or {}).get("setup_url") or OLLAMA_URL)
    if not url.startswith("https://"):
        raise OllamaSetupError("Адрес установщика Ollama должен начинаться с https://")
    setup = download(url, Path(cache_dir) / "OllamaSetup.exe", progress, session)
    if progress:
        progress("Устанавливаю Ollama (1–3 минуты)…", None)
    logf = Path(cache_dir) / "ollama-setup.log"
    try:
        rc = run([str(setup), *SETUP_FLAGS, f"/LOG={logf}"], timeout=1800).returncode
    except subprocess.TimeoutExpired:
        raise OllamaSetupError("Установщик Ollama не завершился за 30 минут.") from None
    except OSError as e:
        raise OllamaSetupError(f"Установщик Ollama не запустился: {e}") from None
    finally:
        try:
            setup.unlink()
        except OSError:
            pass
    if rc != 0:
        raise OllamaSetupError(f"Установщик Ollama завершился с кодом {rc} (журнал: {logf}).")
    exe = (find or find_exe)()
    if not exe:
        raise OllamaSetupError("Ollama установилась, но ollama.exe не найден. Перезагрузи компьютер и запусти Джарвиса.")
    log.info("Ollama установлена: %s", exe)
    return exe


# ------------------------------------------------------------------ models
def wanted_models(cfg: dict) -> list[str]:
    out: list[str] = []
    for m in ((cfg.get("llm") or {}).get("model"),
              (cfg.get("code") or {}).get("model") if (cfg.get("code") or {}).get("enabled", True) else None,
              (cfg.get("memory") or {}).get("embed_model") if (cfg.get("memory") or {}).get("enabled", True) else None):
        if m and m not in out:
            out.append(m)
    return out


def missing_models(cfg: dict, session=requests) -> list[str]:
    try:
        tags = session.get(f"{host_of(cfg)}/api/tags", timeout=5).json()
    except Exception:
        return wanted_models(cfg)
    have = {m.get("name", "") for m in tags.get("models", [])}
    have |= {n[: -len(":latest")] for n in have if n.endswith(":latest")}
    return [m for m in wanted_models(cfg) if m not in have and f"{m}:latest" not in have]


def pull(cfg: dict, model: str, progress: Optional[Progress] = None, session=requests) -> None:
    import json
    try:
        with session.post(f"{host_of(cfg)}/api/pull", json={"model": model, "stream": True}, stream=True,
                          timeout=(10, 3600)) as r:
            r.raise_for_status()
            shown = -1
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if d.get("error"):
                    raise OllamaSetupError(f"{model}: {d['error']}")
                if progress and d.get("total"):
                    pct = int(d.get("completed", 0) * 100 / d["total"])
                    if pct != shown:
                        shown = pct
                        progress(f"Скачиваю модель {model}: {pct}%", pct)
    except requests.RequestException as e:
        raise OllamaSetupError(f"{model}: нет связи с Ollama ({type(e).__name__})") from None


def pull_missing(cfg: dict, progress: Optional[Progress] = None, session=requests) -> list[str]:
    """Pull every configured model that is not there yet. -> list of errors (empty = all good)."""
    errs = []
    for m in missing_models(cfg, session):
        try:
            pull(cfg, m, progress, session)
        except OllamaSetupError as e:
            errs.append(str(e))
    return errs


def ensure(cfg: dict, progress: Optional[Progress] = None, allow_install: bool = False, **kw) -> tuple[bool, str]:
    """Full chain: start (or install, if allowed) + pull missing models. -> (ok, Russian message)."""
    session = kw.get("session", requests)
    if not start(cfg, wait=float((cfg.get("ollama") or {}).get("start_timeout_s", 30)), session=session, progress=progress):
        st = status(cfg, session=session)
        if st != "missing" or not allow_install:
            return False, friendly_error(cfg, st)
        try:
            install(cfg, progress, session=session)
        except OllamaSetupError as e:
            return False, str(e)
        if not start(cfg, wait=60, session=session, progress=progress):
            return False, friendly_error(cfg, "stopped")
    errs = pull_missing(cfg, progress, session)
    if errs:
        return False, "Не скачались модели: " + "; ".join(errs[:3])
    return True, "Ollama работает, модели на месте."
