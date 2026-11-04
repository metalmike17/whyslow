"""GPU readings for NVIDIA, AMD and Intel, in one schema the rest of WhySlow understands.

Each reading is ``{vendor, util, temp, hw_thermal, sw_thermal, power_cap, mem_used, mem_total}`` (MB, %, °C).
Fields a driver does not expose are reported as 0/False, never invented, so consumers treat "unknown" as "fine".

- NVIDIA: ``nvidia-smi`` (the only supported route without root).
- AMD (amdgpu): sysfs ``gpu_busy_percent``, VRAM counters, hwmon temperature/power. Thermal throttling is *inferred*
  from the temperature being within a few degrees of the card's own critical limit.
- Intel (i915 and xe): no utilisation counter is readable without root, but the time the GPU spends *out of* its
  RC6 sleep state is, so ``util = 100 - rc6 residency %``. Throttle reasons come from the driver's throttle files.
  Integrated GPUs share system RAM, so memory is not reported.

The AMD and Intel paths were written from the kernel documentation and tested against simulated sysfs trees, not
real cards (the author has only NVIDIA hardware). They read only; a missing file just means "unknown".
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time

AMD, INTEL, NVIDIA = "0x1002", "0x8086", "0x10de"
THERMAL_MARGIN_C = 3.0       # AMD: this close to the card's critical temperature counts as thermally limited
POWER_CAP_FRACTION = 0.97    # AMD: average power this close to the cap counts as power-limited

_NVSMI_QUERY = ("utilization.gpu,temperature.gpu,clocks_throttle_reasons.active,"
                "clocks_throttle_reasons.hw_thermal_slowdown,"
                "clocks_throttle_reasons.sw_thermal_slowdown,"
                "clocks_throttle_reasons.sw_power_cap,memory.used,memory.total")


def _r(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _num(path: str) -> float | None:
    s = _r(path)
    try:
        return float(s)
    except ValueError:
        return None


def _blank(vendor: str) -> dict:
    return {"vendor": vendor, "util": 0.0, "temp": 0.0, "hw_thermal": False, "sw_thermal": False,
            "power_cap": False, "mem_used": 0.0, "mem_total": 0.0}


# --------------------------------------------------------------------------
# NVIDIA
# --------------------------------------------------------------------------
def read_nvidia(run=subprocess.run, which=shutil.which) -> dict | None:
    if not which("nvidia-smi"):
        return None
    try:
        out = run(["nvidia-smi", f"--query-gpu={_NVSMI_QUERY}", "--format=csv,noheader,nounits"],
                  capture_output=True, text=True, timeout=2).stdout.strip().splitlines()[0]
        f = [x.strip() for x in out.split(",")]
        return {"vendor": "nvidia", "util": float(f[0]), "temp": float(f[1]),
                "hw_thermal": f[3] == "Active", "sw_thermal": f[4] == "Active",
                "power_cap": f[5] == "Active", "mem_used": float(f[6]), "mem_total": float(f[7])}
    except (subprocess.SubprocessError, IndexError, ValueError, OSError):
        return None


# --------------------------------------------------------------------------
# AMD
# --------------------------------------------------------------------------
def _hwmon(dev: str) -> str | None:
    h = sorted(glob.glob(f"{dev}/hwmon/hwmon*"))
    return h[0] if h else None


def read_amd(dev: str) -> dict | None:
    util = _num(f"{dev}/gpu_busy_percent")
    if util is None:
        return None
    g = _blank("amd")
    g["util"] = util
    g["mem_used"] = (_num(f"{dev}/mem_info_vram_used") or 0) / 1048576
    g["mem_total"] = (_num(f"{dev}/mem_info_vram_total") or 0) / 1048576
    hw = _hwmon(dev)
    if hw:
        hot = False
        temps = []
        for p in glob.glob(f"{hw}/temp*_input"):
            t = _num(p)
            if not t:
                continue
            t /= 1000
            temps.append(t)
            crit = _num(p.replace("_input", "_crit"))
            if crit and t >= crit / 1000 - THERMAL_MARGIN_C:
                hot = True
        g["temp"] = max(temps) if temps else 0.0
        g["hw_thermal"] = hot
        avg = _num(f"{hw}/power1_average") or _num(f"{hw}/power1_input")
        cap = _num(f"{hw}/power1_cap")
        g["power_cap"] = bool(avg and cap and avg >= cap * POWER_CAP_FRACTION)
    return g


# --------------------------------------------------------------------------
# Intel (i915 and xe)
# --------------------------------------------------------------------------
def _intel_files(dev: str, card: str) -> dict:
    """Where this driver keeps its RC6 counter and throttle reasons."""
    i915 = [f"{card}/gt/gt0", card]                        # newer kernels, then the older flat layout
    xe = [f"{dev}/tile0/gt0"]
    for base in i915:
        if os.path.exists(f"{base}/rc6_residency_ms") or os.path.exists(f"{base}/power/rc6_residency_ms"):
            rc6 = f"{base}/rc6_residency_ms" if os.path.exists(f"{base}/rc6_residency_ms") else f"{base}/power/rc6_residency_ms"
            return {"rc6": rc6, "kind": "i915", "base": base}
    for base in xe:
        if os.path.exists(f"{base}/gtidle/idle_residency_ms"):
            return {"rc6": f"{base}/gtidle/idle_residency_ms", "kind": "xe", "base": base}
    return {}


def _intel_throttle(info: dict) -> tuple[bool, bool]:
    """(thermal, power) throttling from the driver's own reason files; unknown files count as not throttled."""
    base = info["base"]
    if info["kind"] == "xe":
        d = f"{base}/freq0/throttle"
        thermal = _r(f"{d}/reason_thermal") == "1"
        power = _r(f"{d}/reason_pl1") == "1" or _r(f"{d}/reason_pl2") == "1"
        return thermal, power
    thermal = _r(f"{base}/throttle_reason_thermal") == "1" or _r(f"{base}/throttle_reason_prochot") == "1"
    power = _r(f"{base}/throttle_reason_pl1") == "1" or _r(f"{base}/throttle_reason_pl2") == "1"
    return thermal, power


