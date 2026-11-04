import unittest
from whyslow import hwcheck as h
from whyslow.hwcheck import Snapshot, UsbDisk


def usb(**k):
    d = dict(disk="sdx", product="Drive", mbps=5000, driver="uas", rotational=False, has_media=True)
    d.update(k)
    return UsbDisk(**d)


class UsbTests(unittest.TestCase):
    def test_fast_link_not_flagged(self):          # real case: My Passport at 5000 Mbps
        f, ok = h.check_usb_speed(Snapshot(usb=[usb(rotational=True, driver="usb-storage")]))
        self.assertEqual(f, []); self.assertIn("USB 3 link", ok[0])

    def test_usb2_device_on_usb2_not_flagged(self):
        f, _ = h.check_usb_speed(Snapshot(usb=[usb(mbps=480, driver="usb-storage", product="Card Reader")]))
        self.assertEqual(f, [])

    def test_uas_on_slow_link_flagged(self):
        f, _ = h.check_usb_speed(Snapshot(usb=[usb(mbps=480)]))
        self.assertEqual(f[0].id, "usb-slow-link"); self.assertEqual(f[0].disks, ["sdx"])

    def test_name_hint_flagged(self):
        f, _ = h.check_usb_speed(Snapshot(usb=[usb(mbps=480, driver="usb-storage", product="USB 3.0 SSD")]))
        self.assertEqual(len(f), 1)

    def test_empty_reader_ignored(self):
        f, ok = h.check_usb_speed(Snapshot(usb=[usb(mbps=480, has_media=False)]))
        self.assertEqual((f, ok), ([], []))

    def test_root_hub_not_blamed(self):
        f, _ = h.check_usb_speed(Snapshot(usb=[usb(mbps=480, chain=[("xHCI Host Controller", 480)])]))
        self.assertFalse(any("hub" in e for e in f[0].evidence))

    def test_real_hub_blamed(self):
        f, _ = h.check_usb_speed(Snapshot(usb=[usb(mbps=480, chain=[("USB2.0 Hub", 480)])]))
        self.assertTrue(any("USB2.0 Hub" in e for e in f[0].evidence))


class LinkTests(unittest.TestCase):
    def test_nvme_narrow_high(self):
        f, _ = h.check_pcie_links(Snapshot(nvme_links=[dict(name="nvme0", speed=16, width=2, max_speed=16, max_width=4)]))
        self.assertEqual((f[0].id, f[0].confidence), ("nvme-narrow-link", "high"))

    def test_nvme_full_ok(self):
        f, ok = h.check_pcie_links(Snapshot(nvme_links=[dict(name="nvme0", speed=16, width=4, max_speed=16, max_width=4)]))
        self.assertEqual(f, []); self.assertTrue(ok)

    def test_nvme_slow_speed_only_low(self):
        f, _ = h.check_pcie_links(Snapshot(nvme_links=[dict(name="nvme0", speed=2.5, width=4, max_speed=16, max_width=4)]))
        self.assertEqual(f[0].confidence, "low")

    def test_sata_ssd_3gbps_flagged_but_6_ok(self):
        self.assertEqual(h.check_sata_ssd(Snapshot(sata_ssds=[dict(disk="sda", gbps=3.0)]))[0][0].id, "sata-ssd-slow-link")
        self.assertEqual(h.check_sata_ssd(Snapshot(sata_ssds=[dict(disk="sda", gbps=6.0)]))[0], [])


class CpuTests(unittest.TestCase):
    def test_intel_pstate_powersave_fine(self):     # real machine
        f, _ = h.check_cpu_power(Snapshot(cpu=dict(driver="intel_pstate", governor="powersave", epp="performance")))
        self.assertEqual(f, [])

    def test_acpi_powersave_flagged(self):
        f, _ = h.check_cpu_power(Snapshot(cpu=dict(driver="acpi-cpufreq", governor="powersave")))
        self.assertEqual(f[0].id, "cpu-power-limited")

    def test_no_turbo_flagged(self):
        f, _ = h.check_cpu_power(Snapshot(cpu=dict(driver="intel_pstate", governor="powersave", no_turbo=True)))
        self.assertEqual(len(f), 1)

    def test_power_saver_ok_on_battery_not_on_ac(self):
        c = dict(driver="intel_pstate", governor="powersave", profile="power-saver")
        self.assertEqual(h.check_cpu_power(Snapshot(cpu=c, laptop=True, on_ac=False))[0], [])
        self.assertEqual(len(h.check_cpu_power(Snapshot(cpu=c, laptop=True, on_ac=True))[0]), 1)


