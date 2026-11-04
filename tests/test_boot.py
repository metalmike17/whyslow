import json, tempfile, unittest
from pathlib import Path
from whyslow import boot as b

SUMMARY = """Startup finished in 23.020s (firmware) + 4.921s (loader) + 3.859s (kernel) + 2.701s (initrd) + 8.294s (userspace) = 42.798s
graphical.target reached after 8.294s in userspace.
"""
CHAIN = """The time when unit became active or started is printed after the "@" character.
The time the unit took to start is printed after the "+" character.

graphical.target @8.294s
└─power-profiles-daemon.service @8.273s +21ms
  └─multi-user.target @8.260s
    └─nobara-automount@run-media-alice-0123456789ABCDEF.mount.service @3.166s +1.982s
      └─basic.target @3.150s
        └─systemd-fsck@dev-sdf1.service @4.097s +115ms
          └─dev-sdf1.device @1.340s
"""
BLAME = """8.578s plocate-updatedb.service
5.030s dev-sde.device
3.2s nobara-updater-confirm.service
1min 3.5s slow.service
  21ms tiny.service
"""


class ParseTests(unittest.TestCase):
    def test_duration(self):
        self.assertAlmostEqual(b.parse_duration("1min 3.2s"), 63.2)
        self.assertAlmostEqual(b.parse_duration("21ms"), 0.021)
        self.assertAlmostEqual(b.parse_duration("2h 1min"), 7260)

    def test_summary_uefi(self):
        s = b.parse_summary(SUMMARY)
        self.assertEqual(set(s), {"firmware", "loader", "kernel", "initrd", "userspace", "total"})
        self.assertAlmostEqual(s["firmware"], 23.02); self.assertAlmostEqual(s["total"], 42.798)

    def test_summary_bios_minutes(self):
        s = b.parse_summary("Startup finished in 2.1s (kernel) + 1min 5.2s (userspace) = 1min 7.3s\n")
        self.assertNotIn("firmware", s); self.assertAlmostEqual(s["userspace"], 65.2); self.assertAlmostEqual(s["total"], 67.3)

    def test_summary_missing(self):
        self.assertEqual(b.parse_summary("Bootup is not yet finished"), {})

    def test_chain_names_with_at_sign(self):          # real: nobara-automount@run-media-....mount.service
        c = {s.unit: s for s in b.parse_chain(CHAIN)}
        st = c["nobara-automount@run-media-alice-0123456789ABCDEF.mount.service"]
        self.assertAlmostEqual(st.took, 1.982); self.assertAlmostEqual(st.at, 3.166)
        self.assertIsNone(c["dev-sdf1.device"].took); self.assertNotIn("The", c)

    def test_blame(self):
        d = dict(b.parse_blame(BLAME))
        self.assertAlmostEqual(d["slow.service"], 63.5); self.assertIn("tiny.service", d)


