import os
import unittest
from types import SimpleNamespace
from unittest import mock

from test_analyze import app, sample
from whyslow import offenders as off
from whyslow.analyze import analyze, describe

SAFE = {"wait", "pause", "caution", "kernel"}
IMPACT = {"cpu", "disk", "memory", "network"}


class Database(unittest.TestCase):
    def test_every_entry_is_well_formed(self):
        entries = off.all_entries()
        self.assertGreaterEqual(len(entries), 20)
        self.assertEqual(len({e.id for e in entries}), len(entries), "duplicate ids")
        for e in entries:
            self.assertIn(e.safe, SAFE, e.id)
            self.assertIn(e.impact, IMPACT, e.id)
            self.assertTrue(e.title and e.what, e.id)
            self.assertTrue(e.what.rstrip().endswith("."), f"{e.id}: 'what' should be a sentence")
            for f in e.fix:
                self.assertTrue(f.strip(), e.id)

    def test_kernel_entries_offer_no_stop_commands(self):
        for e in off.all_entries():
            if e.safe == "kernel":
                self.assertFalse(any("kill" in f or "stop" in f.lower() for f in e.fix), e.id)

    def test_every_match_pattern_is_nonempty_lowercase_friendly(self):
        for patterns, e in off._db():
            self.assertTrue(patterns, e.id)
            self.assertTrue(all(p and p == p.lower() for p in patterns), e.id)


class Lookup(unittest.TestCase):
    def test_known_names(self):
        for name, ident in [("baloo_file", "baloo"), ("Baloo_File", "baloo"), ("baloo_file_extractor", "baloo"),
                            ("dnf5", "dnf"), ("dnf-app-center-updater", "dnf"), ("tracker-miner-fs-3", "tracker"),
                            ("kswapd0", "kswapd"), ("btrfs", "btrfs-maintenance"),
                            ("btrfs-cleaner", "btrfs-kernel"), ("usb-storage", "usb-storage"),
                            ("fossilize_replay", "fossilize"), ("qbittorrent", "torrent"),
                            ("ntfs-3g", "ntfs-3g"), ("steamwebhelper", "steamwebhelper")]:
            e = off.lookup(name)
            self.assertIsNotNone(e, name)
            self.assertEqual(e.id, ident, name)

    def test_names_truncated_to_15_chars_by_the_kernel_still_match(self):
        for full, ident in [("fossilize_replay", "fossilize"), ("baloo_file_extractor", "baloo"),
                            ("systemd-journald", "journald"), ("dnf-app-center-updater", "dnf"),
                            ("tracker-miner-fs-3", "tracker"), ("systemd-coredump", "coredump")]:
            truncated = full[:15]
            self.assertEqual(len(truncated), 15)
            e = off.lookup(truncated)
            self.assertIsNotNone(e, f"{truncated!r} (truncated {full})")
            self.assertEqual(e.id, ident, truncated)

    def test_truncation_matching_does_not_over_reach(self):
        self.assertIsNone(off.lookup("fossilize_repl"))        # 14 chars: not a truncation
        self.assertIsNone(off.lookup("fossilize_other"))        # 15 chars but not a prefix of anything
        self.assertIsNone(off.lookup("systemd-logind1234"))     # long, no match

    def test_ordinary_programs_do_not_match(self):
        for name in ["firefox", "python3", "bash", "kitty", "plasmashell", "code", "brave", "Xwayland",
                     "claude", "faugus-launcher", "rtool.exe", "pipewire", "kwin_wayland", "git", "snapshot-viewer"]:
            self.assertIsNone(off.lookup(name), f"{name} must not be flagged as a known offender")

    def test_kworker_flush_is_decoded_to_a_disk(self):
        with mock.patch.object(off, "_disk_of_devnum", lambda M, m: "sdf" if (M, m) == (8, 80) else None):
            e = off.lookup("kworker/u64:11+flush-8:80")
        self.assertEqual(e.disk, "sdf")
        self.assertEqual(e.safe, "kernel")
        self.assertIn("flushing cached writes out to sdf", e.what)

    def test_kworker_without_a_decodable_device_is_generic(self):
        with mock.patch.object(off, "_disk_of_devnum", lambda M, m: None):
            self.assertIsNone(off.lookup("kworker/u64:11+flush-8:99").disk)
        self.assertEqual(off.lookup("kworker/3:1-events").id, "kworker")

    def test_jbd2_thread_is_decoded(self):
        with mock.patch.object(off.devices, "disk_of_source", lambda p: "sdf" if p == "/dev/sdf1" else None):
            e = off.lookup("jbd2/sdf1-8")
        self.assertEqual((e.id, e.disk), ("jbd2", "sdf"))


