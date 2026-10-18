"""1.0.9: «закрой стим / дискорд / вайфай маус» fully QUITS an app, also one that lives only in the tray.

Steps (each verified by checking that the processes are gone):
  1. app-specific graceful exit: Steam -> `steam.exe -shutdown` (closes Steam and its helpers cleanly);
  2. WM_CLOSE to every top-level window of the app's processes, hidden ones too (tray apps keep a hidden main
     window); wait up to WAIT_S;
  3. still running -> `taskkill /PID` (graceful, no /F); then /F only for SAFE_KILL apps, otherwise the skill asks
     «Не закрывается. Убить процесс?».
Hard rules: same user only (no elevation), never the CRITICAL list (system, shell, audio, drivers, antivirus,
Ollama, Jarvis itself), games are left to the windows skill's game guard. All OS access is injectable (tests)."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

WAIT_S = 5.0
CRITICAL = {
    "system", "registry", "idle", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe", "explorer.exe", "sihost.exe", "fontdrvhost.exe",
    "ctfmon.exe", "runtimebroker.exe", "taskhostw.exe", "dllhost.exe", "conhost.exe", "spoolsv.exe",
    "audiodg.exe", "searchindexer.exe", "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
    "textinputhost.exe", "applicationframehost.exe", "securityhealthservice.exe", "securityhealthsystray.exe",
    "msmpeng.exe", "nissrv.exe", "mpdefendercoreservice.exe", "smartscreen.exe", "wudfhost.exe", "dashost.exe",
    "lockapp.exe", "logonui.exe", "userinit.exe", "memory compression", "wlanext.exe", "nvcontainer.exe", "nvdisplay.container.exe", "nvidia web helper.exe", "amdrsserv.exe", "atieclxx.exe",
    "rtkaudusvc64.exe", "rtkauduservice64.exe", "ravbg64.exe", "realtekaudioservice.exe", "igfxem.exe",
    "avp.exe", "avpui.exe", "ekrn.exe", "egui.exe", "avastsvc.exe", "avastui.exe", "avgsvc.exe", "mbamservice.exe",
    "ollama.exe", "ollama app.exe", "ollama_llama_server.exe", "python.exe", "pythonw.exe", "jarvis.exe",
    "qtwebengineprocess.exe", "wscript.exe", "taskmgr.exe", "msiexec.exe", "trustedinstaller.exe", "tiworker.exe",
}
SAFE_KILL = {"steam.exe", "steamwebhelper.exe", "discord.exe", "telegram.exe", "epicgameslauncher.exe", "epicwebhelper.exe",
             "wifimouseserver.exe", "mouseserver.exe", "spotify.exe", "viber.exe", "calculatorapp.exe",
             "battle.net.exe", "galaxyclient.exe", "eadesktop.exe", "upc.exe", "riotclientservices.exe"}
# spoken name -> process names that make up the app (main first)
APPS: dict[str, tuple[str, ...]] = {
    "стим": ("steam.exe", "steamwebhelper.exe", "steamservice.exe"),
    "дискорд": ("discord.exe",),
    "телеграм": ("telegram.exe",),
    "эпик": ("epicgameslauncher.exe", "epicwebhelper.exe"),
    "вайфай маус": ("wifimouseserver.exe", "mouseserver.exe", "wifi mouse server.exe", "wifimouse.exe"),
    "спотифай": ("spotify.exe",),
    "вайбер": ("viber.exe",),
    "обс": ("obs64.exe", "obs32.exe"),
    "батлнет": ("battle.net.exe",),
    "гог": ("galaxyclient.exe",),
    "иа": ("eadesktop.exe",),
    "юбисофт": ("upc.exe", "ubisoftconnect.exe"),
    "райот": ("riotclientservices.exe", "riotclientux.exe"),
    "зум": ("zoom.exe",),
    "скайп": ("skype.exe",),
    "ватсап": ("whatsapp.exe", "whatsapp.root.exe"),
    "слак": ("slack.exe",),
    "торрент": ("utorrent.exe", "qbittorrent.exe", "bittorrent.exe"),
}
SPOKEN: dict[str, tuple[str, ...]] = {
    "вайфай маус": ("вайфай маус", "вай фай маус", "вифи маус", "вайфай мышь", "wifi mouse", "wi fi mouse",
                    "вайфаймаус", "вифимаус", "файфай маус"),
    "батлнет": ("батлнет", "баттл нет", "battle net", "близзард", "blizzard"),
    "гог": ("гог", "gog", "гог галакси", "galaxy"),
    "иа": ("еа", "ea app", "ea", "ориджин", "origin"),
    "юбисофт": ("юбисофт", "ubisoft", "юплей", "uplay"),
    "райот": ("райот", "riot", "риот"),
    "зум": ("зум", "zoom"),
    "скайп": ("скайп", "skype"),
    "ватсап": ("ватсап", "вотсап", "whatsapp"),
    "слак": ("слак", "slack"),
    "торрент": ("торрент", "utorrent", "qbittorrent", "кубиторрент"),
}


@dataclass
class Proc:
    pid: int
    name: str            # lower-case exe name
    exe: str = ""        # full path ('' if unknown)
    user: str = ""
    desc: str = ""       # file description / product name ('' if unknown)


def app_key(spoken: str) -> Optional[str]:
    """Spoken name -> APPS key (windows aliases first, then our own table)."""
    s = " ".join((spoken or "").lower().replace("ё", "е").split())
    if not s:
        return None
    from . import windows as wn
    t = wn.resolve_alias(s)
    if t is not None and t.key in APPS:
        return t.key
    for key, variants in SPOKEN.items():
        for v in variants:
            if len(v) <= 3:
                if re.search(rf"(?:^|\s){re.escape(v)}(?:\s|$)", s):
                    return key
            elif v in s or v.replace(" ", "") in s.replace(" ", ""):
                return key
    return None


def _base(n: str) -> str:
    return re.sub(r"\.exe$", "", (n or "").lower())


def match(spoken: str, procs: Iterable[Proc]) -> list[Proc]:
    """Processes of the app the user named: APPS table, else exe name / description (fuzzy, >= 4 letters)."""
    procs = [p for p in procs if p.name not in CRITICAL]
    key = app_key(spoken)
    if key is not None:
        names = APPS[key]
        return sorted((p for p in procs if p.name in names), key=lambda p: names.index(p.name))
    import difflib
    from .wake import translit
    q = translit(" ".join((spoken or "").lower().split())).replace(" ", "")
    if len(q) < 4:
        return []
    out = []
    for p in procs:
        cands = {_base(p.name).replace(" ", ""), re.sub(r"[^a-z0-9]", "", p.desc.lower())}
        cands.discard("")
        if any(q == c or (len(q) >= 5 and c.startswith(q)) for c in cands) or \
                difflib.get_close_matches(q, list(cands), n=1, cutoff=0.85):
            out.append(p)
    return out


# ------------------------------------------------------------------ OS access (real implementations)
def list_procs() -> list[Proc]:
    if os.name != "nt":
        return []                                         # quitting apps is a Windows feature (and tests stay safe)
    try:
        import psutil
    except ImportError:
        return []
    me = _me()
    out = []
    for p in psutil.process_iter(["pid", "name", "exe", "username"]):
        try:
            info = p.info
            user = str(info.get("username") or "")
            if me and user and user.lower() != me.lower():
                continue                                  # same user only (no elevation, no services)
            out.append(Proc(int(info["pid"]), str(info.get("name") or "").lower(), str(info.get("exe") or ""), user))
        except Exception:                                 # noqa: BLE001
            continue
    return out


def _me() -> str:
    try:
        import psutil
        return str(psutil.Process(os.getpid()).username() or "")
    except Exception:                                     # noqa: BLE001
        return ""


def alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
    except Exception:                                     # noqa: BLE001
        return False


def close_windows(pids: set) -> int:
    """WM_CLOSE to all top-level windows (visible or hidden) of these processes. -> number of windows."""
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes as w
    u = ctypes.windll.user32  # type: ignore[attr-defined]
    hits: list[int] = []
    proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)  # type: ignore[attr-defined]  # Windows-only

    def cb(h, _):
        pid = w.DWORD(0)
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if int(pid.value) in pids and not u.GetWindow(h, 4):                    # unowned top-level only
            hits.append(int(h or 0))
        return True
    u.EnumWindows(proc(cb), 0)
    for h in hits:
        u.PostMessageW(w.HWND(h), 0x0010, 0, 0)                                  # WM_CLOSE
    return len(hits)


def taskkill(pid: int, force: bool = False) -> bool:
    args = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])     # no /T: Steam games are its children
    try:
        r = subprocess.run(args, capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception as e:                                # noqa: BLE001
        log.debug("taskkill: %s", e)
        return False


def run_detached(args: list) -> bool:
    try:
        subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:                                # noqa: BLE001
        log.debug("run %s: %s", args, e)
        return False


def steam_game_running() -> bool:
    try:
        from .skills.modes_skill import steam_game_running as f
        return bool(f())
    except Exception:                                     # noqa: BLE001
        return False


class Quitter:
    """Quit an app's processes and report the truth: 'quit' | 'still' (needs a force decision) | 'none'."""

    def __init__(self, procs: Callable[[], list] = list_procs, alive: Callable[[int], bool] = alive,
                 close_windows: Callable[[set], int] = close_windows, taskkill: Callable[..., bool] = taskkill,
                 run: Callable[[list], bool] = run_detached, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, own_pids: Optional[set] = None, wait_s: float = WAIT_S):
        self.procs, self.alive, self.close_windows, self.taskkill = procs, alive, close_windows, taskkill
        self.run, self.sleep, self.clock, self.wait_s = run, sleep, clock, wait_s
        self.own = set(own_pids or {os.getpid(), os.getppid()})

    def find(self, spoken: str) -> list[Proc]:
        return [p for p in match(spoken, self.procs()) if p.pid not in self.own]

    def _wait(self, pids: Iterable[int], secs: float) -> list[int]:
        pids = list(pids)
        end = self.clock() + secs
        while True:
            left = [p for p in pids if self.alive(p)]
            if not left or self.clock() >= end:
                return left
            self.sleep(0.25)

    def quit(self, found: list[Proc]) -> tuple[str, list[Proc]]:
        found = [p for p in found if p.name not in CRITICAL and p.pid not in self.own]
        if not found:
            return "none", []
        pids = {p.pid for p in found}
        steam = next((p for p in found if p.name == "steam.exe" and p.exe), None)
        if steam is not None:                             # Steam's own clean shutdown (helpers too)
            log.info("Steam: %s -shutdown", steam.exe)
            self.run([steam.exe, "-shutdown"])
            if not self._wait(pids, self.wait_s * 2):
                return "quit", []
        n = self.close_windows(pids)
        log.info("Закрываю %s: WM_CLOSE окнам %d", sorted({p.name for p in found}), n)
        left = self._wait(pids, self.wait_s)
        if left:
            for pid in left:
                self.taskkill(pid, force=False)           # graceful: asks the app to exit
            left = self._wait(left, 3.0)
        if not left:
            return "quit", []
        rest = [p for p in found if p.pid in set(left)]
        safe = [p for p in rest if p.name in SAFE_KILL]
        for p in safe:
            log.info("taskkill /F %s (pid %s) — в списке безопасных", p.name, p.pid)
            self.taskkill(p.pid, force=True)
        if safe:
            left = self._wait([p.pid for p in rest], 3.0)
            rest = [p for p in rest if p.pid in set(left)]
        return ("quit", []) if not rest else ("still", rest)

    def force(self, rest: list[Proc]) -> bool:
        """After the user said «да»: taskkill /F (never CRITICAL / own)."""
        rest = [p for p in rest if p.name not in CRITICAL and p.pid not in self.own]
        for p in rest:
            self.taskkill(p.pid, force=True)
        return not self._wait([p.pid for p in rest], 3.0)
