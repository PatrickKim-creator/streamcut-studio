#!/usr/bin/env bash
set -euo pipefail

# StreamCut Studio — One-Command Local Setup (macOS / Linux)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "=== StreamCut Studio Setup ==="

# 1. Verify Python 3
PYTHON_CMD=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    PYTHON_CMD="$(command -v "$cand")"
    break
  fi
done
if [[ -z "$PYTHON_CMD" ]]; then
  echo "ERROR: Python 3.10+ is required. Install it from https://www.python.org/downloads/ or via Homebrew."
  exit 1
fi
echo "Using Python: $($PYTHON_CMD --version)"

# 2. Verify FFmpeg + FFprobe
if ! command -v ffmpeg >/dev/null 2>&1 && [[ ! -x /opt/homebrew/bin/ffmpeg ]]; then
  echo "WARNING: ffmpeg not found on PATH."
  if command -v brew >/dev/null 2>&1; then
    echo "Installing ffmpeg via Homebrew..."
    brew install ffmpeg
  else
    echo "Please install FFmpeg (https://ffmpeg.org/download.html) before running ./start.sh"
  fi
else
  echo "Found FFmpeg: $(command -v ffmpeg || echo /opt/homebrew/bin/ffmpeg)"
fi

# 3. Create isolated virtual environment & install Python dependencies
if [[ ! -d "$REPO_DIR/.venv" ]]; then
  echo "Creating virtual environment in .venv/ ..."
  "$PYTHON_CMD" -m venv "$REPO_DIR/.venv"
fi

"$REPO_DIR/.venv/bin/python" -m pip install --upgrade pip -q
echo "Installing Python dependencies from requirements.txt..."
"$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

# 4. Check OpenAI Whisper (optional local spoken auto-captions)
if command -v whisper >/dev/null 2>&1 || [[ -x /opt/homebrew/bin/whisper ]] || [[ -x "$REPO_DIR/.venv/bin/whisper" ]]; then
  echo "Found Whisper CLI for local spoken auto-captions."
else
  echo "Whisper CLI not found yet (optional for spoken auto-captions)."
  echo "  - macOS (recommended): brew install openai-whisper"
  echo "  - Python 3.10-3.12:    .venv/bin/pip install openai-whisper"
fi

# 5. Run automated test suite to verify installation
echo ""
echo "Running automated verification tests..."
(cd "$REPO_DIR/clipper_bot" && "$REPO_DIR/.venv/bin/python" -m unittest test_clipper_bot.py)
(cd "$REPO_DIR/shorts_studio" && "$REPO_DIR/.venv/bin/python" -m unittest test_editor.py)
(cd "$REPO_DIR/lead_finder" && "$REPO_DIR/.venv/bin/python" -m unittest test_find_leads.py)

echo ""
echo "Setup complete! Start the studio anytime with:"
echo "  ./start.sh"
