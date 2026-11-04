"""Map file paths to the physical disk they live on, and describe disks.

btrfs/overlay filesystems report anonymous st_dev numbers, so we go through the mount table
(/proc/self/mountinfo) instead: longest-matching mount point -> source device -> whole disk.
"""
from __future__ import annotations

import os
import re
import time
from functools import lru_cache

_OCT = re.compile(r"\\([0-7]{3})")
_REFRESH = 30.0


def _unoct(s: str) -> str:
    return _OCT.sub(lambda m: chr(int(m.group(1), 8)), s)


@lru_cache(maxsize=256)
def disk_of_source(src: str) -> str | None:
    """'/dev/nvme0n1p3' -> 'nvme0n1'; '/dev/mapper/x' -> its underlying disk."""
    if not src.startswith("/dev/"):
        return None
    name = os.path.basename(os.path.realpath(src))
    for _ in range(4):  # dm/md stacks
        if name.startswith("dm-"):
            try:
                slaves = os.listdir(f"/sys/block/{name}/slaves")
            except OSError:
                return name
            if not slaves:
                return name
            name = slaves[0]
            continue
        break
    real = os.path.realpath(f"/sys/class/block/{name}")
    if os.path.exists(os.path.join(real, "partition")):
        name = os.path.basename(os.path.dirname(real))
    if name.startswith("dm-"):
        return disk_of_source(f"/dev/{name}")
    return name


class MountMap:
    """Cached view of real (device-backed) mounts."""

    def __init__(self):
        self._mounts: list[tuple[str, str, str, bool]] = []  # (mountpoint, source, fstype, ro)
        self._loaded = 0.0
        self._by_dir: dict[str, str | None] = {}

    def _load(self):
        mounts = []
        try:
            with open("/proc/self/mountinfo") as f:
                for line in f:
                    left, _, right = line.partition(" - ")
                    lf, rf = left.split(), right.split()
                    if len(rf) < 2 or not rf[1].startswith("/dev/"):
                        continue
                    ro = "ro" in lf[5].split(",")
                    mounts.append((_unoct(lf[4]), rf[1], rf[0], ro))
        except OSError:
            pass
        mounts.sort(key=lambda m: len(m[0]), reverse=True)
        self._mounts, self._loaded, self._by_dir = mounts, time.time(), {}

    def mounts(self) -> list[tuple[str, str, str, bool]]:
        if time.time() - self._loaded > _REFRESH:
            self._load()
        return self._mounts

    def disk_for_path(self, path: str) -> str | None:
        d = os.path.dirname(path) or "/"
        if time.time() - self._loaded > _REFRESH:
            self._load()
        if d in self._by_dir:
            return self._by_dir[d]
        disk = None
        for mp, src, _, _ in self._mounts:
            if mp == "/" or d == mp or d.startswith(mp.rstrip("/") + "/"):
                disk = disk_of_source(src)
                break
        if len(self._by_dir) > 4096:
            self._by_dir.clear()
        self._by_dir[d] = disk
        return disk


_SKIP_FD = ("/dev/", "/proc/", "/sys/", "/memfd:", "/anon_inode:")
FD_WEIGHT = 16 * 1024 * 1024
MAX_FDS = 200


def proc_devices(pid: int, mm: MountMap) -> dict[str, int]:
    """{disk: bytes} of files a process has mapped (page-fault sources) or open (I/O targets)."""
    out: dict[str, int] = {}
    seen: set[str] = set()
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                parts = line.split(None, 5)
                if len(parts) < 6 or not parts[5].startswith("/"):
                    continue
                path = parts[5].rstrip("\n")
                if path.endswith(" (deleted)") or path.startswith(_SKIP_FD):
                    continue
                lo, _, hi = parts[0].partition("-")
                size = int(hi, 16) - int(lo, 16)
                disk = mm.disk_for_path(path)
                if disk:
                    out[disk] = out.get(disk, 0) + size
                    seen.add(path)
    except (OSError, ValueError):
        pass
    try:
        for i, fd in enumerate(os.scandir(f"/proc/{pid}/fd")):
            if i >= MAX_FDS:
                break
            try:
                path = os.readlink(fd.path)
            except OSError:
                continue
            if not path.startswith("/") or path.startswith(_SKIP_FD) or path in seen:
                continue
            seen.add(path)
            disk = mm.disk_for_path(path)
            if disk:
                out[disk] = out.get(disk, 0) + FD_WEIGHT
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# descriptions
# --------------------------------------------------------------------------
def _sys(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


@lru_cache(maxsize=64)
def info(disk: str) -> dict:
    real = os.path.realpath(f"/sys/block/{disk}")
    transport = ("USB" if "/usb" in real else "NVMe" if disk.startswith("nvme")
                 else "virtual" if "/virtual/" in real else "SATA")
    return {
        "model": " ".join(_sys(f"/sys/block/{disk}/device/model").split()),
        "rotational": _sys(f"/sys/block/{disk}/queue/rotational") == "1",
        "transport": transport,
    }


def label(disk: str) -> str:
    """'sdf (WD10JMVW-11AJGS3, spinning USB drive)'. Unknown disks are returned bare."""
    if not os.path.exists(f"/sys/block/{disk}"):
        return disk
    i = info(disk)
    kind = "spinning " if i["rotational"] else "SSD " if i["transport"] != "NVMe" else ""
    tail = f"{kind}{i['transport']} drive" if i["transport"] != "virtual" else "RAID/virtual volume"
    model = f"{i['model']}, " if i["model"] else ""
    return f"{disk} ({model}{tail})"


def is_rotational(disk: str) -> bool:
    return info(disk)["rotational"]


def faster_targets(avoid: str, min_free_gb: float = 20.0, mm: MountMap | None = None) -> list[dict]:
    """Mounted, writable, non-rotational volumes with room — candidates to move data to."""
    mm = mm or MountMap()
    best: dict[str, dict] = {}
    for mp, src, fstype, ro in mm.mounts():
        disk = disk_of_source(src)
        if ro or not disk or disk == avoid or is_rotational(disk) or fstype in ("vfat", "iso9660", "squashfs"):
            continue
        if mp.startswith(("/boot", "/efi", "/var/lib/flatpak", "/var/lib/containers")):
            continue
        try:
            st = os.statvfs(mp)
        except OSError:
            continue
        free = st.f_bavail * st.f_frsize / 1e9
        if free >= min_free_gb and (disk not in best or free > best[disk]["free_gb"]):
            best[disk] = {"disk": disk, "mount": mp, "free_gb": free, "label": label(disk)}
    return sorted(best.values(), key=lambda t: (info(t["disk"])["transport"] != "NVMe", -t["free_gb"]))
