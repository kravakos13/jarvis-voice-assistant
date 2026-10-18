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


SERVER_ENV = {"OLLAMA_FLASH_ATTENTION": "1",   # less VRAM for attention; needed for the q8 KV cache
              "OLLAMA_KV_CACHE_TYPE": "q8_0",   # KV cache at half size, quality loss is negligible
              "OLLAMA_NUM_PARALLEL": "1"}       # one conversation -> one KV cache (parallel slots multiply VRAM)


def _serve_env(host: str, extra: Optional[dict] = None) -> dict:
    """Environment for an Ollama started by Jarvis. The user's own OLLAMA_* variables always win."""
    env = dict(os.environ)
    for k, v in (SERVER_ENV if extra is None else extra).items():
        if v is not None and str(v) != "" and k not in os.environ:
            env[str(k)] = str(v)
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
            popen([app], env=_serve_env(host, oc.get("server_env")))
            log.info("Запущено приложение Ollama: %s", app)
            if wait_until(min(end, clock() + 12)):
                return True
        except OSError as e:
            log.warning("Приложение Ollama не запустилось: %s", e)
    try:
        popen([exe, "serve"], env=_serve_env(host, oc.get("server_env")))
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


MODEL_SIZES_GB = {"qwen2.5:7b": 4.7, "qwen2.5-coder:7b": 4.7, "nomic-embed-text": 0.27, "qwen2.5:3b": 1.9,
                  "qwen2.5:1.5b": 1.0, "qwen2.5:14b": 9.0}


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f}".replace(".", ",")


def _short_error(body: str) -> str:
    """Ollama's {"error": "..."} body -> a short reason (raw text goes to the log only)."""
    import json
    try:
        msg = str(json.loads(body).get("error", ""))
    except Exception:
        msg = body
    msg = msg.strip().splitlines()[0] if msg.strip() else ""
    low = msg.lower()
    if "file does not exist" in low or "not found" in low:
        return "такой модели нет в библиотеке Ollama (проверь название)"
    if "no space" in low or "disk" in low:
        return "не хватает места на диске"
    if any(k in low for k in ("tls", "dial tcp", "lookup", "no such host", "connection", "timeout", "max retries",
                              "resolve", "redirect", "eof", "i/o")):
        return "Ollama не смогла связаться с сервером моделей (проверь интернет)"
    return "ошибка Ollama"


class FatalPullError(OllamaSetupError):
    """Retrying will not help (unknown model name, no disk space, cancelled)."""


def pull(cfg: dict, model: str, progress: Optional[Progress] = None, session=requests,
         cancel=None) -> None:
    """Stream /api/pull. Success only if Ollama says {"status": "success"}: a stream that just ends (server
    restarted, connection dropped) used to count as success in 1.0.2 and left the model missing."""
    import json
    try:
        with session.post(f"{host_of(cfg)}/api/pull", json={"model": model, "stream": True}, stream=True,
                          timeout=(10, 900)) as r:
            if r.status_code >= 400:
                body = ""
                try:
                    body = r.text[:500]
                except Exception:
                    pass
                log.warning("ollama pull %s: HTTP %s %s", model, r.status_code, body)
                reason = _short_error(body)
                cls = FatalPullError if "нет в библиотеке" in reason else OllamaSetupError
                raise cls(f"{model}: {reason} (HTTP {r.status_code})")
            shown, done = -1, False
            for line in r.iter_lines():
                if cancel is not None and cancel.is_set():
                    raise FatalPullError(f"{model}: скачивание отменено")
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    log.debug("ollama pull %s: bad line %r", model, line[:200])
                    continue
                if d.get("error"):
                    log.warning("ollama pull %s: %s", model, d["error"])
                    reason = _short_error(json.dumps({"error": d["error"]}))
                    cls = FatalPullError if ("нет в библиотеке" in reason or "места" in reason) else OllamaSetupError
                    raise cls(f"{model}: {reason}")
                if d.get("status") == "success":
                    done = True
                if progress and d.get("total"):
                    total, comp = float(d["total"]), float(d.get("completed") or 0)
                    pct = int(comp * 100 / total) if total else 0
                    if pct != shown and total > 50 * 1024 ** 2:      # the big layer, not tiny manifests
                        shown = pct
                        progress(f"Скачиваю модель {model}: {pct}% ({_gb(comp)} из {_gb(total)} ГБ)", pct)
            if not done:
                log.warning("ollama pull %s: stream ended without success", model)
                raise OllamaSetupError(f"{model}: загрузка прервалась")
    except requests.RequestException as e:
        log.warning("ollama pull %s: %r", model, e)
        raise OllamaSetupError(f"{model}: нет связи с Ollama ({type(e).__name__})") from None


def wait_ready(cfg: dict, timeout: float = 30.0, session=requests, sleep=time.sleep,
               clock=time.monotonic) -> bool:
    """Wait until the Ollama API answers (fresh install: tray app still starting its server)."""
    deadline = clock() + timeout
    while True:
        if is_running(host_of(cfg), session):
            return True
        if clock() >= deadline:
            return False
        sleep(1.0)


def pull_with_retry(cfg: dict, model: str, progress: Optional[Progress] = None, session=requests,
                    attempts: int = 3, sleep=time.sleep, backoff=(5, 20, 60), cancel=None,
                    starter: Optional[Callable[[], bool]] = None) -> None:
    """pull() up to `attempts` times; between attempts wait for (or restart) the server. Ollama resumes
    partially downloaded layers, so a retry continues where it stopped. Verifies the model is listed."""
    last: Optional[OllamaSetupError] = None
    for n in range(1, attempts + 1):
        if not wait_ready(cfg, 20 if n == 1 else 45, session=session, sleep=sleep):
            log.warning("ollama pull %s: server not answering (attempt %d)", model, n)
            if starter is not None:
                starter()
        try:
            pull(cfg, model, progress, session, cancel=cancel)
            if model in missing_models(cfg, session):
                raise OllamaSetupError(f"{model}: после загрузки модель не появилась в списке")
            log.info("ollama pull %s: ok (attempt %d)", model, n)
            return
        except FatalPullError:
            raise
        except OllamaSetupError as e:
            last = e
            log.warning("ollama pull %s: attempt %d/%d failed: %s", model, n, attempts, e)
            if n < attempts:
                pause = backoff[min(n - 1, len(backoff) - 1)]
                if progress:
                    progress(f"{e}. Повтор {n + 1} из {attempts} через {pause} с…", None)
                sleep(pause)
    assert last is not None
    raise last


def pull_missing(cfg: dict, progress: Optional[Progress] = None, session=requests, attempts: int = 3,
                 sleep=time.sleep, cancel=None, starter: Optional[Callable[[], bool]] = None) -> list[str]:
    """Pull every configured model that is not there yet (with retries). -> list of errors (empty = all good)."""
    errs = []
    for m in missing_models(cfg, session):
        try:
            pull_with_retry(cfg, m, progress, session, attempts=attempts, sleep=sleep, cancel=cancel,
                            starter=starter)
        except OllamaSetupError as e:
            errs.append(str(e))
            if cancel is not None and cancel.is_set():
                break
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
    errs = pull_missing(cfg, progress, session, cancel=kw.get("cancel"),
                        starter=lambda: start(cfg, wait=30, session=session))
    if errs:
        return False, "Не скачались модели: " + "; ".join(errs[:3])
    return True, "Ollama работает, модели на месте."
