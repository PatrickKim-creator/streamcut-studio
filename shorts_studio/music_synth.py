#!/usr/bin/env python3
"""
Copyright-Free Viral Shorts Background Music Synthesizer (48 kHz Stereo WAV)
============================================================================
Synthesizes 5 loop-ready, genre-authentic viral Shorts background tracks using
NumPy/SciPy so every edited Short can automatically get mood-matched BGM
(sidechain-ducked under the streamer's voice so speech stays 100% clear):

  - phonk_drift.wav    : 142 BPM Drift/Brazilian Phonk (808 cowbell + sub kick + hats)
  - vice_synthwave.wav : 116 BPM Vice City 80s Neon Synthwave (pulsing bass + retro arp)
  - comedy_bounce.wav  : 124 BPM Quirky Viral Comedy Pluck (pizzicato bounce + rim clicks)
  - tension_dark.wav   : 128 BPM Dark Suspense / Heist Pulse (sub heartbeat + minor drone)
  - trap_hype.wav      : 134 BPM Modern Gaming Trap (808 glide + crisp hi-hat rolls)
"""

import wave
from pathlib import Path
from typing import Dict, List

import numpy as np
from scipy.signal import butter, sosfilt

SR = 48000

BGM_CATALOG: List[Dict[str, str]] = [
    {"id": "phonk_drift", "file": "phonk_drift.wav", "name": "Phonk Drift (Action / Chaos)", "vibe": "action_hype", "bpm": "142"},
    {"id": "comedy_bounce", "file": "comedy_bounce.wav", "name": "Comedy Bounce (Funny / Fails)", "vibe": "comedy_fail", "bpm": "124"},
    {"id": "tension_dark", "file": "tension_dark.wav", "name": "Dark Suspense (Clutch / Heist)", "vibe": "suspense_clutch", "bpm": "128"},
    {"id": "vice_synthwave", "file": "vice_synthwave.wav", "name": "Vice City Synthwave (Cruising)", "vibe": "vice_vibe", "bpm": "116"},
    {"id": "trap_hype", "file": "trap_hype.wav", "name": "Gaming Trap Hype (All-Round)", "vibe": "general_hype", "bpm": "134"},
]


def _midi_hz(m: float) -> float:
    return 440.0 * (2.0 ** ((m - 69.0) / 12.0))


def _mix_at(buf: np.ndarray, sig: np.ndarray, pos: int, gain: float = 1.0) -> None:
    if pos >= buf.size:
        return
    end = min(buf.size, pos + sig.size)
    if end > pos:
        buf[pos:end] += sig[: end - pos] * gain


