#!/usr/bin/env python3
"""
Studio SFX Synthesizer (48 kHz Stereo WAV Assets)
=================================================
Generates crisp, copyright-free short-form editing sound effects using NumPy/SciPy:
  - whoosh  : smooth bandpass air sweep for cuts & whip-pan transitions
  - boom    : punchy 110 Hz -> 38 Hz sub-bass drop + transient click
  - pop     : bright harmonic UI / caption pop ding
  - impact  : heavy cinematic thud + filtered air burst for climax moments
  - riser   : 1.2 s tension sweep building into a reveal or peak
  - glitch  : crisp digital stutter snap for gaming jump cuts
"""

import wave
from pathlib import Path
from typing import Dict, List

import numpy as np
from scipy.signal import butter, sosfilt

SR = 48000

SFX_CATALOG: List[Dict[str, str]] = [
    {"id": "whoosh", "name": "Whoosh", "tag": "Transition", "duration_label": "0.38s"},
    {"id": "boom", "name": "Bass Boom", "tag": "Reveal / Drop", "duration_label": "0.75s"},
    {"id": "vine_boom", "name": "Vine Boom", "tag": "Shock / Meme", "duration_label": "0.85s"},
    {"id": "pop", "name": "Pop Ding", "tag": "Caption Pop", "duration_label": "0.18s"},
    {"id": "impact", "name": "Hard Impact", "tag": "Climax / Crash", "duration_label": "0.55s"},
    {"id": "riser", "name": "Tension Riser", "tag": "Pre-Peak Build", "duration_label": "1.20s"},
    {"id": "glitch", "name": "Glitch Snap", "tag": "Gaming Cut", "duration_label": "0.22s"},
    {"id": "record_scratch", "name": "Record Scratch", "tag": "Awkward Fail", "duration_label": "0.42s"},
    {"id": "laugh_sting", "name": "Fail Slide", "tag": "Comedy Sting", "duration_label": "0.48s"},
    {"id": "cash_ding", "name": "Win Chime", "tag": "Clutch / Win", "duration_label": "0.34s"},
]


def _write_stereo_wav(path: Path, left: np.ndarray, right: np.ndarray | None = None) -> None:
    if right is None:
        right = left
    peak = max(float(np.max(np.abs(left))), float(np.max(np.abs(right))), 1e-6)
    scale = 0.88 / peak
    l16 = np.clip(left * scale * 32767.0, -32767, 32767).astype(np.int16)
    r16 = np.clip(right * scale * 32767.0, -32767, 32767).astype(np.int16)
    interleaved = np.empty(l16.size * 2, dtype=np.int16)
    interleaved[0::2] = l16
    interleaved[1::2] = r16
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(interleaved.tobytes())


def _make_whoosh() -> tuple[np.ndarray, np.ndarray]:
    dur = 0.38
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    rng = np.random.default_rng(101)
    noise = rng.standard_normal(n)
    # Time-varying swept low/high layers for stereo air whoosh
    low_sos = butter(4, [180, 900], btype="bandpass", fs=SR, output="sos")
    hi_sos = butter(4, [900, 4200], btype="bandpass", fs=SR, output="sos")
    low = sosfilt(low_sos, noise)
    hi = sosfilt(hi_sos, noise)
    env = np.exp(-0.5 * ((t - 0.17) / 0.085) ** 2)
    hi_env = np.exp(-0.5 * ((t - 0.20) / 0.06) ** 2)
    sig = 0.65 * low * env + 0.55 * hi * hi_env
    # Subtle left-to-right stereo pan
    pan = np.linspace(0.32, 0.68, n)
    return sig * (1.0 - pan), sig * pan


