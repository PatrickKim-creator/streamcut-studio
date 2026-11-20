#!/usr/bin/env python3
"""
Pro Media Analyzer & 1-Click Retention Auto-Edit Planner
========================================================
Analyzes any local MP4/MOV or GTA 6 bot clip:
  1. Probes video/audio metadata via ffprobe (dimensions, fps, duration, audio)
  2. Extracts a 320-bar normalized audio waveform for the NLE timeline
  3. Detects dead-air silence regions via FFmpeg silencedetect
  4. Finds the top energy/reaction peak timestamps
  5. Builds a 1-Click Pro Auto-Edit timeline applying real YouTube Shorts craft:
     - Zero dead-air intro (hooks viewer immediately in frame 1)
     - Tight jump-cuts removing long pauses (>0.7s) with 0.14s natural breathing pad
     - Retention pattern-interrupt punch zooms & climax Flash White hit
     - Timed studio SFX (Pop on Hook, Whoosh on key cut, Impact/Boom on climax)
     - 3-second Hook Banner + YouTube Shorts SEO packaging kit (titles/desc/tags)
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from transcriber import _file_cache_key, get_cached_transcript, map_words_to_timeline, transcribe_video

FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg" if shutil.which("/opt/homebrew/bin/ffmpeg") else (shutil.which("ffmpeg") or "ffmpeg")
FFPROBE_BIN = "/opt/homebrew/bin/ffprobe" if shutil.which("/opt/homebrew/bin/ffprobe") else (shutil.which("ffprobe") or "ffprobe")

CACHE_TRANSCRIPTS_DIR = Path(__file__).resolve().parent / "cache" / "transcripts"
CACHE_ANALYSIS_DIR = Path(__file__).resolve().parent / "cache" / "analysis"

# Empirically measured 16:9 facecam crop boxes (normalized 0..1) for popular streamers
STREAMER_CAM_PRESETS: Dict[str, Optional[Dict[str, float]]] = {
    "kaicenat":    {"cam_x": 0.00, "cam_y": 0.395, "cam_w": 0.205, "cam_h": 0.210},
    "ishowspeed":  {"cam_x": 0.00, "cam_y": 0.420, "cam_w": 0.204, "cam_h": 0.230},
    "jynxzi":      {"cam_x": 0.00, "cam_y": 0.430, "cam_w": 0.242, "cam_h": 0.320},
    "xqc":         {"cam_x": 0.00, "cam_y": 0.498, "cam_w": 0.194, "cam_h": 0.274},
    "summit1g":    {"cam_x": 0.00, "cam_y": 0.482, "cam_w": 0.266, "cam_h": 0.268},
    "caseoh_":     {"cam_x": 0.00, "cam_y": 0.125, "cam_w": 0.210, "cam_h": 0.280},
    "caseoh":      {"cam_x": 0.00, "cam_y": 0.125, "cam_w": 0.210, "cam_h": 0.280},
    "darkviperau": {"cam_x": 0.812, "cam_y": 0.172, "cam_w": 0.174, "cam_h": 0.196},
    "lirik":       None,  # No facecam
}


def detect_streamer_cam_box(streamer_name: str) -> Tuple[bool, Dict[str, float]]:
    """Return (has_facecam, cam_box) using known streamer layouts or a smart mid-left default."""
    key = re.sub(r"[^a-z0-9_]", "", str(streamer_name or "").lower())
    for k, box in STREAMER_CAM_PRESETS.items():
        if key == k or (k and k in key):
            if box is None:
                return False, {"cam_x": 0.00, "cam_y": 0.40, "cam_w": 0.21, "cam_h": 0.24}
            return True, dict(box)
    # Default mid-left facecam position (most common across GTA 6 streamers)
    return True, {"cam_x": 0.00, "cam_y": 0.40, "cam_w": 0.21, "cam_h": 0.24}


def probe_media(path: Path) -> Dict:
    """Return resolution, fps, duration, and audio stream presence."""
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True)
    data = json.loads(proc.stdout)
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    fps = 30.0
    rfr = str(v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1")
    if "/" in rfr:
        num, den = rfr.split("/", 1)
        try:
            fps = round(float(num) / max(1.0, float(den)), 2)
        except Exception:
            fps = 30.0

    dur = float((data.get("format") or {}).get("duration") or v.get("duration") or 0.0)
    return {
        "width": int(v.get("width") or 1920),
        "height": int(v.get("height") or 1080),
        "fps": fps if 10.0 <= fps <= 120.0 else 30.0,
        "duration": round(dur, 3),
        "has_audio": a is not None,
        "codec": str(v.get("codec_name") or "h264"),
        "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
    }


def extract_waveform_and_peaks(path: Path, num_bars: int = 320) -> Tuple[List[float], List[Dict]]:
    """
    Decode 8 kHz mono PCM and return:
      - waveform: list of `num_bars` floats normalized 0.0..1.0 for timeline drawing
      - peaks: top 3 distinct high-energy timestamps [{"time": sec, "strength": 0..1}]
    """
    cmd = [
        FFMPEG_BIN, "-v", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20)
        if proc.returncode != 0 or not proc.stdout:
            return [0.08] * num_bars, []
        samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size < 400:
            return [0.08] * num_bars, []

        dur = samples.size / 8000.0
        # Build waveform bars
        chunk = max(1, samples.size // num_bars)
        usable = samples[: chunk * num_bars]
        if usable.size < chunk * num_bars:
            usable = np.pad(usable, (0, chunk * num_bars - usable.size))
        rms = np.sqrt(np.mean(usable.reshape(num_bars, chunk) ** 2, axis=1))
        mx = float(np.max(rms)) or 1.0
        bars = [round(float(x / mx), 3) for x in rms]

        # Find top 3 distinct peaks separated by >= 3.0 seconds
        win = 4000  # 0.5 s bins
        n_bins = max(1, samples.size // win)
        bin_rms = np.sqrt(np.mean(samples[: n_bins * win].reshape(n_bins, win) ** 2, axis=1))
        bmax = float(np.max(bin_rms)) or 1.0
        order = np.argsort(bin_rms)[::-1]
        peaks: List[Dict] = []
        for idx in order:
            t_sec = round(min(dur, (int(idx) + 0.5) * 0.5), 2)
            if all(abs(t_sec - p["time"]) >= 3.0 for p in peaks):
                peaks.append({"time": t_sec, "strength": round(float(bin_rms[idx] / bmax), 2)})
            if len(peaks) >= 3:
                break
        return bars, peaks
    except Exception:
        return [0.08] * num_bars, []


def detect_dead_air(path: Path, duration: float, noise_db: float = -34.0, min_dur: float = 0.65) -> List[Dict]:
    """Detect quiet/dead-air stretches via FFmpeg silencedetect."""
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-nostats", "-i", str(path),
        "-vn", "-af", f"silencedetect=n={noise_db}dB:d={min_dur}",
        "-f", "null", "-",
    ]
    silences: List[Dict] = []
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        log = proc.stderr or ""
        cur_start: Optional[float] = None
        for line in log.splitlines():
            m_s = re.search(r"silence_start:\s*([\d.]+)", line)
            if m_s:
                cur_start = float(m_s.group(1))
            m_e = re.search(r"silence_end:\s*([\d.]+)", line)
            if m_e and cur_start is not None:
                e_val = float(m_e.group(1))
                if e_val - cur_start >= min_dur:
                    silences.append({"start": round(cur_start, 2), "end": round(e_val, 2)})
                cur_start = None
        if cur_start is not None and duration - cur_start >= min_dur:
            silences.append({"start": round(cur_start, 2), "end": round(duration, 2)})
    except Exception:
        pass
    return silences


def build_yt_seo_kit(source_name: str, meta: Optional[Dict] = None) -> Dict:
    """Draft high-CTR YouTube Shorts packaging (3 title styles, description, hashtags)."""
    meta = meta or {}
    streamer = str(meta.get("streamer") or "").strip()
    game = str(meta.get("game") or "GTA 6").strip()
    hook = str(meta.get("chat_hook") or "").strip()
    who = streamer.upper() if streamer else "THIS"

    if streamer:
        titles = [
            f"{streamer} COULDN'T BELIEVE WHAT JUST HAPPENED IN {game} 😱",
            f"Wait For {streamer}'s Reaction... ({game} Viral Moment)",
            f"Bro Really Did That In {game} 💀 #{streamer.lower()}",
        ]
    else:
        clean_base = re.sub(r"[_\-]+", " ", Path(source_name).stem).strip().title()[:36] or "Insane Moment"
        titles = [
            f"Wait Until The End... {clean_base} 😱",
            f"Nobody Expected What Happened Next ({clean_base})",
            f"This Clip Broke Everyone 💀 #shorts",
        ]

    hook_line = f'Chat went crazy: "{hook}"' if hook else "Watch all the way to the climax — zero filler."
    tags = ["#shorts", "#youtubeshorts", "#viral"]
    if "gta" in game.lower() or "vice" in game.lower() or streamer:
        tags = ["#gta6", "#gtavi", "#shorts", "#gaming", "#viralclips"]
        if streamer:
            tags.append(f"#{re.sub(r'[^a-z0-9]', '', streamer.lower())}")

    desc = (
        f"{hook_line}\n"
        f"Subscribe for daily hand-edited {game} highlights & crazy moments.\n\n"
        + " ".join(tags)
    )
    return {
        "titles": titles,
        "description": desc,
        "hashtags": tags,
        "recommended_hook": (hook[:42] if hook else f"{who} LOST IT HERE 😱"),
    }


def classify_clip_vibe(
    transcript_text: str,
    peaks: List[Dict],
    meta: Optional[Dict] = None,
) -> Dict:
    """
    Pick the best viral background music loop & mood-matched SFX palette
    based on the clip's spoken words, audio energy, and stream metadata.
    """
    meta = meta or {}
    txt = f"{transcript_text} {meta.get('trigger_reason', '')} {meta.get('stream_title', '')}".lower()
    audio_z = float(meta.get("audio_z") or 0.0)
    vis = float(meta.get("visual_score") or 0.35)

    action_hits = sum(1 for kw in ("cops", "911", "police", "shoot", "gun", "crash", "explode", "drive", "run", "chase", "high_action", "fight", "hit", "kill", "car") if kw in txt)
    comedy_hits = sum(1 for kw in ("what the hell", "what the", "stop", "bruh", "bro", "stupid", "funny", "no way", "wait", "chill", "bro what", "nah", "pause", "weird") if kw in txt)
    suspense_hits = sum(1 for kw in ("careful", "behind", "quiet", "hide", "watch out", "scary", "creepy", "clutch", "one shot", "dark", "sneak") if kw in txt)
    win_hits = sum(1 for kw in ("let's go", "lets go", "clutch", "money", "rich", "won", "easy", "perfect", "cash") if kw in txt)

    if action_hits >= 2 or (audio_z >= 2.5 and vis >= 0.45):
        return {
            "vibe": "action_hype",
            "label": "Action Chaos • Phonk Drift",
            "bgm_file": "phonk_drift.wav",
            "bgm_volume": 0.17,
            "peak_sfx": "impact",
            "pre_sfx": "riser",
            "cut_sfx": "whoosh",
        }
    if comedy_hits >= 2 or (comedy_hits >= 1 and audio_z < 2.5):
        return {
            "vibe": "comedy_fail",
            "label": "Funny / Reaction • Comedy Bounce",
            "bgm_file": "comedy_bounce.wav",
            "bgm_volume": 0.16,
            "peak_sfx": "vine_boom",
            "pre_sfx": "record_scratch",
            "cut_sfx": "pop",
        }
    if suspense_hits >= 1:
        return {
            "vibe": "suspense_clutch",
            "label": "Suspense / Heist • Dark Pulse",
            "bgm_file": "tension_dark.wav",
            "bgm_volume": 0.16,
            "peak_sfx": "boom",
            "pre_sfx": "riser",
            "cut_sfx": "glitch",
        }
    if win_hits >= 1:
        return {
            "vibe": "general_hype",
            "label": "Clutch Hype • Gaming Trap",
            "bgm_file": "trap_hype.wav",
            "bgm_volume": 0.16,
            "peak_sfx": "cash_ding",
            "pre_sfx": "riser",
            "cut_sfx": "whoosh",
        }
    if "vice" in txt or "gta" in txt or "florida" in txt or "miami" in txt:
        return {
            "vibe": "vice_vibe",
            "label": "Vice City Mood • 80s Synthwave",
            "bgm_file": "vice_synthwave.wav",
            "bgm_volume": 0.15,
            "peak_sfx": "vine_boom",
            "pre_sfx": "whoosh",
            "cut_sfx": "whoosh",
        }
    return {
        "vibe": "general_hype",
        "label": "Viral Energy • Gaming Trap",
        "bgm_file": "trap_hype.wav",
        "bgm_volume": 0.16,
        "peak_sfx": "boom",
        "pre_sfx": "riser",
        "cut_sfx": "whoosh",
    }


def build_auto_edit_plan(
    probe: Dict,
    silences: List[Dict],
    peaks: List[Dict],
    meta: Optional[Dict] = None,
    preset: str = "viral_gaming",
    transcript: Optional[Dict] = None,
) -> Dict:
    """
    Construct a pro-edited timeline JSON applying short-form retention principles:
      - Removes dead-air pauses > 0.7s while keeping 0.14s natural speech breathing room
      - Splits long uninterrupted stretches into dynamic pacing beats (~5-8s max)
      - Keeps peak climax intact with punch-in zoom + Flash White accent + Impact SFX
      - Generates a 3-second retention Hook Banner + speech-aligned starter captions
    """
    dur = max(1.0, float(probe.get("duration") or 10.0))
    meta = meta or {}
    peak_t = float(meta.get("peak_marker_sec") or (peaks[0]["time"] if peaks else dur * 0.5))
    peak_t = max(0.5, min(dur - 0.5, peak_t))

    # 1. Compute active non-silent segments (keep natural 0.14s margin around speech)
    pad = 0.14
    cuts: List[Tuple[float, float]] = []
    cursor = 0.0
    for s in sorted(silences, key=lambda x: x["start"]):
        s_start = max(cursor, s["start"] + pad)
        s_end = min(dur, s["end"] - pad)
        if s_end - s_start < 0.45:
            continue
        # Never cut right across the viral peak moment
        if s_start <= peak_t <= s_end:
            continue
        if s_start - cursor >= 0.45:
            cuts.append((round(cursor, 2), round(s_start, 2)))
        cursor = max(cursor, s_end)
    if dur - cursor >= 0.45:
        cuts.append((round(cursor, 2), round(dur, 2)))

    if not cuts:
        cuts = [(0.0, round(dur, 2))]

    # Ensure total Short stays snappy (under ~45s) centered around the viral peak if source is long
    total_raw = sum(e - s for s, e in cuts)
    if total_raw > 48.0:
        win_s = max(0.0, peak_t - 18.0)
        win_e = min(dur, win_s + 40.0)
        trimmed = []
        for s, e in cuts:
            ns, ne = max(s, win_s), min(e, win_e)
            if ne - ns >= 0.5:
                trimmed.append((round(ns, 2), round(ne, 2)))
        if trimmed:
            cuts = trimmed

    # Split any segment longer than 7.5s at a natural beat (preferring peak_t - 0.5s!)
    refined: List[Tuple[float, float]] = []
    for s, e in cuts:
        if s + 2.2 < peak_t - 0.35 < e - 2.2:
            refined.append((s, round(peak_t - 0.35, 2)))
            refined.append((round(peak_t - 0.35, 2), e))
        elif e - s > 9.0:
            mid = round((s + e) / 2.0, 2)
            refined.append((s, mid))
            refined.append((mid, e))
        else:
            refined.append((s, e))

    # 2. Assign pacing zoom & tasteful transitions (hard cuts default, flash on climax!)
    segments: List[Dict] = []
    timeline_pos = 0.0
    peak_out_time = 0.0
    peak_seg_idx = 0

    for i, (s, e) in enumerate(refined):
        contains_peak = (s - 0.5) <= peak_t <= (e + 0.5)
        seg_dur = round(e - s, 2)
        if contains_peak:
            zoom_mode = "punch"
            zoom_amount = 1.16
            trans = "flash" if i > 0 else "none"
            peak_seg_idx = i
            peak_out_time = round(timeline_pos + max(0.2, min(seg_dur - 0.2, peak_t - s)), 2)
        elif i == 0:
            zoom_mode = "push_in"
            zoom_amount = 1.07
            trans = "none"
        elif i % 2 == 1:
            zoom_mode = "punch"
            zoom_amount = 1.09
            trans = "none"
        else:
            zoom_mode = "none"
            zoom_amount = 1.0
            trans = "none"

        segments.append({
            "id": f"seg_{i + 1}",
            "start": s,
            "end": e,
            "speed": 1.0,
            "zoom_mode": zoom_mode,
            "zoom_amount": zoom_amount,
            "transition": trans,
            "transition_dur": 0.22 if trans != "none" else 0.0,
        })
        timeline_pos += seg_dur

    out_total = max(1.0, round(timeline_pos, 2))
    if peak_out_time <= 0.0:
        peak_out_time = round(out_total * 0.55, 2)

    # 3. Classify clip vibe -> auto-pick viral BGM & mood-matched SFX!
    transcript = transcript or {}
    full_text = str(transcript.get("full_text") or "")
    vibe_cfg = classify_clip_vibe(full_text, peaks, meta=meta)

    sfx_events: List[Dict] = []
    if peak_out_time >= 1.6 and vibe_cfg["pre_sfx"]:
        pre_t = round(peak_out_time - (1.20 if vibe_cfg["pre_sfx"] == "riser" else 0.45), 2)
        sfx_events.append({"id": "sfx_1", "sfx": vibe_cfg["pre_sfx"], "time": max(0.1, pre_t), "volume": 0.34})
    sfx_events.append({"id": "sfx_2", "sfx": vibe_cfg["peak_sfx"], "time": round(max(0.35, peak_out_time - 0.04), 2), "volume": 0.56})
    if len(segments) >= 2:
        cut_t = round(segments[0]["end"] - segments[0]["start"], 2)
        if abs(cut_t - peak_out_time) >= 1.2:
            sfx_events.append({"id": "sfx_3", "sfx": vibe_cfg["cut_sfx"], "time": cut_t, "volume": 0.42})

    # 4. Real Whisper Spoken Auto-Captions (NO "LMAOOO" chat spam — pure spoken captions!)
    seo = build_yt_seo_kit(str(meta.get("mp4_relpath") or "clip.mp4"), meta)
    spoken_words = transcript.get("words") or []
    captions: List[Dict] = map_words_to_timeline(spoken_words, segments, max_words=4) if spoken_words else []
    # Keep Hook Banner OFF by default so no unwanted "LMAOOO" banner blocks the clip
    clean_hook_suggestion = (captions[0]["text"][:36] if captions else "")

    # 5. Preset styling defaults (Viral Gaming / Clean Creator / Cinematic Hype)
    is_landscape = probe.get("width", 1920) > probe.get("height", 1080)
    streamer_name = str(meta.get("streamer") or "")
    has_cam, cam_box = detect_streamer_cam_box(streamer_name)
    if is_landscape and has_cam and (preset == "viral_gaming" or streamer_name):
        layout_mode = "streamer_top"
    elif not is_landscape:
        layout_mode = "fill"
    else:
        layout_mode = "blur_bg"
    look = "gaming_hdr" if preset == "viral_gaming" else ("teal_orange" if preset == "cinematic" else "neutral")
    cap_style = "capcut_pop" if preset != "clean_creator" else "clean_minimal"

    # Auto-pan gameplay slightly away from the original webcam edge so the streamer
    # doesn't appear twice (top facecam + tiny edge sliver in the middle gameplay!)
    auto_pan_x = 0.0
    if layout_mode == "streamer_top":
        if cam_box["cam_x"] < 0.25:
            auto_pan_x = 0.72
        elif cam_box["cam_x"] > 0.55:
            auto_pan_x = -0.68

    return {
        "preset": preset,
        "vibe": vibe_cfg["vibe"],
        "vibe_label": vibe_cfg["label"],
        "peak_out_time": peak_out_time,
        "estimated_duration": out_total,
        "framing": {
            "mode": layout_mode,
            "pan_x": auto_pan_x,
            "pan_y": 0.0,
            "zoom": 1.0,
            "split_ratio": 0.31,
            "game_height": 0.42,
            "top_pad": 0.05,
            **cam_box,
        },
        "color": {
            "preset": look,
            "exposure": 0.02,
            "contrast": 1.06,
            "saturation": 1.12,
            "warmth": 0.0,
            "sharpen": 0.45,
            "vignette": preset == "cinematic",
        },
        "audio": {
            "voice_polish": True,
            "highpass": True,
            "denoise": True,
            "presence_eq": True,
            "compressor": True,
            "loudnorm": True,
            "master_volume": 1.0,
            "bgm_file": vibe_cfg["bgm_file"],
            "bgm_volume": vibe_cfg["bgm_volume"],
            "bgm_ducking": True,
        },
        "hook": {
            "enabled": False,
            "text": clean_hook_suggestion,
            "start": 0.0,
            "end": round(min(2.8, out_total), 2),
            "accent": "#FFE600",
            "font": "impact",
        },
        "caption_style": {
            "style": cap_style,
            "font": "arial_black",
            "size": 64,
            "accent": "#FFE600",
            "y_pct": 76.0 if layout_mode == "streamer_top" else 70.0,
        },
        "export": {
            "resolution": "1440p",
            "fps": 60 if probe.get("fps", 30) >= 50 else 30,
            "crf": 14,
            "seamless_loop": False,
        },
        "segments": segments,
        "captions": captions,
        "sfx": sfx_events,
        "seo": seo,
    }


def analyze_video_file(
    path: Path,
    meta: Optional[Dict] = None,
    preset: str = "viral_gaming",
    auto_caption: bool = True,
    transcript_cache_dir: Optional[Path] = CACHE_TRANSCRIPTS_DIR,
) -> Dict:
    """Run full probe + waveform + dead-air + Whisper auto-captions + vibe-matched Auto-Edit plan."""
    CACHE_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    analysis_cache_file = CACHE_ANALYSIS_DIR / f"{_file_cache_key(path)}.json"
    cached_base: Optional[Dict] = None
    if analysis_cache_file.exists():
        try:
            cached_base = json.loads(analysis_cache_file.read_text(encoding="utf-8"))
        except Exception:
            cached_base = None

    if cached_base and "probe" in cached_base:
        probe = cached_base["probe"]
        waveform = cached_base["waveform"]
        peaks = cached_base["peaks"]
        silences = cached_base["silences"]
    else:
        probe = probe_media(path)
        waveform, peaks = extract_waveform_and_peaks(path, num_bars=320)
        silences = detect_dead_air(path, probe["duration"])
        try:
            analysis_cache_file.write_text(
                json.dumps({"probe": probe, "waveform": waveform, "peaks": peaks, "silences": silences}),
                encoding="utf-8",
            )
        except Exception:
            pass

    if not probe.get("has_audio", True):
        transcript = {"ok": False, "model": "", "full_text": "", "cues": [], "words": []}
    elif auto_caption:
        transcript = transcribe_video(path, cache_dir=transcript_cache_dir)
    else:
        cached_tr = get_cached_transcript(path, cache_dir=transcript_cache_dir)
        transcript = cached_tr if cached_tr else {"ok": False, "pending": True, "model": "", "full_text": "", "cues": [], "words": []}

    plan = build_auto_edit_plan(probe, silences, peaks, meta=meta, preset=preset, transcript=transcript)
    return {
        "probe": probe,
        "waveform": waveform,
        "peaks": peaks,
        "silences": silences,
        "transcript": transcript,
        "plan": plan,
    }
