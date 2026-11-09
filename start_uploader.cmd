@echo off
cd /d "%~dp0"
set PYTHONNOUSERSITE=1
"D:\codex\envs\gate-project\python.exe" -s "%~dp0forward_vehicle_events.py" %*
pause
