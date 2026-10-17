"""Update check against GitHub Releases (repo configured in update.github_repo, empty by default)."""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

import requests

from . import __version__
from . import platform_utils as pu

log = logging.getLogger(__name__)

DEFAULT_REPO = "kravakos13/jarvis-voice-assistant"


def parse_version(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:4]) or (0,)


def check(cfg: dict) -> dict:
    """-> {status: not_configured|error|latest|available, version, url, notes, message}"""
    repo = str((cfg.get("update") or {}).get("github_repo", DEFAULT_REPO) or "").strip().strip("/")
    if not repo:
        return {"status": "not_configured", "message": "Источник обновлений не настроен."}
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return {"status": "error", "message": "Неверный формат репозитория (нужно owner/repo)."}
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=15,
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 404:
            return {"status": "error", "message": "Релизы в репозитории не найдены."}
        r.raise_for_status()
        rel = r.json()
    except Exception as e:
        return {"status": "error", "message": f"Не удалось проверить обновления: {e}"}
    tag = rel.get("tag_name", "")
    assets = rel.get("assets", [])
    asset = (next((a for a in assets if a.get("name", "").lower() == "jarvissetup.exe"), None)
             or next((a for a in assets if a.get("name", "").lower().endswith(".exe")), None))
    if parse_version(tag) <= parse_version(__version__):
        return {"status": "latest", "version": tag, "message": f"Установлена последняя версия ({__version__})."}
    if not asset:
        return {"status": "error", "version": tag, "message": f"Есть версия {tag}, но в релизе нет установщика .exe."}
    return {"status": "available", "version": tag, "url": asset["browser_download_url"],
            "size": asset.get("size", 0), "notes": (rel.get("body") or "")[:1500],
            "message": f"Доступна версия {tag} (у тебя {__version__})."}


def download(url: str, progress: Optional[Callable[[int], None]] = None) -> Path:
    if not url.startswith("https://github.com/") and not url.startswith("https://objects.githubusercontent.com/"):
        raise ValueError("Подозрительный адрес загрузки")
    dest = Path(tempfile.gettempdir()) / "JarvisSetup-update.exe"
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) or 1
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(min(100, done * 100 // total))
    return dest


def run_installer(path: Path) -> None:
    """Inno Setup upgrades in place (same AppId), user data in Documents\\Jarvis stays untouched.
    /SILENT shows progress only; the installer closes the running Jarvis and restarts it."""
    if not pu.IS_WIN:
        raise RuntimeError("Установка обновлений возможна только в Windows")
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
                     close_fds=True)


def pull_models(cfg: dict, progress: Optional[Callable[[str, int], None]] = None) -> list[str]:
    """ollama pull for all configured models. -> list of errors."""
    host = cfg["llm"].get("host", "http://localhost:11434")
    models = []
    for m in (cfg["llm"].get("model"), cfg.get("code", {}).get("model"), cfg.get("memory", {}).get("embed_model", "nomic-embed-text")):
        if m and m not in models:
            models.append(m)
    errors = []
    import json
    for m in models:
        try:
            with requests.post(f"{host}/api/pull", json={"model": m, "stream": True}, stream=True, timeout=(10, 3600)) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    if d.get("error"):
                        raise RuntimeError(d["error"])
                    if progress and d.get("total"):
                        progress(m, int(d.get("completed", 0) * 100 / d["total"]))
        except Exception as e:
            errors.append(f"{m}: {e}")
    return errors


def _stamp_file() -> Path:
    return pu.data_dir("cache") / "update_check.txt"


def due(cfg: dict) -> bool:
    """Automatic check: enabled and not more than once per 24 h."""
    if not (cfg.get("update") or {}).get("check_on_start", True):
        return False
    try:
        import time
        return time.time() - float(_stamp_file().read_text().strip() or 0) > 24 * 3600
    except Exception:
        return True


def mark_checked() -> None:
    import time
    try:
        _stamp_file().write_text(str(time.time()))
    except Exception:
        pass


def auto_check(cfg: dict) -> Optional[dict]:
    """Background startup check. Returns release info only if a NEW, not skipped version exists."""
    if not due(cfg):
        return None
    info = check(cfg)
    if info["status"] in ("latest", "available", "not_configured"):
        mark_checked()
    if info["status"] != "available":
        return None
    skip = str((cfg.get("update") or {}).get("skip_version", "") or "")
    if skip and parse_version(skip) >= parse_version(info["version"]):
        return None
    return info
