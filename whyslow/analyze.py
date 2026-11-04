"""Turn a list of sampler snapshots into a plain-English diagnosis."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import devices, offenders

# (metric, threshold %) — a tick counts as "stalled" when a metric crosses its threshold.
# PSI "some" CPU pressure is high on any busy box, so its bar is much higher.
THRESHOLDS = {"io": 8.0, "mem": 5.0, "cpu": 35.0}
MAX_GAP = 20            # seconds of calm tolerated inside one incident (stalls closer than this merge)
BUSY_DISK = 60.0        # a disk this busy (%) is a plausible stall source
LOW_RAM_FRACTION = 0.12  # below this share of RAM available, call it a real shortage
SECONDARY_RATIO = 0.35  # a secondary cause must reach this share of the primary's weight
VICTIM_WAIT = {"io": 10.0, "mem": 5.0, "cpu": 25.0}   # an app's own tasks waited this % of the time
SELF_INFLICTED_CORES = 3.0  # an app using this many cores is mostly waiting on itself, not a victim of CPU


def _pressure(s: dict) -> dict[str, float]:
    st = s["stall"]
    return {
        "io": st.get("io_full", 0.0) + 0.25 * st.get("io_some", 0.0),
        "mem": st.get("mem_full", 0.0) * 2 + st.get("mem_some", 0.0),
        "cpu": st.get("cpu_some", 0.0),
    }


def _is_hot(s: dict) -> bool:
    p = _pressure(s)
    return any(p[k] >= THRESHOLDS[k] for k in THRESHOLDS)


@dataclass
class Culprit:
    app: str
    detail: str
    weight: float
    devices: dict[str, float] = field(default_factory=dict)  # disk -> MB of its files there
    cores: float = 0.0
    rss_mb: float = 0.0
    known: "offenders.Entry | None" = None   # knowledge-base entry explaining what this process is


@dataclass
class Victim:
    """An app whose *own* tasks were stalled (kernel per-cgroup PSI) — distinct from a culprit."""
    app: str
    game: str | None = None
    waits: dict[str, float] = field(default_factory=dict)   # io | mem | cpu -> % of time stalled
    devices: dict[str, float] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.app} (game: {self.game})" if self.game else self.app

    @property
    def summary(self) -> str:
        words = {"io": "waiting on the disk", "mem": "waiting for memory", "cpu": "waiting for a CPU core"}
        top = sorted(self.waits.items(), key=lambda kv: -kv[1] / VICTIM_WAIT[kv[0]])[:2]
        return " and ".join(f"stalled {words[k]} {v:.0f}% of the time" if i == 0 else f"{words[k]} {v:.0f}%"
                            for i, (k, v) in enumerate(top))


@dataclass
class Episode:
    start: float
    end: float
    cause: str                      # dominant metric: io | mem | cpu
    peak: float
    severity: float
    kind: str = ""                  # io | pagein | ram | cpu
    culprits: list[Culprit] = field(default_factory=list)
    secondary: list[tuple[str, list[Culprit]]] = field(default_factory=list)
    factors: list[str] = field(default_factory=list)
    headline: str = ""
    disks: dict[str, float] = field(default_factory=dict)   # peak busy% per disk during the incident
    main_disk: str | None = None    # busy disk that a culprit demonstrably uses
    suggestions: list[str] = field(default_factory=list)
    victims: list[Victim] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(1.0, self.end - self.start + 1)


# --------------------------------------------------------------------------
def find_episodes(samples: list[dict]) -> list[list[dict]]:
    """Group stalled ticks into incidents; stalls within MAX_GAP seconds are one incident.

    Calm ticks are held back and only committed if another stalled tick follows,
    so an incident never ends on a quiet sample.
    """
    groups: list[list[dict]] = []
    cur: list[dict] = []
    pending: list[dict] = []  # calm ticks seen since the last stalled one
    last_hot = 0.0
    for s in samples:
        if _is_hot(s):
            if cur and s["t"] - last_hot > MAX_GAP + 1:
                groups.append(cur)
                cur = []
            elif cur:
                cur.extend(pending)
            pending = []
            cur.append(s)
            last_hot = s["t"]
        elif cur:
            pending.append(s)
    if cur:
        groups.append(cur)
    return groups


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _app_totals(window: list[dict]) -> dict[str, dict]:
    tot: dict[str, dict] = {}
    n = len(window)
    for s in window:
        for app, e in s["apps"].items():
            t = tot.setdefault(app, {"cpu": 0.0, "io": 0.0, "majflt": 0, "dstate": 0, "dmax": 0,
                                     "rss_first": e["rss"], "rss_last": e["rss"], "devs": {}})
            t["cpu"] += e["cpu"] / n
            t["io"] += (e["rd"] + e["wr"]) / n
            t["majflt"] += e["majflt"]
            t["dstate"] += e["dstate"]
            t["dmax"] = max(t["dmax"], e["dstate"])
            t["rss_last"] = e["rss"]
            for d, mb in e.get("devs", {}).items():
                t["devs"][d] = max(t["devs"].get(d, 0), mb)
    return tot


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 'es' if word.endswith('s') else 's'}"


def _culprits(cause: str, window: list[dict]) -> list[Culprit]:
    """Apps most responsible for `cause`, judged only over ticks where that cause was stalled."""
    cw = [s for s in window if _pressure(s)[cause] >= THRESHOLDS[cause]] or window
    tot = _app_totals(cw)
    out: list[Culprit] = []
    for app, t in tot.items():
        c = None
        if cause == "io":
            if t["io"] > 256 * 1024 or t["dstate"] > 0:
                bits = []
                if t["io"] > 256 * 1024:
                    bits.append(f"{_fmt_bytes(t['io'])}/s disk I/O")
                if t["dstate"]:
                    bits.append(f"up to {_plural(t['dmax'], 'process')} stuck waiting on disk")
                c = Culprit(app, ", ".join(bits), t["io"] + t["dstate"] * 2e6)
        elif cause == "mem":
            grow = (t["rss_last"] - t["rss_first"]) * 1024
            bits = []
            if grow > 100 * 1024 * 1024:
                bits.append(f"grew {_fmt_bytes(grow)}")
            if t["majflt"] > 50:
                bits.append(f"{t['majflt']} major page faults")
            if bits:
                bits.append(f"now using {_fmt_bytes(t['rss_last'] * 1024)}")
                c = Culprit(app, ", ".join(bits), max(grow, 0) + t["majflt"] * 1e5)
        elif t["cpu"] >= 25:
            c = Culprit(app, f"~{t['cpu'] / 100:.1f} cores busy on average", t["cpu"])
        if c:
            c.devices = dict(t["devs"])
            c.cores = t["cpu"] / 100
            c.rss_mb = t["rss_last"] / 1024
            c.known = offenders.lookup(app)
            if c.known and c.known.disk:        # e.g. kernel writeback thread: it works for that disk
                c.devices.setdefault(c.known.disk, 1)
            out.append(c)
    out.sort(key=lambda c: c.weight, reverse=True)
    return out[:4]


def _victims(window: list[dict]) -> list[Victim]:
    """Apps whose own tasks spent a notable share of the incident stalled; games listed first."""
    tot: dict[str, dict] = {}
    for s in window:
        for app, e in s["apps"].items():
            psi = e.get("psi")
            if not psi:
                continue
            t = tot.setdefault(app, {"n": 0, "io": 0.0, "mem": 0.0, "cpu": 0.0, "cores": 0.0,
                                     "game": None, "devs": {}})
            t["n"] += 1
            t["io"] += psi.get("io", 0.0); t["mem"] += psi.get("mem", 0.0); t["cpu"] += psi.get("cpu", 0.0)
            t["cores"] += e.get("cpu", 0.0) / 100
            t["game"] = t["game"] or e.get("game")
            for d, mb in e.get("devs", {}).items():
                t["devs"][d] = max(t["devs"].get(d, 0), mb)
    out = []
    for app, t in tot.items():
        n = t["n"]
        waits = {k: t[k] / n for k in ("io", "mem", "cpu")}
        if t["cores"] / n >= SELF_INFLICTED_CORES:
            waits["cpu"] = 0.0            # a CPU hog starving itself is not a victim of anyone else
        waits = {k: v for k, v in waits.items() if v >= VICTIM_WAIT[k]}
        if waits:
            out.append(Victim(app, t["game"], waits, t["devs"]))
    out.sort(key=lambda v: (v.game is None, -max(w / VICTIM_WAIT[k] for k, w in v.waits.items())))
    return out[:3]


def _busy_disks(window: list[dict]) -> dict[str, float]:
    peak: dict[str, float] = {}
    for s in window:
        for d, v in s["disks"].items():
            peak[d] = max(peak.get(d, 0.0), v["busy"])
    return peak


def _attach_disks(culprits: list[Culprit], peaks: dict[str, float]) -> tuple[str | None, str | None]:
    """Tag culprits that use a busy disk. Returns (disk, top app using it) — evidence, not proof."""
    busy = {d for d, b in peaks.items() if b >= BUSY_DISK}
    main = who = None
    for c in culprits:
        touched = sorted((d for d in c.devices if d in busy), key=lambda d: -peaks[d])
        if touched:
            verb = "is writing to" if c.known and c.known.disk == touched[0] else "has files on"
            c.detail += f"; {verb} {touched[0]}"
            if main is None:
                main, who = touched[0], c.app
    return main, who


def _factors(window: list[dict]) -> list[str]:
    notes = []
    # CPU frequency collapse while busy -> thermal / power throttling
    busy = [s for s in window if s["cpu_util"] > 40 and s["max_mhz"]]
    if busy:
        ratio = sum(s["mhz"] / s["max_mhz"] for s in busy) / len(busy)
        temps = [s["temp"] for s in busy if s["temp"]]
        if ratio < 0.65:
            hot = f", CPU at {max(temps):.0f}°C" if temps else ""
            notes.append(f"CPU was running at only {ratio:.0%} of its max clock while busy{hot} "
                         "(likely thermal or power throttling)")
    gpu = [s["gpu"] for s in window if s.get("gpu")]
    if any(g["hw_thermal"] or g["sw_thermal"] for g in gpu):
        notes.append(f"GPU was thermally throttled (up to {max(g['temp'] for g in gpu):.0f}°C)")
    elif any(g["power_cap"] and g["util"] > 90 for g in gpu):
        notes.append("GPU was pinned at its power cap")
    if gpu and max(g["mem_used"] / max(g["mem_total"], 1) for g in gpu) > 0.95:
        notes.append("GPU memory was nearly full")
    swp = sum(s["vm"].get("pswpout", 0) + s["vm"].get("pswpin", 0) for s in window)
    if swp > 2000:
        notes.append(f"system was swapping ({swp * 4 // 1024} MB moved to/from swap)")
    kills = sum(s["vm"].get("oom_kill", 0) for s in window)
    if kills:
        notes.append(f"the kernel killed {kills} process{'es' if kills != 1 else ''} to free memory (OOM killer)")
    stalls = sum(s["vm"].get("allocstall_normal", 0) + s["vm"].get("allocstall_movable", 0) for s in window)
    if stalls > 50:
        notes.append("apps were forced into direct memory reclaim (allocation stalls)")
    peaks = _busy_disks(window)
    if peaks:
        d, b = max(peaks.items(), key=lambda kv: kv[1])
        if b > 90:
            notes.append(f"disk {d} was {b:.0f}% busy (saturated)")
    return notes


_HEADLINE = {
    "io": "Disk I/O stall: programs were waiting on the disk",
    "mem": "Memory pressure: the system ran short of RAM",
    "cpu": "CPU contention: more work was runnable than cores could run",
}


def _headline(cause: str, window: list[dict], peaks: dict[str, float],
              main: str | None, who: str | None) -> tuple[str, str]:
    """(kind, headline). Names a disk as the source only when a culprit demonstrably uses it."""
    if cause == "cpu":
        return "cpu", _HEADLINE["cpu"]
    if cause == "io":
        if main:
            return "io", f"Disk I/O stall on {devices.label(main)}: programs were waiting for it"
        return "io", _HEADLINE["io"]
    fracs = [s["mem"]["MemAvailable"] / s["mem"]["MemTotal"] for s in window if s["mem"].get("MemTotal")]
    if not fracs or min(fracs) < LOW_RAM_FRACTION:
        return "ram", _HEADLINE["mem"]
    # PSI "memory" also counts waiting on page-ins from disk, which isn't a RAM shortage.
    avail = min(s["mem"]["MemAvailable"] for s in window) / 1048576
    text = ("Page-in stall: programs were waiting for memory-mapped data to load "
            f"(RAM is not exhausted: {avail:.1f} GB still available)")
    hot = [d for d, b in peaks.items() if b >= BUSY_DISK]
    if main:
        text += f"; the busy disk is {devices.label(main)}, which {who} is using"
    elif hot:
        d = max(hot, key=peaks.get)
        text += f"; disk {d} was saturated but none of the culprits use it, so it is probably unrelated"
    return "pagein", text


def analyze(samples: list[dict], min_severity: float = 2.0) -> list[Episode]:
    """Return incidents, worst first."""
    from . import advice  # local import: advice depends on Episode

    episodes = []
    for window in find_episodes(samples):
        sums = {k: 0.0 for k in THRESHOLDS}
        peaks = {k: 0.0 for k in THRESHOLDS}
        for s in window:
            for k, v in _pressure(s).items():
                peaks[k] = max(peaks[k], v)
                if v >= THRESHOLDS[k]:       # calm baseline between stalls must not add weight
                    sums[k] += v / THRESHOLDS[k]
        # I/O and memory stalls hurt interactivity more than CPU share, so favour them.
        weighted = {"io": sums["io"] * 1.2, "mem": sums["mem"] * 1.5, "cpu": sums["cpu"] * 0.6}
        cause = max(weighted, key=weighted.get)
        if weighted[cause] < min_severity:
            continue
        others = [k for k in weighted
                  if k != cause and weighted[k] >= max(min_severity, SECONDARY_RATIO * weighted[cause])]
        severity = weighted[cause] + 0.5 * sum(weighted[k] for k in others)

        ep = Episode(window[0]["t"], window[-1]["t"], cause, peaks[cause], severity)
        ep.disks = _busy_disks(window)
        ep.culprits = _culprits(cause, window)
        main, who = _attach_disks(ep.culprits, ep.disks)
        ep.main_disk = main
        for k in sorted(others, key=weighted.get, reverse=True):
            sec = _culprits(k, window)
            _attach_disks(sec, ep.disks)
            ep.secondary.append((k, sec))
        ep.victims = _victims(window)
        ep.kind, ep.headline = _headline(cause, window, ep.disks, main, who)
        ep.factors = _factors(window)
        try:
            ep.suggestions = advice.suggest(ep)
        except Exception:      # advice is a nicety; never let it break a diagnosis
            ep.suggestions = []
        episodes.append(ep)
    episodes.sort(key=lambda e: e.severity, reverse=True)
    return episodes


def snapshot_summary(samples: list[dict]) -> dict:
    """Current-state summary used when nothing stalled."""
    last = samples[-1]
    return {
        "cpu_util": last["cpu_util"], "load": last["load"],
        "mem_avail_gb": last["mem"].get("MemAvailable", 0) / 1048576,
        "pressure": _pressure(last),
    }


CAUSE_TITLE = {"io": "Disk I/O", "mem": "Memory", "cpu": "CPU contention"}


def describe(ep: Episode) -> list[str]:
    """Headline, culprits, secondary causes, factors and suggestions as plain lines (CLI + GUI)."""
    lines = [ep.headline]
    if ep.culprits:
        lines.append("Likely culprits:")
        for c in ep.culprits:
            lines.append(f"  • {c.app}: {c.detail}")
            if c.known:
                lines.append(f"      ↳ {c.known.blurb()}")
    else:
        lines.append("No single app stood out; load was spread across many processes.")
    if ep.victims:
        lines.append("Hit hardest (their own tasks were stalled):")
        lines += [f"  • {v.label}: {v.summary}" for v in ep.victims]
    for cause, culprits in ep.secondary:
        lines.append(f"Also: {CAUSE_TITLE[cause]}" + (" — " + ", ".join(c.app for c in culprits[:3]) if culprits else ""))
    lines += [f"⚠ {f}" for f in ep.factors]
    if ep.suggestions:
        lines.append("What you can do:")
        lines += [f"  → {s}" for s in ep.suggestions]
    return lines


def render(episodes: list[Episode], samples: list[dict], limit: int = 3) -> str:
    span = samples[-1]["t"] - samples[0]["t"] if len(samples) > 1 else 0
    lines = []
    if not episodes:
        s = snapshot_summary(samples)
        lines.append(f"No slowdown detected in the last {span:.0f}s. Your system looks healthy.")
        lines.append(f"  CPU {s['cpu_util']:.0f}% busy · {s['mem_avail_gb']:.1f} GB RAM available · "
                     f"load {s['load']:.1f}")
        return "\n".join(lines)

    lines.append(f"Found {len(episodes)} incident(s) in the last {span:.0f}s "
                 f"(worst first, showing {min(limit, len(episodes))}):")
    for i, ep in enumerate(episodes[:limit], 1):
        when = time.strftime("%H:%M:%S", time.localtime(ep.start))
        ago = time.time() - ep.end
        lines.append("")
        lines.append(f"#{i}  {when}  ({ep.duration:.0f}s, {ago:.0f}s ago, peak stall {ep.peak:.0f}%)")
        lines.extend("    " + l for l in describe(ep))
    return "\n".join(lines)
