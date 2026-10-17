#!/usr/bin/env python3
"""Local companion for Meshtastic OTA Studio. Python 3.10+, standard library only."""
import argparse
from contextlib import contextmanager
import errno
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import time
import tempfile
import zipfile
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

VERSION = "0.4.6"
DEFAULT_ORIGIN = "https://meshtastic-ota-updater.pages.dev"
USB_IDENTITY_CACHE = {}
DEVICE_OPERATIONS = {}
DEVICE_OPERATIONS_LOCK = threading.Lock()
MAX_REQUEST = 120 * 1024 * 1024
MAX_IMAGE = 24 * 1024 * 1024
ESP32S3_OTA_LOADER_SIZE = 636_496
ESP32S3_OTA_LOADER_SHA256 = "599369c8971dbb060b489f2b0ffd5af73bd2f5ca98946f855aad807dc70fe7a9"
ESP32S3_MAX_FLASH_SIZE = 0x1000000
LOCAL_CONFIG_SECTIONS = ("device", "position", "power", "network", "display", "lora", "bluetooth")
WRITABLE_MODULE_CONFIGS = ("mqtt", "serial", "external_notification", "store_forward", "range_test", "telemetry", "canned_message", "audio", "remote_hardware", "neighbor_info", "detection_sensor", "ambient_lighting", "paxcounter", "traffic_management")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def private_ipv4(value):
    ip = ipaddress.ip_address(value)
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        raise ValueError("Enter a private IPv4 address on the same LAN as this computer.")
    return str(ip)


class DeviceBusyError(RuntimeError):
    """A second operation must not interrupt a node's current connection."""


@contextmanager
def device_operation(transport, target, action):
    # Do not queue commands: a delayed OTA/apply request could unexpectedly run
    # after the user has moved on. The caller can retry when the node is free.
    if transport == "tcp":
        address = private_ipv4(target)
    elif transport == "usb":
        address = os.path.realpath(target)
    else:
        address = str(target).lower()
    key = (transport, address)
    with DEVICE_OPERATIONS_LOCK:
        current = DEVICE_OPERATIONS.get(key)
        if current:
            raise DeviceBusyError(
                f"Companion is already {current} on {target}. "
                "Wait for that operation to finish, then retry."
            )
        DEVICE_OPERATIONS[key] = action
    try:
        yield
    finally:
        with DEVICE_OPERATIONS_LOCK:
            DEVICE_OPERATIONS.pop(key, None)


def firmware_options(blob, filename, board):
    if filename.lower().endswith(".bin"):
        if not blob or len(blob) > MAX_IMAGE:
            raise ValueError("Firmware image is empty or too large.")
        return [{"path": filename, "size": len(blob), "matches_board": board_matches(filename, board)}]
    if not filename.lower().endswith(".zip"):
        raise ValueError("Choose an official .zip archive or an OTA .bin image.")
    try:
        archive = zipfile.ZipFile(BytesIO(blob))
    except (zipfile.BadZipFile, OSError):
        raise ValueError("The selected ZIP archive is invalid.")
    files = []
    for item in archive.infolist():
        if item.is_dir() or not item.filename.lower().endswith(".bin"):
            continue
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts or item.file_size <= 0 or item.file_size > MAX_IMAGE:
            continue
        files.append({"path": item.filename, "size": item.file_size, "matches_board": board_matches(item.filename, board)})
    updates = [item for item in files if "update" in item["path"].lower()]
    if updates:
        files = updates
    files.sort(key=lambda item: (not item["matches_board"], item["path"].lower()))
    if len(files) > 250:
        files = files[:250]
    return files


def board_matches(filename, board):
    norm = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
    key = norm(board)
    return bool(key and key in norm(filename))


def read_firmware(blob, filename, member):
    if filename.lower().endswith(".bin"):
        data = blob
    else:
        if not filename.lower().endswith(".zip") or not member:
            raise ValueError("Choose the OTA image contained in the ZIP archive.")
        try:
            with zipfile.ZipFile(BytesIO(blob)) as archive:
                info = archive.getinfo(member)
                if info.is_dir() or info.file_size <= 0 or info.file_size > MAX_IMAGE:
                    raise ValueError("Selected image is not a valid firmware file.")
                parts = PurePosixPath(info.filename)
                if parts.is_absolute() or ".." in parts.parts:
                    raise ValueError("Unsafe archive path.")
                data = archive.read(info)
        except KeyError:
            raise ValueError("Selected image was not found in the ZIP. Re-select the archive and image.")
        except zipfile.BadZipFile:
            raise ValueError("The selected ZIP archive is invalid.")
    if len(data) < 1024 or len(data) > MAX_IMAGE or data[0] != 0xE9:
        raise ValueError("This does not look like an ESP32 firmware image. Choose the board's OTA update .bin file.")
    return data


def local_route(target):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((target, 3232))
        return probe.getsockname()[0]
    finally:
        probe.close()


