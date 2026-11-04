"""Hardware gotchas: things that make Linux feel slow with no software to blame.

Design: `collect()` reads the machine (no root needed) into plain snapshots; each `check_*` is a pure
function over a snapshot, so every rule is testable with a fake machine. A check only reports what the
evidence supports and says how sure it is. USB link speed is the tricky one: a USB 3 device on a
USB 2 link *reports itself as USB 2.10*, so the descriptor cannot reveal a slow port; we only flag when
other evidence (UAS driver, "USB3" in the product name) points to a faster-capable device.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import devices

SLOW_USB_MBPS = 480
NVME_HOT_C = 70.0
USB3_HINT = re.compile(r"usb ?3|3\.[012]|super ?speed|gen ?[12]|uas", re.I)
SLOW_USB_DIRTY_MB = 1024      # flag dirty-buffer limits above this when slow USB storage is attached


@dataclass
class Finding:
    id: str
    severity: str                  # warn | info
    title: str
    confidence: str = "medium"     # high | medium | low
    evidence: list[str] = field(default_factory=list)
    fix: list[str] = field(default_factory=list)
    disks: list[str] = field(default_factory=list)   # affected disks, for linking to slowdown reports


@dataclass
class UsbDisk:
    disk: str
    product: str
    mbps: int
    driver: str
    rotational: bool
    has_media: bool
    chain: list[tuple[str, int]] = field(default_factory=list)   # upstream hubs: (name, mbps)
    discard_max: int = 0


@dataclass
class Snapshot:
    usb: list[UsbDisk] = field(default_factory=list)
    nvme_links: list[dict] = field(default_factory=list)        # {name, speed, width, max_speed, max_width}
    sata_ssds: list[dict] = field(default_factory=list)         # {disk, gbps}
    disks: dict[str, dict] = field(default_factory=dict)        # name -> {rotational, discard_max, usb, mounted}
    mounts: list[dict] = field(default_factory=list)            # {mp, disk, fstype, fs, opts, ro}
    cpu: dict = field(default_factory=dict)
    swaps: list[dict] = field(default_factory=list)             # {name, disk, kind}
    mem_total_gb: float = 0.0
    nvme_temps: dict[str, float] = field(default_factory=dict)
    steam_libs: list[dict] = field(default_factory=list)        # {path, disk}
    vm: dict = field(default_factory=dict)
    fstrim_timer: bool | None = None
    root_disk: str | None = None
    laptop: bool = False
    on_ac: bool | None = None


# --------------------------------------------------------------------------
# collection (reads the real machine)
# --------------------------------------------------------------------------
def _r(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _num(s: str) -> float:
    m = re.match(r"\s*([\d.]+)", s or "")
    return float(m.group(1)) if m else 0.0


def _usb_device_dir(block_real: str) -> str | None:
    d = block_real
    while d and d != "/":
        if os.path.exists(f"{d}/speed") and os.path.exists(f"{d}/idVendor"):
            return d
        d = os.path.dirname(d)
    return None


def _collect_usb() -> list[UsbDisk]:
    out = []
    for b in sorted(glob.glob("/sys/block/sd*")):
        name = os.path.basename(b)
        real = os.path.realpath(b)
        if "/usb" not in real:
            continue
        d = _usb_device_dir(real)
        if not d:
            continue
        driver = ""
        for intf in glob.glob(f"{d}/{os.path.basename(d)}:1.*"):
            if os.path.islink(f"{intf}/driver"):
                driver = os.path.basename(os.readlink(f"{intf}/driver"))
                break
        chain, up = [], os.path.dirname(d)
        while up and up != "/":
            if os.path.exists(f"{up}/speed") and os.path.exists(f"{up}/idVendor"):
                chain.append((_r(f"{up}/product") or "hub", int(_num(_r(f"{up}/speed")))))
            up = os.path.dirname(up)
        out.append(UsbDisk(name, _r(f"{d}/product"), int(_num(_r(f"{d}/speed"))), driver,
                           _r(f"{b}/queue/rotational") == "1", int(_num(_r(f"{b}/size"))) > 0, chain,
                           int(_num(_r(f"{b}/queue/discard_max_bytes")))))
    return out


def _collect_mounts() -> list[dict]:
    out = []
    try:
        lines = _r("/proc/self/mountinfo").splitlines()
    except OSError:
        return out
    for line in lines:
        left, _, right = line.partition(" - ")
        lf, rf = left.split(), right.split()
        if len(rf) < 2 or not rf[1].startswith("/dev/"):
            continue
        src = rf[1]
        fs = ""
        try:
            st = os.stat(src)
            fs = next((l[len("E:ID_FS_TYPE="):] for l in _r(f"/run/udev/data/b{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}").splitlines()
                       if l.startswith("E:ID_FS_TYPE=")), "")
        except OSError:
            pass
        opts = lf[5] + "," + (rf[2] if len(rf) > 2 else "")
        out.append({"mp": devices._unoct(lf[4]), "disk": devices.disk_of_source(src), "fstype": rf[0], "fs": fs,
                    "opts": opts, "ro": "ro" in lf[5].split(",")})
    return out


def _steam_libraries(mm: devices.MountMap) -> list[dict]:
    paths = set()
    for vdf in ("~/.steam/steam/steamapps/libraryfolders.vdf", "~/.local/share/Steam/steamapps/libraryfolders.vdf",
                "~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/libraryfolders.vdf"):
        try:
            text = Path(vdf).expanduser().read_text(errors="ignore")
        except OSError:
            continue
        paths.update(re.findall(r'"path"\s+"([^"]+)"', text))
    return [{"path": p, "disk": mm.disk_for_path(p + "/x")} for p in sorted(paths) if os.path.isdir(p)]


def collect() -> Snapshot:
    s = Snapshot()
    mm = devices.MountMap()
    s.usb = _collect_usb()
    for n in sorted(glob.glob("/sys/class/nvme/nvme*")):
        d = f"{n}/device"
        s.nvme_links.append({"name": os.path.basename(n), "speed": _num(_r(f"{d}/current_link_speed")),
                             "width": int(_num(_r(f"{d}/current_link_width"))),
                             "max_speed": _num(_r(f"{d}/max_link_speed")), "max_width": int(_num(_r(f"{d}/max_link_width")))})
    s.mounts = _collect_mounts()
    mounted = {m["disk"] for m in s.mounts}
    for b in glob.glob("/sys/block/*"):
        name = os.path.basename(b)
        if name.startswith(("loop", "ram", "zram", "dm-", "sr", "md")):
            continue
        s.disks[name] = {"rotational": _r(f"{b}/queue/rotational") == "1", "discard_max": int(_num(_r(f"{b}/queue/discard_max_bytes"))),
                         "usb": "/usb" in os.path.realpath(b), "mounted": name in mounted}
        m = re.search(r"ata(\d+)/", os.path.realpath(b))
        if m and not s.disks[name]["rotational"]:
            s.sata_ssds.append({"disk": name, "gbps": _num(_r(f"/sys/class/ata_link/link{m.group(1)}/sata_spd"))})
    cp = "/sys/devices/system/cpu"
    s.cpu = {"driver": _r(f"{cp}/cpu0/cpufreq/scaling_driver"), "governor": _r(f"{cp}/cpu0/cpufreq/scaling_governor"),
             "epp": _r(f"{cp}/cpu0/cpufreq/energy_performance_preference"),
             "no_turbo": _r(f"{cp}/intel_pstate/no_turbo") == "1", "boost_off": _r(f"{cp}/cpufreq/boost") == "0"}
    if shutil.which("powerprofilesctl"):
        try:
            s.cpu["profile"] = subprocess.run(["powerprofilesctl", "get"], capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    s.laptop = bool(glob.glob("/sys/class/power_supply/BAT*"))
    ac = [_r(p) for p in glob.glob("/sys/class/power_supply/A*/online")]
    s.on_ac = (any(x == "1" for x in ac) if ac else None)
    mem = {l.split(":")[0]: int(l.split()[1]) for l in _r("/proc/meminfo").splitlines() if ":" in l}
    s.mem_total_gb = mem.get("MemTotal", 0) / 1048576
    for l in _r("/proc/swaps").splitlines()[1:]:
        f = l.split()
        if f:
            kind = "zram" if "zram" in f[0] else ("file" if f[1] == "file" else "partition")
            disk = None if kind == "zram" else (devices.disk_of_source(f[0]) if f[0].startswith("/dev/") else mm.disk_for_path(f[0]))
            s.swaps.append({"name": f[0], "disk": disk, "kind": kind})
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        if _r(f"{h}/name") == "nvme":
            m = re.search(r"nvme\d+", os.path.realpath(h))
            t = _num(_r(f"{h}/temp1_input")) / 1000
            if m and t:
                s.nvme_temps[m.group(0)] = t
    s.steam_libs = _steam_libraries(mm)
    s.vm = {k: int(_num(_r(f"/proc/sys/vm/{k}"))) for k in ("dirty_ratio", "dirty_background_ratio", "dirty_bytes",
                                                             "dirty_background_bytes", "swappiness")}
    try:
        s.fstrim_timer = subprocess.run(["systemctl", "is-enabled", "--quiet", "fstrim.timer"], timeout=4).returncode == 0
    except (OSError, subprocess.SubprocessError):
        s.fstrim_timer = None
    root = next((m for m in s.mounts if m["mp"] == "/"), None)
    s.root_disk = root["disk"] if root else None
    return s


# --------------------------------------------------------------------------
# checks (pure): each returns (findings, ok_notes)
# --------------------------------------------------------------------------
def check_usb_speed(s: Snapshot):
    f, ok = [], []
    for u in s.usb:
        if not u.has_media:
            continue                                   # empty card reader: nothing to be slow
        if u.mbps > SLOW_USB_MBPS:
            ok.append(f"{u.disk} ({u.product}) is on a USB 3 link ({u.mbps} Mbps)")
            continue
        hints = []
        if u.driver == "uas":
            hints.append("it uses the UAS driver, which is mostly found on USB 3 enclosures (it can run over USB 2, but that is unusual)")
        if USB3_HINT.search(u.product):
            hints.append(f"its name ('{u.product}') says USB 3")
        if not hints:
            ok.append(f"{u.disk} ({u.product}) is a USB 2 device on a USB 2 link — nothing to fix")
            continue
        via = [f"{n} ({m} Mbps)" for n, m in u.chain if m <= SLOW_USB_MBPS and not re.search(r"root hub|host controller", n, re.I)]
        ev = [f"connected at {u.mbps} Mbps (about 35 MB/s in practice; USB 3 would allow ~400 MB/s)"] + [h[0].upper() + h[1:] for h in hints]
        if via:
            ev.append(f"it sits behind a USB 2 hub: {', '.join(via)}")
        f.append(Finding("usb-slow-link", "warn", f"{u.disk} ({u.product}) is probably on a USB 2 port or cable", "medium", ev,
                         ["Plug it into a USB 3 port (usually blue, or marked SS) with a USB 3 cable",
                          "Avoid USB 2 hubs and front-panel headers that are wired as USB 2"], [u.disk]))
    return f, ok


def check_pcie_links(s: Snapshot):
    f, ok = [], []
    for n in s.nvme_links:
        if not n["max_width"]:
            continue
        label = f"{n['name']} at x{n['width']} Gen{_gen(n['speed'])}"
        if n["width"] < n["max_width"]:
            f.append(Finding("nvme-narrow-link", "warn", f"{n['name']} is running on fewer PCIe lanes than it supports", "high",
                             [f"link width is x{n['width']} but the drive supports x{n['max_width']}",
                              f"that can cut sequential speed to about {n['width'] / n['max_width']:.0%}"],
                             ["Check the motherboard manual: some M.2 slots share lanes with SATA ports or other M.2 slots",
                              "Try another M.2 slot, and reseat the drive"], [n["name"].replace("nvme", "nvme") + "n1"]))
        elif n["speed"] and n["speed"] < n["max_speed"]:
            f.append(Finding("nvme-slow-link", "info", f"{n['name']} is linked at a lower PCIe speed than it supports", "low",
                             [f"{n['speed']:g} GT/s now vs {n['max_speed']:g} GT/s supported",
                              "this can be normal idle power-saving; it matters only if it stays low under load"],
                             ["Re-check while the drive is busy (e.g. during a large copy)"], []))
        else:
            ok.append(f"{label} (full speed)")
    return f, ok


def _gen(gts: float) -> int:
    return {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5, 64.0: 6}.get(gts, 0)


def check_sata_ssd(s: Snapshot):
    f, ok = [], []
    for d in s.sata_ssds:
        if d["gbps"] and d["gbps"] < 6.0:
            f.append(Finding("sata-ssd-slow-link", "warn", f"SATA SSD {d['disk']} is linked at {d['gbps']:g} Gbps instead of 6", "medium",
                             [f"{d['gbps']:g} Gbps caps it at about {d['gbps'] * 80:.0f} MB/s"],
                             ["Replace the SATA cable (a marginal cable makes the kernel step the link down)",
                              "Use a SATA 6 Gbps port on the motherboard"], [d["disk"]]))
        elif d["gbps"]:
            ok.append(f"SATA SSD {d['disk']} at {d['gbps']:g} Gbps")
    return f, ok


def check_trim(s: Snapshot):
    f, ok = [], []
    has_discard_mount = lambda disk: any(m["disk"] == disk and "discard" in m["opts"] for m in s.mounts)
    internal_ssds = [d for d, i in s.disks.items() if not i["rotational"] and i["discard_max"] > 0 and i["mounted"]]
    if internal_ssds:
        uncovered = [d for d in internal_ssds if not has_discard_mount(d)]
        if s.fstrim_timer or not uncovered:
            ok.append("TRIM is scheduled (fstrim.timer) or enabled at mount" if s.fstrim_timer else "TRIM enabled at mount")
        elif s.fstrim_timer is False:
            f.append(Finding("no-trim", "warn", "SSDs are never TRIMmed", "medium",
                             [f"{', '.join(uncovered)} accept TRIM but fstrim.timer is disabled and they are not mounted with 'discard'"],
                             ["Enable weekly TRIM: `sudo systemctl enable --now fstrim.timer`"], uncovered))
    for d, i in s.disks.items():
        if i["usb"] and not i["rotational"] and i["discard_max"] == 0 and i["mounted"]:
            f.append(Finding("usb-ssd-no-trim", "info", f"{d} is a USB SSD that cannot receive TRIM", "low",
                             ["its USB enclosure does not pass TRIM through, so the SSD cannot be told which blocks are free",
                              "over a long time its write speed can degrade"],
                             ["Some enclosures can be enabled (udev rule setting provisioning_mode); many cannot — "
                              "if it is your game or work drive, an occasional full-disk TRIM from another connection helps"], [d]))
    return f, ok


def check_cpu_power(s: Snapshot):
    c, f, ok = s.cpu, [], []
    if not c or not c.get("governor"):
        return f, ok
    probs = []
    if c["driver"] in ("acpi-cpufreq", "cpufreq-dt", "") and c["governor"] == "powersave":
        probs.append(f"governor is 'powersave' with the {c['driver'] or 'legacy'} driver, which pins the CPU near its lowest clock")
    if c.get("no_turbo") or c.get("boost_off"):
        probs.append("turbo/boost is disabled, so the CPU cannot exceed its base clock")
    if c.get("epp") == "power" and not (s.laptop and s.on_ac is False):
        probs.append("energy preference is 'power' while not on battery")
    if c.get("profile") == "power-saver" and not (s.laptop and s.on_ac is False):
        probs.append("the system power profile is 'power-saver' while not on battery")
    if probs:
        f.append(Finding("cpu-power-limited", "warn", "The CPU is configured to run slowly", "high", probs,
                         ["Set the profile: `powerprofilesctl set balanced` (or `performance`)",
                          "Check BIOS: turbo/boost should be enabled"], []))
    else:
        ok.append(f"CPU power settings look normal ({c['driver']}/{c['governor']}" + (f"/{c['profile']}" if c.get("profile") else "") + ")")
    return f, ok


def check_swap(s: Snapshot):
    f, ok = [], []
    if not s.swaps:
        if s.mem_total_gb and s.mem_total_gb < 32:
            f.append(Finding("no-swap", "warn", "No swap or zram is configured", "medium",
                             [f"{s.mem_total_gb:.0f} GB of RAM and nothing to fall back on",
                              "when memory runs out the system can freeze instead of slowing down"],
                             ["Enable zram (compressed RAM swap): install `zram-generator`, which most distros package"], []))
        return f, ok
    on_hdd = [w for w in s.swaps if w["disk"] and s.disks.get(w["disk"], {}).get("rotational")]
    if on_hdd:
        f.append(Finding("swap-on-hdd", "warn", "Swap lives on a spinning disk", "medium",
                         [f"{w['name']} is on {w['disk']} (hard disk): swapping there is extremely slow" for w in on_hdd],
                         ["Move swap to an SSD, or use zram"], [w["disk"] for w in on_hdd]))
    else:
        kinds = ", ".join(sorted({w["kind"] for w in s.swaps}))
        ok.append(f"swap is configured ({kinds}) and not on a spinning disk")
    return f, ok


def check_ntfs(s: Snapshot):
    f, ok = [], []
    fuse = [m for m in s.mounts if m["fstype"] == "fuseblk" and m["fs"] == "ntfs" and not m["ro"]]
    if fuse:
        f.append(Finding("ntfs-fuse", "info", "NTFS drives are using the slower user-space driver", "high",
                         [f"{m['mp']} ({m['disk']}) is mounted through FUSE (ntfs-3g)" for m in fuse][:4]
                         + ["ntfs-3g routes every read and write through user space: slow and CPU-hungry for big files and games"],
                         ["Linux 5.15+ has the in-kernel driver: remount with `-t ntfs3` (turn off Windows Fast Startup/hibernation first)",
                          "Better still, keep Linux game libraries on a Linux filesystem (ext4/btrfs/xfs)"], sorted({m["disk"] for m in fuse})))
    elif any(m["fs"] == "ntfs" or m["fstype"] in ("ntfs3",) for m in s.mounts):
        ok.append("NTFS drives use the in-kernel driver")
    return f, ok


def check_root_and_games(s: Snapshot):
    f, ok = [], []
    if s.root_disk and s.disks.get(s.root_disk, {}).get("rotational"):
        f.append(Finding("root-on-hdd", "warn", "The system is installed on a spinning disk", "high",
                         [f"/ is on {s.root_disk}, a hard disk: boot, app launches and updates all wait on it"],
                         ["An SSD is the single biggest speed-up available for this machine"], [s.root_disk]))
    elif s.root_disk:
        ok.append(f"the system disk ({s.root_disk}) is not a spinning disk")
    slow = [l for l in s.steam_libs if l["disk"] and s.disks.get(l["disk"], {}).get("rotational")]
    if slow:
        f.append(Finding("steam-on-hdd", "info", "A Steam library is on a spinning disk", "high",
                         [f"{l['path']} is on {l['disk']} (hard disk)" for l in slow]
                         + ["load times and texture streaming stutter are worse from a hard disk"],
                         ["Move your most-played games to an SSD library (Steam → Settings → Storage)"], sorted({l["disk"] for l in slow})))
    return f, ok


def check_nvme_temp(s: Snapshot):
    f, ok = [], []
    hot = {n: t for n, t in s.nvme_temps.items() if t >= NVME_HOT_C}
    for n, t in hot.items():
        f.append(Finding("nvme-hot", "warn", f"{n} is running hot ({t:.0f}°C)", "medium",
                         [f"NVMe drives typically slow themselves down (throttle) around 75–80°C"],
                         ["Add or reseat the M.2 heatsink, and check case airflow"], [n + "n1"]))
    if s.nvme_temps and not hot:
        ok.append("NVMe temperatures are fine (" + ", ".join(f"{n} {t:.0f}°C" for n, t in sorted(s.nvme_temps.items())) + ")")
    return f, ok


def check_dirty(s: Snapshot):
    """Large write-back buffers + slow removable storage = long freezes when copying (commonly reported)."""
    f, ok = [], []
    slow = [u for u in s.usb if u.has_media and (u.mbps <= SLOW_USB_MBPS or u.rotational)]
    if not slow or not s.vm or s.vm.get("dirty_bytes"):
        return f, ok
    limit_gb = s.mem_total_gb * s.vm.get("dirty_ratio", 20) / 100
    if limit_gb * 1024 >= SLOW_USB_DIRTY_MB:
        names = ", ".join(f"{u.disk} ({u.product or 'USB'}{', ' + str(u.mbps) + ' Mbps' if u.mbps <= SLOW_USB_MBPS else ', hard disk'})" for u in slow)
        f.append(Finding("dirty-buffers", "info", "Big copies to slow USB drives can stall the desktop", "low",
                         [f"the kernel may buffer up to ~{limit_gb:.1f} GB of writes (vm.dirty_ratio={s.vm['dirty_ratio']}%) "
                          "before it forces programs to wait",
                          f"slow USB storage attached: {names}",
                          "a slow drive then takes minutes to flush, and anything that syncs to it waits"],
                         ["Commonly recommended cap (trade-off: slightly lower burst speed on fast disks): "
                          "`sudo sysctl vm.dirty_background_bytes=67108864 vm.dirty_bytes=268435456`",
                          "Make it permanent in /etc/sysctl.d/99-dirty.conf"], [u.disk for u in slow]))
    return f, ok


CHECKS = [check_usb_speed, check_pcie_links, check_sata_ssd, check_trim, check_cpu_power, check_swap,
          check_ntfs, check_root_and_games, check_nvme_temp, check_dirty]


def run_checks(s: Snapshot | None = None) -> tuple[list[Finding], list[str]]:
    s = s or collect()
    findings, oks = [], []
    for chk in CHECKS:
        try:
            f, ok = chk(s)
        except Exception as e:          # one broken check must never hide the others
            f, ok = [], [f"(check {chk.__name__} failed: {e})"]
        findings += f
        oks += ok
    findings.sort(key=lambda x: (x.severity != "warn", {"high": 0, "medium": 1, "low": 2}[x.confidence]))
    return findings, oks


_cache: dict = {"t": 0.0, "v": None}


def cached_findings(max_age: float = 120.0) -> list[Finding]:
    """For reports: avoid re-reading sysfs on every analysis."""
    if _cache["v"] is None or time.time() - _cache["t"] > max_age:
        _cache["v"], _cache["t"] = run_checks()[0], time.time()
    return _cache["v"]


def findings_for_disk(disk: str, findings: list[Finding] | None = None) -> list[Finding]:
    return [f for f in (findings if findings is not None else cached_findings()) if disk in f.disks]


def render(findings: list[Finding], oks: list[str]) -> str:
    lines = []
    warns = [f for f in findings if f.severity == "warn"]
    lines.append(f"Hardware check — {len(warns)} thing(s) worth fixing, {len(findings) - len(warns)} to be aware of"
                 if findings else "Hardware check — nothing wrong found")
    for f in findings:
        lines.append("")
        lines.append(f"{'⚠' if f.severity == 'warn' else 'ℹ'}  {f.title}   [{f.confidence} confidence]")
        lines += [f"     • {e}" for e in f.evidence]
        lines += [f"     → {x}" for x in f.fix]
    if oks:
        lines.append("")
        lines.append("Checked and fine:")
        lines += [f"  ✓ {o}" for o in oks]
    return "\n".join(lines)
