import unittest

from whyslow.games import detect


class GameDetection(unittest.TestCase):
    def test_steam_library_path_gives_the_game_folder_name(self):
        self.assertEqual(detect("/home/u/.steam/steamapps/common/Hades II/Ship/Hades2.exe"), "Hades II")
        self.assertEqual(detect(r"Z:\home\u\.steam\steamapps\common\Ready Or Not\ReadyOrNot.exe"), "Ready Or Not")
        self.assertEqual(detect("/mnt/games/SteamLibrary/steamapps/common/Factorio/bin/x64/factorio"), "Factorio")

    def test_windows_game_exe_under_wine_proton(self):
        self.assertEqual(detect(r"C:\Games\Foo\ReadyOrNot-Win64-Shipping.exe -windowed"), "ReadyOrNot-Win64-Shipping")
        self.assertEqual(detect("wine64 /home/u/Games/Bar/bar.exe"), "bar")

    def test_proton_and_wine_helpers_are_not_games(self):
        for cmd in [r"C:\windows\system32\winedevice.exe", r"C:\windows\system32\explorer.exe /desktop",
                    r"C:\windows\system32\services.exe", r"Z:\...\Proton-GE\files\share\xalia\xalia.exe",
                    r"C:\windows\system32\conhost.exe", r"C:\windows\system32\plugplay.exe",
                    r"C:\Program Files (x86)\Steam\steam.exe -silent", "wineserver -p"]:
            self.assertIsNone(detect(cmd), cmd)

    def test_installers_and_repack_helpers_are_not_games(self):
        for cmd in ["/home/u/Downloads/Ready or Not [Repack]/setup.exe", "rtool.exe decode -t100p - -",
                    r"C:\users\steamuser\Temp\is-ABC.tmp\setup.tmp /SL5", "unins000.exe /SILENT",
                    "CrashReportClient.exe", "UnityCrashHandler64.exe", "EasyAntiCheat_EOS.exe"]:
            self.assertIsNone(detect(cmd), cmd)

    def test_ordinary_programs_and_empty_input(self):
        for cmd in ["", "/usr/bin/firefox", "python3 -m whyslow gui", "bash -c echo", "/usr/lib/kf6/baloo_file"]:
            self.assertIsNone(detect(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
