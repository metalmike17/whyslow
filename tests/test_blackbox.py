import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_analyze import app, sample
from whyslow import blackbox as bb


def write_log(path: Path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def run(n=200, start=1000.0, **kw):
    return [sample(start + i, **kw) for i in range(n)]


def hit(cat, line):
    return {"cat": cat, "t": 0, "line": line}


def journal(*hits, last=None, available=True):
    return {"available": available, "hits": list(hits), "last_entry_t": last}


class Detection(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.log = self.dir / "samples.jsonl"

    def test_clean_stop_marker_means_no_incident(self):
        write_log(self.log, run(30) + [{"event": "stop", "t": 1030.0}])
        self.assertIsNone(bb.check_previous_run(self.log, now=5000, boot_t=2000))

    def test_missing_stop_marker_before_this_boot_is_a_system_end(self):
        write_log(self.log, run(30))
        info = bb.check_previous_run(self.log, now=5000, boot_t=2000)
        self.assertEqual(info["scope"], "system")
        self.assertEqual(info["last_t"], 1029.0)

    def test_missing_stop_marker_within_this_boot_is_daemon_only(self):
        write_log(self.log, run(30, start=3000.0))
        self.assertEqual(bb.check_previous_run(self.log, now=5000, boot_t=2000)["scope"], "daemon")

    def test_no_log_or_empty_log_is_not_an_incident(self):
        self.assertIsNone(bb.check_previous_run(self.log, now=5000, boot_t=2000))
        self.log.write_text("")
        self.assertIsNone(bb.check_previous_run(self.log, now=5000, boot_t=2000))

    def test_torn_last_line_after_crash_is_tolerated(self):
        write_log(self.log, run(30))
        with open(self.log, "a") as f:
            f.write('{"t": 1030.0, "stall": {"cpu_so')   # power cut mid-write
        info = bb.check_previous_run(self.log, now=5000, boot_t=2000)
        self.assertEqual(info["scope"], "system")
        self.assertEqual(info["last_t"], 1029.0)

    def test_keeps_only_the_final_window_of_samples(self):
        write_log(self.log, run(1000))
        info = bb.check_previous_run(self.log, now=9000, boot_t=5000)
        self.assertLessEqual(len(info["samples"]), bb.PRE_SECONDS + 1)
        self.assertEqual(info["samples"][-1]["t"], info["last_t"])


class Verdicts(unittest.TestCase):
    def test_memory_exhaustion_without_oom_kill(self):
        s = run(120, mem_some=70, mem_full=45, avail_gb=0.3,
                apps={"chrome": app(rss=12_000_000)})
        s[0]["apps"] = {"chrome": app(rss=2_000_000)}
        s[-1]["mem"].update(SwapTotal=8 * 1048576, SwapFree=int(0.3 * 1048576))
        v = bb.diagnose(s, journal(last=s[-1]["t"] + 1))
        self.assertEqual(v["category"], "memory")
        self.assertEqual(v["confidence"], "high")
        self.assertTrue(any("OOM killer never fired" in e for e in v["evidence"]))
        self.assertEqual(v["facts"]["grower"][0], "chrome")

    def test_oom_in_journal_wins_even_if_ram_looks_ok(self):
        s = run(60)
        v = bb.diagnose(s, journal(hit("oom", "Out of memory: Killed process 4242 (java)")))
        self.assertEqual(v["category"], "memory")
        self.assertIn("Killed process 4242", " ".join(v["evidence"]))

    def test_kernel_trace_outranks_everything(self):
        s = run(60, mem_some=70, mem_full=45, avail_gb=0.3)
        v = bb.diagnose(s, journal(hit("oom", "Out of memory"), hit("kernel", "BUG: soft lockup - CPU#3 stuck")))
        self.assertEqual(v["category"], "kernel")

    def test_gpu_hang(self):
        v = bb.diagnose(run(60), journal(hit("gpu", "NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus")))
        self.assertEqual(v["category"], "gpu")

    def test_thermal_from_temperature_alone_is_medium_confidence(self):
        v = bb.diagnose(run(60, temp=104), journal())
        self.assertEqual((v["category"], v["confidence"]), ("thermal", "medium"))

    def test_healthy_end_is_honest_about_not_knowing(self):
        v = bb.diagnose(run(180), journal())
        self.assertEqual(v["category"], "unknown")
        self.assertEqual(v["confidence"], "low")
        self.assertIn("power loss", v["headline"])

    def test_unreadable_journal_is_disclosed(self):
        v = bb.diagnose(run(60), journal(available=False))
        self.assertTrue(any("journal could not be read" in e for e in v["evidence"]))

    def test_system_kept_logging_after_sampler_stopped(self):
        s = run(60)
        v = bb.diagnose(s, journal(last=s[-1]["t"] + 100))
        self.assertTrue(any("kept logging" in e for e in v["evidence"]))

    def test_memory_units_are_mb_not_gb(self):
        s = run(60, apps={"chrome": app(rss=2_097_152)})            # 2 GiB in kB
        s[-1]["apps"] = {"chrome": app(rss=10_485_760)}             # 10 GiB in kB
        f = bb.diagnose(s, journal())["facts"]
        self.assertAlmostEqual(f["top_rss"][1], 10240, delta=1)     # MB
        self.assertAlmostEqual(f["grower"][1], 8192, delta=1)

    def test_memory_not_blamed_when_ram_is_fine(self):
        v = bb.diagnose(run(60, avail_gb=8.0), journal())
        self.assertNotEqual(v["category"], "memory")


class Journal(unittest.TestCase):
    def runner(self, out, err="", rc=0):
        return lambda cmd, **kw: SimpleNamespace(stdout=out, stderr=err, returncode=rc)

    def test_parses_and_dedups_and_classifies(self):
        out = ("1000.5 host kernel: Out of memory: Killed process 77 (firefox)\n"
               "1000.6 host kernel: Out of memory: Killed process 77 (firefox)\n"
               "1001.0 host kernel: NVRM: Xid (PCI:0000:01:00): 31\n"
               "1002.0 host plasmashell[1]: harmless chatter\n")
        r = bb.journal_hits(1001, runner=self.runner(out))
        self.assertTrue(r["available"])
        self.assertEqual([h["cat"] for h in r["hits"]], ["oom", "gpu"])
        self.assertEqual(r["last_entry_t"], 1002.0)

    def test_real_boot_noise_is_not_evidence(self):
        # Lines copied from a real journal's normal boot/shutdown; each once caused a false alarm.
        out = ("1.0 testhost systemd[1]: mcelog.service - Machine Check Exception Logging Daemon skipped, unmet condition\n"
               "2.0 testhost systemd[1]: Listening on systemd-oomd.socket - Userspace Out-Of-Memory (OOM) Killer Socket.\n"
               "3.0 testhost systemd[1]: Started systemd-oomd.service - Userspace Out-Of-Memory (OOM) Killer.\n"
               "4.0 host plasmashell[9]: debug: Buffer I/O error in cache\n"
               "5.0 host someapp[7]: GPU HANG detected in my own log text\n")
        self.assertEqual(bb.journal_hits(5, runner=self.runner(out))["hits"], [])

    def test_real_failures_are_detected(self):
        out = ("1.0 h kernel: Out of memory: Killed process 4242 (java) total-vm:1kB\n"
               "2.0 h systemd-oomd[812]: Killed /user.slice/app.scope due to memory pressure\n"
               "3.0 h kernel: mce: [Hardware Error]: Machine check events logged\n"
               "4.0 h kernel: NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.\n"
               "5.0 h kernel: INFO: task foo:1 blocked for more than 120 seconds.\n"
               "6.0 h kernel: Buffer I/O error on dev sdf1, logical block 5\n")
        cats = [h["cat"] for h in bb.journal_hits(6, runner=self.runner(out))["hits"]]
        self.assertEqual(cats, ["oom", "oom", "hardware", "gpu", "kernel", "storage"])

    def test_permission_failure_reports_unavailable(self):
        r = bb.journal_hits(1, runner=self.runner("", "No journal files were opened due to insufficient permissions."))
        self.assertFalse(r["available"])

    def test_missing_journalctl_reports_unavailable(self):
        def boom(*a, **k): raise FileNotFoundError
        self.assertFalse(bb.journal_hits(1, runner=boom)["available"])


class MemoryAdvice(unittest.TestCase):
    def inc(self):
        s = run(60, mem_some=70, mem_full=45, avail_gb=0.3)
        return {"scope": "system", "last_t": s[-1]["t"], "detected_t": s[-1]["t"] + 300, "journal": None,
                "samples": s, "verdict": bb.diagnose(s, journal(last=s[-1]["t"]))}

    def text(self, active):
        return " ".join(bb.describe_incident(self.inc(), oom_daemons=lambda: active))

    def test_does_not_tell_you_to_install_what_you_already_run(self):
        t = self.text(["systemd-oomd"])
        self.assertIn("systemd-oomd is running but did not step in", t)
        self.assertNotIn("Install one", t)

    def test_recommends_installing_when_nothing_runs(self):
        self.assertIn("No userspace OOM killer is running", self.text([]))

    def test_earlyoom_present_suggests_tuning(self):
        self.assertIn("earlyoom is running but did not step in", self.text(["earlyoom"]))


class Persistence(unittest.TestCase):
    def test_incident_roundtrip_and_seen_flag(self):
        d = Path(tempfile.mkdtemp())
        info = {"scope": "system", "last_t": 1029.0, "detected_t": 1300.0, "samples": run(30)}
        p = bb.record_incident(info, d, journal_fn=lambda t: journal())
        inc = bb.list_incidents(d)
        self.assertEqual(len(inc), 1)
        self.assertFalse(inc[0]["seen"])
        self.assertEqual(inc[0]["verdict"]["category"], "unknown")
        bb.mark_seen(str(p))
        self.assertTrue(bb.list_incidents(d)[0]["seen"])
        self.assertIn("ended unexpectedly", bb.describe_incident(inc[0])[0])

    def test_daemon_only_incident_does_not_query_journal_or_alarm(self):
        d = Path(tempfile.mkdtemp())
        called = []
        info = {"scope": "daemon", "last_t": 1029.0, "detected_t": 1300.0, "samples": run(30)}
        bb.record_incident(info, d, journal_fn=lambda t: called.append(t))
        self.assertEqual(called, [])
        self.assertIn("system itself kept running", bb.describe_incident(bb.list_incidents(d)[0])[0])


if __name__ == "__main__":
    unittest.main()
