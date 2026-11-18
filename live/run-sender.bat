@echo off
rem Live English sender for the office computer. Edit DEVICE to match --list-devices.
rem First time: py -3.12 -m venv .venv  then  .venv\Scripts\pip install -r live\requirements.txt
set DEVICE=Line In
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (
  echo Set up first: py -3.12 -m venv .venv ^&^& .venv\Scripts\pip install -r live\requirements.txt
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m live.sender --device "%DEVICE%" %*
pause
