@echo off
setlocal
cd /d "%~dp0"

echo === StreamCut Studio Setup (Windows) ===

where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python 3.10+ not found on PATH. Install from https://www.python.org/downloads/
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo WARNING: ffmpeg not found on PATH. Install with: winget install Gyan.FFmpeg
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment in .venv ...
  python -m venv .venv
)

.venv\Scripts\python.exe -m pip install --upgrade pip -q
.venv\Scripts\pip.exe install -r requirements.txt

echo.
echo Optional spoken auto-captions: .venv\Scripts\pip.exe install openai-whisper
echo Setup complete! Run start.bat to launch StreamCut Studio.
endlocal
