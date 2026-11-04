import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_analyze import app, sample
from whyslow import devices, stutter as st

START = 1_800_000_000.0     # fixed wall-clock start for synthetic logs


def make_frames(spikes=(), n_sec=60, base_ms=8.0, cols=None, start=START):
    """Steady `base_ms` frames with 1-frame spikes at the given seconds (offset, ms)."""
    t, ft, now = [], [], 0.0
    spike = dict(spikes)
    while now < n_sec:
        ms = base_ms
        for off, v in spike.items():
            if abs(now - off) < base_ms / 1000 and off not in getattr(make_frames, "_used", set()):
                ms = v
        t.append(start + now + ms / 1000); ft.append(ms); now += ms / 1000
    fr = st.Frames("x.csv", "game", start, t, ft, {})
    for k, v in (cols or {}).items():
        fr.cols[k] = [v] * len(ft)
    return fr


def hit(frames, at):
    return next(h for h in st.detect_hitches(frames) if abs(h.t0 - (START + at)) < 1.5)


def calm(t, **kw):
    return sample(t, **kw)


def window(center, **kw):
    """System samples covering a few seconds around `center` seconds into the log."""
    return [sample(START + center + d, **kw) for d in range(-5, 6)]


GAME = {"wine-game": {**app(rss=6_000_000, devs={"sdf": 6000}), "game": "Ready Or Not"}}


class Parsing(unittest.TestCase):
    def write(self, name, text):
        d = Path(tempfile.mkdtemp()); p = d / name; p.write_text(text); return p

    REAL = ("os,cpu,gpu,ram,kernel,driver,cpuscheduler\nLinux,CPU,GPU,16183572,7.3,,powersave\n"
            "fps,frametime,cpu_load,cpu_power,gpu_load,elapsed\n")

    def rows(self, n=40):
        return "".join(f"60,16.6,10,0,50,{(i + 1) * 16_600_000}\n" for i in range(n))

    def test_real_mangohud_layout_with_filename_timestamp(self):
        p = self.write("glxgears_2026-11-04_01-12-07.csv", self.REAL + self.rows())
        fr = st.parse(p)
        self.assertEqual(fr.program, "glxgears")
        self.assertAlmostEqual(fr.start, time.mktime((2026, 11, 4, 1, 12, 7, 0, 0, -1)))
        self.assertAlmostEqual(fr.t[0] - fr.start, 0.0166, places=3)
        self.assertEqual(len(fr.ft), 40)

    def test_program_name_with_underscores_and_dashes(self):
        p = self.write("ReadyOrNot-Win64-Shipping_2026-11-04_01-12-07.csv", self.REAL + self.rows())
        self.assertEqual(st.parse(p).program, "ReadyOrNot-Win64-Shipping")

    def test_truncated_last_line_is_ignored(self):
        p = self.write("g_2026-11-04_01-12-07.csv", self.REAL + self.rows() + "60,16.6,10")
        self.assertEqual(len(st.parse(p).ft), 40)

    def test_summary_file_and_garbage_are_rejected_with_clear_messages(self):
        s = self.write("g_2026-11-04_01-12-07_summary.csv", "0.1% Min FPS,Average FPS\n1,2\n")
        with self.assertRaisesRegex(st.LogError, "summary"):
            st.parse(s)
        with self.assertRaisesRegex(st.LogError, "Not a MangoHud"):
            st.parse(self.write("x.csv", "a,b\n1,2\n"))

    def test_too_few_frames_rejected(self):
        with self.assertRaisesRegex(st.LogError, "too few"):
            st.parse(self.write("g_2026-11-04_01-12-07.csv", self.REAL + self.rows(5)))

    def test_find_logs_excludes_summaries_and_sorts_newest_first(self):
        d = Path(tempfile.mkdtemp())
        old = d / "a_2026-01-01_00-00-00.csv"; old.write_text("x")
        (d / "a_2026-01-01_00-00-00_summary.csv").write_text("x")
        time.sleep(0.02)
        new = d / "b_2026-01-02_00-00-00.csv"; new.write_text("x")
        (d / "notes.csv").write_text("x")
        self.assertEqual(st.find_logs([str(d)]), [new, old])


class Detection(unittest.TestCase):
    def test_steady_frames_have_no_hitches(self):
        self.assertEqual(st.detect_hitches(make_frames()), [])

    def test_ordinary_jitter_is_not_a_hitch(self):
        fr = make_frames(spikes=[(10, 14.0), (20, 17.0), (30, 24.0)])   # under 25 ms, or < 2.5x
        self.assertEqual(st.detect_hitches(fr), [])

    def test_a_single_spike_is_one_hitch(self):
        h = st.detect_hitches(make_frames(spikes=[(20, 120.0)]))
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0].worst_ms, 120.0)

    def test_close_spikes_merge_and_far_ones_do_not(self):
        fr = make_frames(spikes=[(20.0, 90.0), (20.4, 150.0), (40.0, 80.0)])
        h = st.detect_hitches(fr)
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0].worst_ms, 150.0)

    def test_slow_games_are_judged_against_their_own_normal(self):
        fr = make_frames(base_ms=30.0, spikes=[(20, 60.0)])             # 30 fps game: 60 ms is not 2.5x + 12
        self.assertEqual(st.detect_hitches(fr), [])
        self.assertEqual(len(st.detect_hitches(make_frames(base_ms=30.0, spikes=[(20, 130.0)]))), 1)


