"""Exercise the unified OTA protocol using local socket pairs only."""
import hashlib
import importlib.util
import io
from pathlib import Path
import socket
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "companion_ota", Path(__file__).resolve().parents[1] / "companion/meshtastic_agent.py"
)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def meshtastic_stubs():
    root = types.ModuleType("meshtastic")
    root.__path__ = []
    protobuf = types.ModuleType("meshtastic.protobuf")
    admin = types.ModuleType("meshtastic.protobuf.admin_pb2")
    admin.OTAMode = types.SimpleNamespace(OTA_WIFI=1)
    protobuf.admin_pb2 = admin
    tcp = types.ModuleType("meshtastic.tcp_interface")
    tcp.TCPInterface = Mock
    root.protobuf = protobuf
    root.tcp_interface = tcp
    return {
        "meshtastic": root,
        "meshtastic.protobuf": protobuf,
        "meshtastic.protobuf.admin_pb2": admin,
        "meshtastic.tcp_interface": tcp,
    }


class UnifiedOtaProtocolTests(unittest.TestCase):
    def _invoke(self, data, server_behavior, connect_side_effect=None):
        client, server = socket.socketpair()
        iface = Mock()
        iface.localNode.startOTA = Mock()
        logs = io.StringIO()
        errors = []

        def run_server():
            try:
                with server, server.makefile("rb") as reader:
                    header = reader.readline().decode("ascii").strip()
                    server_behavior(server, reader, header)
            except Exception as error:  # surface thread failures in the test
                errors.append(error)

        worker = threading.Thread(target=run_server, daemon=True)
        worker.start()
        connect = connect_side_effect or (lambda *_args, **_kwargs: client)
        result = None
        invocation_error = None
        with patch.dict(sys.modules, meshtastic_stubs()), \
                patch.object(agent.socket, "create_connection", side_effect=connect), \
                patch.object(agent.time, "sleep", return_value=None), \
                patch("builtins.print", lambda *args, **kwargs: logs.write(" ".join(map(str, args)) + "\n")):
            try:
                result = agent.unified_ota_upload("192.0.2.10", data, iface=iface)
            except Exception as error:
                invocation_error = error
        worker.join(2)
        self.assertFalse(worker.is_alive(), "simulated OTA peer did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(iface.close.call_count, 1)
        if invocation_error is not None:
            raise invocation_error
        return result, logs.getvalue(), iface

    def test_transfer_waits_for_erase_ready_and_final_verification(self):
        image = b"simulated Meshtastic image bytes" * 100
        received = bytearray()

        def peer(conn, _reader, header):
            self.assertEqual(header, f"OTA {len(image)} {hashlib.sha256(image).hexdigest()}")
            conn.sendall(b"ERASING\n")
            conn.sendall(b"OK\n")
            while len(received) < len(image):
                received.extend(conn.recv(4096))
            self.assertEqual(bytes(received), image)
            conn.sendall(b"OK\n")

        result, logs, iface = self._invoke(image, peer)
        self.assertIn("verified the image", result["message"])
        self.assertEqual(iface.localNode.startOTA.call_args.kwargs["ota_file_hash"], hashlib.sha256(image).digest())
        self.assertIn("ERASING", logs)
        self.assertIn("Sent ", logs)

    def test_loader_rejection_is_reported_before_any_image_transfer(self):
        image = b"test image"
        received_after_error = []

        def peer(conn, reader, header):
            self.assertTrue(header.startswith(f"OTA {len(image)} "))
            conn.sendall(b"ERR Hash Rejected\n")
            conn.settimeout(0.1)
            try:
                received_after_error.append(conn.recv(64))
            except TimeoutError:
                received_after_error.append(b"")

        with self.assertRaisesRegex(RuntimeError, "rejected the image before transfer"):
            self._invoke(image, peer)
        self.assertEqual(received_after_error, [b""])

    def test_connection_retry_happens_before_header_only(self):
        image = b"retry test"
        client, server = socket.socketpair()
        peer_errors = []

        def peer():
            try:
                with server, server.makefile("rb") as reader:
                    header = reader.readline().decode("ascii").strip()
                    self.assertTrue(header.startswith(f"OTA {len(image)} "))
                    server.sendall(b"ERR test stop\n")
            except Exception as error:
                peer_errors.append(error)

        worker = threading.Thread(target=peer, daemon=True)
        # This path verifies that a temporary refused connection gets retried;
        # the simulated second connection then rejects before sending data.
        worker.start()
        iface = Mock()
        iface.localNode.startOTA = Mock()
        refusal = ConnectionRefusedError(111, "connection refused")
        with patch.dict(sys.modules, meshtastic_stubs()), \
                patch.object(agent.socket, "create_connection", side_effect=[refusal, client]), \
                patch.object(agent.time, "sleep", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "rejected the image"):
                agent.unified_ota_upload("192.0.2.10", image, iface=iface)
        worker.join(1)
        self.assertEqual(peer_errors, [])
        self.assertEqual(iface.close.call_count, 1)


if __name__ == "__main__":
    unittest.main()
