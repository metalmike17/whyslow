import os, tempfile, unittest
from whyslow import battery as b


def mk(root, name, **files):
    d = os.path.join(root, name); os.makedirs(d, exist_ok=True)
    for k, v in files.items():
        with open(os.path.join(d, k), "w") as f: f.write(str(v))
    return d


def S(t, w, apps=None, st="D", pct=80):
    return {"t": t, "bat": {"w": w, "pct": pct, "st": st}, "apps": {a: {"cpu": c} for a, c in (apps or {}).items()}}


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = self.tmp.name
    def tearDown(self): self.tmp.cleanup()

    def test_mouse_battery_ignored(self):       # real machine: hidpp mouse, scope=Device
        mk(self.root, "hidpp_battery_0", type="Battery", scope="Device", status="Discharging", capacity=50)
        self.assertIsNone(b.system_battery(self.root)); self.assertIsNone(b.read(self.root))

    def test_power_now(self):
        mk(self.root, "BAT0", type="Battery", status="Discharging", capacity=63, power_now=7_500_000)
        self.assertEqual(b.read(self.root), {"w": 7.5, "pct": 63.0, "st": "D"})

    def test_current_times_voltage(self):
        mk(self.root, "BAT0", type="Battery", status="Charging", capacity=10, current_now=1_000_000, voltage_now=12_000_000)
        r = b.read(self.root); self.assertEqual((r["w"], r["st"]), (12.0, "C"))

    def test_ac_adapter_ignored(self):
        mk(self.root, "AC", type="Mains", online=1)
        self.assertIsNone(b.system_battery(self.root))

    def test_info_health_charge_based(self):
        mk(self.root, "BAT0", type="Battery", charge_full=3_000_000, charge_full_design=5_000_000,
           voltage_min_design=10_000_000, cycle_count=412)
        i = b.info(self.root)
        self.assertAlmostEqual(i["full_wh"], 30.0); self.assertAlmostEqual(i["health"], 0.6); self.assertEqual(i["cycles"], 412)


class AnalyzeTests(unittest.TestCase):
    def test_no_battery(self):
        self.assertEqual(b.analyze([{"t": 1, "apps": {}}]).status, "no-battery")

    def test_plugged_in(self):
        self.assertEqual(b.analyze([S(i, 20, st="C") for i in range(300)]).status, "not-discharging")

    def test_too_short(self):
        self.assertEqual(b.analyze([S(i, 8) for i in range(60)]).status, "too-short")

    def _mixed(self, delta=6.0, n=300, other=None):
        out = []
        for i in range(n):
            busy = (i // 50) % 2 == 0
            apps = {"firefox": 40 if busy else 0.5}
            apps.update(other or {})
            out.append(S(i, 5 + (delta if busy else 0), apps))
        return out

    def test_culprit_found_with_delta(self):
        r = b.analyze(self._mixed())
        self.assertEqual(r.status, "ok"); self.assertEqual(r.culprits[0].app, "firefox")
        self.assertAlmostEqual(r.culprits[0].delta_w, 6.0, places=1)

    def test_small_delta_is_noise(self):
        self.assertEqual(b.analyze(self._mixed(delta=0.4)).culprits, [])

    def test_always_busy_app_not_blamed(self):   # no quiet baseline -> no evidence
        s = [S(i, 9, {"steady": 30}) for i in range(300)]
        self.assertEqual(b.analyze(s).culprits, [])

    def test_rarely_busy_app_not_blamed(self):
        s = [S(i, 5 + (6 if i < 10 else 0), {"x": 50 if i < 10 else 0}) for i in range(300)]
        self.assertEqual(b.analyze(s).culprits, [])

    def test_charging_samples_excluded(self):
        s = self._mixed() + [S(1000 + i, 40, {"firefox": 40}, st="C") for i in range(300)]
        self.assertAlmostEqual(b.analyze(s).culprits[0].delta_w, 6.0, places=1)

    def test_idle_is_calm_floor_and_culprits_sorted_by_cost(self):
        s = []
        for i in range(400):
            a_busy, b_busy = (i // 40) % 2 == 0, (i // 40) % 3 == 0
            s.append(S(i, 4 + (2 if a_busy else 0) + (5 if b_busy else 0), {"a": 30 if a_busy else 0, "b": 30 if b_busy else 0}))
        r = b.analyze(s)
        self.assertLess(r.idle_w, 5)
        self.assertEqual(r.culprits, sorted(r.culprits, key=lambda c: -c.cost_wh))
        self.assertGreater(len(r.culprits), 1)

    def test_time_left(self):
        r = b.analyze([S(i, 10, pct=50) for i in range(300)], {"full_wh": 60, "health": 1})
        self.assertAlmostEqual(r.hours_left, 3.0)

    def test_notes(self):
        r = b.analyze([S(i, 10) for i in range(300)], {"full_wh": 40, "health": 0.6, "cycles": 500},
                      {"dgpu_awake": True, "brightness": 0.95})
        text = " ".join(r.notes)
        self.assertIn("discrete GPU", text); self.assertIn("95%", text); self.assertIn("60%", text)
        self.assertEqual(b.analyze([S(i, 10) for i in range(300)], {"full_wh": 40, "health": 0.95}, {"brightness": 0.3}).notes, [])

    def test_render_mentions_correlation_and_culprit(self):
        t = b.render(b.analyze(self._mixed(), {"full_wh": 50, "health": 1}))
        self.assertIn("firefox", t); self.assertIn("correlation", t)

    def test_lookup_blurb_attached(self):
        class E:
            def blurb(self): return "KNOWN"
        r = b.analyze(self._mixed(), lookup=lambda a: E())
        self.assertEqual(r.culprits[0].known, "KNOWN")


class ContextTests(unittest.TestCase):
    def test_dgpu_and_brightness(self):
        with tempfile.TemporaryDirectory() as t:
            mk(t, "class/backlight/intel", max_brightness=100, brightness=90)
            mk(t, "bus/pci/devices/0000:01:00.0", vendor="0x10de", **{"class": "0x030000"})
            mk(t, "bus/pci/devices/0000:01:00.0/power", runtime_status="active")
            c = b.context(t)
            self.assertAlmostEqual(c["brightness"], 0.9); self.assertTrue(c["dgpu_awake"])
            mk(t, "bus/pci/devices/0000:01:00.0/power", runtime_status="suspended")
            self.assertFalse(b.context(t)["dgpu_awake"])
            for vendor in ("0x1002", "0x8086"):                    # AMD / Intel Arc discrete GPUs count too
                mk(t, "bus/pci/devices/0000:01:00.0", vendor=vendor)
                mk(t, "bus/pci/devices/0000:01:00.0/power", runtime_status="active")
                self.assertTrue(b.context(t)["dgpu_awake"], vendor)
            mk(t, "bus/pci/devices/0000:01:00.0", boot_vga="1")      # the integrated/boot GPU is not the "discrete" one
            self.assertFalse(b.context(t)["dgpu_awake"])


if __name__ == "__main__":
    unittest.main()
