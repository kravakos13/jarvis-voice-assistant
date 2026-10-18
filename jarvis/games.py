"""1.0.9: «закрой» never closes games. A window is a game when ANY of these says so:
  * the user's «Не закрывать» list (windows.never_close: exe names or title words)            -> "blocked"
  * a known game exe (cs2.exe, valorant…), an Unreal «-Win64-Shipping.exe»                    -> "game"
  * the exe lives in a game library (steamapps\\common, Epic Games, GOG, Riot, Battle.net…), launchers excluded
  * the window title is a game from the launcher index (Steam / Epic)
  * a borderless window covering its whole monitor (exclusive / borderless fullscreen), or any fullscreen window
    while game mode is on — unless it is an ordinary app (browser, player, office, editor…)
Pure functions over a Window + backend so everything is tested with simulated windows."""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

GAME_DIRS = re.compile(
    r"[\\/](?:steamapps[\\/]common|epic games|gog galaxy[\\/]games|gog games|riot games|battle\.net|"
    r"blizzard(?: entertainment)?|ubisoft(?: game launcher)?[\\/]games|ea games|origin games|xboxgames|"
    r"rockstar games|wargaming\.net|lesta(?: games)?|games)[\\/]", re.I)
LAUNCHERS = {"steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "epicwebhelper.exe", "galaxyclient.exe",
             "galaxyclient helper.exe", "battle.net.exe", "battle.net launcher.exe", "agent.exe", "riotclientux.exe",
             "riotclientservices.exe", "riotclientuxrender.exe", "eadesktop.exe", "origin.exe", "upc.exe",
             "ubisoftconnect.exe", "ubisoftgamelauncher.exe", "rockstarlauncher.exe", "playnite.desktopapp.exe",
             "wgc.exe", "lgc.exe", "gamebar.exe", "xboxpcapp.exe", "unins000.exe"}
GAME_EXES = {
    "cs2.exe", "csgo.exe", "dota2.exe", "hl2.exe", "valorant.exe", "valorant-win64-shipping.exe",
    "fortniteclient-win64-shipping.exe", "gta5.exe", "gta5_enhanced.exe", "rdr2.exe", "league of legends.exe",
    "r5apex.exe", "r5apex_dx12.exe", "overwatch.exe", "tslgame.exe", "eldenring.exe", "cyberpunk2077.exe",
    "witcher3.exe", "worldoftanks.exe", "wotblitz.exe", "worldofwarships.exe", "robloxplayerbeta.exe",
    "rocketleague.exe", "destiny2.exe", "bf2042.exe", "bf1.exe", "bfv.exe", "cod.exe", "modernwarfare.exe",
    "blackops6.exe", "aces.exe", "eurotrucks2.exe", "amtrucks.exe", "rustclient.exe", "fc24.exe", "fc25.exe",
    "fc26.exe", "starfield.exe", "bg3.exe", "bg3_dx11.exe", "hogwartslegacy.exe", "minecraft.windows.exe",
    "javaw.exe#minecraft", "terraria.exe", "stardew valley.exe", "factorio.exe", "hollow_knight.exe",
    "sekiro.exe", "darksoulsiii.exe", "skyrimse.exe", "fallout4.exe", "re4.exe", "re2.exe", "mhw.exe",
    "monsterhunterwilds.exe", "wow.exe", "diablo iv.exe", "hearthstone.exe", "pathofexile_x64.exe",
    "pathofexile.exe", "genshinimpact.exe", "starrail.exe", "zenlesszonezero.exe", "deadbydaylight-win64-shipping.exe",
    "helldivers2.exe", "witcher3_dx12.exe", "forzahorizon5.exe", "forzahorizon4.exe", "rainbowsix.exe",
    "rainbowsix_vulkan.exe", "escapefromtarkov.exe", "warframe.x64.exe", "arma3_x64.exe", "dayz_x64.exe",
    "nms.exe", "sims4.exe", "ts4_x64.exe", "civilizationvi.exe", "civ7.exe", "hoi4.exe", "eu4.exe", "stellaris.exe",
    "ck3.exe", "victoria3.exe", "totalwar.exe", "valheim.exe", "phasmophobia.exe", "lethal company.exe",
    "among us.exe", "osu!.exe", "brawlhalla.exe", "smite.exe", "paladins.exe", "thefinals.exe", "deadlock.exe",
    "marvel-win64-shipping.exe", "palworld-win64-shipping.exe", "stalker2-win64-shipping.exe",
}
SHIPPING = re.compile(r"-win(?:64|gdk)-shipping\.exe$", re.I)
MINECRAFT_TITLE = re.compile(r"^minecraft\b", re.I)
# fullscreen is NOT a game for ordinary apps (fullscreen video, presentation, F11 browser, editors…)
NOT_GAMES = re.compile(
    r"^(?:explorer|chrome|firefox|msedge|opera|brave|vivaldi|browser|yandex|arc|code|devenv|pycharm\w*|idea\w*|"
    r"python\w*|cmd|powershell|pwsh|windowsterminal|conhost|telegram|discord|slack|teams|ms-teams|zoom|skype|"
    r"notepad\w*|winword|excel|powerpnt|onenote|outlook|acrord32|acrobat|sumatrapdf|vlc|mpc-hc\w*|mpc-be\w*|"
    r"potplayer\w*|mpv|wmplayer|video\.ui|microsoft\.media\.player|mediaplayer|obs\w*|spotify|photoshop|"
    r"illustrator|afterfx|premiere\w*|resolve|blender|freecad|openscad|sldworks|solidworks|arduino\w*|"
    r"steam|epicgameslauncher|galaxyclient|battle\.net|mstsc|vmware\w*|virtualbox\w*|anydesk|teamviewer|"
    r"applicationframehost|systemsettings|taskmgr|mspaint|photos|calculator\w*)\.exe$", re.I)


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-zа-яё0-9]+", (s or "").lower().replace("ё", "е")))


