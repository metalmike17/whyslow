"""Laptop battery culprit: which app is draining the battery, with the evidence.

Power per process is not measurable on Linux, so we do not pretend to. Instead we use what the
battery itself reports (watts drawn, every second) and compare it with what was running:
"while Firefox was busy the battery drew 6.1 W more than while it was not". That is a measured
difference, not a model, though it can still be confounded (two apps busy together), so apps are only
named when they were busy for long enough in both states, and the report says it is a correlation.
"""
from __future__ import annotations

import glob
import os
import re
import statistics
from dataclasses import dataclass, field

PS = "/sys/class/power_supply"
BUSY = 10.0              # app cpu% (of one core) counted as "busy"
IDLE = 2.0
MIN_STATE_SAMPLES = 30   # seconds needed in both busy and quiet states before blaming an app
MIN_DELTA_W = 1.0        # smaller differences are noise
MIN_DISCHARGE_S = 120
BRIGHT_HIGH = 0.8


def _r(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _n(path: str) -> float | None:
    s = _r(path)
    try:
        return float(s)
    except ValueError:
        return None


def system_battery(root: str = PS) -> str | None:
    """Directory of the laptop's own battery (not a mouse/keyboard/phone battery), or None."""
    for d in sorted(glob.glob(f"{root}/*")):
        if _r(f"{d}/type") == "Battery" and _r(f"{d}/scope") != "Device":
            return d
    return None


def read(root: str = PS) -> dict | None:
    """Instantaneous reading: {w, pct, st} (st: D discharging, C charging, F full, ?) or None."""
    d = system_battery(root)
    if not d:
        return None
    w = _n(f"{d}/power_now")
    if w is not None:
        w /= 1e6
    else:
        i, v = _n(f"{d}/current_now"), _n(f"{d}/voltage_now")
        w = abs(i) * v / 1e12 if i is not None and v else None
    pct = _n(f"{d}/capacity")
    st = {"Discharging": "D", "Charging": "C", "Full": "F", "Not charging": "F"}.get(_r(f"{d}/status"), "?")
    if w is None or pct is None:
        return None
    return {"w": round(w, 2), "pct": pct, "st": st}


def info(root: str = PS) -> dict | None:
    """Static facts: full capacity (Wh), health vs design, cycles."""
    d = system_battery(root)
    if not d:
        return None
    volt = (_n(f"{d}/voltage_min_design") or _n(f"{d}/voltage_now") or 0) / 1e6
    def wh(name_e, name_c):
        e = _n(f"{d}/{name_e}")
        if e is not None:
            return e / 1e6
        c = _n(f"{d}/{name_c}")
        return c * volt / 1e6 if c is not None and volt else None
    full, design = wh("energy_full", "charge_full"), wh("energy_full_design", "charge_full_design")
    return {"full_wh": full, "design_wh": design,
            "health": (full / design if full and design else None),
            "cycles": int(_n(f"{d}/cycle_count") or 0) or None}


def context(root: str = "/sys", run=None) -> dict:
    """Settings that change battery life, readable without root."""
    out = {}
    for d in glob.glob(f"{root}/class/backlight/*"):
        mx, cur = _n(f"{d}/max_brightness"), _n(f"{d}/brightness")
        if mx and cur is not None:
            out["brightness"] = cur / mx
            break
    out["dgpu_awake"] = False
    for d in glob.glob(f"{root}/bus/pci/devices/*"):
        # a display-class device that is not the boot (integrated) GPU and is powered on: NVIDIA, AMD or Intel Arc
        if (_r(f"{d}/vendor") in ("0x10de", "0x1002", "0x8086") and _r(f"{d}/class").startswith("0x03")
                and _r(f"{d}/boot_vga") != "1" and _r(f"{d}/power/runtime_status") == "active"):
            out["dgpu_awake"] = True
    return out


@dataclass
class Culprit:
    app: str
    delta_w: float          # extra watts while busy vs quiet
    busy_pct: float         # share of the discharge period it was busy
    cost_wh: float          # estimated energy it cost = delta * busy time
    busy_s: int
    quiet_s: int
    known: str = ""


@dataclass
class Report:
    seconds: int = 0
    avg_w: float = 0.0
    idle_w: float = 0.0
    start_pct: float = 0.0
    end_pct: float = 0.0
    hours_left: float | None = None
    hours_at_idle: float | None = None
    culprits: list[Culprit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: str = ""


def _pctile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def analyze(samples: list[dict], bat_info: dict | None = None, ctx: dict | None = None, lookup=None) -> Report:
    r = Report()
    d = [s for s in samples if s.get("bat") and s["bat"]["st"] == "D"]
    if not any(s.get("bat") for s in samples):
        r.status = "no-battery"
        return r
    if len(d) < MIN_DISCHARGE_S:
        r.status = "not-discharging" if not d else "too-short"
        r.seconds = len(d)
        return r
    r.status = "ok"
    r.seconds = len(d)
    w = [s["bat"]["w"] for s in d]
    r.avg_w, r.idle_w = statistics.fmean(w), _pctile(w, 0.1)
    r.start_pct, r.end_pct = d[0]["bat"]["pct"], d[-1]["bat"]["pct"]
    if bat_info and bat_info.get("full_wh") and r.avg_w > 0.5:
        left_wh = bat_info["full_wh"] * r.end_pct / 100
        r.hours_left = left_wh / r.avg_w
        if r.idle_w > 0.5:
            r.hours_at_idle = left_wh / r.idle_w
    apps = {a for s in d for a in s.get("apps", {})}
    for a in apps:
        busy = [s["bat"]["w"] for s in d if s.get("apps", {}).get(a, {}).get("cpu", 0) >= BUSY]
        quiet = [s["bat"]["w"] for s in d if s.get("apps", {}).get(a, {}).get("cpu", 0) < IDLE]
        if len(busy) < MIN_STATE_SAMPLES or len(quiet) < MIN_STATE_SAMPLES:
            continue
        delta = statistics.median(busy) - statistics.median(quiet)
        if delta < MIN_DELTA_W:
            continue
        k = ""
        if lookup:
            e = lookup(a)
            k = e.blurb() if e else ""
        r.culprits.append(Culprit(a, delta, 100 * len(busy) / len(d), delta * len(busy) / 3600, len(busy), len(quiet), k))
    r.culprits.sort(key=lambda c: -c.cost_wh)
    r.culprits = r.culprits[:5]
    ctx = ctx or {}
    if ctx.get("dgpu_awake"):
        r.notes.append("The discrete GPU is powered on, which typically costs several watts. Something is keeping it awake "
                       "(`nvidia-smi` lists the programs using an NVIDIA GPU; on hybrid laptops run apps on the integrated GPU unless they need the big one).")
    if ctx.get("brightness", 0) >= BRIGHT_HIGH:
        r.notes.append(f"Screen brightness is {ctx['brightness']:.0%}; the display is often the biggest single drain. Lowering it is the cheapest win.")
    if bat_info and bat_info.get("health") is not None and bat_info["health"] < 0.8:
        r.notes.append(f"The battery has worn to {bat_info['health']:.0%} of its design capacity"
                       + (f" ({bat_info['cycles']} cycles)" if bat_info.get("cycles") else "") + ", so it holds less than when new.")
    return r


def render(r: Report) -> str:
    if r.status == "no-battery":
        return "No laptop battery found (or no battery readings recorded yet). This feature is for laptops."
    if r.status == "not-discharging":
        return "The laptop has been plugged in; there is no battery drain to explain. Unplug it for a few minutes and check again."
    if r.status == "too-short":
        return f"Only {r.seconds}s on battery recorded; need at least {MIN_DISCHARGE_S}s to say anything useful."
    m = r.seconds // 60
    L = [f"On battery for {m} min: {r.start_pct:.0f}% → {r.end_pct:.0f}%, drawing {r.avg_w:.1f} W on average "
         f"(quietest moments {r.idle_w:.1f} W)."]
    if r.hours_left:
        L.append(f"At this rate the remaining charge lasts about {r.hours_left:.1f} h"
                 + (f"; if it sat as quiet as its calmest moments, about {r.hours_at_idle:.1f} h." if r.hours_at_idle and r.hours_at_idle > r.hours_left * 1.15 else "."))
    if r.culprits:
        L += ["", "Apps that cost battery (measured as: watts drawn while the app was busy vs. while it was not):"]
        for c in r.culprits:
            L.append(f"  • {c.app}: +{c.delta_w:.1f} W while busy, busy {c.busy_pct:.0f}% of the time (≈{c.cost_wh * 1000:.0f} mWh)")
            if c.known:
                L.append(f"      {c.known}")
        L.append("  (This is a correlation: if two apps were busy together, the blame can be shared.)")
    else:
        L.append("\nNo single app stands out: the drain looks steady rather than caused by one program.")
    L += [""] + [f"⚠ {n}" for n in r.notes] if r.notes else []
    return "\n".join(L)
