import unittest
from unittest import mock

from test_analyze import app, sample
from whyslow import devices
from whyslow.analyze import MAX_GAP, analyze, describe


def fake_info(disk):
    return {"sdf": {"model": "My Passport", "rotational": True, "transport": "USB"},
            "nvme0n1": {"model": "Fast", "rotational": False, "transport": "NVMe"}}.get(
        disk, {"model": "", "rotational": False, "transport": "SATA"})


TARGETS = [{"disk": "nvme0n1", "mount": "/home", "free_gb": 800.0, "label": "nvme0n1 (Fast, NVMe drive)"}]


class Patched(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(devices, "info", fake_info),
                  mock.patch.object(devices, "faster_targets", lambda *a, **k: TARGETS),
                  mock.patch.object(devices.os.path, "exists", lambda p: True if "/sys/block/" in p else
                                    __import__("os").path.lexists(p))):
            p.start(); self.addCleanup(p.stop)


def disk_hot(extra=None):
    return {"sdf": {"busy": 100, "rd": 5, "wr": 0}, **(extra or {})}


class IncidentGrouping(Patched):
    def test_stalls_within_gap_merge_into_one_incident(self):
        s = [sample(t, cpu_some=60, apps={"ffmpeg": app(cpu=1200)}) for t in range(8)]
        s += [sample(8 + t) for t in range(MAX_GAP - 5)]
        s += [sample(100 + t, mem_some=20, avail_gb=9, apps={"x": app(majflt=900)}) for t in range(8)]
        for i, x in enumerate(s):
            x["t"] = float(i)
        eps = analyze(s)
        self.assertEqual(len(eps), 1)

    def test_secondary_cause_listed_and_culprits_judged_per_cause(self):
        s = [sample(t, io_full=25, apps={"rsync": app(wr=90_000_000)}) for t in range(6)]
        s += [sample(6 + t) for t in range(10)]           # calm interior
        s += [sample(16 + t, cpu_some=90, apps={"ffmpeg": app(cpu=1300)}) for t in range(10)]
        ep = analyze(s)[0]
        self.assertEqual(ep.cause, "io")
        self.assertEqual([c for c, _ in ep.secondary], ["cpu"])
        self.assertTrue(any("Also: CPU" in l and "ffmpeg" in l for l in describe(ep)))
        self.assertEqual(ep.culprits[0].app, "rsync")     # judged over io-stalled ticks only

    def test_near_threshold_calm_between_stalls_adds_no_weight(self):
        # Two minor stalls bracket 10 ticks of memory pressure just *under* the threshold.
        # If calm ticks counted, that quiet stretch would balloon into a fake memory incident.
        s = [sample(0, io_full=8.0), sample(1, io_full=8.0)]
        s += [sample(2 + t, mem_some=4.5) for t in range(10)]
        s += [sample(12, io_full=8.0), sample(13, io_full=8.0)]
        self.assertEqual(analyze(s, min_severity=8.0), [])

    def test_distant_stalls_stay_separate(self):
        s = [sample(t, io_full=40) for t in range(5)] + [sample(5 + t) for t in range(MAX_GAP + 10)]
        s += [sample(35 + t, io_full=40) for t in range(5)]
        for i, x in enumerate(s):
            x["t"] = float(i)
        self.assertEqual(len(analyze(s)), 2)


