"""Known slowdown culprits: explain what a process is, whether it is safe to stop, and what to do.

Static knowledge lives in data/offenders.toml (community-editable). A few kernel thread names
encode a device (e.g. ``kworker/u64:11+flush-8:80`` = writeback to block device 8:80); those are
decoded dynamically so we can say *which disk* a kernel thread is writing to.
"""
from __future__ import annotations

import fnmatch
import os
import pwd
import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import devices

DATA = Path(__file__).parent / "data" / "offenders.toml"
SAFE_TEXT = {
    "wait": "let it finish",
    "pause": "safe to pause or stop",
    "caution": "don't interrupt it",
    "kernel": "kernel thread — not something you can stop",
}


@dataclass
class Entry:
    id: str
    title: str
    what: str
    safe: str
    impact: str
    when: str = ""
    fix: list[str] = field(default_factory=list)
    disk: str | None = None          # set by dynamic decoders: the disk this thread works for

    @property
    def advice(self) -> str:
        return SAFE_TEXT.get(self.safe, "")

    def blurb(self) -> str:
        """One line for reports: title, plain explanation, and what to do."""
        first = self.what.split(". ")[0].rstrip(".")
        return f"{self.title}: {first}. ({self.advice})"


@lru_cache(maxsize=1)
def _db() -> list[tuple[list[str], Entry]]:
    with open(DATA, "rb") as f:
        raw = tomllib.load(f)["process"]
    out = []
    for p in raw:
        e = Entry(p["id"], p["title"], p["what"], p["safe"], p["impact"], p.get("when", ""), list(p.get("fix", [])))
        out.append(([m.lower() for m in p["match"]], e))
    return out


def all_entries() -> list[Entry]:
    return [e for _, e in _db()]


# --------------------------------------------------------------------------
# dynamic decoders for kernel threads
# --------------------------------------------------------------------------
_FLUSH = re.compile(r"flush-(\d+):(\d+)")
_JBD2 = re.compile(r"^jbd2/([A-Za-z0-9\-]+?)-\d+$")


def _disk_of_devnum(major: int, minor: int) -> str | None:
    name = os.path.basename(os.path.realpath(f"/sys/dev/block/{major}:{minor}"))
    if not name or name == f"{major}:{minor}":
        return None
    return devices.disk_of_source(f"/dev/{name}")


def _decode_kernel(name: str) -> Entry | None:
    if name.startswith("kworker/"):
        m = _FLUSH.search(name)
        disk = _disk_of_devnum(int(m.group(1)), int(m.group(2))) if m else None
        if disk:
            return Entry("kworker-flush", f"Kernel writeback to {devices.label(disk)}",
                         f"The kernel is flushing cached writes out to {disk}. Whatever is writing a lot "
                         "to that drive (a copy, download or install) keeps this thread busy, and a slow drive "
                         "makes it a bottleneck.",
                         "kernel", "disk", fix=[f"See what is writing: it is the app(s) with files on {disk} above"],
                         disk=disk)
        return Entry("kworker", "Kernel worker thread",
                     "A generic kernel thread doing deferred work (writing cached data to disk, finishing I/O, "
                     "driver tasks). The text after '+' names the job.", "kernel", "disk")
    m = _JBD2.match(name)
    if m:
        disk = devices.disk_of_source(f"/dev/{m.group(1)}")
        return Entry("jbd2", "ext4 journal thread",
                     "Commits the ext4 journal for a filesystem; busy while that filesystem is written to heavily.",
                     "kernel", "disk", disk=disk)
    return None


COMM_LEN = 15   # the kernel truncates process names (comm) to 15 characters


def _literal_prefix(pattern: str) -> str:
    return re.match(r"[^*?\[]*", pattern).group(0)


def lookup(app: str) -> Entry | None:
    """The knowledge-base entry for an app/process name, or None."""
    dyn = _decode_kernel(app)
    if dyn:
        return dyn
    low = app.lower()
    for patterns, entry in _db():
        if any(fnmatch.fnmatchcase(low, p) for p in patterns):
            return entry
    if len(low) == COMM_LEN:
        # Probably a truncated name: 'baloo_file_extr' is baloo_file_extractor.
        for patterns, entry in _db():
            if any(_literal_prefix(p).startswith(low) for p in patterns):
                return entry
    return None