def _make_boom() -> tuple[np.ndarray, np.ndarray]:
    dur = 0.75
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    # Pitch drops exponentially from 115 Hz down to 36 Hz
    freq = 36.0 + 79.0 * np.exp(-t * 9.5)
    phase = 2.0 * np.pi * np.cumsum(freq) / SR
    sub = np.sin(phase) * np.exp(-t * 4.4)
    # Short punchy attack transient + warm saturation
    rng = np.random.default_rng(202)
    click = sosfilt(butter(3, 1800, btype="lowpass", fs=SR, output="sos"), rng.standard_normal(n))
    click *= np.exp(-t * 48.0) * 0.45
    sig = np.tanh((sub + click) * 1.45)
    # Smooth 4 ms fade-in to prevent DC click
    fade_in = np.minimum(1.0, t / 0.004)
    sig *= fade_in
    return sig, sig


def _make_pop() -> tuple[np.ndarray, np.ndarray]:
    dur = 0.18
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    freq = 720.0 + 580.0 * (1.0 - np.exp(-t * 45.0))
    phase = 2.0 * np.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * np.exp(-t * 24.0)
    overtone = 0.35 * np.sin(phase * 2.0) * np.exp(-t * 36.0)
    fade_in = np.minimum(1.0, t / 0.003)
    sig = (body + overtone) * fade_in
    return sig, sig


def _make_impact() -> tuple[np.ndarray, np.ndarray]:
    dur = 0.55
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    rng = np.random.default_rng(303)
    freq = 44.0 + 140.0 * np.exp(-t * 18.0)
    phase = 2.0 * np.pi * np.cumsum(freq) / SR
    thud = np.sin(phase) * np.exp(-t * 7.5)
    burst = sosfilt(butter(4, [240, 3400], btype="bandpass", fs=SR, output="sos"), rng.standard_normal(n))
    burst_l = burst * np.exp(-t * 11.0)
    burst_r = sosfilt(butter(4, [280, 3800], btype="bandpass", fs=SR, output="sos"), rng.standard_normal(n)) * np.exp(-t * 11.0)
    fade_in = np.minimum(1.0, t / 0.002)
    left = np.tanh((thud * 1.1 + burst_l * 0.55) * 1.3) * fade_in
    right = np.tanh((thud * 1.1 + burst_r * 0.55) * 1.3) * fade_in
    return left, right


def _make_riser() -> tuple[np.ndarray, np.ndarray]:
    dur = 1.20
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    rng = np.random.default_rng(404)
    progress = t / dur
    # Rising harmonic tone + ascending filtered air shimmer
    freq = 160.0 * (2.0 ** (progress * 2.3))
    phase = 2.0 * np.pi * np.cumsum(freq) / SR
    tone = (0.55 * np.sin(phase) + 0.25 * np.sin(phase * 1.5)) * (progress ** 1.8)
    air = sosfilt(butter(4, [600, 6200], btype="bandpass", fs=SR, output="sos"), rng.standard_normal(n))
    air *= (progress ** 2.2) * 0.65
    # Sharp tail drop in last 25 ms so it cuts cleanly into the payoff
    tail = np.clip((dur - t) / 0.025, 0.0, 1.0)
    sig = (tone + air) * tail
    return sig, sig * 0.96


def _make_glitch() -> tuple[np.ndarray, np.ndarray]:
    dur = 0.22
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    rng = np.random.default_rng(505)
    # Gated digital pulses at 32 Hz with metallic pitch chirp
    gate = (np.sin(2.0 * np.pi * 34.0 * t) > -0.1).astype(np.float32)
    chirp = np.sign(np.sin(2.0 * np.pi * (420.0 + 1800.0 * (t / dur)) * t)) * 0.35
    grit = sosfilt(butter(4, [1100, 7500], btype="bandpass", fs=SR, output="sos"), rng.standard_normal(n)) * 0.7
    env = np.exp(-t * 9.0) * gate
    fade_in = np.minimum(1.0, t / 0.002)
    left = (chirp + grit) * env * fade_in
    right = (chirp * 0.85 - grit * 0.9) * env * fade_in
    return left, right