class DeviceEvidence(Patched):
    def test_culprit_on_busy_disk_is_tagged_and_named_in_headline(self):
        s = [sample(t, io_full=40, disks=disk_hot(),
                    apps={"installer": app(wr=80_000_000, devs={"sdf": 300, "nvme0n1": 50})}) for t in range(8)]
        ep = analyze(s)[0]
        self.assertEqual(ep.main_disk, "sdf")
        self.assertIn("has files on sdf", ep.culprits[0].detail)
        self.assertIn("Disk I/O stall on sdf", ep.headline)

    def test_unrelated_busy_disk_is_not_blamed(self):
        s = [sample(t, mem_some=20, avail_gb=9, disks=disk_hot(),
                    apps={"updater": app(majflt=5000, devs={"nvme0n1": 300})}) for t in range(8)]
        ep = analyze(s)[0]
        self.assertIsNone(ep.main_disk)
        self.assertIn("probably unrelated", ep.headline)
        self.assertNotIn("sdf", ep.culprits[0].detail)

    def test_idle_disk_not_tagged_even_if_culprit_uses_it(self):
        s = [sample(t, io_full=40, disks={"sdf": {"busy": 5, "rd": 0, "wr": 0}},
                    apps={"a": app(wr=80_000_000, devs={"sdf": 100})}) for t in range(8)]
        self.assertIsNone(analyze(s)[0].main_disk)


class Advice(Patched):
    def test_rotational_disk_suggests_move_to_ssd_with_target(self):
        s = [sample(t, io_full=40, disks=disk_hot(),
                    apps={"game": app(wr=80_000_000, devs={"sdf": 6000})}) for t in range(8)]
        adv = " ".join(analyze(s)[0].suggestions)
        self.assertIn("spinning disk", adv)
        self.assertIn("/home", adv)
        self.assertIn("800 GB free", adv)

    def test_fast_disk_gets_ionice_not_move_advice(self):
        s = [sample(t, io_full=40, disks={"nvme0n1": {"busy": 100, "rd": 0, "wr": 1}},
                    apps={"copy": app(wr=900_000_000, devs={"nvme0n1": 500})}) for t in range(8)]
        adv = " ".join(analyze(s)[0].suggestions)
        self.assertIn("ionice -c3", adv)
        self.assertNotIn("Moving its data", adv)

    def test_two_apps_on_same_disk(self):
        s = [sample(t, io_full=40, disks=disk_hot(),
                    apps={"a": app(wr=80_000_000, devs={"sdf": 100}),
                          "b": app(wr=70_000_000, devs={"sdf": 100})}) for t in range(8)]
        self.assertTrue(any("one at a time" in x for x in analyze(s)[0].suggestions))

    def test_updater_detected_and_commands_are_quoted(self):
        s = [sample(t, mem_some=20, avail_gb=9, apps={"mystery-updater": app(majflt=5000)})
             for t in range(8)]
        adv = analyze(s)[0].suggestions[0]
        self.assertIn("kill -STOP", adv)
        self.assertIn("pgrep -d, -f mystery-updater", adv)

    def test_renice_only_for_heavy_cpu(self):
        heavy = [sample(t, cpu_some=70, apps={"build": app(cpu=900)}) for t in range(8)]
        light = [sample(t, cpu_some=70, apps={"tiny": app(cpu=60)}) for t in range(8)]
        self.assertTrue(any("renice" in x for x in analyze(heavy)[0].suggestions))
        self.assertFalse(any("renice" in x for x in analyze(light)[0].suggestions))

    def test_ram_shortage_suggests_closing_biggest(self):
        s = [sample(t, mem_some=30, mem_full=15, avail_gb=0.4,
                    apps={"chrome": app(rss=3_000_000, majflt=300), "small": app(rss=50_000, majflt=300)})
             for t in range(8)]
        adv = " ".join(analyze(s)[0].suggestions)
        self.assertIn("Closing chrome", adv)

    def test_quiet_incident_without_evidence_gives_no_advice(self):
        s = [sample(t, cpu_some=60, apps={"x": app(cpu=50)}) for t in range(8)]
        self.assertEqual([x for x in analyze(s)[0].suggestions if "renice" in x], [])

    def test_advice_failure_never_breaks_diagnosis(self):
        s = [sample(t, io_full=40, apps={"a": app(wr=80_000_000)}) for t in range(8)]
        with mock.patch("whyslow.advice.suggest", side_effect=RuntimeError("boom")):
            eps = analyze(s)
        self.assertEqual(eps[0].suggestions, [])


if __name__ == "__main__":
    unittest.main()
