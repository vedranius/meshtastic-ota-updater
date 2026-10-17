"""Exercise HTTP operation conflicts without connecting to or flashing hardware."""
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


spec = importlib.util.spec_from_file_location(
    "companion", Path(__file__).resolve().parents[1] / "companion/meshtastic_agent.py"
)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class QuietHandler(agent.Handler):
    def log_message(self, *_args):
        pass


class OperationTests(unittest.TestCase):
    def setUp(self):
        agent.DEVICE_OPERATIONS.clear()
        self.server = agent.http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.server.allowed_origins = {agent.DEFAULT_ORIGIN}
        self.server.access_token = ""
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertEqual(agent.DEVICE_OPERATIONS, {})

    def request(self, path, fields=None):
        headers = {"Origin": agent.DEFAULT_ORIGIN}
        data = None
        if fields is not None:
            boundary = "companion-regression-boundary"
            data = b""
            for name, value in fields.items():
                data += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                         f'{value}\r\n').encode()
            data += f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        try:
            response = urlopen(Request(self.base + path, data=data, headers=headers), timeout=4)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_identity_blocks_second_identity_settings_and_ota_but_not_other_nodes(self):
        entered, release = threading.Event(), threading.Event()
        first_result = []

        def identify(target):
            if target == "192.168.1.20":
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("Test did not release its identity operation")
            return {"ip": target, "hardware": "HELTEC_WIRELESS_TRACKER"}

        iface = Mock()
        with patch.object(agent, "identify_meshtastic", side_effect=identify), \
                patch.object(agent, "open_device", return_value=iface), \
                patch.object(agent, "device_configuration", return_value={"local": {}}), \
                patch.object(agent, "read_firmware", return_value=b"fake image"), \
                patch.object(agent, "ota_upload") as upload:
            worker = threading.Thread(target=lambda: first_result.append(
                self.request("/api/identify?ip=192.168.1.20")))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                for path, fields in (
                    ("/api/identify?ip=192.168.1.20", None),
                    ("/api/config/read?transport=tcp&target=192.168.1.20", None),
                    ("/api/update", {"ip": "192.168.1.20"}),
                ):
                    status, body = self.request(path, fields)
                    self.assertEqual(status, 409)
                    self.assertEqual(body["code"], "device_busy")
                self.assertEqual(self.request("/api/identify?ip=192.168.1.21")[0], 200)
                upload.assert_not_called()
                iface.close.assert_not_called()
            finally:
                release.set()
                worker.join(4)
            self.assertEqual(first_result[0][0], 200)
            status, _body = self.request("/api/config/read?transport=tcp&target=192.168.1.20")
            self.assertEqual(status, 200)
            iface.close.assert_called_once()

    def test_usb_loader_check_blocks_identification_and_repair_on_same_port(self):
        entered, release = threading.Event(), threading.Event()
        result = []

        def inspect(*_args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Test did not release its USB operation")
            return {"port": "/dev/ttyACM0", "patchedLoaderInstalled": True}

        with patch.object(agent, "inspect_esp32s3_ota_loader", side_effect=inspect), \
                patch.object(agent, "usb_meshtastic_identity") as identify, \
                patch.object(agent, "repair_esp32s3_ota_loader") as repair:
            worker = threading.Thread(target=lambda: result.append(
                self.request("/api/usb/ota-loader/status?port=/dev/ttyACM0")))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.request("/api/usb/identify?port=/dev/ttyACM0")[0], 409)
                self.assertEqual(self.request("/api/usb/repair-ota-loader", {"port": "/dev/ttyACM0"})[0], 409)
                identify.assert_not_called()
                repair.assert_not_called()
            finally:
                release.set()
                worker.join(4)
            self.assertEqual(result[0][0], 200)

    def test_failed_operation_releases_the_device(self):
        with patch.object(agent, "identify_meshtastic", side_effect=RuntimeError("handshake failed")):
            self.assertEqual(self.request("/api/identify?ip=192.168.1.20")[0], 400)
        with patch.object(agent, "identify_meshtastic", return_value={"verified": True}):
            self.assertEqual(self.request("/api/identify?ip=192.168.1.20")[0], 200)

    def test_ota_keeps_device_reserved_until_the_upload_finishes(self):
        entered, release = threading.Event(), threading.Event()
        result = []

        def upload(*_args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Test did not release its simulated upload")
            return {"message": "simulated upload complete"}

        with patch.object(agent, "read_firmware", return_value=b"fake image"), \
                patch.object(agent, "ota_upload", side_effect=upload), \
                patch.object(agent, "identify_meshtastic") as identify:
            worker = threading.Thread(target=lambda: result.append(
                self.request("/api/update", {"ip": "192.168.1.20"})))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.request("/api/identify?ip=192.168.1.20")[0], 409)
                self.assertEqual(self.request("/api/config/read?transport=tcp&target=192.168.1.20")[0], 409)
                identify.assert_not_called()
            finally:
                release.set()
                worker.join(4)
            self.assertEqual(result[0][0], 200)

    def test_previous_app_recovery_refuses_without_explicit_rom_mode(self):
        request = Request(
            self.base + "/api/usb/ota-recovery/restore",
            data=json.dumps({"port": "/dev/ttyACM0", "manual_boot": False}).encode(),
            headers={"Origin": agent.DEFAULT_ORIGIN, "Content-Type": "application/json"},
        )
        with patch.object(agent, "recover_esp32s3_previous_app") as recover:
            try:
                response = urlopen(request, timeout=4)
            except HTTPError as error:
                response = error
            with response:
                self.assertEqual(response.status, 400)
                self.assertIn("ROM download mode", json.load(response)["error"])
            recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
