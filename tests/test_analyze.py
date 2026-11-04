import unittest

from whyslow.analyze import MAX_GAP, analyze, find_episodes, render

GB = 1048576  # kB per GB


def sample(t, *, io_full=0, io_some=0, mem_some=0, mem_full=0, cpu_some=5, apps=None,
           avail_gb=8.0, total_gb=16.0, disks=None, cpu_util=20, mhz=4000, max_mhz=4500,
           temp=60, vm=None, gpu=None):
    return {
        "t": float(t), "dt": 1.0,
        "stall": {"io_full": io_full, "io_some": io_some, "mem_some": mem_some,
                  "mem_full": mem_full, "cpu_some": cpu_some},
        "vm": vm or {}, "disks": disks or {}, "cpu_util": cpu_util, "mhz": mhz,
        "max_mhz": max_mhz, "temp": temp, "load": 1.0,
        "mem": {"MemAvailable": int(avail_gb * GB), "MemTotal": int(total_gb * GB)},
        "gpu": gpu, "apps": apps or {},
    }


def app(**kw):
    base = {"cpu": 0.0, "rd": 0, "wr": 0, "majflt": 0, "dstate": 0, "rss": 100_000, "n": 1, "devs": {}}
    base.update(kw)
    return base


class AnalyzeTests(unittest.TestCase):
    def test_quiet_system_reports_nothing(self):
        samples = [sample(t) for t in range(60)]
        self.assertEqual(analyze(samples), [])
        self.assertIn("healthy", render([], samples))

    def test_io_stall_blames_heaviest_writer(self):
        samples = [sample(t) for t in range(10)]
        samples += [sample(10 + t, io_full=40, io_some=60,
                           disks={"sda": {"busy": 99, "rd": 0, "wr": 1}},
                           apps={"rsync": app(wr=150_000_000, dstate=2),
                                 "firefox": app(cpu=10)}) for t in range(8)]
        samples += [sample(18 + t) for t in range(10)]
        eps = analyze(samples)
        self.assertEqual(len(eps), 1)
        ep = eps[0]
        self.assertEqual(ep.cause, "io")
        self.assertEqual(ep.culprits[0].app, "rsync")
        self.assertTrue(any("sda" in f for f in ep.factors))
        self.assertAlmostEqual(ep.duration, 8, delta=1)

    def test_real_ram_shortage(self):
        samples = [sample(t, mem_some=30, mem_full=15, avail_gb=0.5,
                          vm={"pswpin": 3000, "pswpout": 3000},
                          apps={"chrome": app(rss=1_000_000 + t * 400_000, majflt=300)})
                   for t in range(10)]
        ep = analyze(samples)[0]
        self.assertEqual(ep.cause, "mem")
        self.assertIn("ran short of RAM", ep.headline)
        self.assertEqual(ep.culprits[0].app, "chrome")
        self.assertTrue(any("swapping" in f for f in ep.factors))

    def test_pagein_stall_is_not_called_ram_shortage(self):
        samples = [sample(t, mem_some=20, avail_gb=9.0,
                          disks={"sdf": {"busy": 100, "rd": 1, "wr": 0}},
                          apps={"wine-game": app(majflt=5000, rss=6_000_000, devs={"sdf": 6000})})
                   for t in range(10)]
        ep = analyze(samples)[0]
        self.assertIn("Page-in stall", ep.headline)
        self.assertIn("9.0 GB still available", ep.headline)
        self.assertIn("wine-game is using", ep.headline)
        self.assertEqual(ep.main_disk, "sdf")

    def test_cpu_contention(self):
        samples = [sample(t, cpu_some=70, cpu_util=100,
                          apps={"ffmpeg": app(cpu=1400), "other": app(cpu=30)}) for t in range(8)]
        ep = analyze(samples)[0]
        self.assertEqual(ep.cause, "cpu")
        self.assertEqual(ep.culprits[0].app, "ffmpeg")
        self.assertIn("14.0 cores", ep.culprits[0].detail)

    def test_thermal_throttle_factor(self):
        samples = [sample(t, cpu_some=60, cpu_util=100, mhz=1800, max_mhz=4500, temp=96,
                          apps={"build": app(cpu=1200)}) for t in range(8)]
        ep = analyze(samples)[0]
        self.assertTrue(any("throttling" in f and "96" in f for f in ep.factors))

    def test_gpu_thermal_factor(self):
        gpu = {"util": 99, "temp": 91, "hw_thermal": True, "sw_thermal": False,
               "power_cap": False, "mem_used": 1000, "mem_total": 8000}
        samples = [sample(t, cpu_some=60, gpu=gpu) for t in range(8)]
        self.assertTrue(any("GPU was thermally throttled" in f for f in analyze(samples)[0].factors))

    def test_short_lull_keeps_one_episode_but_long_gap_splits(self):
        hot = lambda t: sample(t, io_full=30)
        samples = [hot(0), hot(1), sample(2), hot(3), hot(4)]  # 1s lull -> single episode
        self.assertEqual(len(find_episodes(samples)), 1)
        gap = MAX_GAP + 5
        samples = [hot(0), hot(1)] + [sample(t) for t in range(2, 2 + gap)] + [hot(2 + gap), hot(3 + gap)]
        self.assertEqual(len(find_episodes(samples)), 2)

    def test_worst_episode_ranked_first(self):
        g = MAX_GAP + 10
        samples = ([sample(t) for t in range(5)] + [sample(5 + t, io_full=12) for t in range(3)]
                   + [sample(8 + t) for t in range(g)]
                   + [sample(8 + g + t, io_full=60) for t in range(6)] + [sample(14 + g)])
        eps = analyze(samples)
        self.assertEqual(len(eps), 2)
        self.assertGreater(eps[0].severity, eps[1].severity)
        self.assertEqual(eps[0].start, float(8 + g))

    def test_blip_below_min_severity_ignored(self):
        samples = [sample(0), sample(1, io_full=9), sample(2)]
        self.assertEqual(analyze(samples), [])


if __name__ == "__main__":
    unittest.main()
