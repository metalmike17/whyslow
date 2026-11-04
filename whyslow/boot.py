"""Slow-boot explainer: where did the time go between power button and desktop?

Wraps ``systemd-analyze`` (no root needed) and turns it into an answer a person can act on:
- the stages (firmware / boot menu / kernel / initrd / services) with a verdict on each,
- the *critical chain*: the services that actually delayed the desktop (blame lists many that run in
  parallel and cost nothing),
- hardware waits (``.device`` units) which blame mislabels as slow services,
- known boot-time offenders and their fixes,
- a per-boot history (recorded by the daemon) so "slow" means slow for *this* machine.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "whyslow"
HISTORY = STATE / "boots.jsonl"
KEEP_BOOTS = 60

STAGE_NAMES = {"firmware": "Firmware (BIOS/UEFI)", "loader": "Boot menu / bootloader", "kernel": "Kernel",
               "initrd": "Initial ramdisk", "userspace": "System services"}
SLOW_STEP = 1.0           # a chain step must cost this long to be mentioned
SLOW_FIRMWARE = 12.0
SLOW_LOADER = 3.0
SLOW_INITRD = 8.0
SLOW_KERNEL = 8.0
SLOW_USERSPACE = 20.0
SLOW_DEVICE_AT = 3.0      # a device on the chain appearing later than this means boot waited for hardware
HISTORY_SLOWER = 1.3      # this boot vs. typical, to call it unusually slow

_DUR = re.compile(r"(\d+(?:\.\d+)?)(us|ms|min|h|s)\b")
_UNIT = {"us": 1e-6, "ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0}


def parse_duration(text: str) -> float:
    """'1min 3.2s' -> 63.2; '21ms' -> 0.021."""
    return sum(float(n) * _UNIT[u] for n, u in _DUR.findall(text))


def parse_summary(text: str) -> dict[str, float]:
    """Stage durations from the 'Startup finished in ...' line."""
    m = re.search(r"Startup finished in (.+)", text)
    out: dict[str, float] = {}
    if not m:
        return out
    body = m.group(1)
    for dur, stage in re.findall(r"([\d.]+(?:us|ms|min|h|s)(?:\s+[\d.]+(?:us|ms|min|h|s))*)\s+\((\w+)\)", body):
        out[stage] = parse_duration(dur)
    t = re.search(r"=\s*([\d.]+(?:us|ms|min|h|s)(?:\s+[\d.]+(?:us|ms|min|h|s))*)\s*$", body.strip())
    if t:
        out["total"] = parse_duration(t.group(1))
    elif out:
        out["total"] = sum(out.values())
    return out


@dataclass
class Step:
    unit: str
    at: float                # seconds after userspace start when it became active
    took: float | None       # None for units that only report @ (targets, devices)


def parse_chain(text: str) -> list[Step]:
    """critical-chain output -> steps, latest first (as printed)."""
    steps = []
    for line in text.splitlines():
        m = re.match(r"^[\s│└─├]*(\S+)\s+@([\d.a-z ]+?)(?:\s+\+([\d.a-z ]+))?\s*$", line)
        if not m or "." not in m.group(1):
            continue
        steps.append(Step(m.group(1), parse_duration(m.group(2)), parse_duration(m.group(3)) if m.group(3) else None))
    return steps


def parse_blame(text: str) -> list[tuple[str, float]]:
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*((?:[\d.]+(?:us|ms|min|h|s)\s+)+)(\S+)\s*$", line)
        if m:
            out.append((m.group(2), parse_duration(m.group(1))))
    return out


# --------------------------------------------------------------------------
# knowledge: boot-time offenders (matched against unit names)
# --------------------------------------------------------------------------
HINTS: list[tuple[str, str, list[str]]] = [
    (r"(NetworkManager|systemd-networkd)-wait-online",
     "waits for the network to be fully up before continuing. Desktops rarely need that to reach the login screen.",
     ["`sudo systemctl disable NetworkManager-wait-online.service` (re-enable if something mounts network shares at boot)"]),
    (r"^systemd-udev-settle",
     "waits for every device to finish probing; it is a legacy service that often blocks boot for seconds.",
     ["Find what pulls it in: `systemctl list-dependencies --reverse systemd-udev-settle.service`, then disable that"]),
    (r"^akmods",
     "builds third-party kernel modules (e.g. NVIDIA) at boot. Slow right after a kernel update; normally a one-off.",
     ["If it runs every boot, check `journalctl -u akmods` for a build that keeps failing"]),
    (r"^dkms",
     "rebuilds out-of-tree kernel modules at boot.", ["Slow once after a kernel update; if every boot, check `dkms status`"]),
    (r"^plymouth",
     "the boot splash. It is rarely the true cause, but it can hold boot while waiting for the graphics driver.",
     ["Remove `splash` from the kernel command line to test"]),
    (r"^(dnf|packagekit|apt-daily|flatpak-system-helper)",
     "package-manager housekeeping that starts during boot.", ["Stop it from running at boot: disable its timer"]),
    (r"^(plocate|mlocate)-updatedb",
     "re-indexes every file for `locate`. It runs in the background while you log in, so it does not delay the desktop, "
     "but it competes with it for the disk.", ["Safe to ignore, or `sudo systemctl disable --now plocate-updatedb.timer`"]),
    (r"^systemd-fsck@",
     "checks a filesystem before it is mounted. Slow on big or dirty drives.",
     ["For drives not needed to boot, add `nofail,x-systemd.automount` to their /etc/fstab line so boot does not wait"]),
    (r"^lvm2|^mdmonitor|^mdadm",
     "assembles RAID/LVM volumes at boot.", ["Slow with many disks; nothing safe to cut unless the array is unused"]),
    (r"automount@|\.mount$",
     "mounts a drive during boot. External or secondary drives (especially NTFS through ntfs-3g) can add seconds here.",
     ["For drives not needed to log in, add `nofail,x-systemd.automount` to the /etc/fstab line (mounts on first access instead)"]),
    (r"^firewalld",
     "starts the firewall. Usually 1–2 s.", []),
    (r"^(bluetooth|cups|avahi-daemon|ModemManager)",
     "an optional service most desktops start but may not need.", ["Disable if unused: `sudo systemctl disable --now <name>`"]),
]
_HINTS = [(re.compile(p), why, fix) for p, why, fix in HINTS]


def hint_for(unit: str):
    for rx, why, fix in _HINTS:
        if rx.search(unit):
            return why, fix
    return None


def _unit_label(unit: str) -> str:
    return unit.replace("\\x2d", "-").replace("\\x2f", "/")


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------
@dataclass
class Finding:
    title: str
    detail: str
    fix: list[str] = field(default_factory=list)
    seconds: float = 0.0
    confidence: str = "medium"


@dataclass
class Report:
    stages: dict[str, float] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    background: list[tuple[str, float]] = field(default_factory=list)   # slow, but not delaying the desktop
    typical_total: float | None = None
    boots_seen: int = 0
    ok: bool = True
    error: str = ""


def analyze(summary: dict[str, float], chain: list[Step], blame: list[tuple[str, float]],
            history: list[dict] | None = None, usb_storage: int = 0) -> Report:
    r = Report(stages={k: v for k, v in summary.items() if k != "total"})
    total = summary.get("total", sum(r.stages.values()))
    r.stages["total"] = total
    past = [h["total"] for h in (history or []) if h.get("total")]
    r.boots_seen = len(past)
    if len(past) >= 3:
        s = sorted(past); r.typical_total = s[len(s) // 2]

    fw = r.stages.get("firmware", 0)
    if fw >= SLOW_FIRMWARE:
        r.findings.append(Finding(
            f"Firmware takes {fw:.0f}s before Linux even starts", 
            f"That is {fw / total:.0%} of your total boot time, spent in the motherboard's own startup code, outside the "
            "operating system's control." + (f" {usb_storage} USB storage devices are attached, and firmware often probes each one."
                                              if usb_storage >= 3 else ""),
            ["In BIOS/UEFI setup (reboot with `systemctl reboot --firmware-setup`): enable Fast Boot, "
             "disable network/PXE boot and unused boot devices, and set the main drive first",
             "Try booting once with USB drives unplugged to see how much they add"], fw, "medium"))
    ld = r.stages.get("loader", 0)
    if ld >= SLOW_LOADER:
        r.findings.append(Finding(
            f"The boot menu waits {ld:.0f}s",
            "Most of this is the menu's countdown, which you can shorten.",
            ["GRUB: set `GRUB_TIMEOUT=1` in /etc/default/grub then regenerate its config; "
             "systemd-boot: `timeout` in loader.conf"], ld, "medium"))
    for stage, limit in (("initrd", SLOW_INITRD), ("kernel", SLOW_KERNEL)):
        v = r.stages.get(stage, 0)
        if v >= limit:
            r.findings.append(Finding(f"{STAGE_NAMES[stage]} takes {v:.0f}s", 
                                      "Unusually long. Usually a slow drive being read, or something waiting for hardware.",
                                      ["Check `journalctl -b -k` for long gaps or errors during early boot"], v, "low"))

    # critical chain: the services that actually delayed the desktop
    on_chain = set()
    for st in chain:
        on_chain.add(st.unit)
        if st.unit.endswith(".device") or st.took is None:
            continue
        if st.took >= SLOW_STEP:
            name = _unit_label(st.unit)
            h = hint_for(st.unit)
            r.findings.append(Finding(f"{name} delayed the desktop by {st.took:.1f}s",
                                      "It is on the critical chain: later steps had to wait for it." + (f" It {h[0]}" if h else ""),
                                      h[1] if h else [], st.took, "high"))
    # hardware waits: a .device on the chain that appeared late means boot waited for that hardware
    for st in chain:
        if st.unit.endswith(".device") and st.at >= SLOW_DEVICE_AT:
            name = _unit_label(st.unit)
            r.findings.append(Finding(f"Boot waited {st.at:.1f}s for hardware: {name}",
                                      "A device on the critical path appeared this long after userspace started. "
                                      "systemd lists its wait as a '.device' unit, which `systemd-analyze blame` shows as a slow service, but nothing is "
                                      "running: it is waiting for the drive/USB device to show up.",
                                      ["If it is an external or secondary drive, mark it non-essential in /etc/fstab with `nofail,x-systemd.automount`"],
                                      st.at, "medium"))
            break
    # slow units that did NOT delay the desktop
    for unit, t in blame:
        if unit.endswith((".device",)) or unit in on_chain or t < 3.0:
            continue
        r.background.append((_unit_label(unit), t))
    r.background = r.background[:5]
    r.findings.sort(key=lambda f: -f.seconds)
    if not chain and not summary:
        r.ok, r.error = False, "systemd-analyze produced no data"
    return r


def render(r: Report, userspace_note: bool = True) -> str:
    if not r.ok:
        return f"Could not analyze boot: {r.error}"
    L = []
    tot = r.stages.get("total", 0)
    head = f"Last boot took {tot:.0f}s from power button to desktop"
    if r.typical_total:
        head += f" (your typical boot: {r.typical_total:.0f}s over {r.boots_seen} boots" + \
                (" — slower than usual)" if tot > r.typical_total * HISTORY_SLOWER else ")")
    L.append(head + ".")
    L.append("")
    for k in ("firmware", "loader", "kernel", "initrd", "userspace"):
        if k in r.stages:
            v = r.stages[k]
            bar = "█" * max(1, round(24 * v / tot)) if tot else ""
            L.append(f"  {STAGE_NAMES[k]:<24} {v:6.1f}s  {v / tot:4.0%}  {bar}")
    if r.findings:
        L += ["", "What slowed it down:"]
        for f in r.findings:
            L.append(f"  ⚠ {f.title}   [{f.confidence} confidence]")
            L.append(f"      {f.detail}")
            L += [f"      → {x}" for x in f.fix]
    else:
        L += ["", "Nothing stands out: no stage or service is unusually slow."]
    if r.background:
        L += ["", "Slow, but not delaying your desktop (they run in parallel, so no need to fix):"]
        L += [f"  · {n}  {t:.1f}s" for n, t in r.background]
    fw = r.stages.get("firmware", 0)
    if fw and tot and fw / tot > 0.4 and userspace_note:
        L += ["", f"Note: {fw / tot:.0%} of the boot is firmware; software tuning can only affect the other {1 - fw / tot:.0%}."]
    return "\n".join(L)


# --------------------------------------------------------------------------
# real machine
# --------------------------------------------------------------------------
def _run(args: list[str], run=subprocess.run) -> str:
    try:
        return run(["systemd-analyze", "--no-pager", *args], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _usb_storage_count() -> int:
    try:
        return sum(1 for b in os.listdir("/sys/block") if b.startswith("sd") and "/usb" in os.path.realpath(f"/sys/block/{b}"))
    except OSError:
        return 0


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def load_history(path: Path = HISTORY) -> list[dict]:
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def record_boot(summary: dict[str, float], path: Path = HISTORY, bid: str | None = None) -> bool:
    """Remember this boot once (keyed by boot id). Returns True if newly recorded."""
    bid = bid or boot_id()
    if not summary.get("total") or not bid:
        return False
    hist = load_history(path)
    if any(h.get("id") == bid for h in hist):
        return False
    hist.append({"id": bid, "t": int(time.time()), **{k: round(v, 2) for k, v in summary.items()}})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(h) + "\n" for h in hist[-KEEP_BOOTS:]))
    tmp.replace(path)
    return True


def current_summary(run=subprocess.run) -> dict[str, float]:
    return parse_summary(_run([], run))


def run_analysis(run=subprocess.run, history_path: Path = HISTORY) -> Report:
    summary = parse_summary(_run([], run))
    chain = parse_chain(_run(["critical-chain"], run))
    blame = parse_blame(_run(["blame"], run))
    if not summary:
        return Report(ok=False, error="systemd-analyze gave no timing (is this a systemd system, and has boot finished?)")
    hist = [h for h in load_history(history_path) if h.get("id") != boot_id()]
    record_boot(summary, history_path)
    return analyze(summary, chain, blame, hist, _usb_storage_count())