class AnalyzeTests(unittest.TestCase):
    def run_(self, summary=SUMMARY, chain=CHAIN, blame=BLAME, **k):
        return b.analyze(b.parse_summary(summary), b.parse_chain(chain), b.parse_blame(blame), **k)

    def titles(self, r): return [f.title for f in r.findings]

    def test_slow_firmware_flagged_with_usb_count(self):
        r = self.run_(usb_storage=7)
        f = next(x for x in r.findings if x.title.startswith("Firmware"))
        self.assertIn("7 USB", f.detail)

    def test_fast_firmware_not_flagged(self):
        r = self.run_(summary=SUMMARY.replace("23.020s", "6.0s"))
        self.assertFalse(any(t.startswith("Firmware") for t in self.titles(r)))

    def test_loader_flagged(self):
        self.assertTrue(any("boot menu" in t for t in self.titles(self.run_())))
        self.assertFalse(any("boot menu" in t for t in self.titles(self.run_(summary=SUMMARY.replace("4.921s", "1.0s")))))

    def test_critical_chain_step_blamed_with_hint(self):
        r = self.run_()
        f = next(x for x in r.findings if "nobara-automount" in x.title)
        self.assertEqual(f.confidence, "high"); self.assertTrue(any("nofail" in x for x in f.fix))

    def test_fast_chain_steps_ignored(self):
        self.assertFalse(any("power-profiles" in t or "fsck" in t for t in self.titles(self.run_())))

    def test_background_service_not_blamed_but_listed(self):
        r = self.run_()
        self.assertFalse(any("plocate" in t for t in self.titles(r)))
        self.assertIn("plocate-updatedb.service", [n for n, _ in r.background])

    def test_device_units_are_not_slow_services(self):
        self.assertNotIn("dev-sde.device", [n for n, _ in self.run_().background])

    def test_chain_service_not_also_listed_as_background(self):
        blame = BLAME + "9.0s nobara-automount@run-media-alice-0123456789ABCDEF.mount.service\n"
        self.assertFalse(any("nobara-automount" in n for n, _ in self.run_(blame=blame).background))

    def test_late_device_flagged_early_one_not(self):
        late = CHAIN.replace("dev-sdf1.device @1.340s", "dev-sdf1.device @6.0s")
        self.assertTrue(any("waited" in t for t in self.titles(self.run_(chain=late))))
        self.assertFalse(any("waited" in t for t in self.titles(self.run_())))

    def test_history_typical_and_slower(self):
        r = self.run_(history=[{"total": 20}, {"total": 22}, {"total": 21}])
        self.assertEqual(r.typical_total, 21); self.assertIn("slower than usual", b.render(r))
        r2 = self.run_(history=[{"total": 44}, {"total": 42}, {"total": 43}])
        self.assertNotIn("slower than usual", b.render(r2))
        self.assertIsNone(self.run_(history=[{"total": 20}]).typical_total)

    def test_hint_wait_online(self):
        self.assertIsNotNone(b.hint_for("NetworkManager-wait-online.service"))
        self.assertIsNone(b.hint_for("nothing-special.service"))

    def test_render_firmware_note_and_clean_boot(self):
        self.assertIn("firmware", b.render(self.run_()).split("Note:")[-1])
        clean = b.render(b.analyze({"kernel": 2, "userspace": 5, "total": 7}, [], []))
        self.assertIn("Nothing stands out", clean)

    def test_firmware_note_only_when_dominant(self):
        r = b.analyze({"firmware": 5, "kernel": 3, "userspace": 20, "total": 28}, [], [])
        self.assertNotIn("Note:", b.render(r))

    def test_background_list_capped_at_five(self):
        blame = "".join(f"{9 - i * 0.1:.1f}s svc{i}.service\n" for i in range(9))
        self.assertEqual(len(self.run_(blame=blame).background), 5)

    def test_no_data(self):
        r = b.analyze({}, [], [])
        self.assertFalse(r.ok); self.assertIn("Could not", b.render(r))


class HistoryTests(unittest.TestCase):
    def test_record_once_per_boot_and_trim(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "boots.jsonl"
            self.assertTrue(b.record_boot({"total": 40, "firmware": 20}, p, "A"))
            self.assertFalse(b.record_boot({"total": 41}, p, "A"))
            self.assertTrue(b.record_boot({"total": 30}, p, "B"))
            self.assertEqual([h["id"] for h in b.load_history(p)], ["A", "B"])
            self.assertFalse(b.record_boot({}, p, "C"))
            for i in range(b.KEEP_BOOTS + 10):
                b.record_boot({"total": 1}, p, f"x{i}")
            self.assertEqual(len(b.load_history(p)), b.KEEP_BOOTS)

    def test_corrupt_history_line_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "h"; p.write_text('{"id":"a","total":3}\nGARBAGE\n')
            self.assertEqual(len(b.load_history(p)), 1)

    def test_run_analysis_with_fake_systemd(self):
        class R:  # fake subprocess.run
            def __init__(s, out): s.stdout = out
        def fake(cmd, **k):
            return R(BLAME if "blame" in cmd else CHAIN if "critical-chain" in cmd else SUMMARY)
        with tempfile.TemporaryDirectory() as d:
            r = b.run_analysis(fake, Path(d) / "h")
            self.assertTrue(r.ok); self.assertEqual(len(b.load_history(Path(d) / "h")), 1 if b.boot_id() else 0)
        self.assertFalse(b.run_analysis(lambda *a, **k: R(""), Path("/nonexistent/h")).ok)


if __name__ == "__main__":
    unittest.main()
