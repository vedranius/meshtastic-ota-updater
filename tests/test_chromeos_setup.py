import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ChromeOsSetupTests(unittest.TestCase):
    def test_chromeos_launcher_uses_default_pages_origin_and_private_environment(self):
        script = (ROOT / "companion" / "start-chromeos.sh").read_text()
        self.assertIn("python3 -m venv .venv", script)
        self.assertIn("pip install --upgrade pip meshtastic esptool", script)
        self.assertIn("--origin \"https://meshtastic-ota-updater.pages.dev\"", script)
        self.assertIn("ChromeOS Chromebook", (ROOT / "companion" / "README.txt").read_text())

    def test_chromeos_ui_offers_copyable_setup_and_sets_usb_expectations(self):
        page = (ROOT / "public" / "index.html").read_text()
        self.assertIn("/(CrOS)/i.test(navigator.userAgent)", page)
        self.assertIn("copy-chromeos-command", page)
        self.assertIn("bash start-chromeos.sh", page)
        self.assertIn("ChromeOS does not pass every USB serial device through to Linux.", page)
        self.assertIn("ChromeOS ne prosljeđuje svaki USB serijski uređaj Linuxu.", page)


if __name__ == "__main__":
    unittest.main()
