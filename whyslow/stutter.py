"""Game stutter: read a MangoHud frame-time log, find the hitches, and explain each one using
whyslow's recorded system history from that exact moment.

Honesty rules: a cause is only named when the system data shows it. When the system was calm, the
verdict says the stutter is probably inside the game, and says that this is by elimination.
"""
from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import analyze as A
from . import devices

HITCH_MIN_MS = 25.0      # a frame this slow is at most 40 fps...
HITCH_FACTOR = 2.5       # ...and must be this many times slower than that second's median frame
HITCH_ABS_MS = 12.0      # ...and at least this much slower in absolute terms
MERGE_GAP = 1.0          # hitch frames closer than this (s) are one event
EARLY_SESSION = 120.0    # hitches this soon after logging began suggest shader compilation / cache warmup
PAD_BEFORE, PAD_AFTER = 3.0, 2.0   # system samples considered around a hitch (s)
GPU_BOUND = 97.0

_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")
LOG_DIRS = ("~/MangoHud", "~/.local/share/MangoHud", "~/mangohud_logs", "/tmp/MangoHud", "~/Documents/MangoHud", "~")


class LogError(Exception):
    pass


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
@dataclass
class Frames:
    path: str
    program: str
    start: float                      # wall-clock time (epoch s) logging began
    t: list[float]                    # wall-clock time of each frame's end
    ft: list[float]                   # frame time, ms
    cols: dict[str, list[float]] = field(default_factory=dict)


def parse(path: str | Path) -> Frames:
    path = Path(path)
    lines = path.read_text(errors="ignore").splitlines()
    hdr = next((i for i, l in enumerate(lines) if {"fps", "frametime"} <= set(l.split(","))), None)
    if hdr is None:
        if lines and "Average FPS" in lines[0]:
            raise LogError("That is MangoHud's *summary* file. Use the per-frame log "
                           "(same name without '_summary').")
        raise LogError("Not a MangoHud frame log (no 'fps,frametime' column header found).")
    names = lines[hdr].split(",")
    cols: dict[str, list[float]] = {n: [] for n in names}
    for l in lines[hdr + 1:]:
        parts = l.split(",")
        if len(parts) != len(names):
            continue                      # a truncated last line
        try:
            vals = [float(x) for x in parts]
        except ValueError:
            continue
        for n, v in zip(names, vals):
            cols[n].append(v)
    ft = cols["frametime"]
    if len(ft) < 30:
        raise LogError("The log has too few frames to analyze.")
    if "elapsed" in cols:                 # nanoseconds since logging began
        el = [x / 1e9 for x in cols["elapsed"]]
    else:
        el, acc = [], 0.0
        for x in ft:
            acc += x / 1000
            el.append(acc)
    m = _TS.search(path.stem)
    if m:                                 # the file name carries the moment logging began (local time)
        y, mo, d, h, mi, s = map(int, m.groups())
        start = time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
        program = path.stem[: m.start()].rstrip("_") or "game"
    else:
        start = path.stat().st_mtime - el[-1]
        program = path.stem
    return Frames(str(path), program, start, [start + e for e in el], ft, cols)


def find_logs(dirs=LOG_DIRS) -> list[Path]:
    """MangoHud per-frame logs in the usual places, newest first (summary files excluded)."""
    found: dict[Path, float] = {}
    for d in dirs:
        p = Path(d).expanduser()
        if p.is_dir():
            for f in p.glob("*_????-??-??_??-??-??.csv"):
                if not f.stem.endswith("_summary"):
                    found[f] = f.stat().st_mtime
    return sorted(found, key=found.get, reverse=True)


# --------------------------------------------------------------------------
# hitch detection
# --------------------------------------------------------------------------
@dataclass
class Hitch:
    t0: float
    t1: float
    frames: int
    worst_ms: float
    baseline_ms: float


def detect_hitches(fr: Frames) -> list[Hitch]:
    t_ref = fr.t[0]
    buckets: dict[int, list[float]] = {}
    for t, ft in zip(fr.t, fr.ft):
        buckets.setdefault(int(t - t_ref), []).append(ft)
    med = {b: statistics.median(v) for b, v in buckets.items()}
    hot = [(t, ft, med[int(t - t_ref)]) for t, ft in zip(fr.t, fr.ft)
           if ft >= HITCH_MIN_MS and ft >= max(HITCH_FACTOR * med[int(t - t_ref)], med[int(t - t_ref)] + HITCH_ABS_MS)]
    out: list[Hitch] = []
    for t, ft, m in hot:
        if out and t - out[-1].t1 <= MERGE_GAP:
            h = out[-1]
            h.t1, h.frames, h.worst_ms = t, h.frames + 1, max(h.worst_ms, ft)
        else:
            out.append(Hitch(t - ft / 1000, t, 1, ft, m))
    return out


