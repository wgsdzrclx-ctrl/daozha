@echo off
cd /d "%~dp0"
set PYTHONNOUSERSITE=1
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  "D:\codex\envs\gate-project\python.exe" -s -m venv --system-site-packages .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -s -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Start start_backend_receiver.cmd.
pause
exit /b 0
:failed
echo Setup failed. Please check the error above.
pause
exit /b 1
