"""Best-effort detection of 'this process belongs to a game', from its command line alone.

Linux has no universal marker for games. We recognise the common launchers' fingerprints:
- Steam / Proton native layout: ``.../steamapps/common/<Game>/...``
- Windows games under Wine/Proton/umu (Faugus, Lutris, Heroic, Bottles...): a game ``.exe`` on the
  command line that is not a Windows system helper, installer or crash reporter.
Native non-Steam games cannot be recognised this way; callers must treat None as "unknown".
"""
from __future__ import annotations

import re

_STEAM_PATH = re.compile(r"steamapps[/\\]common[/\\]([^/\\\"']+)", re.I)
_EXE = re.compile(r"([^/\\\s\"']+)\.exe\b", re.I)

# Windows plumbing, installers, launchers' helpers and crash reporters: not the game itself.
_NOT_GAMES = re.compile(
    r"^(explorer|services|winedevice|plugplay|svchost|rpcss|conhost|wineboot|start|steam|cmd|wineserver|"
    r"iexplore|tabtip|wine|wine64|rundll32|regedit|msiexec|taskmgr|notepad|winecfg|control|lsass|"
    r"setup|unins\d*|vcredist.*|dxsetup|dotnet.*|ndp.*|netfx.*|installer|"
    r"crashreport.*|crashpad.*|unitycrashhandler.*|bugsplat.*|easyanticheat.*|beservice.*|"
    r"steamwebhelper|gameoverlayui|steamservice|steam_monitor|epicwebhelper|eosoverlay.*|"
    r"xalia|lsteamclient|steamclient.*|ubiconnect.*|upc|origin.*|ealink.*|eadesktop.*|"
    r"rtool|cls-.*|arc|unarc|7z|7za|xtool|precomp.*|srep.*|zstd.*)$", re.I)


def detect(cmdline: str) -> str | None:
    """The game's display name if this command line looks like a game, else None."""
    if not cmdline:
        return None
    m = _STEAM_PATH.search(cmdline)
    if m:
        return m.group(1).strip()
    for name in _EXE.findall(cmdline):
        if not _NOT_GAMES.match(name):
            return name
    return None
