"""Safety checks for the narrow ESP32-S3 OTA-slot recovery path."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "companion_recovery", Path(__file__).resolve().parents[1] / "companion/meshtastic_agent.py"
)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def entry(ptype, subtype, offset, size, label):
    raw = bytearray(32)
    raw[0:2] = (0x50AA).to_bytes(2, "little")
    raw[2:4] = bytes((ptype, subtype))
    raw[4:8] = offset.to_bytes(4, "little")
    raw[8:12] = size.to_bytes(4, "little")
    raw[12:28] = label.encode("ascii")[:16].ljust(16, b"\0")
    return bytes(raw)


def table_bytes(with_md5=True, rows=None):
    data = bytearray(b"\xff" * 0x1000)
    rows = rows or (
        entry(1, 0, 0xE000, 0x2000, "otadata"),
        entry(0, 0x10, 0x10000, 0x330000, "ota_0"),
        entry(0, 0x11, 0x340000, 0x330000, "ota_1"),
    )
    payload = b"".join(rows)
    if with_md5:
        payload += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(payload).digest()
    data[:len(payload)] = payload
    return bytes(data)


class OtaRecoveryTests(unittest.TestCase):
    def test_accepts_meshtastic_s3_ota_layout(self):
        app0, app1, otadata = agent.esp32s3_recovery_geometry(table_bytes())
        self.assertEqual((app0["offset"], app0["size"]), (0x10000, 0x330000))
        self.assertEqual((app1["offset"], app1["size"]), (0x340000, 0x330000))
        self.assertEqual((otadata["offset"], otadata["size"]), (0xE000, 0x2000))

    def test_refuses_missing_or_misplaced_otadata(self):
        table = table_bytes(rows=(
            entry(1, 1, 0xE000, 0x2000, "other"),
            entry(0, 0x10, 0x10000, 0x330000, "ota_0"),
            entry(0, 0x11, 0x340000, 0x330000, "ota_1"),
        ))
        with self.assertRaisesRegex(ValueError, "selection partition"):
            agent.esp32s3_recovery_geometry(table)
        table = table_bytes(rows=(
            entry(1, 0, 0xF000, 0x2000, "otadata"),
            entry(0, 0x10, 0x10000, 0x330000, "ota_0"),
            entry(0, 0x11, 0x340000, 0x330000, "ota_1"),
        ))
        with self.assertRaisesRegex(ValueError, "not the supported"):
            agent.esp32s3_recovery_geometry(table)

    def test_refuses_invalid_partition_table_checksum(self):
        table = bytearray(table_bytes())
        table[3 * 32 + 16] ^= 1
        with self.assertRaisesRegex(ValueError, "checksum is invalid"):
            agent.esp32s3_recovery_geometry(bytes(table))

    def test_recovery_check_requires_valid_app0_and_reports_metadata_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            def read(_port, offset, length, destination, manual_boot=False):
                self.assertTrue(manual_boot)
                if offset == 0x8000:
                    payload = table_bytes()
                elif offset == 0x10000:
                    payload = b"\xe9" + b"\x00" * (length - 1)
                else:
                    payload = b"\x12" * length
                Path(destination).write_bytes(payload)

            with patch.dict(sys.modules, {"esptool": types.ModuleType("esptool")}), \
                    patch("importlib.metadata.version", return_value="5.6.0"), \
                    patch.object(agent, "verify_esp32s3_security_state") as security, \
                    patch.object(agent, "run_esptool_read", side_effect=read):
                result = agent.inspect_esp32s3_previous_app_recovery("/dev/ttyACM0")
        self.assertTrue(result["recoverySafe"])
        self.assertFalse(result["alreadyDefaultBootSelection"])
        self.assertEqual(result["otadataSha256"], hashlib.sha256(b"\x12" * 0x2000).hexdigest())
        security.assert_called_once_with("/dev/ttyACM0", manual_boot=True)

    def test_recovery_erases_only_otadata_and_verifies_erased_bytes(self):
        expected = {"port": "/dev/ttyACM0", "recoverySafe": True,
                    "otadataSha256": "a" * 64,
                    "app0": {"offset": "0x10000", "size": "0x330000"},
                    "otadata": {"offset": "0xe000", "size": "0x2000"}}
        completed = Mock(returncode=0, stdout="erase complete", stderr="")

        def verify_read(_port, offset, length, destination, manual_boot=False):
            self.assertEqual((offset, length, manual_boot), (0xE000, 0x2000, True))
            Path(destination).write_bytes(b"\xff" * length)

        with patch.object(agent, "list_serial_ports", return_value=[{"path": "/dev/ttyACM0"}]), \
                patch.object(agent, "inspect_esp32s3_previous_app_recovery", return_value=expected), \
                patch("subprocess.run", return_value=completed) as run, \
                patch.object(agent, "run_esptool_read", side_effect=verify_read):
            result = agent.recover_esp32s3_previous_app("/dev/ttyACM0", "a" * 64)
        command = run.call_args.args[0]
        self.assertEqual(command[-3:], ["erase-region", "0xe000", "0x2000"])
        self.assertNotIn("write-flash", command)
        self.assertIn("app0 firmware and NVS/settings were not written", result["message"])

    def test_recovery_refuses_changed_selection_or_blank_selection(self):
        status = {"port": "/dev/ttyACM0", "recoverySafe": True, "otadataSha256": "b" * 64}
        with patch.object(agent, "list_serial_ports", return_value=[{"path": "/dev/ttyACM0"}]), \
                patch.object(agent, "inspect_esp32s3_previous_app_recovery", return_value=status), \
                patch("subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "changed since it was checked"):
                agent.recover_esp32s3_previous_app("/dev/ttyACM0", "a" * 64)
            run.assert_not_called()
            status["recoverySafe"] = False
            with self.assertRaisesRegex(ValueError, "already blank"):
                agent.recover_esp32s3_previous_app("/dev/ttyACM0", "b" * 64)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