def _make_vine_boom() -> tuple[np.ndarray, np.ndarray]:
    dur = 0.85
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    # Iconic resonant deep sub hit with long moody room tail
    freq = 41.0 + 95.0 * np.exp(-t * 14.0)
    phase = 2.0 * np.pi * np.cumsum(freq) / SR
    sub = np.sin(phase) * np.exp(-t * 3.1)
    harm = 0.35 * np.sin(phase * 2.0) * np.exp(-t * 6.5)
    rng = np.random.default_rng(606)
    room = sosfilt(butter(3, [80, 420], btype="bandpass", fs=SR, output="sos"), rng.standard_normal(n))
    room *= np.exp(-t * 4.5) * 0.42
    sig = np.tanh((sub + harm + room) * 1.65) * np.minimum(1.0, t / 0.002)
    return sig, sig * 0.98


def _make_record_scratch() -> tuple[np.ndarray, np.ndarray]:
    dur = 0.42
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    # Rapid downward vinyl scrub pitch + vinyl grit
    freq = 1250.0 * np.exp(-t * 5.5) * (1.0 + 0.35 * np.sin(2.0 * np.pi * 16.0 * t))
    phase = 2.0 * np.pi * np.cumsum(freq) / SR
    scrub = np.tanh(np.sin(phase) * 2.4) * np.exp(-t * 4.2)
    rng = np.random.default_rng(607)
    grit = sosfilt(butter(3, [1400, 6200], btype="bandpass", fs=SR, output="sos"), rng.standard_normal(n))
    sig = (scrub * 0.8 + grit * np.exp(-t * 6.0) * 0.35) * np.minimum(1.0, t / 0.004)
    return sig, sig


def _make_laugh_sting() -> tuple[np.ndarray, np.ndarray]:
    dur = 0.48
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    # Classic descending comedic slide sting
    freq = 640.0 * (0.48 ** (t / dur))
    phase = 2.0 * np.pi * np.cumsum(freq) / SR
    sig = (np.sin(phase) + 0.38 * np.sin(phase * 2.0)) * np.exp(-t * 3.8)
    fade_in = np.minimum(1.0, t / 0.008)
    fade_out = np.clip((dur - t) / 0.04, 0.0, 1.0)
    out = np.tanh(sig * 1.2) * fade_in * fade_out
    return out, out


def _make_cash_ding() -> tuple[np.ndarray, np.ndarray]:
    dur = 0.34
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    # Bright two-tone metallic win chime (E6 -> B6)
    s1 = np.sin(2.0 * np.pi * 1318.5 * t) * np.exp(-t * 14.0)
    t2 = np.maximum(0.0, t - 0.065)
    s2 = (t >= 0.065) * (np.sin(2.0 * np.pi * 1975.5 * t2) + 0.3 * np.sin(2.0 * np.pi * 3951.0 * t2)) * np.exp(-t2 * 11.0)
    sig = (s1 * 0.65 + s2 * 0.85) * np.minimum(1.0, t / 0.002)
    return sig, sig * 0.96


_BUILDERS = {
    "whoosh": _make_whoosh,
    "boom": _make_boom,
    "vine_boom": _make_vine_boom,
    "pop": _make_pop,
    "impact": _make_impact,
    "riser": _make_riser,
    "glitch": _make_glitch,
    "record_scratch": _make_record_scratch,
    "laugh_sting": _make_laugh_sting,
    "cash_ding": _make_cash_ding,
}


def ensure_sfx_assets(sfx_dir: Path) -> Dict[str, Path]:
    """Generate any missing built-in SFX WAV files and return {sfx_id: Path}."""
    sfx_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, Path] = {}
    for sfx_id, fn in _BUILDERS.items():
        wav_path = sfx_dir / f"{sfx_id}.wav"
        if not wav_path.exists() or wav_path.stat().st_size < 1024:
            left, right = fn()
            _write_stereo_wav(wav_path, left, right)
        paths[sfx_id] = wav_path
    return paths