class AnalyzerIntegration(unittest.TestCase):
    def test_known_culprit_is_explained_with_a_fix(self):
        s = [sample(t, io_full=40, apps={"baloo_file": app(wr=80_000_000, rd=60_000_000)}) for t in range(8)]
        ep = analyze(s)[0]
        self.assertEqual(ep.culprits[0].known.id, "baloo")
        text = "\n".join(describe(ep))
        self.assertIn("↳ Baloo (KDE file indexer)", text)
        self.assertTrue(any("balooctl6 suspend" in x for x in ep.suggestions))

    def test_unknown_culprit_gets_no_explanation_line(self):
        s = [sample(t, io_full=40, apps={"mystery": app(wr=80_000_000)}) for t in range(8)]
        ep = analyze(s)[0]
        self.assertIsNone(ep.culprits[0].known)
        self.assertNotIn("↳", "\n".join(describe(ep)))

    def test_kernel_writeback_thread_pins_the_disk(self):
        s = [sample(t, io_full=40, disks={"sdf": {"busy": 100, "rd": 0, "wr": 9}},
                    apps={"kworker/u64:11+flush-8:80": app(wr=90_000_000, dstate=1)}) for t in range(8)]
        with mock.patch.object(off, "_disk_of_devnum", lambda M, m: "sdf"):
            ep = analyze(s)[0]
        self.assertEqual(ep.main_disk, "sdf")
        self.assertIn("is writing to sdf", ep.culprits[0].detail)

    def test_kernel_threads_get_no_stop_advice(self):
        s = [sample(t, mem_some=30, avail_gb=9, apps={"kswapd0": app(cpu=90, majflt=500)}) for t in range(8)]
        ep = analyze(s)[0]
        self.assertEqual(ep.culprits[0].known.id, "kswapd")
        self.assertFalse(any("kswapd" in x.lower() for x in ep.suggestions))

    def test_generic_updater_hint_is_skipped_when_database_knows_it(self):
        s = [sample(t, mem_some=20, avail_gb=9, apps={"dnf5": app(majflt=5000)}) for t in range(8)]
        adv = " ".join(analyze(s)[0].suggestions)
        self.assertIn("DNF", adv)
        self.assertNotIn("kill -STOP", adv)


class WhatIs(unittest.TestCase):
    def test_package_lookup_uses_first_tool_that_answers(self):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd[0])
            return SimpleNamespace(stdout="foo-1.2\n" if cmd[0] == "dpkg" else "", stderr="", returncode=0 if cmd[0] == "dpkg" else 1)
        self.assertEqual(off.package_of("/usr/bin/foo", run), "foo-1.2")
        self.assertEqual(calls, ["rpm", "dpkg"])

    def test_package_lookup_failure_is_none(self):
        def boom(*a, **k): raise OSError
        self.assertIsNone(off.package_of("/x", boom))

    def test_not_owned_file_is_not_reported(self):
        run = lambda cmd, **kw: SimpleNamespace(stdout="file /x is not owned by any package", stderr="", returncode=0)
        self.assertIsNone(off.package_of("/x", run))

    def test_biography_of_this_process(self):
        b = off.biography(os.getpid(), run=lambda *a, **k: SimpleNamespace(stdout="", stderr="", returncode=1))
        self.assertEqual(b["pid"], os.getpid())
        self.assertTrue(b["exe"].endswith(("python3", "python3.14", "python")) or "python" in b["exe"])
        self.assertGreater(b["rss_mb"], 1)
        self.assertGreater(b["threads"], 0)

    def test_biography_of_missing_pid_is_none(self):
        self.assertIsNone(off.biography(2 ** 22 + 12345))

    def test_whatis_unknown_name_and_missing_pid(self):
        self.assertIn("not in the known-offenders database", off.render_whatis("definitely-not-a-process-xyz"))
        self.assertIn("No process with pid", off.render_whatis(str(2 ** 22 + 12345)))

    def test_whatis_known_but_not_running(self):
        out = off.render_whatis("fossilize_replay")
        self.assertIn("Fossilize", out)
        self.assertIn("Not running right now", out)


if __name__ == "__main__":
    unittest.main()