class GpuReader:
    """Stateful because Intel utilisation is a difference between two RC6 readings."""

    def __init__(self, sys_root: str = "/sys", nvidia=read_nvidia, now=time.monotonic):
        self.root, self.nvidia, self.now = sys_root, nvidia, now
        self._rc6: dict[str, tuple[float, float]] = {}      # card -> (monotonic time, rc6 ms)
        self._warm = False

    def _cards(self):
        for card in sorted(glob.glob(f"{self.root}/class/drm/card[0-9]*")):
            if "-" in os.path.basename(card):
                continue                                    # connectors (card0-HDMI-A-1)
            dev = f"{card}/device"
            vendor = _r(f"{dev}/vendor")
            if vendor in (AMD, INTEL):
                yield card, dev, vendor

    def _intel(self, card: str, dev: str) -> dict | None:
        info = _intel_files(dev, card)
        if not info:
            return None
        rc6 = _num(info["rc6"])
        if rc6 is None:
            return None
        t = self.now()
        prev = self._rc6.get(card)
        self._rc6[card] = (t, rc6)
        g = _blank("intel")
        g["hw_thermal"], g["power_cap"] = _intel_throttle(info)
        hw = _hwmon(dev)
        if hw:
            temp = _num(f"{hw}/temp1_input")
            g["temp"] = temp / 1000 if temp else 0.0
        if prev and t > prev[0]:
            idle_pct = 100 * (rc6 - prev[1]) / ((t - prev[0]) * 1000)
            g["util"] = round(min(100.0, max(0.0, 100 - idle_pct)), 1)
        else:
            return None                                     # first reading has nothing to compare with
        return g

    def read_all(self) -> list[dict]:
        out = []
        nv = self.nvidia()
        if nv:
            out.append(nv)
        for card, dev, vendor in self._cards():
            g = read_amd(dev) if vendor == AMD else self._intel(card, dev)
            if g:
                out.append(g)
        return out

    def read(self) -> dict | None:
        """The busiest GPU (a game runs on one of them). None when no GPU reports anything."""
        gpus = self.read_all()
        if not gpus:
            return None
        return max(gpus, key=lambda g: (g["util"], g["mem_used"]))
