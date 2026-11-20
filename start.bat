@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Run setup.bat first.
  exit /b 1
)

set OMP_NUM_THREADS=4
set MKL_NUM_THREADS=4
set PYTHONUNBUFFERED=1

echo ================================================================
echo   StreamCut Studio - 24/7 Live Clipper + YouTube Shorts Studio
echo ================================================================
echo   Open in browser: http://127.0.0.1:8790/
echo   Press Ctrl+C to stop cleanly.
echo ================================================================

cd shorts_studio
..\.venv\Scripts\python.exe server.py --port 8790
endlocal
