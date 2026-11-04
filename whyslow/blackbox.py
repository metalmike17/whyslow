"""Flight recorder: notice that the system (or the sampler) ended without a clean shutdown,
and explain what was happening in the minutes before.

How it works
- The daemon appends a {"event": "stop"} marker to the log on a clean SIGTERM (what systemd sends
  at shutdown). At the next start, a log whose last record is not that marker ended abnormally.
- If the last sample predates this boot, the *whole system* died (freeze, reset, power loss).
  If it is from the current boot, only the sampler died — a much smaller deal.
- The kernel journal around the last sample is searched for OOM kills, GPU hangs, overheating,
  kernel oopses, hardware errors and storage errors. We look it up by time window rather than
  boot id, so it stays correct even if several boots passed without the daemon running.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "whyslow"
LOG = STATE_DIR / "samples.jsonl"
PRE_SECONDS = 180          # seconds of samples kept with each incident
JOURNAL_BEFORE = 900       # journal lookback from the last sample
JOURNAL_AFTER = 120        # ...and lookahead (to see whether the system kept logging after we stopped)
SYSTEM_SLACK = 5.0         # seconds of clock slack when comparing the last sample with boot time

# category -> regex over journal lines. Patterns are case-sensitive and anchored to real failure
# messages: unit-start lines such as "Started systemd-oomd.service - ... OOM Killer" or
# "mcelog.service - Machine Check Exception Logging Daemon skipped" are boot noise, not evidence.
JOURNAL_PATTERNS = {
    "kernel": re.compile(r"Kernel panic|\bBUG: |\bOops: |Call Trace:|hung_task|blocked for more than \d+ seconds|"
                         r"soft lockup|hard LOCKUP|rcu.*detected stalls"),
    "gpu": re.compile(r"NVRM: Xid|amdgpu.*(ring .* timeout|GPU reset|gpu hang|VM_L2_PROTECTION_FAULT)|GPU HANG|"
                      r"i915.*(GPU hang|Resetting)|\bxe\b.*(GT reset|Timedout job)|nouveau.*(fault|timeout)", re.I),
    "thermal": re.compile(r"critical temperature reached|Package temperature above threshold|"
                          r"thermal.*(shutting down|shutdown)"),
    "hardware": re.compile(r"\[Hardware Error\]|Machine check events logged|mce: .*(uncorrect|fatal)|"
                           r"EDAC .*(UE|uncorrect)", re.I),
    "oom": re.compile(r"Out of memory: |oom-kill:|oom_reaper: |Killed process \d+|"
                      r"systemd-oomd\[\d+\]: (Killed|Considered)|earlyoom\[\d+\]: .*sending SIG"),
    "storage": re.compile(r"blk_update_request: .*error|Buffer I/O error|ata\d+\.\d+: (exception|failed command|"
                          r"hard resetting)|nvme\d.*(I/O .* timeout|controller is down|resetting controller)|"
                          r"EXT4-fs error|BTRFS (error|critical)", re.I),
}
# Only trust these categories when the line comes from the kernel itself.
KERNEL_ONLY = {"kernel", "gpu", "thermal", "hardware", "storage"}


# --------------------------------------------------------------------------
# detection (called once, at daemon start, before anything new is written)
# --------------------------------------------------------------------------
def _tail_records(log: Path, nbytes: int = 1_500_000) -> list[dict]:
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - nbytes))
            lines = f.read().decode(errors="ignore").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue   # partial first line of the tail, or a torn last write after a crash
    return out


def boot_time(now: float | None = None) -> float:
    """Wall-clock time this boot started (CLOCK_BOOTTIME counts suspended time too)."""
    return (now if now is not None else time.time()) - time.clock_gettime(time.CLOCK_BOOTTIME)


def check_previous_run(log: Path = LOG, now: float | None = None, boot_t: float | None = None) -> dict | None:
    """Return info about an abnormal end of the previous run, or None if it ended cleanly."""
    recs = _tail_records(log)
    if not recs or recs[-1].get("event") == "stop":
        return None
    samples = [r for r in recs if "stall" in r]
    if not samples:
        return None
    last_t = samples[-1]["t"]
    bt = boot_t if boot_t is not None else boot_time(now)
    return {
        "scope": "system" if last_t < bt - SYSTEM_SLACK else "daemon",
        "last_t": last_t,
        "detected_t": now if now is not None else time.time(),
        "samples": [s for s in samples if s["t"] >= last_t - PRE_SECONDS],
    }


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------
def journal_hits(last_t: float, runner=None) -> dict:
    """Search the journal around last_t. {'available', 'hits': [{cat,t,line}], 'last_entry_t'}."""
    cmd = ["journalctl", "--no-pager", "-q", "-o", "short-unix",
           "--since", f"@{int(last_t - JOURNAL_BEFORE)}", "--until", f"@{int(last_t + JOURNAL_AFTER)}"]
    try:
        r = (runner or subprocess.run)(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "hits": [], "last_entry_t": None}
    if "insufficient permissions" in (r.stderr or "").lower() or (r.returncode != 0 and not r.stdout):
        return {"available": False, "hits": [], "last_entry_t": None}
    hits, last_entry, seen = [], None, set()
    for line in r.stdout.splitlines():
        head, _, rest = line.partition(" ")
        try:
            t = float(head)
        except ValueError:
            continue
        last_entry = t
        for cat, rx in JOURNAL_PATTERNS.items():
            if cat in KERNEL_ONLY and " kernel: " not in " " + rest:
                continue
            if rx.search(rest):
                key = (cat, rest[:80])
                if key not in seen:        # kernel traces repeat the same line many times
                    seen.add(key)
                    hits.append({"cat": cat, "t": t, "line": rest.strip()[:240]})
                break
    return {"available": True, "hits": hits[:60], "last_entry_t": last_entry}


# --------------------------------------------------------------------------
# diagnosis
# --------------------------------------------------------------------------
def _facts(samples: list[dict]) -> dict:
    tail = [s for s in samples if s["t"] >= samples[-1]["t"] - 60] or samples
    avail = [s["mem"]["MemAvailable"] / s["mem"]["MemTotal"] for s in tail if s["mem"].get("MemTotal")]
    swap = [1 - s["mem"]["SwapFree"] / s["mem"]["SwapTotal"] for s in tail if s["mem"].get("SwapTotal")]
    gpu = [s["gpu"]["temp"] for s in samples if s.get("gpu")]
    temp = [s["temp"] for s in tail if s.get("temp")]
    # biggest RAM grower across the whole window
    growth: dict[str, float] = {}
    first = {a: e["rss"] for a, e in samples[0]["apps"].items()}
    for a, e in samples[-1]["apps"].items():
        if a in first:
            growth[a] = (e["rss"] - first[a]) / 1024          # MB
    top = max(samples[-1]["apps"].items(), key=lambda kv: kv[1]["rss"], default=(None, None))
    return {
        "min_avail": min(avail) if avail else 1.0,
        "max_swap_used": max(swap) if swap else 0.0,
        "mem_full": max((s["stall"].get("mem_full", 0) for s in tail), default=0),
        "mem_some": max((s["stall"].get("mem_some", 0) for s in tail), default=0),
        "io_full": max((s["stall"].get("io_full", 0) for s in tail), default=0),
        "cpu_some": max((s["stall"].get("cpu_some", 0) for s in tail), default=0),
        "cpu_temp": max(temp) if temp else None,
        "gpu_temp": max(gpu) if gpu else None,
        "oom_kills": sum(s["vm"].get("oom_kill", 0) for s in samples),
        "grower": max(growth.items(), key=lambda kv: kv[1]) if growth else None,
        "top_rss": (top[0], top[1]["rss"] / 1024) if top[0] else None,   # MB
    }


def diagnose(samples: list[dict], journal: dict | None) -> dict:
    """Best explanation for why the run ended. Honest about confidence."""
    f = _facts(samples)
    hits = {}
    for h in (journal or {}).get("hits", []):
        hits.setdefault(h["cat"], []).append(h)
    have_journal = bool(journal and journal.get("available"))
    ev: list[str] = []

    def q(cat):  # a representative journal line, as evidence
        return f"kernel log: “{hits[cat][0]['line'][:110]}”"

    mem_exhausted = f["min_avail"] < 0.05 or (f["min_avail"] < 0.10 and f["max_swap_used"] > 0.85)
    thrashing = f["mem_full"] >= 30 or f["mem_some"] >= 60

    if "kernel" in hits:
        cat, head, conf = "kernel", "A kernel problem (oops, lockup or hung task) was logged just before the end", "high"
        ev.append(q("kernel"))
    elif "gpu" in hits:
        cat, head, conf = "gpu", "The GPU driver reported a hang or reset just before the end", "high"
        ev.append(q("gpu"))
    elif "thermal" in hits or (f["cpu_temp"] or 0) >= 100 or (f["gpu_temp"] or 0) >= 95:
        cat, head, conf = "thermal", "The system was overheating", "high" if "thermal" in hits else "medium"
        if "thermal" in hits:
            ev.append(q("thermal"))
        if f["cpu_temp"]:
            ev.append(f"CPU reached {f['cpu_temp']:.0f}°C in the last minute")
        if f["gpu_temp"]:
            ev.append(f"GPU reached {f['gpu_temp']:.0f}°C")
    elif "hardware" in hits:
        cat, head, conf = "hardware", "A hardware error (machine check / memory error) was logged", "high"
        ev.append(q("hardware"))
    elif "oom" in hits or mem_exhausted or f["oom_kills"]:
        cat, conf = "memory", "high" if (mem_exhausted and thrashing) or "oom" in hits else "medium"
        head = ("The system ran out of memory"
                + (" and thrashed until it stopped responding" if thrashing and "oom" not in hits else ""))
        ev.append(f"only {f['min_avail']:.0%} of RAM was available" +
                  (f" and swap was {f['max_swap_used']:.0%} full" if f["max_swap_used"] > 0.3 else ""))
        if "oom" in hits:
            ev.append(q("oom"))
        elif have_journal:
            ev.append("the kernel OOM killer never fired, so nothing stepped in to free memory")
    elif "storage" in hits:
        cat, head, conf = "storage", "Disk errors were logged just before the end", "medium"
        ev.append(q("storage"))
    elif f["io_full"] >= 50:
        cat, head, conf = "storage", "Disk I/O stalled almost completely before the end", "low"
        ev.append(f"programs were fully blocked on I/O {f['io_full']:.0f}% of the time")
    elif f["cpu_some"] >= 80 or thrashing:
        cat, head, conf = "overload", "The system was heavily overloaded before the end", "low"
        ev.append(f"CPU pressure reached {f['cpu_some']:.0f}%, memory pressure {f['mem_some']:.0f}%")
    else:
        cat, conf = "unknown", "low"
        head = ("No warning signs were visible in the last few minutes — this looks like a power loss, "
                "a manual reset, or a sudden hardware/driver lockup that left no trace")
        ev.append("memory, disk, CPU and temperatures all looked normal until the last sample")

    if journal and journal.get("available") and journal.get("last_entry_t") is not None:
        gap = journal["last_entry_t"] - samples[-1]["t"]
        if gap > 45:
            ev.append(f"the system kept logging for {gap:.0f}s after the sampler stopped — "
                      "so this may have been the sampler dying, not the machine")
    if not have_journal:
        ev.append("the system journal could not be read, so kernel messages were not checked")
    return {"category": cat, "headline": head, "confidence": conf, "evidence": ev, "facts": f}


def active_oom_daemons() -> list[str]:
    """Which userspace OOM killers are running right now (systemd-oomd, earlyoom)."""
    out = []
    for unit in ("systemd-oomd", "earlyoom"):
        try:
            if subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=5).returncode == 0:
                out.append(unit)
        except (OSError, subprocess.SubprocessError):
            pass
    return out


def memory_advice(active: list[str]) -> list[str]:
    if "earlyoom" in active:
        first = ("earlyoom is running but did not step in before the system froze — raise its thresholds "
                 "(e.g. `-m 8 -s 8`) so it acts earlier.")
    elif "systemd-oomd" in active:
        first = ("systemd-oomd is running but did not step in before the system froze. It only watches units "
                 "configured for it (ManagedOOMMemoryPressure=/ManagedOOMSwap=), so a runaway app elsewhere "
                 "slips past. earlyoom watches overall free RAM and reacts sooner.")
    else:
        first = ("No userspace OOM killer is running. Install one (earlyoom, or enable systemd-oomd) so a "
                 "runaway app is killed before the whole system locks up.")
    return [first, "Check which app was growing (listed above) — that is your leak or heaviest workload."]


ADVICE = {
    "thermal": ["Check fans, dust and the fan curve; consider lowering the CPU/GPU power limit."],
    "gpu": ["Update the GPU driver; if it repeats, try a different driver version or lower GPU clocks."],
    "kernel": ["Save the kernel messages (`journalctl -k -b -1`) and report them to your distro."],
    "hardware": ["Run a memory test (memtest86+) and check CPU/RAM overclocks or XMP/EXPO settings."],
    "storage": ["Check drive health with `smartctl -a /dev/<disk>` and the cable/port."],
    "overload": ["Find what was running heavily right before (see the timeline) and limit it."],
    "unknown": ["If it repeats, suspect power: PSU, power strip, or an unstable overclock.",
                "If the screen froze but the machine was on, try SSH-ing in next time to see if it is alive."],
}


# --------------------------------------------------------------------------
# incidents on disk
# --------------------------------------------------------------------------
def incident_dir(state_dir: Path | None = None) -> Path:
    return (state_dir or STATE_DIR) / "incidents"


def record_incident(info: dict, state_dir: Path | None = None, journal_fn=journal_hits) -> Path:
    """Diagnose an abnormal end and persist it (survives log trimming)."""
    journal = journal_fn(info["last_t"]) if info["scope"] == "system" else None
    verdict = diagnose(info["samples"], journal)
    d = incident_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(info['last_t']))}-{info['scope']}.json"
    doc = {"kind": "unclean_end", "scope": info["scope"], "last_t": info["last_t"],
           "detected_t": info["detected_t"], "verdict": verdict,
           "journal": journal, "samples": info["samples"], "seen": False}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")))
    tmp.replace(path)
    return path


def list_incidents(state_dir: Path | None = None) -> list[dict]:
    d = incident_dir(state_dir)
    out = []
    for p in sorted(d.glob("*.json"), reverse=True) if d.exists() else []:
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        doc["path"] = str(p)
        out.append(doc)
    return out


def mark_seen(path: str) -> None:
    p = Path(path)
    try:
        doc = json.loads(p.read_text())
        doc["seen"] = True
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, separators=(",", ":")))
        tmp.replace(p)
    except (OSError, ValueError):
        pass


def describe_incident(inc: dict, oom_daemons=active_oom_daemons) -> list[str]:
    v = inc["verdict"]
    when = time.strftime("%a %H:%M:%S", time.localtime(inc["last_t"]))
    ago = max(0, inc["detected_t"] - inc["last_t"])
    if inc["scope"] == "daemon":
        lines = [f"The background sampler stopped unexpectedly at {when} (the system itself kept running).",
                 "It was probably killed or crashed; nothing is wrong with the machine."]
        return lines
    lines = [f"Your system ended unexpectedly at {when} ({ago / 60:.0f} min before it was back up).",
             f"Most likely: {v['headline']}  [{v['confidence']} confidence]"]
    lines += [f"  • {e}" for e in v["evidence"]]
    f = v["facts"]
    if v["category"] in ("memory", "overload"):
        if f.get("grower") and f["grower"][1] > 200:
            lines.append(f"Fastest-growing app in the last 3 minutes: {f['grower'][0]} (+{f['grower'][1] / 1024:.1f} GB)")
        if f.get("top_rss"):
            lines.append(f"Biggest app at the end: {f['top_rss'][0]} ({f['top_rss'][1] / 1024:.1f} GB)")
    other = [h for h in (inc.get("journal") or {}).get("hits", []) if h["cat"] not in (v["category"],)][:3]
    if other:
        lines.append("Other notable kernel messages:")
        lines += [f"  • [{h['cat']}] {h['line'][:120]}" for h in other]
    lines.append("What you can do:")
    advice = memory_advice(oom_daemons()) if v["category"] == "memory" else ADVICE.get(v["category"], [])
    lines += [f"  → {a}" for a in advice]
    return lines