def stats(fr: Frames) -> dict:
    ft = sorted(fr.ft)
    n = len(ft)
    pct = lambda p: ft[min(n - 1, int(n * p))]
    total_s = sum(fr.ft) / 1000
    return {"duration": fr.t[-1] - fr.t[0], "frames": n, "avg_fps": n / total_s if total_s else 0.0,
            "low1_fps": 1000 / pct(0.99), "low01_fps": 1000 / pct(0.999), "median_ms": statistics.median(fr.ft)}


# --------------------------------------------------------------------------
# explaining a hitch with system history
# --------------------------------------------------------------------------
CAUSE_TITLE = {
    "disk": "The disk couldn't keep up",
    "memory": "Memory pressure",
    "background_cpu": "A background program took the CPU",
    "thermal": "Thermal / power throttling",
    "gpu_bound": "The GPU was the bottleneck",
    "shader": "Probably shader compilation or cache warm-up (by elimination)",
    "game_internal": "Something inside the game (by elimination)",
    "unknown": "Cannot tell (no system data for this moment)",
}


@dataclass
class Verdict:
    cause: str
    confidence: str
    evidence: list[str] = field(default_factory=list)


def _context(samples: list[dict], h: Hitch) -> dict | None:
    win = [s for s in samples if h.t0 - PAD_BEFORE <= s["t"] <= h.t1 + PAD_AFTER]
    if not win:
        return None
    pr = {k: max(A._pressure(s)[k] for s in win) for k in A.THRESHOLDS}
    game, gpsi, gdevs, grss = None, {"io": 0.0, "mem": 0.0, "cpu": 0.0}, {}, 0
    for s in win:
        for app, e in s["apps"].items():
            if e.get("game") and e["rss"] >= grss:
                game, grss = (app, e["game"]), e["rss"]
    if game:
        for s in win:
            e = s["apps"].get(game[0])
            if e:
                for k in gpsi:
                    gpsi[k] = max(gpsi[k], (e.get("psi") or {}).get(k, 0.0))
                for d, mb in e.get("devs", {}).items():
                    gdevs[d] = max(gdevs.get(d, 0), mb)
    others: dict[str, dict] = {}
    for s in win:
        for app, e in s["apps"].items():
            if game and app == game[0]:
                continue
            o = others.setdefault(app, {"cpu": 0.0, "io": 0.0, "n": 0, "devs": {}})
            o["cpu"] += e["cpu"]; o["io"] += e["rd"] + e["wr"]; o["n"] += 1
            for d, mb in e.get("devs", {}).items():
                o["devs"][d] = max(o["devs"].get(d, 0), mb)
    for o in others.values():
        o["cpu"] /= len(win); o["io"] /= len(win)
    disks: dict[str, float] = {}
    for s in win:
        for d, v in s["disks"].items():
            disks[d] = max(disks.get(d, 0.0), v["busy"])
    busy_run = [s for s in win if s["cpu_util"] > 40 and s["max_mhz"]]
    ratio = (sum(s["mhz"] / s["max_mhz"] for s in busy_run) / len(busy_run)) if busy_run else 1.0
    gpus = [s["gpu"] for s in win if s.get("gpu")]
    return {"pr": pr, "game": game, "gpsi": gpsi, "gdevs": gdevs, "others": others, "disks": disks,
            "freq_ratio": ratio, "gpu_throttled": any(g["hw_thermal"] or g["sw_thermal"] for g in gpus),
            "swap_io": sum(s["vm"].get("pswpin", 0) + s["vm"].get("pswpout", 0) for s in win),
            "temp": max((s["temp"] for s in win if s.get("temp")), default=None)}


def _mh(fr: Frames, h: Hitch, col: str) -> float | None:
    """Average of a MangoHud column over the frames around a hitch."""
    v = fr.cols.get(col)
    if not v:
        return None
    sel = [x for t, x in zip(fr.t, v) if h.t0 - 0.5 <= t <= h.t1 + 0.5]
    return sum(sel) / len(sel) if sel else None


