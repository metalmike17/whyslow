import unittest
from unittest import mock

from test_analyze import app, sample
from whyslow import devices
from whyslow.analyze import analyze, describe


def with_psi(io=0.0, mem=0.0, cpu_wait=0.0, game=None, **kw):
    e = app(**kw)
    e["psi"] = {"io": io, "iof": 0, "mem": mem, "memf": 0, "cpu": cpu_wait}
    if game:
        e["game"] = game
    return e


def incident(apps, n=8, **kw):
    return analyze([sample(t, **kw, apps=apps) for t in range(n)])[0]


class Victims(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(devices, "info", lambda d: {"model": "", "rotational": d == "sdf", "transport": "USB"}),
                  mock.patch.object(devices, "faster_targets", lambda *a, **k: [{"disk": "nvme0n1", "mount": "/home", "free_gb": 500.0, "label": "x"}]),
                  mock.patch.object(devices.os.path, "exists", lambda p: True if "/sys/block/" in p else __import__("os").path.lexists(p))):
            p.start(); self.addCleanup(p.stop)

    def test_app_whose_own_tasks_waited_on_io_is_a_victim(self):
        ep = incident({"writer": app(wr=90_000_000), "editor": with_psi(io=35.0)}, io_full=40)
        self.assertEqual([v.app for v in ep.victims], ["editor"])
        self.assertAlmostEqual(ep.victims[0].waits["io"], 35.0)
        text = "\n".join(describe(ep))
        self.assertIn("Hit hardest", text)
        self.assertIn("editor: stalled waiting on the disk 35% of the time", text)

    def test_small_waits_are_not_victims(self):
        ep = incident({"writer": app(wr=90_000_000), "editor": with_psi(io=3.0, mem=1.0, cpu_wait=5.0)}, io_full=40)
        self.assertEqual(ep.victims, [])
        self.assertNotIn("Hit hardest", "\n".join(describe(ep)))

    def test_a_cpu_hog_is_not_a_victim_of_its_own_contention(self):
        ep = incident({"hog": with_psi(cpu_wait=60.0, cpu=1400.0), "meek": with_psi(cpu_wait=50.0, cpu=20.0)},
                      cpu_some=70)
        self.assertEqual([v.app for v in ep.victims], ["meek"])

    def test_games_are_listed_first_and_labelled(self):
        ep = incident({"w": app(wr=90_000_000), "editor": with_psi(io=60.0), "wine-game": with_psi(io=20.0, game="Ready Or Not")},
                      io_full=40)
        self.assertEqual(ep.victims[0].app, "wine-game")
        self.assertEqual(ep.victims[0].label, "wine-game (game: Ready Or Not)")

    def test_at_most_three_victims(self):
        apps = {f"a{i}": with_psi(io=30.0 + i) for i in range(6)}
        apps["w"] = app(wr=90_000_000)
        self.assertEqual(len(incident(apps, io_full=40).victims), 3)

    def test_samples_without_psi_data_still_work(self):
        self.assertEqual(incident({"w": app(wr=90_000_000)}, io_full=40).victims, [])

    def test_game_on_saturated_spinning_disk_gets_a_move_to_ssd_suggestion(self):
        apps = {"wine-game": with_psi(io=40.0, game="Ready Or Not", devs={"sdf": 6000})}
        ep = incident(apps, io_full=40, disks={"sdf": {"busy": 100, "rd": 5, "wr": 0}})
        adv = " ".join(ep.suggestions)
        self.assertIn("Ready Or Not spent 40% of its time waiting on I/O", adv)
        self.assertIn("Moving the game to an SSD", adv)
        self.assertIn("/home", adv)

    def test_no_move_advice_when_the_game_is_on_an_ssd_or_disk_is_idle(self):
        fast = incident({"wine-game": with_psi(io=40.0, game="G", devs={"nvme0n1": 6000})},
                        io_full=40, disks={"nvme0n1": {"busy": 100, "rd": 0, "wr": 0}})
        idle = incident({"wine-game": with_psi(io=40.0, game="G", devs={"sdf": 6000})},
                        io_full=40, disks={"sdf": {"busy": 5, "rd": 0, "wr": 0}})
        for ep in (fast, idle):
            self.assertFalse(any("Moving the game" in s for s in ep.suggestions))

    def test_no_move_advice_for_non_games(self):
        ep = incident({"editor": with_psi(io=40.0, devs={"sdf": 6000})},
                      io_full=40, disks={"sdf": {"busy": 100, "rd": 0, "wr": 0}})
        self.assertFalse(any("spent" in s and "waiting on I/O" in s for s in ep.suggestions))


if __name__ == "__main__":
    unittest.main()
