@echo off
cd /d "%~dp0"
set PYTHONNOUSERSITE=1
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" -s "%~dp0camera_receiver.py" --config "%~dp0config.ini" --collection %*
pause
