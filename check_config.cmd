@echo off
cd /d "%~dp0"
set PYTHONNOUSERSITE=1
set PYTHONUTF8=1
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Please run setup_fastapi.cmd first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -s "%~dp0camera_receiver.py" --config "%~dp0config.ini" --check-config %*
pause
