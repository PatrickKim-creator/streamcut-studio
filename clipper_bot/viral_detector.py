#!/usr/bin/env python3
"""
Tri-Signal Live Virality Detector (Chat Reactions + Audio Reactions + Visual Action)
===================================================================================
Evaluates rolling stream segments using adaptive per-streamer baselines so screams,
laughter explosions, crashes, and genuine chat reaction surges trigger clips while
normal commentary, loud game music, giveaway spam, and sub-alert floods do not.

Audio is measured in dBFS over 0.5 s bins and compared to the streamer's own recent
segments with a robust median/MAD baseline, so one loud moment doesn't desensitise
the detector for the next few minutes.
"""

import re
import shutil
import statistics
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, List, Optional

import numpy as np

from chat_watcher import ChatSnapshot

FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg" if shutil.which("/opt/homebrew/bin/ffmpeg") else (shutil.which("ffmpeg") or "ffmpeg")

SILENCE_DB = -48.0
AUDIO_WARMUP_SEGMENTS = 3


@dataclass
class AudioStats:
    peak_db: float          # mean loudness of the loudest 1 s (two 0.5 s bins), dBFS
    median_db: float        # typical loudness inside the segment, dBFS
    crest: float            # peak RMS / median RMS (sudden-spike sharpness)
    peak_offset_sec: float  # where in the segment the loudest moment is
    duration_sec: float


@dataclass
class SegmentEvaluation:
    segment_path: str
    viral_score: float
    should_clip: bool
    audio_rms: float
    audio_z: float
    chat_z: float
    chat_mps: float
    visual_score: float
    peak_offset_sec: float
    trigger_reason: str
    top_chat_messages: List[str]
    duration_sec: float = 0.0
    chat_reactors: int = 0
    chat_suppressed: bool = False
    audio_peak_db: float = -90.0


def _to_db(rms: float) -> float:
    return float(20.0 * np.log10(max(1e-5, rms)))


def analyze_segment_audio(segment_path: Path) -> AudioStats:
    """Decode 8 kHz mono PCM via ffmpeg and summarise loudness in 0.5 s bins."""
    cmd = [
        FFMPEG_BIN, "-v", "error", "-i", str(segment_path),
        "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-",
    ]
    empty = AudioStats(-90.0, -90.0, 1.0, 5.0, 0.0)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=12)
        if proc.returncode != 0 or not proc.stdout:
            return empty
        samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        duration = samples.size / 8000.0
        if samples.size < 4000:
            return AudioStats(-90.0, -90.0, 1.0, 0.0, duration)

        win = 4000  # 0.5 s
        n_bins = samples.size // win
        rms_bins = np.sqrt(np.mean(samples[: n_bins * win].reshape(n_bins, win) ** 2, axis=1))
        top2 = np.sort(rms_bins)[-2:] if n_bins >= 2 else rms_bins
        peak_rms = float(np.mean(top2))
        median_rms = float(np.median(rms_bins))

        # Smooth over 1 s so the peak marker lands on a sustained reaction, not a click
        smooth = np.convolve(rms_bins, np.ones(2) / 2.0, mode="same") if n_bins >= 2 else rms_bins
        peak_idx = int(np.argmax(smooth))
        return AudioStats(
            peak_db=round(_to_db(peak_rms), 2),
            median_db=round(_to_db(median_rms), 2),
            crest=round(min(8.0, float(np.max(rms_bins)) / max(1e-4, median_rms)), 2),
            peak_offset_sec=round((peak_idx + 0.5) * 0.5, 2),
            duration_sec=round(duration, 3),
        )
    except Exception:
        return empty


