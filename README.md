# Meshtastic OTA Updater

Open-source Cloudflare Pages application for discovering Meshtastic nodes on a local network, updating ESP32 firmware over Wi-Fi OTA, repairing affected ESP32-S3 OTA loaders over USB, and comparing or copying device settings.

**Test web app:** https://meshtastic-ota-updater.pages.dev/

Contributions are welcome through GitHub forks and pull requests. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Cloudflare Pages deployment

The Pages site follows this order: start the agent, choose one of its LAN interfaces and discover Meshtastic mDNS services, read the selected node's identity over TCP/4403, then choose a stable/beta release. The app loads that board and version's official Meshtastic target manifest and offers its OTA image. ZIP upload and archive scanning are not required.

Pages cannot join a visitor's LAN. The companion discovers devices, reads identity, applies settings, and performs firmware updates. Modern ESP32 firmware (2.7.18+) uses Meshtastic's OTA request over TCP/4403 followed by the unified OTA stream over TCP/3232; older firmware uses the legacy OTA loader flow.

## Deploy with the Cloudflare dashboard

1. Push this repository to GitHub.
2. In Cloudflare, create a Pages project connected to the repository.
3. Set the project root directory to `.` (the repository root).
4. Leave the build command empty. Set the build output directory to `public`.
5. Deploy. The `functions/` directory supplies the official releases, device catalog, target manifest, and OTA image endpoints.

The app does not require a database, Cloudflare secret, or binding. Release metadata comes from the Meshtastic firmware GitHub releases API. Board data and firmware manifests/images come from official Meshtastic services. Configuration profiles are stored in the user's browser or downloaded as JSON; they are not sent to Cloudflare.

## Start the agent

Python 3.10 or newer is required. The included starter script creates a private virtual environment and installs the official `meshtastic` Python package on first run.

- Windows: double-click `companion/start-windows.bat` and enter the Pages origin.
- Linux/macOS: run `./companion/start-linux-macos.sh https://your-project.pages.dev`.

Keep the terminal open during identification and flashing. Local mode binds only to `127.0.0.1:8765` and accepts requests only from the exact Pages origin given at startup.

## Agent on another computer or server

The agent supports a remote LAN endpoint using HTTPS and a bearer token. First run its starter script once to install the dependency; then stop it and launch it with a trusted TLS certificate and key:

```sh
.venv/bin/python meshtastic_agent.py \
  --origin https://meshtastic-ota-updater.pages.dev \
  --bind 0.0.0.0 --port 8765 \
  --token 'replace-with-a-random-secret-at-least-24-characters' \
  --certfile /path/to/agent.crt --keyfile /path/to/agent.key
```

Run from the `companion` directory. On Windows use `.venv\Scripts\python.exe`. In Step 01, enter the agent's HTTPS URL and the same token. The certificate must be trusted by the browser and valid for the host name. Permit local-network access when asked, and allow inbound port 8765 only from the trusted LAN. Do not publish the agent port to the public internet. The token is not saved by the page.

## How device matching and OTA work

1. The agent lists its private IPv4 interfaces. Choose one and search; discovery queries `_meshtastic._tcp` mDNS on that interface.
2. For every discovered device, the agent queries Meshtastic TCP port 4403 and reads long/short name, hardware model, and firmware version when available. If TCP metadata is unavailable, select the correct board manually from the official ESP32 catalog.
3. Choose stable or beta and a version. Each version includes its official release notes. Pages loads the official `firmware-{board}-{version}.mt.json` target manifest and selects the `app0` Meshtastic application image required by Unified Wi-Fi OTA. The smaller `mt-*-ota.bin` is an OTA loader image for serial partition flashing and must not be sent as the Wi-Fi OTA payload.
4. For firmware 2.7.18 and newer, the agent requests Wi-Fi OTA over Meshtastic TCP/4403, waits for the node to reboot into the OTA loader, then streams the verified image to the node over TCP/3232 using the official Meshtastic Python OTA client. Older firmware is asked to reboot into its legacy OTA loader and uses the legacy Espressif invitation flow.

The agent must be able to reach the node on TCP/4403 and TCP/3232. Avoid guest Wi-Fi/client isolation or restrictive VLAN rules, and allow these connections through firewalls. A discovered device may be reachable for TCP/4403 identification while an OTA transfer is still blocked.

## Scope

The configuration manager can read and compare configurations by category, save/import/export browser-local JSON profiles, apply selected config sections and channel slots sequentially, and set a generated unique long/short name on each target. Targets can use Meshtastic TCP over Wi-Fi, USB serial ports, or Bluetooth LE devices visible to the agent computer. Copying excludes the security config, node identity, fixed-position flag, board-specific pin/GPIO assignments, and Bluetooth pairing PIN. Channel settings are opt-in and include their PSKs; network and module settings such as Wi-Fi/MQTT credentials are also copied when selected. The comparison table masks secret-like values unless explicitly revealed.

Firmware updates in this app remain Wi-Fi OTA for ESP32 Meshtastic nodes with a matching official OTA image. This updates the OTA application partition; it does not erase the entire flash or perform a clean installation, and device settings are preserved. The app also includes a targeted USB repair for the ESP32-S3 Wi-Fi OTA-loader stack-overflow fix from upstream commit `02e63d9`, available for ESP32-S3 boards whose selected official firmware manifest contains the matching `mt-esp32s3-ota.bin` app1 partition. The app obtains the partition address and size from that exact board/version manifest, and the agent checks the target, partition metadata, non-overlapping app0/app1 partition geometry, image size, and SHA-256 before writing. It does not erase `app0` or configuration. The repair image is a local build of the upstream source commit, not an official tagged OTA release; the bundled source archive and SHA-256 manifest are under `public/firmware/`. General USB/serial and Bluetooth firmware flashing, nRF52, and RP2040 firmware updates are not implemented here. Hardware auto-identification requires TCP/4403; otherwise the user selects the board manually. Flash confirmation is deliberate because incompatible firmware may require USB recovery. OTA images are limited to 24 MiB and held in agent memory during transfer. The web interface is available in Croatian and English using the language selector.


Update note: use companion agent v0.3.7 or newer. It fixes protobuf config reads, registers the POST handler, and uses the official unified OTA flow for firmware 2.7.18+ with legacy support for older nodes.


## License

This project is distributed under the GNU General Public License v3.0; see [LICENSE](LICENSE). The bundled OTA-loader repair image is built from Meshtastic upstream source commit `02e63d9`; its source archive and build details are included under `public/firmware/`.
