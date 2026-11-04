import tempfile, unittest
from pathlib import Path
from whyslow import service as sv


class Rec:
    def __init__(self): self.calls = []
    def __call__(self, cmd, **k): self.calls.append(cmd)


class ServiceTests(unittest.TestCase):
    def test_enable_without_packaged_unit_writes_user_unit_and_tray(self):
        with tempfile.TemporaryDirectory() as d:
            home, run = Path(d), Rec()
            out = sv.enable(home, run, packaged=home / "none.service")
            u = home / ".config/systemd/user/whyslow.service"
            self.assertIn("-m whyslow daemon", u.read_text()); self.assertIn(sv.MARK, u.read_text())
            self.assertIn("gui --minimized", (home / ".config/autostart" / sv.TRAY_NAME).read_text())
            self.assertIn(["systemctl", "--user", "enable", "--now", "whyslow.service"], run.calls)
            self.assertTrue(out)

    def test_enable_with_packaged_unit_writes_no_unit(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); packaged = home / "p.service"; packaged.write_text("x")
            sv.enable(home, Rec(), packaged=packaged)
            self.assertFalse((home / ".config/systemd/user/whyslow.service").exists())

    def test_appimage_unit_starts_the_appimage_not_a_temp_interpreter(self):
        import os
        old = os.environ.get("APPIMAGE")
        os.environ["APPIMAGE"] = "/home/u/My Apps/WhySlow.AppImage"
        try:
            with tempfile.TemporaryDirectory() as d:
                home = Path(d); packaged = home / "p.service"; packaged.write_text("x")
                sv.enable(home, Rec(), packaged=packaged)          # even with a packaged unit: that one is not ours
                unit = (home / ".config/systemd/user/whyslow.service").read_text()
                self.assertIn("ExecStart='/home/u/My Apps/WhySlow.AppImage' daemon", unit)
                self.assertNotIn("PYTHONPATH", unit)
                self.assertIn("'/home/u/My Apps/WhySlow.AppImage' gui --minimized",
                              (home / ".config/autostart" / sv.TRAY_NAME).read_text())
        finally:
            os.environ.pop("APPIMAGE", None)
            if old: os.environ["APPIMAGE"] = old

    def test_clean_appimage_env_strips_only_bundled_paths(self):
        env = {"APPDIR": "/tmp/.mount_x", "LD_LIBRARY_PATH": "/tmp/.mount_x/usr/lib:/opt/cuda/lib",
               "PYTHONHOME": "/tmp/.mount_x/usr", "PYTHONPATH": "/tmp/.mount_x/usr/lib/python3.12/site-packages"}
        self.assertTrue(sv.clean_appimage_env(env))
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/cuda/lib")
        self.assertNotIn("PYTHONHOME", env); self.assertNotIn("PYTHONPATH", env)
        env2 = {"APPDIR": "/a", "LD_LIBRARY_PATH": "/a/usr/lib"}
        sv.clean_appimage_env(env2); self.assertNotIn("LD_LIBRARY_PATH", env2)

    def test_clean_env_noop_outside_appimage(self):
        env = {"LD_LIBRARY_PATH": "/x", "PYTHONPATH": "/y"}
        self.assertFalse(sv.clean_appimage_env(env)); self.assertEqual(env, {"LD_LIBRARY_PATH": "/x", "PYTHONPATH": "/y"})
        env = {"APPDIR": "/a", "PYTHONPATH": "/user/own"}
        sv.clean_appimage_env(env); self.assertEqual(env["PYTHONPATH"], "/user/own")

    def test_no_tray(self):
        with tempfile.TemporaryDirectory() as d:
            sv.enable(Path(d), Rec(), packaged=Path(d) / "n", tray=False)
            self.assertFalse((Path(d) / ".config/autostart").exists())

    def test_disable_removes_only_our_files_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            sv.enable(home, Rec(), packaged=home / "n")
            data = home / ".local/state/whyslow"; data.mkdir(parents=True); (data / "samples.jsonl").write_text("x")
            out = sv.disable(home, Rec())
            self.assertFalse((home / ".config/systemd/user/whyslow.service").exists())
            self.assertFalse((home / ".config/autostart" / sv.TRAY_NAME).exists())
            self.assertTrue((data / "samples.jsonl").exists()); self.assertTrue(any("kept" in o for o in out))

    def test_disable_leaves_a_hand_written_unit(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); u = home / ".config/systemd/user/whyslow.service"
            u.parent.mkdir(parents=True); u.write_text("[Unit]\nDescription=mine\n")
            sv.disable(home, Rec())
            self.assertTrue(u.exists())


if __name__ == "__main__":
    unittest.main()