def analyze_segment_visual(segment_path: Path) -> float:
    """Estimate visual scene-cut / action intensity (0.0 to 1.0) using fast downscaled scdet."""
    cmd = [
        FFMPEG_BIN, "-v", "info", "-i", str(segment_path),
        "-vf", "scale=240:-2,scdet=t=11", "-an", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        log = (proc.stderr or "") + (proc.stdout or "")
        cuts = len(re.findall(r"lavfi\.scd\.time:", log))
        return round(min(1.0, 0.20 + cuts * 0.22), 2)
    except Exception:
        return 0.40


class StreamViralityDetector:
    """Maintains rolling per-streamer audio baseline and fuses Chat + Audio + Visual signals."""

    def __init__(
        self,
        score_threshold: float = 68.0,
        chat_z_threshold: float = 2.4,
        audio_z_threshold: float = 2.2,
    ):
        self.score_threshold = score_threshold
        self.chat_z_threshold = chat_z_threshold
        self.audio_z_threshold = audio_z_threshold
        self._audio_history: Deque[float] = deque(maxlen=30)

    @property
    def audio_warmed_up(self) -> bool:
        return len(self._audio_history) >= AUDIO_WARMUP_SEGMENTS

    def _compute_audio_z(self, stats: AudioStats) -> float:
        if stats.peak_db <= SILENCE_DB:
            return -1.0  # muted / silent segments never spike (and don't pollute the baseline)
        # Sharp spikes (screams, crashes) count a little extra vs. constant loud music
        metric = stats.peak_db + 1.5 * max(0.0, min(3.0, stats.crest - 1.5))
        hist = list(self._audio_history)
        self._audio_history.append(metric)
        if len(hist) < AUDIO_WARMUP_SEGMENTS:
            return 0.0  # warm-up: learn this streamer's mic & game volume first
        med = statistics.median(hist)
        mad = statistics.median([abs(x - med) for x in hist]) * 1.4826
        std = max(1.5, mad)  # in dB: never treat <1.5 dB wiggles as a reaction
        return round(max(-2.0, min(8.0, (metric - med) / std)), 2)

    def evaluate_segment(
        self,
        segment_path: Path,
        chat_snap: Optional[ChatSnapshot] = None,
        run_visual: bool = True,
    ) -> SegmentEvaluation:
        stats = analyze_segment_audio(segment_path)
        audio_z = self._compute_audio_z(stats)

        has_active_chat = bool(chat_snap and chat_snap.connected and chat_snap.total_messages >= 5)
        chat_z = chat_snap.chat_z if has_active_chat else 0.0
        chat_mps = chat_snap.current_mps if chat_snap else 0.0
        top_msgs = list(chat_snap.top_messages) if chat_snap else []

        if run_visual and (audio_z >= 1.0 or chat_z >= 1.2 or not has_active_chat):
            visual_score = analyze_segment_visual(segment_path)
        else:
            visual_score = 0.35

        # Normalise z-scores into 0..100 sub-scores (z=0 -> 35, z=2.5 -> 80, z>=3.6 -> 100)
        audio_pts = max(0.0, min(100.0, 35.0 + audio_z * 18.0))
        chat_pts = max(0.0, min(100.0, 35.0 + chat_z * 18.0))
        visual_pts = visual_score * 100.0

        if has_active_chat:
            viral_score = 0.45 * chat_pts + 0.35 * audio_pts + 0.20 * visual_pts
        else:
            # YouTube / non-IRC streams rely on Audio + Visual action; give a small synergy boost when either spikes
            synergy = 6.0 if (audio_z >= 1.2 or visual_score >= 0.60) else 0.0
            viral_score = 0.72 * audio_pts + 0.28 * visual_pts + synergy
        viral_score = round(min(99.5, max(0.0, viral_score)), 1)

        reasons = []
        if has_active_chat and chat_z >= self.chat_z_threshold:
            reasons.append(f"chat_spike_{chat_z:.1f}z")
        if audio_z >= self.audio_z_threshold:
            reasons.append(f"audio_hype_{audio_z:.1f}z")
        if visual_score >= 0.60:
            reasons.append("high_action")

        reactors = chat_snap.reaction_users if chat_snap else 0
        min_floor = max(50.0, self.score_threshold - 7.0)
        should_clip = (
            viral_score >= self.score_threshold
            # Real crowd reaction burst (requires solid combined score so weak noise doesn't sneak in)
            or (has_active_chat and chat_z >= self.chat_z_threshold + 0.8 and reactors >= 5 and viral_score >= min_floor)
            # Loud streamer scream / laugh explosion (even on a steady camera without scene cuts!)
            or (audio_z >= self.audio_z_threshold + 1.1 and viral_score >= min_floor)
            # Non-IRC / YouTube stream audio or high-action trigger
            or (not has_active_chat and (audio_z >= self.audio_z_threshold or (visual_score >= 0.64 and audio_z >= 0.8)) and viral_score >= min_floor)
        )
        reason_str = "+".join(reasons) if reasons else f"score_{viral_score:.0f}"

        # Peak marker: loud audio reaction if there is one, otherwise densest chat burst
        dur = stats.duration_sec or 10.0
        peak = stats.peak_offset_sec
        if audio_z < 1.0 and has_active_chat and chat_snap and chat_snap.peak_offset_sec >= 0 and chat_z >= 1.0:
            peak = chat_snap.peak_offset_sec
        peak = round(max(0.0, min(dur, peak)), 2)

        return SegmentEvaluation(
            segment_path=str(segment_path),
            viral_score=viral_score,
            should_clip=should_clip,
            audio_rms=round(10 ** (stats.peak_db / 20.0), 4),
            audio_z=audio_z,
            chat_z=chat_z,
            chat_mps=chat_mps,
            visual_score=visual_score,
            peak_offset_sec=peak,
            trigger_reason=reason_str,
            top_chat_messages=top_msgs,
            duration_sec=stats.duration_sec,
            chat_reactors=chat_snap.reaction_users if chat_snap else 0,
            chat_suppressed=bool(chat_snap and chat_snap.suppressed),
            audio_peak_db=stats.peak_db,
        )