def explain(h: Hitch, fr: Frames, samples: list[dict]) -> Verdict:
    ctx = _context(samples, h)
    gpu = _mh(fr, h, "gpu_load")
    if ctx is None:
        ev = ["whyslow's background recorder had no data for this moment (was the daemon running?)"]
        if gpu is not None and gpu >= GPU_BOUND:
            return Verdict("gpu_bound", "medium", [f"MangoHud shows the GPU at {gpu:.0f}% load"] + ev)
        return Verdict("unknown", "low", ev)

    pr, gp = ctx["pr"], ctx["gpsi"]
    game_name = ctx["game"][1] if ctx["game"] else None
    busy = {d: b for d, b in ctx["disks"].items() if b >= A.BUSY_DISK}

    # 1) disk
    if gp["io"] >= A.VICTIM_WAIT["io"] or pr["io"] >= A.THRESHOLDS["io"]:
        ev = []
        if gp["io"] >= A.VICTIM_WAIT["io"]:
            ev.append(f"the game's own tasks were waiting on I/O {gp['io']:.0f}% of the time")
        if pr["io"] >= A.THRESHOLDS["io"]:
            ev.append(f"system-wide I/O stall reached {pr['io']:.0f}%")
        mine = [d for d in busy if d in ctx["gdevs"]]
        if mine:
            d = max(mine, key=busy.get)
            ev.append(f"{devices.label(d)} was {busy[d]:.0f}% busy and the game's files are on it")
        elif busy:
            d = max(busy, key=busy.get)
            ev.append(f"{devices.label(d)} was {busy[d]:.0f}% busy (the game has no files there, so another program was using it)")
        heavy = sorted(((o["io"], a) for a, o in ctx["others"].items() if o["io"] > 20e6), reverse=True)
        if heavy:
            ev.append(f"{heavy[0][1]} was moving {heavy[0][0] / 1e6:.0f} MB/s in the background")
        conf = "high" if gp["io"] >= A.VICTIM_WAIT["io"] and (mine or heavy) else "medium"
        return Verdict("disk", conf, ev)

    # 2) memory
    if pr["mem"] >= A.THRESHOLDS["mem"] or gp["mem"] >= A.VICTIM_WAIT["mem"] or ctx["swap_io"] > 4000:
        ev = []
        if gp["mem"] >= A.VICTIM_WAIT["mem"]:
            ev.append(f"the game's own tasks waited on memory {gp['mem']:.0f}% of the time")
        if pr["mem"] >= A.THRESHOLDS["mem"]:
            ev.append(f"system memory pressure reached {pr['mem']:.0f}%")
        if ctx["swap_io"] > 4000:
            ev.append(f"{ctx['swap_io'] * 4 // 1024} MB moved to/from swap around the hitch")
        return Verdict("memory", "high" if gp["mem"] >= A.VICTIM_WAIT["mem"] else "medium", ev)

    # 3) another program took the CPU
    hogs = sorted(((o["cpu"], a) for a, o in ctx["others"].items() if o["cpu"] >= 200), reverse=True)
    if hogs and (pr["cpu"] >= A.THRESHOLDS["cpu"] or gp["cpu"] >= A.VICTIM_WAIT["cpu"]):
        ev = [f"{hogs[0][1]} was using ~{hogs[0][0] / 100:.0f} cores while the game was running"]
        if gp["cpu"] >= A.VICTIM_WAIT["cpu"]:
            ev.append(f"the game's threads spent {gp['cpu']:.0f}% of the time waiting for a free core")
        return Verdict("background_cpu", "high" if gp["cpu"] >= A.VICTIM_WAIT["cpu"] else "medium", ev)

    # 4) throttling
    if ctx["freq_ratio"] < 0.65 or ctx["gpu_throttled"]:
        ev = []
        if ctx["freq_ratio"] < 0.65:
            hot = f" ({ctx['temp']:.0f}°C)" if ctx["temp"] else ""
            ev.append(f"the CPU ran at only {ctx['freq_ratio']:.0%} of its max clock while busy{hot}")
        if ctx["gpu_throttled"]:
            ev.append("the GPU reported thermal throttling")
        return Verdict("thermal", "medium", ev)

    # 5) GPU simply maxed out
    if gpu is not None and gpu >= GPU_BOUND:
        return Verdict("gpu_bound", "medium", [f"MangoHud shows the GPU at {gpu:.0f}% load — frames took longer "
                                               "because the GPU was the bottleneck, not because of a stall"])

    # 6) the system was calm: the cause is inside the game (by elimination — say so)
    calm = ["the system was calm around this hitch: no disk, memory or CPU pressure, no throttling, GPU not maxed"]
    early = h.t0 - fr.t[0] <= EARLY_SESSION
    if early:
        return Verdict("shader", "low", calm + ["it happened in the first two minutes of the log, when shader "
                                                "compilation and cache warm-up are most common"])
    return Verdict("game_internal", "low", calm + ["likely asset streaming, shader compilation, or the game's own code"])


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
FIXES = {
    "disk": "Move the game to an SSD, and pause background downloads/indexers while playing.",
    "memory": "Close memory-hungry apps before launching, and make sure swap/zram is configured.",
    "background_cpu": "Close or lower the priority of the background program while gaming "
                      "(`renice -n 10 -p <pid>`), or limit it to fewer cores.",
    "thermal": "Check cooling and the fan curve; consider a modest power/clock limit.",
    "gpu_bound": "Lower resolution or graphics settings, or enable upscaling (FSR/DLSS).",
    "shader": "Let the shader cache warm up over a session or two; in Steam, enable Shader Pre-Caching.",
    "game_internal": "Try a different Proton/Wine version, and check the game's known-issues page.",
}


