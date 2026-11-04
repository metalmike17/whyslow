"""whyslow — explain, in plain English, why your Linux box just stuttered."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .analyze import analyze, render
from . import blackbox
from .sampler import Sampler

STATE_DIR = blackbox.STATE_DIR
LOG = blackbox.LOG
KEEP_SECONDS = 7200      # two hours: long enough to analyze a gaming session afterwards
STALE_AFTER = 10

UNIT = """[Unit]
Description=WhySlow background sampler

[Service]
Environment=PYTHONPATH={pkgdir}
ExecStart={python} -m whyslow daemon
Nice=10
Restart=on-failure

[Install]
WantedBy=default.target
"""


def load_log(window: int) -> list[dict]:
    if not LOG.exists():
        return []
    cutoff = time.time() - window
    out = []
    with open(LOG, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max(8_000_000, int(window * 4096))))  # tail only; ~3KB per sample
        data = f.read().decode(errors="ignore").splitlines()
    for line in data:
        try:
            s = json.loads(line)
        except ValueError:
            continue
        if "stall" in s and s["t"] >= cutoff:
            out.append(s)
    return out


def live_sample(seconds: int) -> list[dict]:
    sm = Sampler()
    sm.sample()
    out = []
    for _ in range(seconds):
        time.sleep(1)
        s = sm.sample()
        if s:
            out.append(s)
    return out


def trim_log() -> None:
    cutoff = time.time() - KEEP_SECONDS
    keep = []
    for l in LOG.read_text(errors="ignore").splitlines():
        try:
            if json.loads(l)["t"] >= cutoff:
                keep.append(l)
        except (ValueError, KeyError):
            continue   # torn line from a crash
    tmp = LOG.with_suffix(".tmp")
    tmp.write_text("\n".join(keep) + "\n")
    tmp.replace(LOG)


FSYNC_EVERY = 5   # samples; bounds what a hard freeze can lose to ~5s


def _durable_write(f, text: str) -> None:
    f.write(text)
    f.flush()
    os.fsync(f.fileno())


def cmd_daemon(args) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # Before writing anything: did the previous run end abnormally? If so, diagnose and keep a report.
    try:
        prev = blackbox.check_previous_run()
        if prev:
            path = blackbox.record_incident(prev)
            print(f"previous run ended abnormally ({prev['scope']}); report saved to {path}", file=sys.stderr)
    except Exception as e:  # never let post-mortem trouble stop the sampler
        print(f"post-mortem failed: {e}", file=sys.stderr)

    def open_log():
        f = open(LOG, "a+", buffering=1)
        f.seek(0, os.SEEK_END)
        if f.tell():                      # a crash can leave a torn last line; start on a fresh one
            f.seek(f.tell() - 1)
            if f.read(1) != "\n":
                f.write("\n")
        return f

    sm = Sampler()
    n = 0
    boot_noted = False
    f = open_log()
    try:
        while running:
            t0 = time.time()
            s = sm.sample()
            if s:
                f.write(json.dumps(s, separators=(",", ":")) + "\n")
                n += 1
                if n % FSYNC_EVERY == 0:
                    f.flush()
                    os.fsync(f.fileno())
            if not boot_noted and n % 30 == 5:      # remember this boot's timing once systemd has finished starting
                try:
                    from . import boot
                    summary = boot.current_summary()
                    if summary:
                        boot.record_boot(summary)
                        boot_noted = True
                except Exception:
                    boot_noted = True               # best-effort only
            if n and n % 300 == 0:
                f.close()
                trim_log()
                f = open_log()
            time.sleep(max(0.0, args.interval - (time.time() - t0)))
    finally:
        _durable_write(f, json.dumps({"event": "stop", "t": round(time.time(), 2)}) + "\n")
        f.close()
    return 0


def cmd_stutter(args) -> int:
    from . import stutter
    if args.how:
        print(stutter.HOW_TO)
        return 0
    if args.file:
        path = Path(args.file).expanduser()
    else:
        logs = stutter.find_logs()
        if not logs:
            print("No MangoHud frame logs found. Run `whyslow stutter --how` to see how to record one.")
            return 1
        path = logs[0]
        print(f"(using newest log: {path})", file=sys.stderr)
    try:
        fr = stutter.parse(path)
    except (OSError, stutter.LogError) as e:
        print(f"Cannot read {path}: {e}", file=sys.stderr)
        return 1
    samples = [s for s in load_log(int(time.time() - fr.t[0]) + 60) if fr.t[0] - 10 <= s["t"] <= fr.t[-1] + 10]
    if not samples:
        print("Note: whyslow has no recorded system data for this play session, so causes can't be "
              "identified (run the daemon while you play; it keeps ~2 hours).\n", file=sys.stderr)
    elif samples[0]["t"] > fr.t[0] + 30:
        print(f"Note: whyslow's recording only starts {samples[0]['t'] - fr.t[0]:.0f}s into this session.\n",
              file=sys.stderr)
    print(stutter.report(fr, samples, top=args.top))
    return 0


def cmd_hardware(args) -> int:
    from . import hwcheck
    findings, oks = hwcheck.run_checks()
    print(hwcheck.render(findings, oks))
    return 0


def battery_report() -> str:
    from . import battery, offenders
    return battery.render(battery.analyze(load_log(KEEP_SECONDS), battery.info(), battery.context(), offenders.lookup))


def cmd_battery(args) -> int:
    print(battery_report())
    return 0


def boot_report() -> str:
    from . import boot
    return boot.render(boot.run_analysis())


def cmd_boot(args) -> int:
    print(boot_report())
    return 0


def cmd_report(args) -> int:
    from . import share
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}
    bad = skip - set(share.SECTIONS)
    if bad:
        print(f"unknown section(s): {', '.join(sorted(bad))} (choose from {', '.join(share.SECTIONS)})", file=sys.stderr)
        return 2
    try:
        text = share.build(set(share.SECTIONS) - skip, strict=args.strict)
    except share.LeakError as e:
        print(e, file=sys.stderr)
        return 1
    if args.output:
        Path(args.output).expanduser().write_text(text + "\n")
        print(f"saved {args.output}. Nothing was uploaded; read it before you share it.", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_enable(args) -> int:
    from . import service
    try:
        for line in (service.disable() if args.cmd == "disable" else service.enable(tray=not args.no_tray)):
            print(line)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"failed: {e}. Is systemd's user session running?", file=sys.stderr)
        return 1
    return 0


def cmd_postmortem(args) -> int:
    incs = blackbox.list_incidents()
    if not args.all:
        incs = [i for i in incs if i["scope"] == "system"]
    if args.json:
        print(json.dumps([{k: v for k, v in i.items() if k != "samples"} for i in incs], indent=2))
        return 0
    if not incs:
        print("No unexpected shutdowns recorded. (The daemon must be running to record them: "
              "`whyslow daemon`.)")
        return 0
    print("\n".join(blackbox.describe_incident(incs[0])))
    blackbox.mark_seen(incs[0]["path"])
    if len(incs) > 1:
        print("\nEarlier:")
        for i in incs[1:8]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(i["last_t"]))
            print(f"  {when}  {i['verdict']['headline'][:90]}  [{i['verdict']['confidence']}]")
    return 0


def cmd_explain(args) -> int:
    samples = load_log(args.window)
    fresh = samples and time.time() - samples[-1]["t"] < STALE_AFTER
    if not fresh:
        if not args.json:
            print(f"(no recent background data — sampling live for {args.live}s; "
                  "run `whyslow daemon` to catch stutters after the fact)", file=sys.stderr)
        samples = live_sample(args.live)
    if len(samples) < 2:
        print("Not enough data yet.", file=sys.stderr)
        return 1
    episodes = analyze(samples)
    if args.json:
        print(json.dumps([{**e.__dict__, "culprits": [c.__dict__ for c in e.culprits]}
                          for e in episodes], indent=2))
    else:
        print(render(episodes, samples, limit=args.top))
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import service
    service.clean_appimage_env()
    p = argparse.ArgumentParser(prog="whyslow", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    for sp in (p, sub.add_parser("explain", help="diagnose recent slowdowns (default)")):
        sp.add_argument("-w", "--window", type=int, default=120, help="seconds of history (default 120)")
        sp.add_argument("-n", "--top", type=int, default=3, help="episodes to show")
        sp.add_argument("--live", type=int, default=8, help="seconds to sample if no daemon data")
        sp.add_argument("--json", action="store_true")

    d = sub.add_parser("daemon", help="run the background sampler")
    d.add_argument("-i", "--interval", type=float, default=1.0)
    sub.add_parser("unit", help="print a systemd user unit for the daemon")
    wi = sub.add_parser("whatis", help="explain what a process is (by name or pid)")
    wi.add_argument("target", help="process name (e.g. baloo_file) or pid")
    st = sub.add_parser("stutter", help="explain game stutter from a MangoHud frame-time log")
    st.add_argument("file", nargs="?", help="MangoHud CSV (default: newest one found)")
    st.add_argument("--how", action="store_true", help="show how to record a MangoHud log")
    st.add_argument("-n", "--top", type=int, default=5, help="worst hitches to detail")
    sub.add_parser("hardware", help="check for hardware/config gotchas that make the system slow")
    rp = sub.add_parser("report", help="make an anonymized report you can paste into a forum post or bug report")
    rp.add_argument("-o", "--output", help="write to a file instead of printing")
    rp.add_argument("--strict", action="store_true", help="also hide names of programs that are not well-known")
    rp.add_argument("--skip", default="", help="comma-separated sections to leave out: system,slowdowns,crashes,hardware,boot,battery")
    en = sub.add_parser("enable", help="start recording in the background and show a tray icon at login")
    en.add_argument("--no-tray", action="store_true", help="only the background sampler")
    sub.add_parser("disable", help="stop recording and remove the login tray icon")
    sub.add_parser("boot", help="explain why the last boot was slow")
    sub.add_parser("battery", help="explain what is draining the laptop battery")
    pm = sub.add_parser("postmortem", help="explain why the system last ended unexpectedly")
    pm.add_argument("--all", action="store_true", help="include sampler-only crashes")
    pm.add_argument("--json", action="store_true")
    g = sub.add_parser("gui", help="open the graphical window")
    g.add_argument("--minimized", action="store_true", help="start hidden in the tray")

    args = p.parse_args(argv)
    if args.cmd == "daemon":
        return cmd_daemon(args)
    if args.cmd == "gui":
        try:
            from .gui import run
        except ModuleNotFoundError as e:
            if e.name and e.name.startswith("PySide6"):
                print("The window needs PySide6. Install it with your package manager (Fedora: python3-pyside6, "
                      "Arch: pyside6, Debian/Ubuntu: python3-pyside6.qtwidgets) or `pip install --user PySide6`.\n"
                      "Everything else works from the command line: whyslow --help", file=sys.stderr)
                return 1
            raise
        return run(minimized=args.minimized)
    if args.cmd == "stutter":
        return cmd_stutter(args)
    if args.cmd in ("enable", "disable"):
        return cmd_enable(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "boot":
        return cmd_boot(args)
    if args.cmd == "battery":
        return cmd_battery(args)
    if args.cmd == "hardware":
        return cmd_hardware(args)
    if args.cmd == "whatis":
        from .offenders import render_whatis
        print(render_whatis(args.target))
        return 0
    if args.cmd == "postmortem":
        return cmd_postmortem(args)
    if args.cmd == "unit":
        print(UNIT.format(python=sys.executable, pkgdir=Path(__file__).resolve().parent.parent), end="")
        return 0
    return cmd_explain(args)