def legacy_ota_upload(target, data, password, filename):
    """Upload to the pre-2.7.18 ESP OTA loader after asking firmware to reboot into it."""
    from meshtastic.tcp_interface import TCPInterface

    control = TCPInterface(hostname=target, portNumber=4403)
    try:
        control.localNode.rebootOTA(secs=1)
    finally:
        control.close()
    time.sleep(5)

    md5 = hashlib.md5(data).hexdigest()
    host_ip = local_route(target)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host_ip, 0))
        listener.listen(1)
        listener.settimeout(15)
        host_port = listener.getsockname()[1]
        udp.bind((host_ip, 0))
        udp.settimeout(2)
        invitation = f"0 {host_port} {len(data)} {md5}\n".encode()
        answer = ""
        for _ in range(10):
            udp.sendto(invitation, (target, 3232))
            try:
                answer = udp.recvfrom(256)[0].decode("ascii", "replace").strip()
                break
            except socket.timeout:
                continue
        if not answer:
            raise RuntimeError("Legacy OTA loader did not answer. Confirm it rebooted into Wi-Fi OTA mode and check LAN/firewall reachability.")
        if answer.startswith("AUTH"):
            if not password:
                raise RuntimeError("This older node requests an OTA password. Enter its configured OTA password and retry.")
            fields = answer.split()
            if len(fields) < 2:
                raise RuntimeError("The node returned an invalid OTA authentication challenge.")
            nonce = fields[1]
            cnonce_text = f"{os.path.basename(filename)}{len(data)}{md5}{target}"
            if len(nonce) == 32:
                cnonce = hashlib.md5(cnonce_text.encode()).hexdigest()
                password_hash = hashlib.md5(password.encode()).hexdigest()
                response = hashlib.md5(f"{password_hash}:{nonce}:{cnonce}".encode()).hexdigest()
            elif len(nonce) == 64:
                cnonce = hashlib.sha256(cnonce_text.encode()).hexdigest()
                password_hash = hashlib.sha256(password.encode()).hexdigest()
                salt = f"{nonce}:{cnonce}".encode()
                derived = hashlib.pbkdf2_hmac("sha256", password_hash.encode(), salt, 10000).hex()
                response = hashlib.sha256(f"{derived}:{nonce}:{cnonce}".encode()).hexdigest()
            else:
                raise RuntimeError("The node uses an unsupported OTA authentication challenge.")
            udp.sendto(f"200 {cnonce} {response}\n".encode(), (target, 3232))
            try:
                auth_result = udp.recvfrom(128)[0].decode("ascii", "replace").strip()
            except socket.timeout:
                raise RuntimeError("No response to OTA password authentication. Check the device password.")
            if auth_result != "OK" and len(nonce) == 64:
                success = False
                for _ in range(10):
                    udp.sendto(invitation, (target, 3232))
                    try:
                        challenge = udp.recvfrom(256)[0].decode("ascii", "replace").strip()
                    except socket.timeout:
                        continue
                    if not challenge.startswith("AUTH "):
                        break
                    nonce = challenge.split()[1]
                    cnonce = hashlib.sha256(cnonce_text.encode()).hexdigest()
                    password_hash = hashlib.md5(password.encode()).hexdigest()
                    derived = hashlib.pbkdf2_hmac("sha256", password_hash.encode(), f"{nonce}:{cnonce}".encode(), 10000).hex()
                    response = hashlib.sha256(f"{derived}:{nonce}:{cnonce}".encode()).hexdigest()
                    udp.sendto(f"200 {cnonce} {response}\n".encode(), (target, 3232))
                    try:
                        success = udp.recvfrom(128)[0].decode("ascii", "replace").strip() == "OK"
                    except socket.timeout:
                        pass
                    if success:
                        break
                if not success:
                    raise RuntimeError("OTA password authentication failed. Verify the password configured on the node.")
            elif auth_result != "OK":
                raise RuntimeError("OTA password authentication failed. Verify the password configured on the node.")
        elif answer != "OK":
            raise RuntimeError("Unexpected OTA reply from node: " + answer[:120])

        try:
            conn, _peer = listener.accept()
        except socket.timeout:
            raise RuntimeError("The legacy OTA node did not connect back to this computer. Check its firewall and LAN isolation.")
        with conn:
            conn.settimeout(12)
            sent = 0
            last_ack = b""
            while sent < len(data):
                block = data[sent:sent + 1024]
                conn.sendall(block)
                sent += len(block)
                last_ack = conn.recv(10)
                if not last_ack:
                    raise RuntimeError("The node closed the OTA connection before the upload completed.")
            if b"OK" not in last_ack:
                conn.settimeout(30)
                try:
                    final = conn.recv(32)
                except socket.timeout:
                    final = b""
                if final and b"OK" not in final:
                    raise RuntimeError("Upload finished, but the node returned: " + final.decode("utf-8", "replace")[:120])
        return {"message": f"Sent {len(data):,} bytes to legacy OTA loader on {target}. The node should reboot after verifying the image."}
    finally:
        listener.close()
        udp.close()


def unified_ota_upload(target, data, iface=None):
    """Use Meshtastic's Admin OTA request and unified TCP/3232 updater."""
    from meshtastic import ota
    from meshtastic.protobuf import admin_pb2
    from meshtastic.tcp_interface import TCPInterface

    image_path = None
    stage = "preparing the OTA request"
    try:
        with tempfile.NamedTemporaryFile(prefix="meshtastic-ota-", suffix=".bin", delete=False) as image:
            image.write(data)
            image_path = image.name
        updater = ota.ESP32WiFiOTA(image_path, target)
        if iface is None:
            stage = "opening a bounded Meshtastic TCP/4403 session"
            iface = TCPInterface(
                hostname=target,
                portNumber=4403,
                timeout=15,
                noNodes=True,
                connectNow=False,
            )
            iface.connect()
        stage = "sending the Wi-Fi OTA reboot request over TCP/4403"
        iface.localNode.startOTA(
            ota_mode=admin_pb2.OTAMode.OTA_WIFI,
            ota_file_hash=updater.hash_bytes(),
        )
        # startOTA makes the node drop TCP while it reboots. Close our control
        # socket immediately so the Meshtastic client's heartbeat/reconnect
        # thread cannot keep writing to the dead socket during OTA.
        try:
            iface.close()
        finally:
            iface = None
        stage = "connecting to the node's Wi-Fi OTA service on TCP/3232"
        time.sleep(5)
        for attempt in range(10):
            try:
                stage = f"transferring the image over TCP/3232 (attempt {attempt + 1}/10)"
                updater.update()
                return {"message": f"Unified Wi-Fi OTA completed for {target}. The node verified the image and should reboot."}
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as error:
                raise RuntimeError(
                    "The node reset the TCP/3232 OTA connection before acknowledging the image header. "
                    "On ESP32-S3 boards this can indicate the older OTA Loader v1.0.1 stack-overflow bug; "
                    "the loader may need a one-time USB update. The node rebooted back to normal firmware; "
                    "do not repeat the OTA attempt until the loader is updated."
                ) from error
            except TimeoutError as error:
                raise RuntimeError(
                    "The node accepted the OTA reboot request, but the Wi-Fi OTA service did not answer on TCP/3232 in time. "
                    "The image transfer could not be confirmed. Do not retry until the node is back online; on ESP32-S3, "
                    "check the OTA loader over USB before another Wi-Fi update."
                ) from error
            except Exception as error:
                # Only retry when the OTA service is not reachable yet. Once a
                # transfer starts, any failure is terminal: restarting the OTA
                # handshake could interrupt a partially written image.
                if attempt < 9 and isinstance(error, OSError) and error.errno in {
                    errno.ECONNREFUSED,
                    errno.ENETUNREACH,
                    errno.EHOSTUNREACH,
                }:
                    time.sleep(2)
                    continue
                raise RuntimeError(
                    f"Unified Wi-Fi OTA failed during {stage}: {error}. "
                    "The firmware transfer was not retried automatically. Check that the node returned to Wi-Fi; "
                    "if it stays offline, inspect or repair its OTA loader over USB before trying again."
                ) from error
    except (ValueError, RuntimeError):
        raise
    except BrokenPipeError as error:
        if "sending the Wi-Fi OTA reboot request" in stage:
            raise RuntimeError(
                "The node closed its Meshtastic TCP/4403 connection while the Wi-Fi OTA reboot request was being sent. "
                "The request may have reached the node; check whether it rebooted and returned to Wi-Fi before retrying. "
                "If it stays offline, inspect or repair the OTA loader over USB."
            ) from error
        raise RuntimeError(f"Unified Wi-Fi OTA failed while {stage}: {error}") from error
    except Exception as error:
        raise RuntimeError(f"Unified Wi-Fi OTA failed while {stage}: {error}") from error
    finally:
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass
        if image_path:
            try:
                os.unlink(image_path)
            except OSError:
                pass


