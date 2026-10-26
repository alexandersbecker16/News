@echo off
cd /d "%~dp0"
echo Starting Source Desk from %cd%
echo.
start /b python -c "import time,webbrowser; time.sleep(1.2); webbrowser.open('http://127.0.0.1:8765')"
python app.py
if errorlevel 1 (
  echo.
  echo Python failed to start. Make sure Python is installed and on your PATH.
  pause
)