def blocked(w: Any, never_close: Iterable[str]) -> bool:
    """User list «Не закрывать»: exe names («cs2.exe», «cs2») or words of the title («Counter-Strike»)."""
    exe, title = (w.exe or "").lower(), _norm(w.title)
    for item in never_close or ():
        it = str(item or "").strip().lower()
        if not it:
            continue
        if it.endswith(".exe") and exe == it or exe and exe == it + ".exe":
            return True
        n = _norm(it)
        if n and len(n) >= 3 and re.search(rf"(?:^| ){re.escape(n)}(?: |$)", title):
            return True
    return False


def _index_titles(launcher: Any) -> list[str]:
    try:
        return [_norm(e.name) for e in launcher.games() if len(_norm(e.name)) >= 4]
    except Exception:                                   # noqa: BLE001 — index not built / no launcher
        return []


def why_game(w: Any, backend: Any = None, launcher: Any = None, game_mode: bool = False,
             path: Optional[str] = None) -> str:
    """-> '' (not a game) or a short reason for the log ('exe', 'library', 'index', 'fullscreen', 'game mode')."""
    exe = (w.exe or "").lower()
    if exe in LAUNCHERS:
        return ""                                       # «закрой стим» must keep working
    if exe in GAME_EXES or SHIPPING.search(exe) or (exe == "javaw.exe" and MINECRAFT_TITLE.search(w.title or "")):
        return "exe"
    if path is None:
        path = ""
        if backend is not None and hasattr(backend, "exe_path"):
            try:
                path = backend.exe_path(w.pid) or ""
            except Exception:                           # noqa: BLE001
                path = ""
    if path and GAME_DIRS.search(path) and not re.search(r"[\\/](?:launcher|portal|redist\w*|_commonredist|"
                                                         r"directx|vcredist)[\\/]", path, re.I):
        return "library"
    t = _norm(w.title)
    if t and any(t == g or t.startswith(g + " ") for g in _index_titles(launcher)):
        return "index"
    if NOT_GAMES.match(exe) or getattr(w, "is_browser", False):
        return ""
    covers, borderless = (False, False)
    if backend is not None and hasattr(backend, "fullscreen"):
        try:
            covers, borderless = backend.fullscreen(w.hwnd)
        except Exception:                               # noqa: BLE001
            covers, borderless = (False, False)
    if covers and borderless:
        return "fullscreen"
    if covers and game_mode:
        return "game mode"
    return ""


def game_mode_on(ctx: Any) -> bool:
    """The existing game-mode detection: profile «game» (auto-switched for fullscreen / Steam games)."""
    try:
        get = ctx.extra.get("get_profile") if ctx is not None else None
        return callable(get) and str(get()) == "game"
    except Exception:                                   # noqa: BLE001
        return False