# --------------------------------------------------------------------------
# process biography
# --------------------------------------------------------------------------
def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def package_of(path: str, run=subprocess.run) -> str | None:
    """Which installed package owns this file (rpm, dpkg or pacman), if we can tell."""
    path = path.removesuffix(" (deleted)")
    attempts = (("rpm", ["rpm", "-qf", "--qf", "%{NAME}-%{VERSION}", path]),
                ("dpkg", ["dpkg", "-S", path]),
                ("pacman", ["pacman", "-Qo", "-q", path]))
    for tool, cmd in attempts:
        if not shutil.which(tool) and run is subprocess.run:
            continue
        try:
            r = run(cmd, capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            continue
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out and "not owned" not in out:
            return out.split(":")[0].split("\n")[0]
    return None


def pids_by_name(name: str) -> list[int]:
    """Running processes whose name or executable matches, biggest memory first."""
    low = name.lower()
    found = []
    for e in os.scandir("/proc"):
        if not e.name.isdigit():
            continue
        comm = _read(f"/proc/{e.name}/comm").strip().lower()
        try:
            exe = os.path.basename(os.readlink(f"/proc/{e.name}/exe")).lower()
        except OSError:
            exe = ""
        if low in (comm, exe) or fnmatch.fnmatchcase(comm, low):
            rss = 0
            for line in _read(f"/proc/{e.name}/status").splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
            found.append((rss, int(e.name)))
    return [pid for _, pid in sorted(found, reverse=True)]


def biography(pid: int, run=subprocess.run) -> dict | None:
    """Everything cheap we can learn about a running process, or None if it is gone."""
    stat = _read(f"/proc/{pid}/stat")
    if not stat:
        return None
    rp = stat.rfind(")")
    f = stat[rp + 2:].split()
    clk = os.sysconf("SC_CLK_TCK")
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = ""
    status = dict(l.split(":", 1) for l in _read(f"/proc/{pid}/status").splitlines() if ":" in l)
    uid = int(status.get("Uid", "0").split()[0] or 0)
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    cg = (_read(f"/proc/{pid}/cgroup").strip().splitlines() or [""])[-1].split(":", 2)[-1]
    unit = next((p for p in reversed(cg.split("/")) if p.endswith((".service", ".scope"))), "")
    ppid = int(f[1])
    boot = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
    mm = devices.MountMap()
    disks = sorted(devices.proc_devices(pid, mm).items(), key=lambda kv: -kv[1])[:3]
    return {
        "pid": pid, "comm": stat[stat.find("(") + 1: rp], "exe": exe,
        "cmdline": " ".join(_read(f"/proc/{pid}/cmdline").split("\0"))[:200].strip(),
        "user": user, "unit": unit, "package": package_of(exe, run) if exe else None,
        "parent": (ppid, _read(f"/proc/{ppid}/comm").strip()),
        "started": boot + int(f[19]) / clk,
        "cpu_seconds": (int(f[11]) + int(f[12])) / clk,
        "rss_mb": int(status.get("VmRSS", "0 kB").split()[0]) / 1024,
        "threads": int(status.get("Threads", "1")),
        "disks": [(d, mb // 1048576) for d, mb in disks],
    }


def render_whatis(target: str, run=subprocess.run) -> str:
    """Plain-text answer to 'what is this process?' for a name or a pid."""
    if target.isdigit():
        pids, name = [int(target)], None
        bio = biography(int(target), run)
        if not bio:
            return f"No process with pid {target}."
        name = bio["comm"]
    else:
        name, pids = target, pids_by_name(target)
    entry = lookup(name)
    lines = []
    if entry:
        lines += [f"{entry.title}", f"  {entry.what}", f"  Safe to stop? {entry.advice}  ·  mostly uses: {entry.impact}"]
        if entry.when:
            lines.append(f"  Typically runs: {entry.when}")
        if entry.fix:
            lines.append("  What you can do:")
            lines += [f"    → {x}" for x in entry.fix]
    else:
        lines.append(f"'{name}' is not in the known-offenders database"
                     + (" — here is what the system says about it:" if pids else "."))
    if pids:
        bio = biography(pids[0], run)
        if bio:
            lines.append("")
            lines.append(f"Running now: pid {bio['pid']}" + (f" (+{len(pids) - 1} more)" if len(pids) > 1 else ""))
            lines.append(f"  program : {bio['exe'] or bio['comm']}" + (f"  [{bio['package']}]" if bio["package"] else ""))
            if bio["cmdline"]:
                lines.append(f"  command : {bio['cmdline']}")
            lines.append(f"  started : {time.strftime('%a %H:%M:%S', time.localtime(bio['started']))}"
                         f" by {bio['user']}, parent {bio['parent'][1]} ({bio['parent'][0]})")
            if bio["unit"]:
                lines.append(f"  unit    : {bio['unit']}")
            lines.append(f"  using   : {bio['rss_mb']:.0f} MB RAM, {bio['cpu_seconds']:.0f}s CPU so far, {bio['threads']} threads")
            if bio["disks"]:
                lines.append("  files on: " + ", ".join(f"{devices.label(d)} (~{mb} MB)" for d, mb in bio["disks"]))
    elif entry:
        lines.append("")
        lines.append("Not running right now.")
    return "\n".join(lines)