def ota_upload(target, data, password, filename):
    target = private_ipv4(target)
    iface = open_identified_meshtastic(target, timeout=10, wait_config=True)
    identity = identify_connected(iface)
    version = re.match(r"^(\d+)\.(\d+)\.(\d+)", identity.get("firmware", ""))
    if not version:
        iface.close()
        raise RuntimeError("Could not read the installed firmware version over TCP/4403, so I cannot choose the correct OTA protocol.")
    installed = tuple(int(part) for part in version.groups())
    if installed >= (2, 7, 18):
        return unified_ota_upload(target, data, iface=iface)
    iface.close()
    return legacy_ota_upload(target, data, password, filename)


def arp_neighbors():
    rows = set()
    try:
        with open("/proc/net/arp", encoding="ascii", errors="ignore") as arp:
            next(arp, None)
            for line in arp:
                parts = line.split()
                if len(parts) >= 4 and parts[2] != "0x0":
                    try:
                        rows.add(private_ipv4(parts[0]))
                    except ValueError:
                        pass
    except OSError:
        try:
            output = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=3, check=False).stdout
            for match in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", output):
                try:
                    rows.add(private_ipv4(match))
                except ValueError:
                    pass
        except (OSError, subprocess.TimeoutExpired):
            pass
    return sorted(rows, key=lambda value: tuple(map(int, value.split("."))))


def local_networks():
    found = []
    if sys.platform.startswith("linux"):
        try:
            output = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=3, check=False).stdout
            for line in output.splitlines():
                match = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)", line)
                if match:
                    found.append((match.group(1), match.group(2)))
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "darwin":
        try:
            output = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3, check=False).stdout
            iface = ""
            for line in output.splitlines():
                head = re.match(r"^(\S+): flags=", line)
                if head:
                    iface = head.group(1)
                match = re.search(r"\binet\s+(\d+(?:\.\d+){3})\s+netmask\s+(0x[\da-fA-F]+|\d+(?:\.\d+){3})", line)
                if iface and match:
                    mask = match.group(2)
                    if mask.startswith("0x"):
                        mask = str(ipaddress.IPv4Address(int(mask, 16)))
                    found.append((iface, f"{match.group(1)}/{mask}"))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    elif sys.platform.startswith("win"):
        command = "Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,PrefixLength | ConvertTo-Json -Compress"
        try:
            output = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=5, check=False).stdout
            entries = json.loads(output or "[]")
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries:
                found.append((str(entry.get("InterfaceAlias", "Network")), f"{entry['IPAddress']}/{entry['PrefixLength']}"))
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            pass
    if not found:
        try:
            found = [("network", f"{addr}/32") for addr in socket.gethostbyname_ex(socket.gethostname())[2]]
        except OSError:
            pass
    networks = []
    seen = set()
    for name, cidr in found:
        try:
            iface = ipaddress.ip_interface(cidr)
            address = private_ipv4(str(iface.ip))
            key = (name, address)
            if key in seen:
                continue
            seen.add(key)
            networks.append({"id": f"{name}|{address}", "name": name, "ip": address, "cidr": str(iface), "network": str(iface.network)})
        except ValueError:
            continue
    return networks


def dns_name(data, offset):
    labels = []
    cursor = offset
    end = None
    visited = set()
    while cursor < len(data):
        length = data[cursor]
        if length == 0:
            cursor += 1
            break
        if length & 0xC0 == 0xC0:
            if cursor + 1 >= len(data):
                raise ValueError("Truncated mDNS name.")
            pointer = ((length & 0x3F) << 8) | data[cursor + 1]
            if pointer in visited:
                raise ValueError("Invalid mDNS compression pointer.")
            visited.add(pointer)
            if end is None:
                end = cursor + 2
            cursor = pointer
            continue
        if length & 0xC0 or cursor + 1 + length > len(data):
            raise ValueError("Invalid mDNS label.")
        labels.append(data[cursor + 1:cursor + 1 + length].decode("utf-8", "replace"))
        cursor += 1 + length
    return ".".join(labels), end if end is not None else cursor


