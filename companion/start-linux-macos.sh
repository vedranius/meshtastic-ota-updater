#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  printf 'Python 3 nije pronađen. Instaliraj Python 3.10 ili noviji.\n' >&2
  exit 1
fi
if [ "$#" -lt 1 ]; then
  printf 'URL Cloudflare Pages stranice [https://meshtastic-ota-updater.pages.dev/]: '
  IFS= read -r page_origin
  page_origin=${page_origin:-https://meshtastic-ota-updater.pages.dev}
else
  page_origin=$1
fi
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import meshtastic, esptool; from importlib.metadata import version; assert int(version("esptool").split(".")[0]) >= 5' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --upgrade pip meshtastic esptool
fi
exec .venv/bin/python meshtastic_agent.py --origin "$page_origin"
