#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 nije instaliran u Linux okruženju Chromebooka. Pokreni ChromeOS naredbu za postavljanje prikazanu na web stranici."
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Potreban je Python 3.10 ili noviji. Ažuriraj Linux pakete i pokušaj ponovno."
  exit 1
fi

if ! python3 -c 'import venv' >/dev/null 2>&1; then
  echo "Nedostaje Python venv modul. Pokreni: sudo apt-get install -y python3-venv"
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

if ! .venv/bin/python -c 'import meshtastic, esptool; from importlib.metadata import version; assert int(version("esptool").split(".")[0]) >= 5' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --upgrade pip meshtastic esptool
fi

exec .venv/bin/python meshtastic_agent.py --origin "https://meshtastic-ota-updater.pages.dev"
