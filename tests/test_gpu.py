import os, tempfile, unittest
from whyslow import gpu as g


def put(root, rel, text):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f: f.write(str(text))


def amd(root, card="card0", busy=40, used=2 * 1048576 * 1024, total=8 * 1048576 * 1024, temp=60000, crit=100000,
        avg=100_000_000, cap=200_000_000):
    d = f"class/drm/{card}/device"
    put(root, f"{d}/vendor", "0x1002"); put(root, f"{d}/gpu_busy_percent", busy)
    put(root, f"{d}/mem_info_vram_used", used); put(root, f"{d}/mem_info_vram_total", total)
    put(root, f"{d}/hwmon/hwmon3/temp1_input", temp); put(root, f"{d}/hwmon/hwmon3/temp1_crit", crit)
    put(root, f"{d}/hwmon/hwmon3/power1_average", avg); put(root, f"{d}/hwmon/hwmon3/power1_cap", cap)


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


def intel(root, card="card0", rc6=0, layout="i915", **extra):
    d = f"class/drm/{card}"
    put(root, f"{d}/device/vendor", "0x8086")
    if layout == "i915":
        put(root, f"{d}/gt/gt0/rc6_residency_ms", rc6)
        for k, v in extra.items(): put(root, f"{d}/gt/gt0/{k}", v)
    elif layout == "old":
        put(root, f"{d}/power/rc6_residency_ms", rc6)
    else:
        put(root, f"{d}/device/tile0/gt0/gtidle/idle_residency_ms", rc6)
        for k, v in extra.items(): put(root, f"{d}/device/tile0/gt0/freq0/throttle/{k}", v)


NONE = lambda: None


class AmdTests(unittest.TestCase):
    def read(self, **k):
        with tempfile.TemporaryDirectory() as d:
            amd(d, **k)
            return g.GpuReader(d, nvidia=NONE).read()

    def test_basic(self):
        r = self.read()
        self.assertEqual(r["vendor"], "amd"); self.assertEqual(r["util"], 40)
        self.assertEqual((r["mem_used"], r["mem_total"]), (2048, 8192)); self.assertEqual(r["temp"], 60)
        self.assertFalse(r["hw_thermal"] or r["power_cap"])

    def test_thermal_near_critical(self):
        self.assertTrue(self.read(temp=98000)["hw_thermal"])
        self.assertFalse(self.read(temp=90000)["hw_thermal"])

    def test_power_cap(self):
        self.assertTrue(self.read(avg=199_000_000)["power_cap"])
        self.assertFalse(self.read(avg=150_000_000)["power_cap"])

    def test_no_busy_file_means_not_amdgpu_data(self):
        with tempfile.TemporaryDirectory() as d:
            put(d, "class/drm/card0/device/vendor", "0x1002")
            self.assertIsNone(g.GpuReader(d, nvidia=NONE).read())

    def test_missing_hwmon_is_unknown_not_hot(self):
        with tempfile.TemporaryDirectory() as d:
            put(d, "class/drm/card0/device/vendor", "0x1002"); put(d, "class/drm/card0/device/gpu_busy_percent", 5)
            r = g.GpuReader(d, nvidia=NONE).read()
            self.assertEqual((r["temp"], r["hw_thermal"], r["mem_total"]), (0.0, False, 0.0))


