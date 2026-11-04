"""`whyslow enable` / `whyslow disable`: opt in to (or out of) the always-on parts.

Installing a package never starts recording by itself; the user turns it on explicitly. Enabling does two
things in the user's own home, no root: starts the background sampler as a systemd *user* service, and adds a
tray icon at login. Both are removed again by `disable`. The recorded data stays in ~/.local/state/whyslow.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGED_UNIT = Path("/usr/lib/systemd/user/whyslow.service")
MARK = "# written by `whyslow enable`"
TRAY_NAME = "io.github.metalmike17.WhySlow-tray.desktop"


def _appimage() -> str | None:
    """Path of the running AppImage (set by the AppImage runtime), whose mount point is temporary."""
    return os.environ.get("APPIMAGE")


def _exe() -> str:
    ai = _appimage()
    return shlex.quote(ai) if ai else (shutil.which("whyslow") or f"{sys.executable} -m whyslow")


def _unit_text(pkgdir: Path) -> str:
    if _appimage():            # the AppImage's own interpreter path vanishes on exit: start the AppImage itself
        return f"""{MARK}
[Unit]
Description=WhySlow background sampler

[Service]
ExecStart={_exe()} daemon
Nice=10
Restart=on-failure

[Install]
WantedBy=default.target
"""
    return f"""{MARK}
[Unit]
Description=WhySlow background sampler

[Service]
Environment=PYTHONPATH={pkgdir}
ExecStart={sys.executable} -m whyslow daemon
Nice=10
Restart=on-failure

[Install]
WantedBy=default.target
"""


def _tray_text(exe: str) -> str:
    return f"""{MARK}
[Desktop Entry]
Type=Application
Name=WhySlow (tray)
Comment=Shows a tray icon and warns about slowdowns and crashes
Exec={exe} gui --minimized
Icon=io.github.metalmike17.WhySlow
X-GNOME-Autostart-enabled=true
"""


def enable(home: Path | None = None, run=subprocess.run, packaged: Path = PACKAGED_UNIT,
           tray: bool = True) -> list[str]:
    home = home or Path.home()
    out = []
    unit_dir = home / ".config/systemd/user"
    if _appimage() or not packaged.exists():
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "whyslow.service").write_text(_unit_text(Path(__file__).resolve().parent.parent))
        out.append("installed a user service (no packaged unit found)")
    run(["systemctl", "--user", "daemon-reload"], check=True)
    run(["systemctl", "--user", "enable", "--now", "whyslow.service"], check=True)
    out.append("background sampler enabled and started")
    if tray:
        ad = home / ".config/autostart"
        ad.mkdir(parents=True, exist_ok=True)
        (ad / TRAY_NAME).write_text(_tray_text(_exe()))
        out.append("tray icon will start at your next login")
    return out


def disable(home: Path | None = None, run=subprocess.run) -> list[str]:
    home = home or Path.home()
    out = []
    run(["systemctl", "--user", "disable", "--now", "whyslow.service"], check=False)
    out.append("background sampler stopped and disabled")
    unit = home / ".config/systemd/user/whyslow.service"
    if unit.exists() and MARK in unit.read_text():      # only remove what we wrote
        unit.unlink()
        run(["systemctl", "--user", "daemon-reload"], check=False)
    tray = home / ".config/autostart" / TRAY_NAME
    if tray.exists():
        tray.unlink()
        out.append("tray autostart removed")
    out.append("recorded data was kept in ~/.local/state/whyslow (delete it to forget everything)")
    return out


def clean_appimage_env(env=None) -> bool:
    """Inside an AppImage the bundled libraries and Python are on LD_LIBRARY_PATH/PYTHONHOME. Host programs we run
    (systemd-analyze, systemctl, nvidia-smi, rpm...) would load those instead of their own and break, so remove
    them from the environment children inherit. Our own process is unaffected: it is already running."""
    env = os.environ if env is None else env
    appdir = env.get("APPDIR")
    if not appdir:
        return False
    keep = [x for x in env.get("LD_LIBRARY_PATH", "").split(":") if x and not x.startswith(appdir)]
    if keep:
        env["LD_LIBRARY_PATH"] = ":".join(keep)
    else:
        env.pop("LD_LIBRARY_PATH", None)
    for k in ("PYTHONHOME", "PYTHONPATH"):
        if env.get(k, "").startswith(appdir):
            del env[k]
    return True