class Explaining(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(devices, "info", lambda d: {"model": "", "rotational": d == "sdf", "transport": "USB"}),
                  mock.patch.object(devices.os.path, "exists", lambda p: True if "/sys/block/" in p else __import__("os").path.lexists(p))):
            p.start(); self.addCleanup(p.stop)

    def verdict(self, samples, at=30, cols=None, **kw):
        fr = make_frames(spikes=[(at, 150.0)], cols=cols, **kw)
        return st.explain(hit(fr, at), fr, samples)

    def test_game_waiting_on_busy_disk_that_holds_its_files(self):
        s = window(30, io_full=40, disks={"sdf": {"busy": 100, "rd": 5, "wr": 0}},
                   apps={**GAME, "wine-game": {**GAME["wine-game"], "psi": {"io": 45.0, "mem": 0, "cpu": 2}}})
        v = self.verdict(s)
        self.assertEqual((v.cause, v.confidence), ("disk", "high"))
        self.assertTrue(any("game's files are on it" in e for e in v.evidence))

    def test_disk_busy_but_game_has_no_files_there_names_the_other_program(self):
        s = window(30, io_full=40, disks={"sdd": {"busy": 100, "rd": 0, "wr": 9}},
                   apps={**GAME, "torrent": app(wr=90_000_000)})
        v = self.verdict(s)
        self.assertEqual(v.cause, "disk")
        self.assertTrue(any("another program was using it" in e for e in v.evidence))
        self.assertTrue(any("torrent was moving 90 MB/s" in e for e in v.evidence))

    def test_background_cpu_hog(self):
        s = window(30, cpu_some=60, apps={**GAME, "wine-game": {**GAME["wine-game"], "psi": {"io": 0, "mem": 0, "cpu": 40.0}},
                                           "transcode": app(cpu=1300)})
        v = self.verdict(s)
        self.assertEqual((v.cause, v.confidence), ("background_cpu", "high"))
        self.assertIn("transcode was using ~13 cores", v.evidence[0])

    def test_memory_pressure(self):
        v = self.verdict(window(30, mem_some=30, mem_full=10, avail_gb=9, apps=GAME))
        self.assertEqual(v.cause, "memory")

    def test_cpu_throttling(self):
        v = self.verdict(window(30, cpu_util=90, mhz=1500, max_mhz=4500, temp=97, apps=GAME))
        self.assertEqual(v.cause, "thermal")
        self.assertTrue(any("97" in e for e in v.evidence))

    def test_gpu_maxed_out_is_not_called_a_stall(self):
        v = self.verdict(window(30, apps=GAME), cols={"gpu_load": 99.0})
        self.assertEqual(v.cause, "gpu_bound")

    def test_calm_system_late_in_session_is_game_internal_and_low_confidence(self):
        v = self.verdict(window(300, apps=GAME), cols={"gpu_load": 40.0}, at=300, n_sec=400)
        self.assertEqual((v.cause, v.confidence), ("game_internal", "low"))
        self.assertIn("calm", v.evidence[0])

    def test_calm_system_early_in_session_suggests_shader_compilation(self):
        s = window(30, apps=GAME)
        fr = make_frames(spikes=[(30, 150.0)], cols={"gpu_load": 40.0})
        self.assertEqual(st.explain(hit(fr, 30), fr, s).cause, "shader")

    def test_no_recorded_data_is_admitted_not_guessed(self):
        v = self.verdict([])
        self.assertEqual((v.cause, v.confidence), ("unknown", "low"))
        self.assertIn("no data", v.evidence[0])

    def test_samples_far_from_the_hitch_are_not_used(self):
        far = [sample(START + 500 + d, io_full=60, disks={"sdf": {"busy": 100, "rd": 0, "wr": 0}}) for d in range(10)]
        self.assertEqual(self.verdict(far).cause, "unknown")


class Report(unittest.TestCase):
    def test_clean_run_report(self):
        out = st.report(make_frames(), [])
        self.assertIn("No hitches found", out)

    def test_report_summarises_causes_and_fixes(self):
        fr = make_frames(spikes=[(20, 160.0), (40, 110.0)], cols={"gpu_load": 30.0})
        s = window(20, io_full=40, apps=GAME, disks={"sdf": {"busy": 100, "rd": 0, "wr": 0}}) + window(40, apps=GAME)
        out = st.report(fr, s)
        self.assertIn("2 hitch(es)", out)
        self.assertIn("The disk couldn't keep up", out)
        self.assertIn("What to try", out)
        self.assertIn("Move the game to an SSD", out)

    def test_coverage_note_when_some_hitches_have_no_data(self):
        fr = make_frames(spikes=[(20, 160.0), (50, 120.0)])
        out = st.report(fr, window(20, apps=GAME))
        self.assertIn("system data for 1 of 2 hitches", out)


if __name__ == "__main__":
    unittest.main()
