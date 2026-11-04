"""Turn a diagnosed incident into concrete, evidence-based suggestions.

Rules only fire on facts found in the incident. Suggestions are text for a human to act on;
whyslow never runs any of these commands itself.
"""
from __future__ import annotations

import shlex

from . import devices

UPDATER_HINTS = ("dnf", "packagekit", "updater", "discover", "flatpak", "pacman", "apt", "yum", "rpm",
                 "gnome-software", "snap")
MIN_CORES_TO_RENICE = 3.0
MIN_RSS_MB_TO_CLOSE = 500


def _pgrep(app: str) -> str:
    return f"$(pgrep -d, -f {shlex.quote(app)})"


def _swap_is_all_zram() -> bool | None:
    try:
        with open("/proc/swaps") as f:
            names = [l.split()[0] for l in f.read().splitlines()[1:] if l.strip()]
    except OSError:
        return None
    return bool(names) and all("zram" in n for n in names)


def suggest(ep) -> list[str]:  # ep: analyze.Episode (not imported to avoid a cycle)
    out: list[str] = []
    top = ep.culprits[0] if ep.culprits else None

    # 1) A culprit is using a saturated disk -> where should its data live?
    if ep.main_disk and ep.kind in ("io", "pagein"):
        users = [c for c in ep.culprits if ep.main_disk in c.devices]
        who = users[0].app
        if devices.is_rotational(ep.main_disk):
            msg = (f"{who} has files on {ep.main_disk}, a spinning disk that was saturated. "
                   "Moving its data (e.g. the game or install folder) to an SSD would remove most of this stall")
            targets = devices.faster_targets(ep.main_disk)
            if targets:
                t = targets[0]
                msg += f" — e.g. {t['mount']} ({t['free_gb']:.0f} GB free on {t['label']})"
            out.append(msg + ".")
        else:
            out.append(f"{ep.main_disk} was saturated even though it is fast, so a big sequential "
                       f"job ({who}) is probably hogging it. Run it while you're idle, or lower its I/O priority: "
                       f"`ionice -c3 -p {_pgrep(who)}`.")
        try:
            from . import hwcheck
            for f in hwcheck.findings_for_disk(ep.main_disk):
                out.append(f"Hardware: {f.title}. {f.fix[0] if f.fix else ''}".strip())
        except Exception:
            pass                       # hardware hints are a bonus; never break a report
        if len(users) >= 2:
            names = ", ".join(c.app for c in users[:3])
            out.append(f"{names} were all using {ep.main_disk} at the same time. Running them one at a time "
                       "avoids the contention.")

    # 2) Page-in stall where the busy disk is unrelated: say where the culprits' data actually is
    if ep.kind == "pagein" and not ep.main_disk and top and top.devices:
        disk = max(top.devices, key=top.devices.get)
        if devices.is_rotational(disk):
            out.append(f"{top.app}'s data lives on {devices.label(disk)}, a spinning disk; "
                       "page-ins from it are slow. Moving it to an SSD would help.")

    # 2b) A *game* whose own tasks waited on I/O while a spinning disk holding its files was saturated
    for v in ep.victims:
        if v.game and v.waits.get("io", 0) >= 10:
            for d in sorted(v.devices, key=lambda d: -v.devices[d]):
                if ep.disks.get(d, 0) >= 60 and devices.is_rotational(d):
                    msg = (f"{v.game} spent {v.waits['io']:.0f}% of its time waiting on I/O, and its files are on "
                           f"{d}, a saturated spinning disk. Moving the game to an SSD is the fix most likely to "
                           "remove these hitches")
                    t = devices.faster_targets(d)
                    out.append(msg + (f" — e.g. {t[0]['mount']} ({t[0]['free_gb']:.0f} GB free)." if t else "."))
                    break

    # 3a) Knowledge-base fixes for culprits we recognise (top two, one actionable line each)
    for c in [c for c in ep.culprits if c.known and c.known.fix and c.known.safe != "kernel"][:2]:
        out.append(f"{c.known.title}: {c.known.fix[0]}.".replace("..", "."))

    # 3b) Generic updater hint, only for updaters the knowledge base does not already explain
    for c in ep.culprits:
        if not c.known and any(h in c.app.lower() for h in UPDATER_HINTS):
            out.append(f"A system updater ({c.app}) was active during this slowdown. Let it finish before heavy "
                       f"tasks, or pause it: `kill -STOP {_pgrep(c.app)}` (resume with `kill -CONT`).")
            break

    # 4) CPU contention from one app
    if ep.kind == "cpu" and top and top.cores >= MIN_CORES_TO_RENICE:
        out.append(f"{top.app} was using ~{top.cores:.0f} cores. To keep the desktop responsive while it runs, "
                   f"lower its priority: `renice -n 10 -p {_pgrep(top.app)}`.")

    # 5) Real RAM shortage -> biggest consumer
    if ep.kind == "ram":
        big = max(ep.culprits, key=lambda c: c.rss_mb, default=None)
        if big and big.rss_mb >= MIN_RSS_MB_TO_CLOSE:
            out.append(f"Closing {big.app} would free the most memory (it is using {big.rss_mb / 1024:.1f} GB).")

    # 6) Factors
    for f in ep.factors:
        if f.startswith("CPU was running at only"):
            out.append("The CPU slowed itself down from heat or power limits. If this happens under normal load, "
                       "check fans, dust and the fan curve.")
        elif "GPU was thermally throttled" in f:
            out.append("The GPU overheated and slowed down. Check case airflow or raise the GPU fan curve.")
        elif "swapping" in f:
            z = _swap_is_all_zram()
            if z:
                out.append("ℹ Swap here is zram (compressed RAM), so that swapping did not hit your disk.")

    # de-duplicate, keep order
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:5]