def _kick(dur: float = 0.28, f_hi: float = 135.0, f_lo: float = 42.0, drive: float = 1.8) -> np.ndarray:
    n = int(SR * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    freq = f_lo + (f_hi - f_lo) * np.exp(-t * 28.0)
    phase = 2.0 * np.pi * np.cumsum(freq) / SR
    sig = np.sin(phase) * np.exp(-t * 9.5)
    return np.tanh(sig * drive) * np.minimum(1.0, t / 0.002)


def _snare(rng: np.random.Generator, dur: float = 0.16, tone_hz: float = 185.0) -> np.ndarray:
    n = int(SR * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    noise = sosfilt(butter(3, [900, 7200], btype="bandpass", fs=SR, output="sos"), rng.standard_normal(n))
    body = np.sin(2.0 * np.pi * tone_hz * t) * np.exp(-t * 28.0)
    return (noise * np.exp(-t * 19.0) * 0.75 + body * 0.45) * np.minimum(1.0, t / 0.002)


def _hihat(rng: np.random.Generator, dur: float = 0.055) -> np.ndarray:
    n = int(SR * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    noise = sosfilt(butter(4, 6500, btype="highpass", fs=SR, output="sos"), rng.standard_normal(n))
    return noise * np.exp(-t * 58.0) * 0.45


def _cowbell(midi: float, dur: float = 0.19) -> np.ndarray:
    """Signature 808 Phonk cowbell (detuned twin square waves through tight bandpass)."""
    n = int(SR * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    f1 = _midi_hz(midi)
    f2 = f1 * 1.498
    sq = np.sign(np.sin(2.0 * np.pi * f1 * t)) * 0.6 + np.sign(np.sin(2.0 * np.pi * f2 * t)) * 0.4
    filt = sosfilt(butter(3, [max(250, f1 * 0.75), min(6000, f2 * 2.1)], btype="bandpass", fs=SR, output="sos"), sq)
    env = np.exp(-t * 15.0) * np.minimum(1.0, t / 0.003)
    return np.tanh(filt * 1.7) * env


def _pluck(midi: float, dur: float = 0.24, bright: float = 1.0) -> np.ndarray:
    n = int(SR * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    f = _midi_hz(midi)
    sig = (
        np.sin(2 * np.pi * f * t)
        + 0.45 * bright * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t * 14.0)
        + 0.22 * bright * np.sin(2 * np.pi * 3 * f * t) * np.exp(-t * 22.0)
    )
    return sig * np.exp(-t * 11.5) * np.minimum(1.0, t / 0.003)


def _sub808(midi: float, dur: float = 0.48, drive: float = 2.0) -> np.ndarray:
    n = int(SR * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    f = _midi_hz(midi)
    sig = np.sin(2 * np.pi * f * t) + 0.22 * np.sin(4 * np.pi * f * t)
    env = np.exp(-t * 3.8) * np.minimum(1.0, t / 0.005)
    return np.tanh(sig * drive) * env


def _write_stereo(path: Path, left: np.ndarray, right: np.ndarray) -> None:
    # Seamless loop micro-fade (10 ms) at boundaries so -stream_loop -1 never clicks
    fade = int(SR * 0.01)
    ramp = np.linspace(0.0, 1.0, fade)
    left[:fade] *= ramp
    right[:fade] *= ramp
    left[-fade:] *= ramp[::-1]
    right[-fade:] *= ramp[::-1]

    peak = max(float(np.max(np.abs(left))), float(np.max(np.abs(right))), 1e-6)
    scale = 0.86 / peak
    l16 = np.clip(left * scale * 32767.0, -32767, 32767).astype(np.int16)
    r16 = np.clip(right * scale * 32767.0, -32767, 32767).astype(np.int16)
    inter = np.empty(l16.size * 2, dtype=np.int16)
    inter[0::2] = l16
    inter[1::2] = r16
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(inter.tobytes())


def _build_phonk_drift() -> tuple[np.ndarray, np.ndarray]:
    bpm = 142.0
    step = 60.0 / bpm / 4.0  # 16th note
    n_steps = 64  # 4 bars (~6.76 s loop)
    total = int(SR * step * n_steps)
    l, r = np.zeros(total), np.zeros(total)
    rng = np.random.default_rng(701)

    kk = _kick(0.32, 150, 38, 2.5)
    sn = _snare(rng, 0.16, 195)
    hh = _hihat(rng, 0.045)

    # Dark Phonk C-minor cowbell hook
    mel = [
        60, 0, 63, 0, 67, 0, 66, 63, 60, 0, 63, 0, 70, 0, 67, 0,
        60, 0, 63, 0, 67, 0, 68, 67, 63, 0, 62, 0, 58, 0, 60, 0,
    ]
    bass_notes = [36, 36, 36, 39, 43, 39, 34, 36]

    for s in range(n_steps):
        pos = int(s * step * SR)
        bar_step = s % 16
        if bar_step in (0, 6, 10):
            _mix_at(l, kk, pos, 0.85)
            _mix_at(r, kk, pos, 0.85)
        if bar_step in (4, 12):
            _mix_at(l, sn, pos, 0.65)
            _mix_at(r, sn, pos, 0.65)
        if s % 2 == 0 or bar_step in (11, 15):
            g = 0.28 if s % 2 == 0 else 0.18
            _mix_at(l, hh, pos, g * 0.9)
            _mix_at(r, hh, pos, g * 1.1)
        if s % 8 == 0:
            sub = _sub808(bass_notes[(s // 8) % len(bass_notes)], step * 7.5, 2.3)
            _mix_at(l, sub, pos, 0.62)
            _mix_at(r, sub, pos, 0.62)
        note = mel[s % len(mel)]
        if note:
            cb = _cowbell(note, step * 1.6)
            _mix_at(l, cb, pos, 0.42)
            _mix_at(r, cb, pos + 180, 0.38)
    return l, r


def _build_vice_synthwave() -> tuple[np.ndarray, np.ndarray]:
    bpm = 116.0
    step = 60.0 / bpm / 4.0
    n_steps = 64  # 4 bars (~8.28 s loop)
    total = int(SR * step * n_steps)
    l, r = np.zeros(total), np.zeros(total)
    rng = np.random.default_rng(702)

    kk = _kick(0.24, 120, 48, 1.5)
    sn = _snare(rng, 0.24, 170)
    hh = _hihat(rng, 0.05)

    # Classic Miami Vice A-minor -> F -> C -> G synthwave progression
    roots = [33, 29, 36, 31]
    arps = [
        [57, 60, 64, 69],
        [53, 57, 60, 65],
        [60, 64, 67, 72],
        [55, 59, 62, 67],
    ]
    for s in range(n_steps):
        pos = int(s * step * SR)
        bar = (s // 16) % 4
        bs = s % 16
        if bs % 4 == 0:
            _mix_at(l, kk, pos, 0.72)
            _mix_at(r, kk, pos, 0.72)
        if bs in (4, 12):
            _mix_at(l, sn, pos, 0.58)
            _mix_at(r, sn, pos, 0.58)
        if bs % 2 == 1:
            _mix_at(l, hh, pos, 0.20)
            _mix_at(r, hh, pos, 0.22)
        # Driving 8th-note retro analog bass pulse
        if bs % 2 == 0:
            b = _pluck(roots[bar] + 12, step * 1.8, bright=1.3)
            _mix_at(l, b, pos, 0.45)
            _mix_at(r, b, pos, 0.45)
        # Stereo shimmering 16th arpeggio
        n_midi = arps[bar][bs % 4]
        arp = _pluck(n_midi, step * 1.5, bright=1.5)
        pan = 0.35 + 0.3 * ((bs % 4) / 3.0)
        _mix_at(l, arp, pos, 0.36 * (1.0 - pan))
        _mix_at(r, arp, pos, 0.36 * pan)
    return l, r


def _build_comedy_bounce() -> tuple[np.ndarray, np.ndarray]:
    bpm = 124.0
    step = 60.0 / bpm / 4.0
    n_steps = 64  # ~7.74 s loop
    total = int(SR * step * n_steps)
    l, r = np.zeros(total), np.zeros(total)
    rng = np.random.default_rng(703)

    # Playful bouncy pizzicato bass + staccato marimba melody (classic viral funny Shorts vibe)
    kk = _kick(0.16, 110, 56, 1.2)
    rim = _snare(rng, 0.05, 420)
    bass_line = [
        48, 0, 0, 55, 0, 0, 52, 0, 48, 0, 50, 0, 55, 0, 53, 0,
        45, 0, 0, 52, 0, 0, 48, 0, 43, 0, 47, 0, 50, 0, 55, 0,
    ]
    mel = [
        72, 0, 76, 79, 0, 76, 74, 0, 72, 0, 74, 0, 76, 0, 0, 0,
        69, 0, 72, 76, 0, 74, 72, 0, 71, 0, 74, 0, 79, 0, 76, 0,
    ]
    for s in range(n_steps):
        pos = int(s * step * SR)
        bs = s % 16
        if bs in (0, 8):
            _mix_at(l, kk, pos, 0.45)
            _mix_at(r, kk, pos, 0.45)
        if bs in (4, 12, 14):
            _mix_at(l, rim, pos, 0.36)
            _mix_at(r, rim, pos, 0.32)
        bn = bass_line[s % len(bass_line)]
        if bn:
            p = _pluck(bn, step * 1.9, bright=0.6)
            _mix_at(l, p, pos, 0.58)
            _mix_at(r, p, pos, 0.58)
        mn = mel[s % len(mel)]
        if mn:
            p = _pluck(mn, step * 1.4, bright=1.4)
            _mix_at(l, p, pos, 0.36)
            _mix_at(r, p, pos + 140, 0.34)
    return l, r


def _build_tension_dark() -> tuple[np.ndarray, np.ndarray]:
    bpm = 128.0
    step = 60.0 / bpm / 4.0
    n_steps = 64  # ~7.5 s loop
    total = int(SR * step * n_steps)
    t = np.linspace(0, step * n_steps, total, endpoint=False)
    rng = np.random.default_rng(704)

    # Eerie low D-minor pulse drone + heartbeat sub + tense clock ticks
    drone = (
        0.22 * np.sin(2 * np.pi * _midi_hz(38) * t)
        + 0.14 * np.sin(2 * np.pi * _midi_hz(45) * t)
        + 0.08 * np.sin(2 * np.pi * _midi_hz(53) * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.4 * t))
    )
    l = drone.copy()
    r = drone * 0.96
    hb = _kick(0.35, 92, 34, 1.8)
    tick = _hihat(rng, 0.03)
    notes = [62, 0, 0, 0, 65, 0, 0, 0, 64, 0, 0, 0, 61, 0, 0, 0]

    for s in range(n_steps):
        pos = int(s * step * SR)
        bs = s % 16
        if bs in (0, 3, 8, 11):
            g = 0.72 if bs in (0, 8) else 0.45
            _mix_at(l, hb, pos, g)
            _mix_at(r, hb, pos, g)
        if bs % 2 == 0:
            _mix_at(l, tick, pos, 0.24)
            _mix_at(r, tick, pos, 0.24)
        nm = notes[bs]
        if nm:
            pl = _pluck(nm, step * 3.5, bright=0.7)
            _mix_at(l, pl, pos, 0.25)
            _mix_at(r, pl, pos + 240, 0.25)
    return l, r


def _build_trap_hype() -> tuple[np.ndarray, np.ndarray]:
    bpm = 134.0
    step = 60.0 / bpm / 4.0
    n_steps = 64  # ~7.16 s loop
    total = int(SR * step * n_steps)
    l, r = np.zeros(total), np.zeros(total)
    rng = np.random.default_rng(705)

    kk = _kick(0.28, 130, 40, 2.0)
    sn = _snare(rng, 0.15, 200)
    hh = _hihat(rng, 0.04)
    bells = [64, 0, 67, 0, 71, 0, 67, 0, 62, 0, 67, 0, 69, 0, 67, 0]
    subs = [40, 40, 36, 38]

    for s in range(n_steps):
        pos = int(s * step * SR)
        bs = s % 16
        bar = (s // 16) % 4
        if bs in (0, 7, 10):
            _mix_at(l, kk, pos, 0.80)
            _mix_at(r, kk, pos, 0.80)
        if bs in (4, 12):
            _mix_at(l, sn, pos, 0.62)
            _mix_at(r, sn, pos, 0.62)
        # Modern trap hi-hat roll at end of bar
        if bs % 2 == 0 or bs in (13, 14, 15):
            _mix_at(l, hh, pos, 0.24)
            _mix_at(r, hh, pos, 0.26)
        if bs == 0:
            sub = _sub808(subs[bar], step * 12.0, 1.9)
            _mix_at(l, sub, pos, 0.56)
            _mix_at(r, sub, pos, 0.56)
        bm = bells[bs]
        if bm:
            pl = _pluck(bm + 12, step * 1.9, bright=1.35)
            _mix_at(l, pl, pos, 0.28)
            _mix_at(r, pl, pos + 160, 0.26)
    return l, r


_BGM_BUILDERS = {
    "phonk_drift": _build_phonk_drift,
    "vice_synthwave": _build_vice_synthwave,
    "comedy_bounce": _build_comedy_bounce,
    "tension_dark": _build_tension_dark,
    "trap_hype": _build_trap_hype,
}


def ensure_bgm_assets(bgm_assets_dir: Path) -> Dict[str, Path]:
    """Generate any missing built-in viral BGM WAV loops and return {file_name: Path}."""
    bgm_assets_dir.mkdir(parents=True, exist_ok=True)
    out: Dict[str, Path] = {}
    for item in BGM_CATALOG:
        fname = item["file"]
        wav_path = bgm_assets_dir / fname
        if not wav_path.exists() or wav_path.stat().st_size < 4096:
            l, r = _BGM_BUILDERS[item["id"]]()
            _write_stereo(wav_path, l, r)
        out[fname] = wav_path
    return out
