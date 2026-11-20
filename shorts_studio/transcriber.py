#!/usr/bin/env python3
"""
Local OpenAI Whisper Auto-Caption Transcriber (Word-Level Timestamps)
=====================================================================
Runs Homebrew OpenAI Whisper (/opt/homebrew/bin/whisper) locally on the clip's
audio track to generate true spoken captions with word-level timestamps — never
chat-spam filler — and caches transcripts by file content hash so re-loading or
re-editing a clip is instant.
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional


def _find_bin(name: str) -> Optional[str]:
    venv_dir = Path(sys.executable).resolve().parent
    for cand in (venv_dir / name, venv_dir / f"{name}.exe", Path("/opt/homebrew/bin") / name):
        if cand.exists():
            return str(cand)
    return shutil.which(name)


WHISPER_BIN = _find_bin("whisper")
FFMPEG_BIN = _find_bin("ffmpeg") or "ffmpeg"

# Common Whisper silence hallucinations to drop cleanly
_HALLUCINATION_RE = re.compile(
    r"^(thank(s| you)( for watching)?|subscribe|please subscribe|music|applause|laughter|you|\.+)$",
    re.IGNORECASE,
)


def _file_cache_key(path: Path) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _pick_cached_model() -> str:
    """Prefer already-cached English/multilingual Whisper models to avoid network waits."""
    cache_dir = Path.home() / ".cache" / "whisper"
    for candidate in ("base.en", "base", "tiny.en", "tiny", "small.en", "small"):
        if (cache_dir / f"{candidate}.pt").exists():
            return candidate
    return "base.en"


def _group_words_into_cues(words: List[Dict], max_words: int = 4, max_gap: float = 0.42) -> List[Dict]:
    """
    Chunk word-level timestamps into punchy 2-4 word YouTube Shorts caption cues,
    breaking naturally on punctuation or brief speech pauses while preserving exact
    per-word start/end times for karaoke active-word highlighting.
    """
    cues: List[Dict] = []
    cur: List[Dict] = []

    def _flush() -> None:
        if not cur:
            return
        text = " ".join(w["word"] for w in cur).strip()
        clean = re.sub(r"\s+([,.!?])", r"\1", text)
        if not clean or _HALLUCINATION_RE.match(clean.strip(" .!?")):
            cur.clear()
            return
        start = round(max(0.0, float(cur[0]["start"])), 3)
        end = round(max(start + 0.22, float(cur[-1]["end"])), 3)
        cues.append({
            "id": f"cap_{len(cues) + 1}",
            "start": start,
            "end": end,
            "text": clean,
            "words": [
                {"word": w["word"], "start": round(float(w["start"]), 3), "end": round(float(w["end"]), 3)}
                for w in cur
            ],
        })
        cur.clear()

    for w in words:
        tok = str(w.get("word") or "").strip()
        if not tok:
            continue
        w_s = float(w.get("start") or 0.0)
        w_e = max(w_s + 0.06, float(w.get("end") or (w_s + 0.18)))
        if cur:
            gap = w_s - float(cur[-1]["end"])
            if gap > max_gap or len(cur) >= max_words:
                _flush()
        cur.append({"word": tok, "start": w_s, "end": w_e})
        if tok[-1] in ".?!":
            _flush()
    _flush()
    return cues


def get_cached_transcript(
    video_path: Path,
    cache_dir: Optional[Path] = None,
) -> Optional[Dict]:
    """Return already-cached Whisper transcript in 0ms if available, else None."""
    if not cache_dir or not video_path.exists():
        return None
    cache_file = cache_dir / f"{_file_cache_key(video_path)}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data.get("ok"):
                return data
        except Exception:
            pass
    return None


def transcribe_video(
    video_path: Path,
    cache_dir: Optional[Path] = None,
    model: Optional[str] = None,
    timeout: int = 90,
) -> Dict:
    """
    Transcribe spoken dialogue from `video_path` using local Whisper CLI.
    Returns {"ok": bool, "model": str, "full_text": str, "cues": [...], "words": [...]}.
    """
    if not WHISPER_BIN or not video_path.exists():
        return {"ok": False, "model": "", "full_text": "", "cues": [], "words": [], "error": "Whisper unavailable"}

    cache_file: Optional[Path] = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{_file_cache_key(video_path)}.json"
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass

    chosen_model = model or _pick_cached_model()
    with tempfile.TemporaryDirectory(prefix="whisper_cap_") as td:
        tmp = Path(td)
        # Extract lightweight 16 kHz mono WAV first (faster & cleaner for Whisper)
        wav = tmp / "audio.wav"
        subprocess.run(
            [
                FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
                "-af", "highpass=f=85,dynaudnorm=f=150:g=11",
                str(wav),
            ],
            check=True,
            timeout=25,
        )

        cmd = [
            WHISPER_BIN,
            str(wav),
            "--model", chosen_model,
            "--language", "en",
            "--word_timestamps", "True",
            "--fp16", "False",
            "--condition_on_previous_text", "False",
            "--no_speech_threshold", "0.55",
            "--output_format", "json",
            "--output_dir", str(tmp),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        json_out = tmp / "audio.json"
        if proc.returncode != 0 or not json_out.exists():
            return {
                "ok": False,
                "model": chosen_model,
                "full_text": "",
                "cues": [],
                "words": [],
                "error": (proc.stderr or proc.stdout or "Whisper failed")[-300:],
            }

        data = json.loads(json_out.read_text(encoding="utf-8"))
        words: List[Dict] = []
        for seg in data.get("segments") or []:
            if float(seg.get("no_speech_prob") or 0.0) > 0.72:
                continue
            seg_words = seg.get("words") or []
            if seg_words:
                for w in seg_words:
                    tok = str(w.get("word") or "").strip()
                    if not tok:
                        continue
                    words.append({
                        "word": tok,
                        "start": round(float(w.get("start") or seg.get("start") or 0.0), 3),
                        "end": round(float(w.get("end") or seg.get("end") or 0.0), 3),
                    })
            else:
                # Fallback if Whisper returned a segment without word breakdown
                txt = str(seg.get("text") or "").strip()
                toks = txt.split()
                if toks:
                    s0 = float(seg.get("start") or 0.0)
                    e0 = max(s0 + 0.2, float(seg.get("end") or (s0 + 1.0)))
                    step = (e0 - s0) / len(toks)
                    for i, tk in enumerate(toks):
                        words.append({"word": tk, "start": round(s0 + i * step, 3), "end": round(s0 + (i + 1) * step, 3)})

        cues = _group_words_into_cues(words, max_words=4)
        result = {
            "ok": True,
            "model": chosen_model,
            "full_text": str(data.get("text") or "").strip(),
            "cues": cues,
            "words": words,
        }
        if cache_file:
            try:
                cache_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
            except Exception:
                pass
        return result


def map_words_to_timeline(words: List[Dict], segments: List[Dict], max_words: int = 4) -> List[Dict]:
    """
    Remap source-video Whisper word timestamps cleanly onto the edited NLE timeline
    (accounting for cut dead-air segments and speed ramps) so captions stay lip-synced.
    """
    if not words or not segments:
        return []
    mapped: List[Dict] = []
    tl_cursor = 0.0
    for seg in segments:
        s = float(seg.get("start", 0.0))
        e = float(seg.get("end", 0.0))
        sp = max(0.5, min(2.5, float(seg.get("speed") or 1.0)))
        out_dur = max(0.1, (e - s) / sp)
        for w in words:
            ws = float(w.get("start", 0.0))
            we = float(w.get("end", ws + 0.15))
            mid = (ws + we) * 0.5
            if s - 0.05 <= mid <= e + 0.05:
                ns = tl_cursor + max(0.0, ws - s) / sp
                ne = tl_cursor + min(e - s, max(ws - s + 0.06, we - s)) / sp
                mapped.append({"word": w["word"], "start": round(ns, 3), "end": round(max(ns + 0.06, ne), 3)})
        tl_cursor += out_dur
    return _group_words_into_cues(mapped, max_words=max_words)