def discover_meshtastic(interface_ip, timeout=2.5):
    """Query local mDNS/DNS-SD for Meshtastic TCP services."""
    selected = next((item for item in local_networks() if item["ip"] == interface_ip), None)
    if selected is None:
        raise ValueError("Select an active private network interface first.")
    network = ipaddress.ip_interface(selected["cidr"]).network
    service = "_meshtastic._tcp.local"
    encoded = b"".join(bytes([len(label)]) + label.encode("ascii") for label in service.split(".")) + b"\0"
    packet = struct.pack("!HHHHHH", int(time.time() * 1000) & 0xFFFF, 0, 1, 0, 0, 0) + encoded + struct.pack("!HH", 12, 0x8001)
    records = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", 5353))
            membership = socket.inet_aton("224.0.0.251") + socket.inet_aton(interface_ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        except OSError:
            # Some OSes already have a system mDNS daemon on port 5353.
            sock.close()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.bind(("", 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
        sock.settimeout(0.25)
        sock.sendto(packet, ("224.0.0.251", 5353))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, peer = sock.recvfrom(9000)
            except socket.timeout:
                continue
            if len(data) < 12:
                continue
            try:
                _ident, _flags, questions, answers, authorities, additionals = struct.unpack_from("!HHHHHH", data)
                offset = 12
                for _ in range(questions):
                    _qname, offset = dns_name(data, offset)
                    offset += 4
                for _ in range(answers + authorities + additionals):
                    name, offset = dns_name(data, offset)
                    rtype, _class, ttl, size = struct.unpack_from("!HHIH", data, offset)
                    offset += 10
                    rdata_offset = offset
                    raw = data[offset:offset + size]
                    offset += size
                    item = {"name": name, "type": rtype, "ttl": ttl, "raw": raw, "rdata_offset": rdata_offset, "packet": data, "peer": peer[0]}
                    if rtype == 12:
                        item["target"], _ = dns_name(data, rdata_offset)
                    elif rtype == 33 and size >= 6:
                        _priority, _weight, item["port"] = struct.unpack_from("!HHH", raw)
                        item["target"], _ = dns_name(data, rdata_offset + 6)
                    elif rtype == 16:
                        txt, cursor = [], 0
                        while cursor < len(raw):
                            n = raw[cursor]
                            cursor += 1
                            if cursor + n > len(raw):
                                break
                            txt.append(raw[cursor:cursor + n].decode("utf-8", "replace"))
                            cursor += n
                        item["txt"] = txt
                    elif rtype == 1 and size == 4:
                        item["address"] = socket.inet_ntoa(raw)
                    records.append(item)
            except (ValueError, struct.error, IndexError):
                continue
    finally:
        sock.close()

    instances = {r["target"] for r in records if r["type"] == 12 and r["name"].lower() == service}
    found = []
    for instance in instances:
        srv = next((r for r in records if r["type"] == 33 and r["name"].lower() == instance.lower()), None)
        txt = next((r.get("txt", []) for r in records if r["type"] == 16 and r["name"].lower() == instance.lower()), [])
        txt_map = {}
        for entry in txt:
            if "=" in entry:
                key, value = entry.split("=", 1)
                txt_map[key.strip().lower()] = value.strip()
        advertised_name = next((txt_map[key] for key in ("longname", "long_name", "name", "hostname", "device_name") if txt_map.get(key)), "")
        host = srv["target"] if srv else ""
        address = next((r["address"] for r in records if r["type"] == 1 and r["name"].lower() == host.lower()), "")
        peer = next((r["peer"] for r in records if r["name"].lower() == instance.lower()), "")
        ip = address or peer
        try:
            ip = private_ipv4(ip)
        except ValueError:
            continue
        if ipaddress.ip_address(ip) not in network:
            continue
        found.append({"ip": ip, "name": advertised_name, "serviceName": instance.rstrip("."), "host": host.rstrip("."), "port": srv.get("port") if srv else None, "txt": txt, "service": service})
    unique = {item["ip"]: item for item in found}
    return list(unique.values())


def identify_meshtastic(target, timeout=10):
    """Read the node's own hardware/name/firmware metadata over Meshtastic TCP."""
    iface = open_identified_meshtastic(target, timeout)
    try:
        identity = identify_connected(iface)
        identity["ip"] = private_ipv4(target)
        identity["verified"] = bool(identity.get("hardware"))
        return identity
    finally:
        try:
            iface.close()
        except Exception:
            pass


def open_identified_meshtastic(target, timeout=10, wait_config=False):
    """Open one bounded TCP session and wait for local identity metadata."""
    target = private_ipv4(target)
    try:
        from meshtastic.tcp_interface import TCPInterface
    except ImportError as error:
        raise RuntimeError("Meshtastic agent setup is incomplete. Restart it so it can install the Meshtastic Python library.") from error
    iface = TCPInterface(
        hostname=target,
        portNumber=4403,
        timeout=timeout,
        noNodes=True,
        connectNow=False,
    )
    try:
        iface.connect()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            user = iface.getMyUser()
            metadata = getattr(iface, "metadata", None)
            if user or metadata:
                if wait_config:
                    wait_for_device_config(iface.localNode, timeout=timeout)
                return iface
            time.sleep(0.1)
        raise RuntimeError("The TCP port answered, but the node did not provide identity metadata. Enable Meshtastic TCP on this device or choose its board manually.")
    except Exception as error:
        try:
            iface.close()
        except Exception:
            pass
        detail = str(error).strip() or error.__class__.__name__
        if "timed out waiting for connection completion" in detail.lower():
            raise RuntimeError(
                f"TCP/4403 is reachable at {target}, but the node did not finish the Meshtastic handshake within {timeout} seconds. "
                "Close other Meshtastic apps or clients connected to this node, confirm TCP access is enabled, then retry."
            ) from error
        raise RuntimeError(f"Could not open a Meshtastic TCP/4403 session with {target}: {detail}") from error


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError as error:
        raise RuntimeError("USB support is unavailable. Restart the agent so it can install the Meshtastic package.") from error
    ports = []
    for port in list_ports.comports():
        path = port.device or ""
        description = port.description or ""
        hwid = port.hwid or ""
        # Linux exposes many unconnected motherboard UARTs as ttyS devices.
        # They are not USB candidates and probing them creates noisy failures.
        if re.fullmatch(r"/dev/ttyS\d+", path):
            continue
        usb_candidate = (
            port.vid is not None
            or port.pid is not None
            or any(token in (description + " " + hwid).lower() for token in ("usb", "uart", "cdc"))
            or re.search(r"(?:/dev/tty(?:USB|ACM)|/dev/cu\.|^COM\d+$)", path, re.IGNORECASE)
        )
        if not usb_candidate:
            continue
        ports.append({"path": path, "name": description or path, "hwid": hwid})
    return ports


def usb_meshtastic_identity(port, timeout=12):
    """Read the connected node identity over its explicitly selected USB serial port."""
    if port not in {item["path"] for item in list_serial_ports()}:
        raise ValueError("That USB port is not currently available. Refresh the USB device list.")
    try:
        from meshtastic.serial_interface import SerialInterface
    except ImportError as error:
        raise RuntimeError("USB identification requires the Meshtastic Python package. Restart the companion to install it.") from error
    iface = None
    try:
        iface = SerialInterface(devPath=port, timeout=timeout, noNodes=True, connectNow=False)
        iface.connect()
        deadline = time.monotonic() + timeout
        user = None
        metadata = None
        while time.monotonic() < deadline:
            user = iface.getMyUser() or {}
            metadata = getattr(iface, "metadata", None) or {}
            if user.get("hwModel") or user.get("hw_model"):
                break
            time.sleep(0.1)
        user = user or {}
        if hasattr(metadata, "firmware_version"):
            firmware = metadata.firmware_version
        elif isinstance(metadata, dict):
            firmware = metadata.get("firmwareVersion") or metadata.get("firmware_version") or ""
        else:
            firmware = ""
        hardware = user.get("hwModel") or user.get("hw_model") or ""
        if not hardware:
            raise RuntimeError("A Meshtastic device was not identified on this USB port. Check that it has booted into Meshtastic firmware and close other serial programs.")
        identity = {
            "port": port,
            "name": user.get("longName") or user.get("long_name") or "",
            "shortName": user.get("shortName") or user.get("short_name") or "",
            "hardware": str(hardware),
            "firmware": str(firmware or ""),
            "verified": True,
        }
        port_info = next((item for item in list_serial_ports() if item["path"] == port), None)
        if port_info:
            for key in usb_hwid_identities(port_info["hwid"]):
                USB_IDENTITY_CACHE[key] = (time.monotonic(), identity)
        return identity
    except (OSError, TimeoutError) as error:
        raise RuntimeError("Could not read Meshtastic identity over USB: " + str(error)) from error
    except Exception as error:
        detail = str(error).strip() or error.__class__.__name__
        if "timed out waiting for connection completion" in detail.lower():
            raise RuntimeError(
                f"USB port {port} opened, but the node did not finish the Meshtastic handshake within {timeout} seconds. "
                "Make sure it has booted normally and close other serial or Meshtastic programs."
            ) from error
        raise RuntimeError(f"Meshtastic identity read failed on USB port {port}: {detail}") from error
    finally:
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass


def cached_usb_identity(port):
    """Reuse a recently identified node when it has been manually put in ROM download mode."""
    current = next((item for item in list_serial_ports() if item["path"] == port), None)
    if not current:
        raise ValueError("The USB port disappeared. Return the node to Meshtastic mode and scan USB again.")
    now = time.monotonic()
    current_keys = set(usb_hwid_identities(current["hwid"]))
    for key, (timestamp, identity) in list(USB_IDENTITY_CACHE.items()):
        if now - timestamp > 600:
            USB_IDENTITY_CACHE.pop(key, None)
            continue
        if key and key in current_keys:
            return {**identity, "port": port}
    raise RuntimeError("Identify the Meshtastic device while it is running normally first, then enter ROM download mode and retry.")


def usb_hwid_identities(hwid):
    """Return stable USB serial/location identifiers across ROM-mode re-enumeration."""
    identities = []
    for label in ("SER", "LOCATION"):
        match = re.search(r"(?:^|\s)" + label + r"=([^\s]+)", hwid or "", re.IGNORECASE)
        if match and match.group(1).lower() not in ("n/a", "none"):
            identities.append(label + ":" + match.group(1).lower())
    if not identities and hwid and hwid.lower() != "n/a":
        identities.append("HWID:" + hwid.strip().lower())
    return identities


def verify_esp32s3_security_state(port, manual_boot=False):
    """Refuse a generic repair unless ROM confirms secure boot and encryption are disabled."""
    if port not in {item["path"] for item in list_serial_ports()}:
        raise ValueError("That USB port is not currently available. Refresh the USB device list.")
    command = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port]
    if manual_boot:
        command += ["--before", "no-reset", "--after", "no-reset"]
    command += ["--no-stub", "get-security-info"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Could not verify ESP32-S3 security state over USB; no flash was changed.") from error
    except OSError as error:
        raise RuntimeError("Could not start esptool security check: " + str(error)) from error
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode:
        raise RuntimeError("Could not verify ESP32-S3 Secure Boot and flash-encryption state; no flash was changed. " + output[-2000:])
    secure_boot = re.search(r"Secure Boot:\s*(Enabled|Disabled)", output, re.IGNORECASE)
    flash_encryption = re.search(r"Flash Encryption:\s*(Enabled|Disabled)", output, re.IGNORECASE)
    if not secure_boot or not flash_encryption:
        raise RuntimeError("The ROM did not report Secure Boot and flash-encryption state; refusing the generic loader repair.")
    if secure_boot.group(1).lower() != "disabled" or flash_encryption.group(1).lower() != "disabled":
        raise RuntimeError("Secure Boot or flash encryption is enabled. This generic loader image cannot be safely written to that protected device.")


def run_esptool_read(port, offset, length, destination, manual_boot=False):
    if port not in {item["path"] for item in list_serial_ports()}:
        raise ValueError("That USB port is not currently available. Refresh the USB device list.")
    command = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port]
    if manual_boot:
        command += ["--before", "no-reset", "--after", "no-reset"]
    command += ["--baud", "460800", "read-flash", hex(offset), hex(length), destination]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("USB flash inspection timed out. Check the cable and close other serial programs.") from error
    except OSError as error:
        raise RuntimeError("Could not start esptool: " + str(error)) from error
    if result.returncode:
        output = (result.stdout + "\n" + result.stderr).strip()
        raise RuntimeError("Could not inspect ESP32-S3 flash over USB. " + output[-3000:])


def esp32s3_ota1_geometry(partition_table):
    """Parse the standard ESP-IDF partition table and return safe OTA app geometry."""
    if len(partition_table) != 0x1000:
        raise ValueError("Could not read the ESP32 partition table from USB flash.")
    partitions = []
    for position in range(0, len(partition_table), 32):
        entry = partition_table[position:position + 32]
        magic = int.from_bytes(entry[0:2], "little")
        if magic in (0xFFFF, 0xEBEB):
            break
        if magic != 0x50AA:
            continue
        ptype, subtype = entry[2], entry[3]
        offset = int.from_bytes(entry[4:8], "little")
        size = int.from_bytes(entry[8:12], "little")
        label = entry[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
        partitions.append({"type": ptype, "subtype": subtype, "offset": offset, "size": size, "label": label})
    app0 = next((p for p in partitions if p["type"] == 0 and p["subtype"] == 0x10), None)
    app1 = next((p for p in partitions if p["type"] == 0 and p["subtype"] == 0x11), None)
    if not app0 or not app1:
        raise ValueError("This device's partition table has no app0/app1 OTA application pair; no loader was changed.")
    if (app0["offset"] != 0x10000 or app0["size"] <= 0 or app1["offset"] < app0["offset"] + app0["size"]
            or app1["offset"] % 0x10000 or app1["size"] < ESP32S3_OTA_LOADER_SIZE
            or app1["size"] > ESP32S3_MAX_FLASH_SIZE or app1["offset"] + app1["size"] > ESP32S3_MAX_FLASH_SIZE):
        raise ValueError("The device's app1 partition is not a supported safe destination; no loader was changed.")
    return app0, app1


def inspect_esp32s3_ota_loader(port, manual_boot=False):
    """Identify a node over Meshtastic USB, then read its partition and installed loader."""
    identity = cached_usb_identity(port) if manual_boot else usb_meshtastic_identity(port)
    try:
        import esptool  # noqa: F401
        from importlib.metadata import version as package_version
        if int(package_version("esptool").split(".")[0]) < 5:
            raise RuntimeError("USB inspection requires esptool 5 or newer. Restart the updated companion to install it.")
    except ImportError as error:
        raise RuntimeError("USB inspection requires esptool 5 or newer. Restart the updated companion to install it.") from error
    verify_esp32s3_security_state(port, manual_boot)
    with tempfile.TemporaryDirectory(prefix="meshtastic-loader-check-") as workdir:
        table_path = os.path.join(workdir, "partition-table.bin")
        run_esptool_read(port, 0x8000, 0x1000, table_path, manual_boot)
        with open(table_path, "rb") as source:
            app0, app1 = esp32s3_ota1_geometry(source.read())
        loader_path = os.path.join(workdir, "installed-loader.bin")
        run_esptool_read(port, app1["offset"], ESP32S3_OTA_LOADER_SIZE, loader_path, manual_boot)
        with open(loader_path, "rb") as source:
            installed_hash = hashlib.sha256(source.read()).hexdigest()
    patched = installed_hash == ESP32S3_OTA_LOADER_SHA256
    return {
        **identity,
        "architecture": "esp32-s3",
        "app0": {"offset": hex(app0["offset"]), "size": hex(app0["size"])},
        "app1": {"offset": hex(app1["offset"]), "size": hex(app1["size"])},
        "loaderSha256": installed_hash,
        "patchedLoaderInstalled": patched,
        "recommendRepair": not patched,
        "manualBoot": manual_boot,
    }


def repair_esp32s3_ota_loader(port, blob, manual_boot=False):
    """Write the verified S3 loader to app1 geometry read directly from this device."""
    if len(blob) != ESP32S3_OTA_LOADER_SIZE or hashlib.sha256(blob).hexdigest() != ESP32S3_OTA_LOADER_SHA256:
        raise ValueError("OTA loader image failed its size or SHA-256 check.")
    identity = cached_usb_identity(port) if manual_boot else usb_meshtastic_identity(port)
    try:
        import esptool  # noqa: F401
        from importlib.metadata import version as package_version
        if int(package_version("esptool").split(".")[0]) < 5:
            raise RuntimeError("USB loader repair requires esptool 5 or newer. Restart the updated companion to install it.")
    except ImportError as error:
        raise RuntimeError("USB loader repair requires esptool 5 or newer. Restart the updated companion to install it.") from error
    verify_esp32s3_security_state(port, manual_boot)

    with tempfile.TemporaryDirectory(prefix="meshtastic-ota-loader-") as workdir:
        table_path = os.path.join(workdir, "partition-table.bin")
        run_esptool_read(port, 0x8000, 0x1000, table_path, manual_boot)
        with open(table_path, "rb") as source:
            app0, app1 = esp32s3_ota1_geometry(source.read())
        image_path = os.path.join(workdir, "patched-loader.bin")
        with open(image_path, "wb") as image:
            image.write(blob)
        command = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port]
        if manual_boot:
            command += ["--before", "no-reset"]
        command += ["--baud", "460800", "write-flash", "--flash-mode", "keep", "--flash-size", "keep",
                   hex(app1["offset"]), image_path]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("USB flash timed out. Check the cable, close serial monitors, and try again.") from error
        except OSError as error:
            raise RuntimeError("Could not start esptool: " + str(error)) from error
        output = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode:
            raise RuntimeError("USB loader flash failed. " + output[-6000:])
    return {"ok": True, "message": f"Patched ESP32-S3 OTA loader written for {identity['hardware']} to app1 at {hex(app1['offset'])}. Main firmware and configuration were not erased.", "output": output[-6000:], "identity": identity, "app0": {"offset": hex(app0["offset"]), "size": hex(app0["size"])}, "app1": {"offset": hex(app1["offset"]), "size": hex(app1["size"])}}


def scan_bluetooth():
    try:
        from meshtastic.ble_interface import BLEInterface
        devices = BLEInterface.scan()
    except Exception as error:
        raise RuntimeError("Bluetooth scan failed: " + str(error)) from error
    return [{"address": d.address, "name": d.name or d.address} for d in devices]


def open_device(transport, target, timeout=20):
    """Open one explicitly selected Meshtastic device using the official client library."""
    if transport == "tcp":
        target = private_ipv4(target)
        from meshtastic.tcp_interface import TCPInterface
        iface = TCPInterface(
            hostname=target,
            portNumber=4403,
            timeout=timeout,
            noNodes=True,
            connectNow=False,
        )
        try:
            iface.connect()
            return iface
        except Exception as error:
            try:
                iface.close()
            except Exception:
                pass
            detail = str(error).strip() or error.__class__.__name__
            raise RuntimeError(
                f"Could not open a Meshtastic settings session with {target} on TCP/4403: {detail}. "
                "Check that the node is online, TCP access is enabled, and no other client is holding its connection."
            ) from error
    if transport == "usb":
        from meshtastic.serial_interface import SerialInterface
        paths = {item["path"] for item in list_serial_ports()}
        if target not in paths:
            raise ValueError("That USB port is not currently available. Refresh the USB device list.")
        return SerialInterface(devPath=target, timeout=timeout, noNodes=True)
    if transport == "bluetooth":
        if not re.fullmatch(r"[0-9A-Fa-f:_-]{12,20}", str(target)):
            raise ValueError("Select a Bluetooth device from the scan results.")
        from meshtastic.ble_interface import BLEInterface
        return BLEInterface(address=target)
    raise ValueError("Choose Wi-Fi/TCP, USB, or Bluetooth as the connection type.")


def message_dict(message):
    from google.protobuf.json_format import MessageToDict
    try:
        return MessageToDict(message, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
    except TypeError:  # Older protobuf used by some OS Python packages.
        return MessageToDict(message, preserving_proto_field_name=True, including_default_value_fields=True)


def safe_config_group(group):
    """Remove per-node identity and credentials from shareable setting profiles."""
    excluded = {"private_key", "public_key", "admin_key", "local_device_secret", "is_licensed", "is_unmessagable", "fixed_position", "fixed_latitude", "fixed_longitude", "fixed_altitude", "node_id", "node_num", "mac_address", "serial_number", "hw_model", "hardware_model", "long_name", "short_name", "fixed_pin", "rxd", "txd"}
    if isinstance(group, dict):
        return {key: safe_config_group(value) for key, value in group.items() if key.lower() not in excluded and not key.lower().endswith(("_gpio", "_pin"))}
    if isinstance(group, list):
        return [safe_config_group(value) for value in group]
    return group


def config_field_present(message, name):
    """Read only populated message fields; scalar fields may not support presence."""
    field = message.DESCRIPTOR.fields_by_name.get(name)
    if field is None:
        return False
    # HasField raises ValueError for proto3 scalar fields without explicit presence
    # (for example LocalModuleConfig.version). They are schema metadata, not a
    # nested configuration section to include in a profile.
    presence = getattr(field, "has_presence", None)
    if presence is False and getattr(field, "message_type", None) is None:
        return False
    try:
        return message.HasField(name)
    except (ValueError, AttributeError):
        return False


def wait_for_device_config(node, timeout=20):
    """Wait briefly for the official client library's config dump to complete."""
    deadline = time.monotonic() + timeout
    while node.channels is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if node.channels is None:
        raise RuntimeError("Timed out waiting for the device configuration. Check the connection and try again.")


def device_configuration(iface):
    node = iface.localNode
    wait_for_device_config(node)
    local_message, module_message = node.localConfig, node.moduleConfig
    local = message_dict(local_message)
    module = message_dict(module_message)
    # Device identity is held in User metadata, not shared as part of profiles.
    local.pop("security", None)
    return {
        "identity": identify_connected(iface),
        "local": {key: safe_config_group(local[key]) for key in LOCAL_CONFIG_SECTIONS if key in local and config_field_present(local_message, key)},
        "module": {key: safe_config_group(value) for key, value in module.items() if config_field_present(module_message, key)},
        "channels": [message_dict(channel) for channel in (node.channels or [])],
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def identify_connected(iface):
    user = iface.getMyUser() or {}
    metadata = getattr(iface, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = vars(metadata) if metadata is not None and hasattr(metadata, "__dict__") else {}
    firmware = metadata.get("firmwareVersion") or metadata.get("firmware_version") or getattr(getattr(iface, "metadata", None), "firmware_version", "")
    my_info = getattr(iface, "myInfo", None)
    node_num = my_info.get("my_node_num", 0) if isinstance(my_info, dict) else getattr(my_info, "my_node_num", 0)
    return {
        "name": user.get("longName") or user.get("long_name") or "",
        "shortName": user.get("shortName") or user.get("short_name") or "",
        "hardware": str(user.get("hwModel") or user.get("hw_model") or ""),
        "firmware": str(firmware or ""),
        "nodeNum": int(node_num or 0),
    }


def apply_configuration(iface, request):
    from google.protobuf.json_format import ParseDict
    node = iface.localNode
    wait_for_device_config(node)
    profile = request.get("profile") or {}
    sections = request.get("sections") or {}
    writes = []
    requested_local = any(enabled and name in LOCAL_CONFIG_SECTIONS and name in profile.get("local", {}) for name, enabled in sections.get("local", {}).items())
    requested_module = any(enabled and name in WRITABLE_MODULE_CONFIGS and name in profile.get("module", {}) for name, enabled in sections.get("module", {}).items())
    requested_channels = bool(request.get("channels"))
    if requested_local or requested_module or requested_channels:
        node.beginSettingsTransaction()
    try:
        for name, enabled in sections.get("local", {}).items():
            if enabled and name in LOCAL_CONFIG_SECTIONS and name in profile.get("local", {}):
                destination = getattr(node.localConfig, name)
                ParseDict(profile["local"][name], destination, ignore_unknown_fields=True)
                node.writeConfig(name)
                writes.append(name)
        available_modules = set(WRITABLE_MODULE_CONFIGS)
        for name, enabled in sections.get("module", {}).items():
            if enabled and name in available_modules and name in profile.get("module", {}):
                destination = getattr(node.moduleConfig, name)
                ParseDict(profile["module"][name], destination, ignore_unknown_fields=True)
                node.writeConfig(name)
                writes.append(name)
        available_channels = {int(channel.index): channel for channel in (node.channels or [])}
        source_channels = {int(channel.get("index", -1)): channel for channel in profile.get("channels", [])}
        for index in request.get("channels", []):
            index = int(index)
            if index not in available_channels or index not in source_channels:
                raise ValueError(f"Channel {index} is not present on both the profile and destination device.")
            ParseDict(source_channels[index], available_channels[index], ignore_unknown_fields=True)
            node.writeChannel(index)
            writes.append(f"channel_{index}")
        if writes:
            node.commitSettingsTransaction()
        owner = request.get("owner") or {}
        long_name = str(owner.get("longName") or "").strip()
        short_name = str(owner.get("shortName") or "").strip()
        if long_name or short_name:
            node.setOwner(long_name=long_name or None, short_name=short_name or None)
            writes.append("owner")
    except Exception:
        raise
    return writes


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MeshtasticLocalAgent/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[local-agent] " + fmt % args, file=sys.stderr)

    def cors(self):
        origin = self.headers.get("Origin", "")
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Credentials", "false")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_json(self, status, value):
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def origin_allowed(self):
        return self.headers.get("Origin", "") in self.server.allowed_origins

    def authorized(self):
        if not self.server.access_token:
            return True
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, "Bearer " + self.server.access_token)

    def do_OPTIONS(self):
        if self.headers.get("Origin", "") not in self.server.allowed_origins:
            self.send_error(403)
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.cors()
        self.end_headers()

    def do_GET(self):
        if not self.origin_allowed():
            self.send_json(403, {"error": "This web origin is not allowed to use the local companion."})
            return
        if not self.authorized():
            self.send_json(401, {"error": "Agent access token is missing or incorrect."})
            return
        route = urlparse(self.path)
        if route.path == "/api/health":
            self.send_json(200, {"ok": True, "version": VERSION})
        elif route.path == "/api/networks":
            self.send_json(200, {"networks": local_networks()})
        elif route.path == "/api/neighbors":
            self.send_json(200, {"devices": arp_neighbors(), "note": "ARP neighbors are suggestions only; this does not confirm OTA mode."})
        elif route.path == "/api/discover":
            try:
                interface_ip = parse_qs(route.query).get("interface", [""])[0]
                self.send_json(200, {"devices": discover_meshtastic(interface_ip)})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except OSError as error:
                self.send_json(500, {"error": "Local mDNS discovery failed: " + str(error)})
        elif route.path == "/api/identify":
            try:
                query = parse_qs(route.query)
                ip = query.get("ip", [""])[0]
                with device_operation("tcp", ip, "reading device identity"):
                    identity = identify_meshtastic(ip)
                self.send_json(200, identity)
            except DeviceBusyError as error:
                self.send_json(409, {"error": str(error), "code": "device_busy"})
            except (ValueError, RuntimeError) as error:
                self.send_json(400, {"error": str(error)})
        elif route.path == "/api/usb/ports":
            try:
                self.send_json(200, {"devices": list_serial_ports()})
            except RuntimeError as error:
                self.send_json(500, {"error": str(error)})
        elif route.path == "/api/usb/identify":
            try:
                port = parse_qs(route.query).get("port", [""])[0]
                with device_operation("usb", port, "reading device identity"):
                    identity = usb_meshtastic_identity(port)
                self.send_json(200, identity)
            except DeviceBusyError as error:
                self.send_json(409, {"error": str(error), "code": "device_busy"})
            except (ValueError, RuntimeError) as error:
                self.send_json(400, {"error": str(error)})
        elif route.path == "/api/usb/ota-loader/status":
            try:
                port = parse_qs(route.query).get("port", [""])[0]
                manual_boot = parse_qs(route.query).get("manual", [""])[0] == "1"
                with device_operation("usb", port, "checking the OTA loader"):
                    status = inspect_esp32s3_ota_loader(port, manual_boot)
                self.send_json(200, status)
            except DeviceBusyError as error:
                self.send_json(409, {"error": str(error), "code": "device_busy"})
            except (ValueError, RuntimeError) as error:
                self.send_json(400, {"error": str(error)})
        elif route.path == "/api/bluetooth/scan":
            try:
                self.send_json(200, {"devices": scan_bluetooth()})
            except RuntimeError as error:
                self.send_json(500, {"error": str(error)})
        elif route.path == "/api/config/read":
            try:
                query = parse_qs(route.query)
                transport, target = query.get("transport", [""])[0], query.get("target", [""])[0]
                with device_operation(transport, target, "reading device settings"):
                    iface = open_device(transport, target)
                    try:
                        config = device_configuration(iface)
                    finally:
                        iface.close()
                self.send_json(200, config)
            except DeviceBusyError as error:
                self.send_json(409, {"error": str(error), "code": "device_busy"})
            except Exception as error:
                self.send_json(400, {"error": str(error)})
        else:
            self.send_json(404, {"error": "Unknown local companion route."})

    def parse_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_REQUEST:
            raise ValueError("Request must contain a file smaller than 120 MiB.")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("Expected a multipart firmware upload.")
        raw = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode() + b"\r\n\r\n" + self.rfile.read(length)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            value = part.get_payload(decode=True) or b""
            if part.get_filename() is not None:
                fields[name] = (part.get_filename(), value)
            else:
                fields[name] = value.decode("utf-8", "replace")
        return fields

    def do_POST(self):
        if not self.origin_allowed():
            self.send_json(403, {"error": "This web origin is not allowed to use the local companion."})
            return
        if not self.authorized():
            self.send_json(401, {"error": "Agent access token is missing or incorrect."})
            return
        try:
            if self.path == "/api/config/apply":
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4 * 1024 * 1024:
                    raise ValueError("Configuration request is empty or too large.")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                results = []
                for target in request.get("targets", []):
                    transport, address = target.get("transport", ""), target.get("target", "")
                    try:
                        with device_operation(transport, address, "applying device settings"):
                            iface = open_device(transport, address)
                            try:
                                writes = apply_configuration(iface, request)
                            finally:
                                try:
                                    iface.close()
                                except Exception:
                                    pass
                        results.append({"target": address, "transport": transport, "ok": True, "written": writes})
                    except Exception as error:
                        results.append({"target": address, "transport": transport, "ok": False, "error": str(error)})
                self.send_json(200, {"results": results})
                return
            fields = self.parse_form()
            filename, blob = fields.get("firmware", ("", b""))
            if self.path == "/api/usb/repair-ota-loader":
                port = fields.get("port", "")
                with device_operation("usb", port, "repairing the OTA loader"):
                    result = repair_esp32s3_ota_loader(port, blob, fields.get("manual_boot") == "1")
                self.send_json(200, result)
                return
            if self.path == "/api/firmware/options":
                images = firmware_options(blob, filename, fields.get("board", ""))
                self.send_json(200, {"images": images})
                return
            if self.path == "/api/update":
                target = private_ipv4(fields.get("ip", ""))
                image = read_firmware(blob, filename, fields.get("member", ""))
                with device_operation("tcp", target, "updating firmware"):
                    result = ota_upload(target, image, fields.get("password", ""), fields.get("member") or filename)
                self.send_json(200, result)
                return
            self.send_json(404, {"error": "Unknown local companion route."})
        except DeviceBusyError as error:
            self.send_json(409, {"error": str(error), "code": "device_busy"})
        except (ValueError, RuntimeError, OSError, zipfile.BadZipFile) as error:
            self.send_json(400, {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description="Local Meshtastic Wi-Fi OTA companion")
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help=f"Exact Cloudflare Pages origin (default: {DEFAULT_ORIGIN})")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", default="127.0.0.1", help="Bind address; keep 127.0.0.1 for local-only use")
    parser.add_argument("--token", default="", help="Required for remote agents; use at least 24 random characters")
    parser.add_argument("--certfile", default="", help="TLS certificate file, required for a remote agent")
    parser.add_argument("--keyfile", default="", help="TLS private key file, required for a remote agent")
    args = parser.parse_args()
    origin = args.origin.rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        parser.error("--origin must be HTTPS (localhost is allowed for Wrangler development).")
    if not parsed.netloc or parsed.path not in ("", "/"):
        parser.error("--origin must be an origin only, without a path.")
    local_only = args.bind == "127.0.0.1"
    if not local_only and (len(args.token) < 24 or not args.certfile or not args.keyfile):
        parser.error("Remote binding requires --token (24+ characters), --certfile and --keyfile. Use HTTPS and keep the endpoint private.")
    if args.bind not in ("127.0.0.1", "0.0.0.0"):
        parser.error("--bind must be 127.0.0.1 (local) or 0.0.0.0 (remote LAN).")
    server = http.server.ThreadingHTTPServer((args.bind, args.port), Handler)
    server.allowed_origins = {origin}
    server.access_token = args.token
    if not local_only:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(args.certfile, args.keyfile)
        except (OSError, ssl.SSLError) as error:
            server.server_close()
            parser.error("Could not load TLS certificate and key: " + str(error))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    protocol = "https" if not local_only else "http"
    print(f"Meshtastic companion {VERSION} listening on {protocol}://{args.bind}:{args.port}")
    print(f"Allowed web origin: {origin}")
    print("Keep this process open during OTA. Stop with Ctrl+C.")
    if not local_only:
        print("Remote mode enabled: HTTPS + access token are required for every request.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping local companion.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
