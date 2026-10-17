@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  set /p PAGE_ORIGIN=Cloudflare Pages URL [https://meshtastic-ota-updater.pages.dev/]:
  if not defined PAGE_ORIGIN set "PAGE_ORIGIN=https://meshtastic-ota-updater.pages.dev"
) else (
  set PAGE_ORIGIN=%~1
)
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto setup_error
.venv\Scripts\python.exe -c "import meshtastic, esptool; from importlib.metadata import version; assert int(version('esptool').split('.')[0]) >= 5" >nul 2>&1
if errorlevel 1 .venv\Scripts\python.exe -m pip install --upgrade pip meshtastic esptool
if errorlevel 1 goto setup_error
.venv\Scripts\python.exe meshtastic_agent.py --origin "%PAGE_ORIGIN%"
goto finish
:setup_error
echo Agent setup failed. Install Python 3.10+ and try again.
:finish
pause
