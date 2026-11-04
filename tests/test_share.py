import unittest
from whyslow import share as sh
from whyslow.share import Redactor, LeakError


def R(**k):
    d = dict(user="alice", realname="Alice Example", host="workstation-pc", home="/home/alice")
    d.update(k)
    return Redactor(**d)


class RedactTests(unittest.TestCase):
    def test_username_home_host_name(self):
        t = R().apply("owner alice at workstation-pc, file /home/alice/Games/x, Alice Example, ALICE")
        for bad in ("alice", "workstation-pc", "Michael", "Gordon", "/home"):
            self.assertNotIn(bad.lower(), t.lower())
        self.assertIn("~/Games/x", t)

    def test_distro_word_in_hostname_not_over_redacted(self):
        self.assertIn("Nobara Linux", R().apply("Nobara Linux 44 on workstation-pc"))

    def test_mount_labels_and_hex_ids(self):
        t = R().apply("/run/media/alice/0123456789ABCDEF and /media/alice/My Secret Drive and /mnt/photos-2019/x")
        self.assertNotIn("0123", t); self.assertNotIn("photos", t); self.assertNotIn("alice", t)
        self.assertIn("<drive>", t)

    def test_systemd_unit_not_an_email(self):
        t = R().apply("nobara-automount@run-media-alice-0123456789ABCDEF.mount.service took 2s")
        self.assertNotIn("<email>", t); self.assertNotIn("alice", t); self.assertNotIn("0123456789ABCDEF", t)
        self.assertIn("nobara-automount", t)

    def test_real_email_removed(self):
        self.assertEqual(R().apply("mail bob.smith+x@example.co.uk now"), "mail <email> now")

    def test_uuid_mac_ip(self):
        t = R().apply("cc041ad6-4e22-4c52-a351-cc666bfe58c2 aa:bb:cc:dd:ee:ff 192.168.1.20 fe80:0:0:0:1:2:3:4")
        self.assertEqual(t, "<uuid> <mac> <ip> <ip>")

    def test_loopback_and_versions_kept(self):
        t = R().apply("127.0.0.1 kernel 7.3.1-201 driver 615.78.08 x86_64")
        self.assertIn("127.0.0.1", t); self.assertIn("615.78.08", t)

    def test_serial_and_by_id(self):
        t = R().apply("serial: X1CA347QKATK and /dev/disk/by-id/ata-KINGSTON_X1CA347QKATK-part1")
        self.assertNotIn("X1CA347QKATK", t)

    def test_short_username_only_in_obvious_places(self):
        r = R(user="al", realname="", host="")
        t = r.apply("/home/al/x and /run/media/al/foo and al@pc, but also total, algorithm, and 'al' words")
        self.assertIn("total", t); self.assertIn("algorithm", t)
        self.assertNotIn("/al/", t); self.assertNotIn("al@", t)

    def test_strict_masks_unknown_apps_keeps_known(self):
        r = R(hide_apps={"mysecretapp", "photoeditor"}, keep={"photoeditor"})
        t = r.apply("mysecretapp used 3 cores; photoeditor ok; mysecretapp-helper stays")
        self.assertNotIn("mysecretapp used", t); self.assertIn("app-1 used", t); self.assertIn("photoeditor ok", t)
        self.assertIn("mysecretapp-helper", t)      # not a word-boundary match: different program


class HelperTests(unittest.TestCase):
    def test_disk_label(self):
        self.assertIsNone(sh.disk_label("sdi", dict(usb=True, rotational=False), 0))
        self.assertEqual(sh.disk_label("nvme0n1", dict(usb=False, rotational=False), 1024), "NVMe SSD 1024 GB")
        self.assertEqual(sh.disk_label("sdf", dict(usb=True, rotational=True), 64), "USB drive 64 GB")
        self.assertEqual(sh.disk_label("sda", dict(usb=False, rotational=True), 640), "HDD 640 GB")
        self.assertEqual(sh.disk_label("sdb", dict(usb=False, rotational=False), 500), "SATA SSD 500 GB")

    def test_apps_to_hide(self):
        known = {"baloo_file": 1}
        self.assertEqual(sh.apps_to_hide({"baloo_file", "Browser", "secret"}, known.get), {"secret"})

    def test_replacement_labels(self):
        r = R()
        self.assertEqual(r.apply("workstation-pc"), "host"); self.assertEqual(r.apply("Alice Example"), "name")
        self.assertEqual(r.apply("alice"), "user")

    def test_short_name_leaves_ordinary_words(self):
        r = R(user="me", realname="", host="")
        self.assertEqual(r.apply("send it to me"), "send it to me")
        self.assertIn("/user/", r.apply("/srv/me/x"))

    def test_loopback_kept_exact(self):
        self.assertEqual(R().apply("0.0.0.0 127.0.0.1"), "0.0.0.0 127.0.0.1")


class LeakTests(unittest.TestCase):
    def test_leaks_detected(self):
        self.assertEqual(R().leaks("hello alice"), ["username"])
        self.assertEqual(R().leaks("on workstation-pc"), ["hostname"])
        self.assertEqual(R().leaks("clean text"), [])

    def test_compose_fail_closed(self):
        class Bad(Redactor):
            def apply(self, text): return text        # a broken redactor must not leak
        with self.assertRaises(LeakError):
            sh.compose({"system": "user is alice"}, Bad("alice", "", "", "/home/alice"))

    def test_compose_clean(self):
        out = sh.compose({"system": "OS: X\nowner: alice", "boot": "fast"}, R())
        self.assertIn("## System", out); self.assertIn("## Boot time", out); self.assertNotIn("alice", out)
        self.assertNotIn("## Battery", out)             # empty sections omitted

    def test_compose_section_order_and_fences(self):
        out = sh.compose({"boot": "b", "system": "s"}, R())
        self.assertLess(out.index("## System"), out.index("## Boot time"))
        self.assertEqual(out.count("```"), 4)


class FactsTests(unittest.TestCase):
    def test_system_facts_has_no_identifiers(self):
        r = Redactor.for_current_user()
        text = "\n".join(sh.system_facts())
        self.assertEqual(r.leaks(text), [])
        self.assertIn("Kernel:", text)


if __name__ == "__main__":
    unittest.main()