class IntelTests(unittest.TestCase):
    def two_reads(self, layout="i915", busy_ms=250, **extra):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(); intel(d, rc6=1000, layout=layout, **extra)
            r = g.GpuReader(d, nvidia=NONE, now=clock)
            first = r.read()
            clock.t += 1.0
            key = {"i915": "gt/gt0/rc6_residency_ms", "old": "power/rc6_residency_ms",
                   "xe": "device/tile0/gt0/gtidle/idle_residency_ms"}[layout]
            put(d, f"class/drm/card0/{key}", 1000 + (1000 - busy_ms))      # idle for the rest of the second
            return first, r.read()

    def test_first_read_has_no_baseline(self):
        first, _ = self.two_reads()
        self.assertIsNone(first)

    def test_utilisation_is_one_minus_rc6(self):
        for layout in ("i915", "old", "xe"):
            _, r = self.two_reads(layout, busy_ms=250)
            self.assertAlmostEqual(r["util"], 25.0, places=1, msg=layout)
            self.assertEqual(r["vendor"], "intel")

    def test_fully_idle_and_clamped(self):
        _, r = self.two_reads(busy_ms=0); self.assertEqual(r["util"], 0.0)
        _, r = self.two_reads(busy_ms=1000); self.assertEqual(r["util"], 100.0)

    def test_throttle_reasons_i915(self):
        _, r = self.two_reads(throttle_reason_thermal=1, throttle_reason_pl1=0)
        self.assertTrue(r["hw_thermal"]); self.assertFalse(r["power_cap"])
        _, r = self.two_reads(throttle_reason_pl1=1)
        self.assertTrue(r["power_cap"]); self.assertFalse(r["hw_thermal"])

    def test_prochot_counts_as_thermal(self):
        _, r = self.two_reads(throttle_reason_prochot=1)
        self.assertTrue(r["hw_thermal"])

    def test_counter_glitch_is_clamped(self):
        _, r = self.two_reads(busy_ms=1300)          # rc6 went backwards: still a valid percentage
        self.assertEqual(r["util"], 100.0)

    def test_temperature_from_hwmon_in_celsius(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(); intel(d, rc6=0)
            put(d, "class/drm/card0/device/hwmon/hwmon5/temp1_input", 45000)
            r = g.GpuReader(d, nvidia=NONE, now=clock); r.read(); clock.t += 1
            put(d, "class/drm/card0/gt/gt0/rc6_residency_ms", 500)
            self.assertEqual(r.read()["temp"], 45.0)

    def test_throttle_reasons_xe(self):
        _, r = self.two_reads("xe", reason_thermal=1)
        self.assertTrue(r["hw_thermal"])

    def test_memory_not_reported_for_shared_ram(self):
        _, r = self.two_reads(); self.assertEqual((r["mem_used"], r["mem_total"]), (0.0, 0.0))

    def test_no_rc6_counter_means_no_data(self):
        with tempfile.TemporaryDirectory() as d:
            put(d, "class/drm/card0/device/vendor", "0x8086")
            self.assertIsNone(g.GpuReader(d, nvidia=NONE).read())


class SelectionTests(unittest.TestCase):
    def test_connectors_and_foreign_devices_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            amd(d, card="card1", busy=10)
            put(d, "class/drm/card1-HDMI-A-1/device/vendor", "0x1002")
            put(d, "class/drm/card1-HDMI-A-1/device/gpu_busy_percent", 99)
            put(d, "class/drm/card0/device/vendor", "0x1234")           # e.g. evdi / virtual
            self.assertEqual([x["util"] for x in g.GpuReader(d, nvidia=NONE).read_all()], [10])

    def test_other_vendors_ignored_even_with_amd_like_files(self):
        with tempfile.TemporaryDirectory() as d:
            amd(d); put(d, "class/drm/card0/device/vendor", "0x1234")
            self.assertEqual(g.GpuReader(d, nvidia=NONE).read_all(), [])

    def test_hybrid_reports_busiest(self):
        with tempfile.TemporaryDirectory() as d:
            amd(d, card="card0", busy=5)
            nv = lambda: {**g._blank("nvidia"), "util": 90.0}
            r = g.GpuReader(d, nvidia=nv).read()
            self.assertEqual((r["vendor"], r["util"]), ("nvidia", 90.0))
            nv2 = lambda: {**g._blank("nvidia"), "util": 1.0}
            self.assertEqual(g.GpuReader(d, nvidia=nv2).read()["vendor"], "amd")

    def test_no_gpu(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(g.GpuReader(d, nvidia=NONE).read())

    def test_schema_matches_what_consumers_use(self):
        keys = {"vendor", "util", "temp", "hw_thermal", "sw_thermal", "power_cap", "mem_used", "mem_total"}
        with tempfile.TemporaryDirectory() as d:
            amd(d); self.assertEqual(set(g.GpuReader(d, nvidia=NONE).read()), keys)


class NvidiaTests(unittest.TestCase):
    class R:
        def __init__(s, out): s.stdout = out

    def test_parse(self):
        out = "37, 61, 0x0000000000000000, Not Active, Active, Not Active, 1234, 8151\n"
        r = g.read_nvidia(lambda *a, **k: self.R(out), which=lambda x: "/bin/x")
        self.assertEqual((r["util"], r["temp"], r["sw_thermal"], r["hw_thermal"], r["mem_total"]), (37, 61, True, False, 8151))

    def test_missing_tool_or_garbage(self):
        self.assertIsNone(g.read_nvidia(which=lambda x: None))
        self.assertIsNone(g.read_nvidia(lambda *a, **k: self.R("N/A"), which=lambda x: "/bin/x"))


if __name__ == "__main__":
    unittest.main()