def report(fr: Frames, samples: list[dict], top: int = 5) -> str:
    st = stats(fr)
    hitches = detect_hitches(fr)
    lines = [f"Game stutter report — {fr.program}",
             f"  {st['duration'] / 60:.1f} min · {st['avg_fps']:.0f} fps average · "
             f"1% low {st['low1_fps']:.0f} fps · 0.1% low {st['low01_fps']:.0f} fps"]
    if not hitches:
        lines.append("  No hitches found — frame times were steady (nothing ≥25 ms and ≥2.5× the normal frame).")
        return "\n".join(lines)
    per_min = len(hitches) / max(st["duration"] / 60, 1 / 60)
    lines.append(f"  {len(hitches)} hitch(es), {per_min:.1f} per minute, worst {max(h.worst_ms for h in hitches):.0f} ms")

    verdicts = [(h, explain(h, fr, samples)) for h in hitches]
    covered = sum(1 for _, v in verdicts if v.cause != "unknown")
    if covered < len(verdicts):
        lines.append(f"  (whyslow had system data for {covered} of {len(verdicts)} hitches)")
    counts: dict[str, int] = {}
    for _, v in verdicts:
        counts[v.cause] = counts.get(v.cause, 0) + 1
    lines.append("")
    lines.append("What caused them:")
    for cause, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {n:>3} × {CAUSE_TITLE[cause]}")
    lines.append("")
    lines.append(f"Worst {min(top, len(verdicts))} hitches:")
    for h, v in sorted(verdicts, key=lambda hv: -hv[0].worst_ms)[:top]:
        when = time.strftime("%H:%M:%S", time.localtime(h.t0))
        lines.append(f"  {when} (+{h.t0 - fr.t[0]:.0f}s): {h.worst_ms:.0f} ms"
                     + (f" ×{h.frames} frames" if h.frames > 1 else "")
                     + f" — {CAUSE_TITLE[v.cause]} [{v.confidence}]")
        lines += [f"      • {e}" for e in v.evidence]
    fixes = [FIXES[c] for c, _ in sorted(counts.items(), key=lambda kv: -kv[1]) if c in FIXES]
    if fixes:
        lines.append("")
        lines.append("What to try (most common cause first):")
        lines += [f"  → {f}" for f in fixes[:3]]
    return "\n".join(lines)


HOW_TO = """To record frame times with MangoHud (already installed on this machine):

  Steam:        set the game's launch options to   mangohud %command%
  Faugus/Lutris/Heroic: enable "MangoHud" in the game's settings
  Anything:     MANGOHUD_CONFIG="autostart_log=5,log_duration=600,output_folder=$HOME/MangoHud" mangohud <game>

Press Shift+F2 in-game to start/stop logging (or use autostart_log). Logs are CSV files named
<game>_<date>_<time>.csv. Then run:   whyslow stutter            (newest log found)
                                      whyslow stutter FILE.csv

Run whyslow's background daemon while you play: it records what the system was doing, and
only about the last two hours are kept."""