class MiscTests(unittest.TestCase):
    def test_ntfs_fuse_flagged_rw_only(self):
        m = dict(mp="/mnt/a", disk="sdb", fstype="fuseblk", fs="ntfs", opts="", ro=False)
        self.assertEqual(h.check_ntfs(Snapshot(mounts=[m]))[0][0].id, "ntfs-fuse")
        self.assertEqual(h.check_ntfs(Snapshot(mounts=[dict(m, ro=True)]))[0], [])
        self.assertEqual(h.check_ntfs(Snapshot(mounts=[dict(m, fstype="ntfs3")]))[0], [])

    def test_exfat_fuse_not_ntfs(self):
        m = dict(mp="/mnt/a", disk="sdb", fstype="fuseblk", fs="exfat", opts="", ro=False)
        self.assertEqual(h.check_ntfs(Snapshot(mounts=[m]))[0], [])

    def test_swap_on_hdd(self):
        s = Snapshot(swaps=[dict(name="/dev/sda2", disk="sda", kind="partition")], disks={"sda": dict(rotational=True)})
        self.assertEqual(h.check_swap(s)[0][0].id, "swap-on-hdd")

    def test_zram_fine_and_no_swap_flagged_on_small_ram(self):
        self.assertEqual(h.check_swap(Snapshot(swaps=[dict(name="/dev/zram0", disk=None, kind="zram")]))[0], [])
        self.assertEqual(h.check_swap(Snapshot(mem_total_gb=8))[0][0].id, "no-swap")
        self.assertEqual(h.check_swap(Snapshot(mem_total_gb=64))[0], [])

    def test_root_and_steam_on_hdd(self):
        s = Snapshot(root_disk="sda", disks={"sda": dict(rotational=True), "sdb": dict(rotational=False)},
                     steam_libs=[dict(path="/g", disk="sda")])
        self.assertEqual({f.id for f in h.check_root_and_games(s)[0]}, {"root-on-hdd", "steam-on-hdd"})
        s2 = Snapshot(root_disk="sdb", disks={"sdb": dict(rotational=False)}, steam_libs=[dict(path="/g", disk="sdb")])
        self.assertEqual(h.check_root_and_games(s2)[0], [])

    def test_nvme_hot(self):
        self.assertEqual(h.check_nvme_temp(Snapshot(nvme_temps={"nvme0": 78.0}))[0][0].id, "nvme-hot")
        self.assertEqual(h.check_nvme_temp(Snapshot(nvme_temps={"nvme0": 45.0}))[0], [])

    def test_dirty_only_with_slow_usb(self):
        vm = dict(dirty_ratio=20, dirty_bytes=0)
        self.assertEqual(h.check_dirty(Snapshot(usb=[usb()], vm=vm, mem_total_gb=15))[0], [])
        self.assertEqual(h.check_dirty(Snapshot(usb=[usb(mbps=480)], vm=vm, mem_total_gb=15))[0][0].id, "dirty-buffers")
        self.assertEqual(h.check_dirty(Snapshot(usb=[usb(mbps=480)], vm=dict(vm, dirty_bytes=1), mem_total_gb=15))[0], [])
        self.assertEqual(h.check_dirty(Snapshot(usb=[usb(mbps=480)], vm=vm, mem_total_gb=2))[0], [])

    def test_trim(self):
        base = dict(disks={"sda": dict(rotational=False, discard_max=1, usb=False, mounted=True)})
        self.assertEqual(h.check_trim(Snapshot(fstrim_timer=False, **base))[0][0].id, "no-trim")
        self.assertEqual(h.check_trim(Snapshot(fstrim_timer=True, **base))[0], [])
        u = Snapshot(disks={"sdb": dict(rotational=False, discard_max=0, usb=True, mounted=True)})
        self.assertEqual(h.check_trim(u)[0][0].id, "usb-ssd-no-trim")

    def test_broken_check_does_not_hide_others(self):
        s = Snapshot(nvme_temps={"nvme0": 80.0})
        orig = h.CHECKS
        try:
            h.CHECKS = [lambda s: 1 / 0] + orig
            f, ok = h.run_checks(s)
        finally:
            h.CHECKS = orig
        self.assertTrue(any(x.id == "nvme-hot" for x in f)); self.assertTrue(any("failed" in o for o in ok))

    def test_findings_for_disk(self):
        f = [h.Finding("a", "warn", "t", disks=["sda"]), h.Finding("b", "warn", "t", disks=["sdb"])]
        self.assertEqual([x.id for x in h.findings_for_disk("sda", f)], ["a"])


if __name__ == "__main__":
    unittest.main()
