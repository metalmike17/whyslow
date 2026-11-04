"""Collects one-second snapshots of system health from /proc, /sys and cgroups.

No root, no eBPF, no third-party dependencies.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time

from . import battery, games
from .gpu import GpuReader
from .devices import MountMap, proc_devices

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
TOP_APPS = 12


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    s = _read(path)
    try:
        return int(s) if s is not None else None
    except ValueError:
        return None


_SCOPE_RE = re.compile(r"^app-(?:[a-z]+-)?(?P<name>.+?)(?:@[A-Za-z0-9]+)?(?:-\d+)?\.(?:scope|service)$")
_ESC_RE = re.compile(r"\\x([0-9a-fA-F]{2})")
# Helper-process names that say nothing about which app they belong to.
_GENERIC_COMM = {"Browser", "Web Content", "Isolated Web Co", "Privileged Cont", "RDD Process",
                 "Socket Process", "Utility Process", "GPU Process", "Renderer", "WebExtensions",
                 "Isolated Servic", "Web Co", "gpu-process", "zygote", "sandbox"}


def _unescape(name: str) -> str:
    """systemd escapes '-' as \\x2d in unit names."""
    return _ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), name)


CGROUP_ROOT = "/sys/fs/cgroup"


def cgroup_info(pid: int, comm: str) -> tuple[str, str | None]:
    """(app name, that app's *dedicated* cgroup dir or None).

    The dir is only returned when the app owns its own unit (app-*.scope / *.service). A process in a
    shared scope (a terminal, a login session) would make per-cgroup pressure meaningless for the app.
    """
    data = _read(f"/proc/{pid}/cgroup")
    if data:
        path = data.strip().splitlines()[-1].split(":", 2)[-1]
        parts = path.split("/")
        for i in range(len(parts) - 1, -1, -1):
            part = parts[i]
            m = _SCOPE_RE.match(part)
            if m:
                name = _unescape(m.group("name"))
                # org.mozilla.firefox -> firefox ; gnome-terminal etc. untouched
                name = name.rsplit(".", 1)[-1] if name.count(".") >= 2 else name
                return name, CGROUP_ROOT + "/".join(parts[: i + 1])
            if part.endswith(".service") and not part.startswith(("user@", "session-")):
                return _unescape(part[: -len(".service")]), CGROUP_ROOT + "/".join(parts[: i + 1])
    if comm in _GENERIC_COMM:
        try:
            return os.path.basename(os.readlink(f"/proc/{pid}/exe")).removesuffix(" (deleted)"), None
        except OSError:
            pass
    return comm, None


def app_from_cgroup(pid: int, comm: str) -> str:
    """Map a pid to a human-meaningful app name using its systemd cgroup."""
    return cgroup_info(pid, comm)[0]


def read_cgroup_psi(cgdir: str) -> dict[str, int] | None:
    """Cumulative stall microseconds of one cgroup, or None if it has no pressure files."""
    out: dict[str, int] = {}
    for res, key in (("cpu", "cpu"), ("io", "io"), ("memory", "mem")):
        data = _read(f"{cgdir}/{res}.pressure")
        if data is None:
            if res == "cpu":
                return None
            continue
        for line in data.splitlines():
            kind, *fields = line.split()
            for fld in fields:
                if fld.startswith("total="):
                    out[f"{key}_{kind}"] = int(fld[6:])
    return out


# --------------------------------------------------------------------------
# PSI
# --------------------------------------------------------------------------
def read_psi() -> dict[str, int]:
    """Cumulative stall time in microseconds: {'cpu_some':..., 'mem_full':...}."""
    out: dict[str, int] = {}
    for res in ("cpu", "memory", "io"):
        data = _read(f"/proc/pressure/{res}")
        if not data:
            continue
        key = "mem" if res == "memory" else res
        for line in data.splitlines():
            kind, *fields = line.split()
            for fld in fields:
                if fld.startswith("total="):
                    out[f"{key}_{kind}"] = int(fld[6:])
    return out


# --------------------------------------------------------------------------
# system-wide counters
# --------------------------------------------------------------------------
def read_vmstat() -> dict[str, int]:
    wanted = {"pswpin", "pswpout", "pgmajfault", "allocstall_normal",
              "allocstall_movable", "pgscan_direct", "compact_stall", "oom_kill"}
    out = {k: 0 for k in wanted}
    data = _read("/proc/vmstat") or ""
    for line in data.splitlines():
        k, _, v = line.partition(" ")
        if k in wanted:
            out[k] = int(v)
    return out


def read_meminfo() -> dict[str, int]:
    out = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Dirty", "Writeback"):
            out[k] = int(v.split()[0])
    return out


def read_diskstats() -> dict[str, tuple[int, int, int]]:
    """{device: (io_ticks_ms, read_sectors, write_sectors)} for whole disks."""
    out = {}
    for line in (_read("/proc/diskstats") or "").splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        if not os.path.exists(f"/sys/block/{name}") or name.startswith(("loop", "ram", "zram", "dm-")):
            continue
        out[name] = (int(f[12]), int(f[5]), int(f[9]))
    return out


def read_cpu_times() -> tuple[int, int]:
    """(busy_jiffies, total_jiffies) across all cores."""
    line = (_read("/proc/stat") or "cpu 0 0 0 0").splitlines()[0].split()[1:]
    vals = [int(x) for x in line]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals[:8])
    return total - idle, total


def read_cpu_freq() -> tuple[float, float]:
    """(avg current MHz, avg max MHz) over cores that expose cpufreq."""
    cur, mx = [], []
    for d in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"):
        c = _read_int(f"{d}/scaling_cur_freq")
        m = _read_int(f"{d}/cpuinfo_max_freq")
        if c and m:
            cur.append(c / 1000)
            mx.append(m / 1000)
    if not cur:
        return 0.0, 0.0
    return sum(cur) / len(cur), sum(mx) / len(mx)


_TEMP_FILES: list[str] | None = None


def read_cpu_temp() -> float | None:
    global _TEMP_FILES
    if _TEMP_FILES is None:
        _TEMP_FILES = []
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            if (_read(f"{hw}/name") or "").strip() in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
                _TEMP_FILES += glob.glob(f"{hw}/temp*_input")
    temps = [t / 1000 for t in (_read_int(p) for p in _TEMP_FILES) if t]
    return max(temps) if temps else None


# --------------------------------------------------------------------------
# per-process, aggregated into apps
# --------------------------------------------------------------------------
def read_procs() -> dict[int, dict]:
    procs = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = _read(f"/proc/{pid}/stat")
        if not stat:
            continue
        # comm may contain spaces/parens: split around the last ')'
        lp, rp = stat.find("("), stat.rfind(")")
        comm = stat[lp + 1: rp]
        f = stat[rp + 2:].split()
        if len(f) < 22:
            continue
        info = {
            "comm": comm, "state": f[0],
            "majflt": int(f[9]),
            "ticks": int(f[11]) + int(f[12]),
            "rss_kb": int(f[21]) * PAGE_KB,
            "rd": 0, "wr": 0,
        }
        io = _read(f"/proc/{pid}/io")
        if io:
            for line in io.splitlines():
                if line.startswith("read_bytes:"):
                    info["rd"] = int(line.split()[1])
                elif line.startswith("write_bytes:"):
                    info["wr"] = int(line.split()[1])
        procs[pid] = info
    return procs


class Sampler:
    """Call sample() once a second; returns a JSON-able dict (None on first call)."""

    def __init__(self, gpu_every: int = 3):
        self._bat_ok = battery.system_battery() is not None
        self._prev: dict | None = None
        self._app_cache: dict[tuple[int, str], tuple[str, str | None]] = {}
        self._cgprev: dict[str, tuple[float, dict[str, int]]] = {}
        self._gamecache: dict[int, tuple[float, str | None]] = {}
        self._gpu_every = gpu_every
        self._tick = 0
        self._gpu: dict | None = None
        self._gpureader = GpuReader()
        self._mm = MountMap()
        self._devcache: dict[int, tuple[float, dict[str, int]]] = {}

    def _app(self, pid: int, comm: str) -> str:
        key = (pid, comm)
        if key not in self._app_cache:
            if len(self._app_cache) > 8192:
                self._app_cache.clear()
            self._app_cache[key] = cgroup_info(pid, comm)
        return self._app_cache[key][0]

    def _cg_pct(self, cgdir: str, now: float) -> dict[str, float] | None:
        """How long this app's *own* tasks were stalled over the last interval (percent)."""
        tot = read_cgroup_psi(cgdir)
        if tot is None:
            return None
        prev = self._cgprev.get(cgdir)
        if len(self._cgprev) > 500:
            self._cgprev.clear()
        self._cgprev[cgdir] = (now, tot)
        if not prev:
            return None
        dt = max(now - prev[0], 1e-3)
        pct = {k: round(min(100.0, max(0.0, (v - prev[1].get(k, v)) / (dt * 1e6) * 100)), 1) for k, v in tot.items()}
        return {"cpu": pct.get("cpu_some", 0.0), "io": pct.get("io_some", 0.0), "iof": pct.get("io_full", 0.0),
                "mem": pct.get("mem_some", 0.0), "memf": pct.get("mem_full", 0.0)}

    GAME_TTL = 30.0

    def _game(self, pids: list[tuple[int, int]], now: float) -> str | None:
        """Name of the game this app hosts, judged from its biggest processes' command lines."""
        found = None
        for _, pid in sorted(pids, reverse=True)[: self.DEV_PIDS_PER_APP]:
            hit = self._gamecache.get(pid)
            if hit is None or now - hit[0] > self.GAME_TTL:
                cmd = (_read(f"/proc/{pid}/cmdline") or "").replace("\0", " ")
                hit = (now, games.detect(cmd))
                self._gamecache[pid] = hit
            found = found or hit[1]
        if len(self._gamecache) > 4000:
            self._gamecache.clear()
        return found

    DEV_PIDS_PER_APP = 3     # biggest processes of each app
    DEV_BUDGET = 10          # max /proc/<pid>/maps scans per tick
    DEV_TTL = 20.0

    def _devs(self, pids: list[tuple[int, int]], now: float, budget: list[int]) -> dict[str, int]:
        """Which disks an app's biggest processes have files mapped/open on, in MB."""
        total: dict[str, int] = {}
        for _, pid in sorted(pids, reverse=True)[: self.DEV_PIDS_PER_APP]:
            hit = self._devcache.get(pid)
            if (hit is None or now - hit[0] > self.DEV_TTL) and budget[0] > 0:
                budget[0] -= 1
                hit = (now, proc_devices(pid, self._mm))
                self._devcache[pid] = hit
            if hit:
                for d, b in hit[1].items():
                    total[d] = total.get(d, 0) + b
        top = sorted(total.items(), key=lambda kv: -kv[1])[:3]
        return {d: b // 1048576 for d, b in top if b >= 1048576}

    def sample(self) -> dict | None:
        now = time.time()
        cur = {
            "t": now, "psi": read_psi(), "vm": read_vmstat(),
            "disk": read_diskstats(), "cpu": read_cpu_times(),
            "procs": read_procs(),
        }
        prev, self._prev = self._prev, cur
        self._tick += 1
        if self._tick % self._gpu_every == 1:
            self._gpu = self._gpureader.read()
        if prev is None:
            return None

        dt = max(cur["t"] - prev["t"], 1e-3)
        stall = {k: round(min(100.0, (v - prev["psi"].get(k, v)) / (dt * 1e6) * 100), 1)
                 for k, v in cur["psi"].items()}
        vm = {k: cur["vm"][k] - prev["vm"][k] for k in cur["vm"]}

        disks = {}
        for name, (ticks, rs, ws) in cur["disk"].items():
            p = prev["disk"].get(name)
            if p:
                disks[name] = {
                    "busy": round(min(100.0, (ticks - p[0]) / (dt * 10)), 1),
                    "rd": int((rs - p[1]) * 512 / dt), "wr": int((ws - p[2]) * 512 / dt),
                }

        busy, total = cur["cpu"]
        pbusy, ptotal = prev["cpu"]
        cpu_util = round(100 * (busy - pbusy) / max(total - ptotal, 1), 1)
        mhz, max_mhz = read_cpu_freq()
        mem = read_meminfo()

        apps: dict[str, dict] = {}
        for pid, p in cur["procs"].items():
            a = self._app(pid, p["comm"])
            e = apps.setdefault(a, {"cpu": 0.0, "rd": 0, "wr": 0, "majflt": 0,
                                    "dstate": 0, "rss": 0, "n": 0, "pids": []})
            e["rss"] += p["rss_kb"]
            e["n"] += 1
            e["pids"].append((p["rss_kb"], pid))
            if p["state"] == "D":
                e["dstate"] += 1
            q = prev["procs"].get(pid)
            if q and q["comm"] == p["comm"]:
                e["cpu"] += (p["ticks"] - q["ticks"]) / CLK_TCK / dt * 100
                e["rd"] += max(0, int((p["rd"] - q["rd"]) / dt))
                e["wr"] += max(0, int((p["wr"] - q["wr"]) / dt))
                e["majflt"] += max(0, p["majflt"] - q["majflt"])

        def score(item):
            _, e = item
            return (e["cpu"] / 100 + (e["rd"] + e["wr"]) / 5e7 + e["majflt"] / 200
                    + e["dstate"] * 0.5 + e["rss"] / 4e6 * 0.05)

        top = sorted(apps.items(), key=score, reverse=True)[:TOP_APPS]
        budget = [self.DEV_BUDGET]
        if len(self._devcache) > 2000:
            self._devcache = {k: v for k, v in self._devcache.items() if k in cur["procs"]}
        for _, e in top:
            e["cpu"] = round(e["cpu"], 1)
            pids = e.pop("pids")
            e["devs"] = self._devs(pids, now, budget)
            big = max(pids)[1]
            cg = self._app_cache.get((big, cur["procs"][big]["comm"]), (None, None))[1]
            psi = self._cg_pct(cg, now) if cg else None
            if psi:
                e["psi"] = psi
            g = self._game(pids, now)
            if g:
                e["game"] = g

        out = {
            "t": round(now, 2), "dt": round(dt, 2),
            "stall": stall, "vm": vm, "disks": disks,
            "cpu_util": cpu_util, "mhz": round(mhz), "max_mhz": round(max_mhz),
            "temp": read_cpu_temp(),
            "load": os.getloadavg()[0],
            "mem": mem, "gpu": self._gpu,
            "apps": dict(top),
        }
        if self._bat_ok:
            bat = battery.read()
            if bat:
                out["bat"] = bat
            elif self._tick % 60 == 0:          # laptop-less machines: stop probing after a while
                self._bat_ok = battery.system_battery() is not None
        return out
