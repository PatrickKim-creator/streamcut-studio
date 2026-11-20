#!/usr/bin/env python3
"""
Editor-Ready Viral Clip Exporter (CapCut / Premiere Pro / DaVinci Resolve Ready)
================================================================================
Snapshots rolling ring-buffer .ts chunks safely before overwrite, stitches a
clean Constant-Frame-Rate (CFR) 16:9 H.264/AAC MP4 master, optionally renders a
9:16 vertical draft, captures a peak-moment thumbnail JPG, writes full viral
metadata JSON (including top hyped chat lines & peak marker timestamp), and
maintains clips_to_edit/EDIT_QUEUE.csv + SSD disk quota enforcement.
"""

import csv
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg" if shutil.which("/opt/homebrew/bin/ffmpeg") else (shutil.which("ffmpeg") or "ffmpeg")
FFPROBE_BIN = "/opt/homebrew/bin/ffprobe" if shutil.which("/opt/homebrew/bin/ffprobe") else (shutil.which("ffprobe") or "ffprobe")


class ClipRejected(Exception):
    """Raised when an exported clip fails the quality gate (frozen / black / silent)."""


@dataclass
class ExportedClip:
    clip_id: str
    created_at: str
    date_folder: str
    streamer: str
    platform: str
    stream_title: str
    game: str
    viral_score: float
    chat_z: float
    audio_z: float
    visual_score: float
    trigger_reason: str
    duration_sec: float
    peak_marker_sec: float
    mp4_relpath: str
    thumb_relpath: str
    vertical_relpath: Optional[str]
    top_chat_messages: List[str]
    chat_hook: str = ""
    quality: Optional[Dict] = None


def check_clip_quality(path: Path, duration: float) -> Dict:
    """
    One fast downscaled decode pass measuring frozen video (Twitch 'commercial break'
    slates, BRB screens, dropped streams), black frames, and silence.
    """
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-nostats", "-threads", "2", "-i", str(path),
        "-vf", "fps=4,scale=120:-2,freezedetect=n=0.003:d=2,blackdetect=d=1:pix_th=0.10",
        "-af", "silencedetect=n=-50dB:d=2",
        "-f", "null", "-",
    ]
    out = {"frozen_sec": 0.0, "black_sec": 0.0, "silent_sec": 0.0}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        log = proc.stderr or ""
        out["frozen_sec"] = round(sum(float(x) for x in re.findall(r"lavfi\.freezedetect\.freeze_duration: ([\d.]+)", log)), 2)
        out["black_sec"] = round(sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", log)), 2)
        out["silent_sec"] = round(sum(float(x) for x in re.findall(r"silence_duration: ([\d.]+)", log)), 2)
        # A freeze/silence still running at EOF never logs its duration; account for it
        f_starts = re.findall(r"lavfi\.freezedetect\.freeze_start: ([\d.]+)", log)
        f_ends = re.findall(r"lavfi\.freezedetect\.freeze_end: ([\d.]+)", log)
        if len(f_starts) > len(f_ends):
            out["frozen_sec"] = round(out["frozen_sec"] + max(0.0, duration - float(f_starts[-1])), 2)
        s_starts = re.findall(r"silence_start: ([\d.]+)", log)
        s_ends = re.findall(r"silence_end: ([\d.]+)", log)
        if len(s_starts) > len(s_ends):
            out["silent_sec"] = round(out["silent_sec"] + max(0.0, duration - float(s_starts[-1])), 2)
    except Exception:
        pass
    return out


def _quality_problem(q: Dict, duration: float) -> Optional[str]:
    if duration <= 0:
        return "empty clip"
    if q["frozen_sec"] >= 0.5 * duration:
        return f"video frozen for {q['frozen_sec']:.0f}s (ad break / BRB screen?)"
    if q["black_sec"] >= 0.5 * duration:
        return f"black screen for {q['black_sec']:.0f}s"
    if q["silent_sec"] >= 0.6 * duration:
        return f"silent for {q['silent_sec']:.0f}s"
    return None


def remove_from_edit_queue(clips_root: Path, mp4_relpath: str) -> None:
    """Drop a deleted clip's row from EDIT_QUEUE.csv so the queue matches the folder."""
    csv_path = clips_root / "EDIT_QUEUE.csv"
    if not csv_path.exists():
        return
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        rows = [r for r in reader if r.get("mp4_relpath") != mp4_relpath]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def set_clip_kept(clips_root: Path, mp4_relpath: str, kept: bool = True) -> bool:
    """Mark a clip as chosen/kept (or unkept) in its _meta.json so auto-expire skips it."""
    clean = str(mp4_relpath or "").lstrip("/")
    root = clips_root.resolve()
    mp4 = (root / clean).resolve()
    if root not in mp4.parents or not mp4.exists():
        return False
    meta_path = mp4.parent / f"{mp4.stem}_meta.json"
    if not meta_path.exists():
        return False
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data["kept"] = bool(kept)
        if kept:
            data["kept_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def enforce_clip_expiry(
    clips_root: Path,
    max_age_minutes: float = 10.0,
    protected_relpaths: Optional[Iterable[str]] = None,
    now_ts: Optional[float] = None,
) -> List[str]:
    """
    Auto-delete unchosen clips older than `max_age_minutes`, strictly in oldest-first
    order. Clips marked `kept: true` in their _meta.json (e.g. opened/chosen in the
    editor or starred) or listed in `protected_relpaths` are never deleted.
    Returns list of deleted mp4 relative paths in the order they were deleted.
    """
    if max_age_minutes <= 0 or not clips_root.exists():
        return []
    now = now_ts if now_ts is not None else time.time()
    max_age_sec = float(max_age_minutes) * 60.0
    protected = {str(p).lstrip("/") for p in (protected_relpaths or ())}
    root = clips_root.resolve()

    mp4s = sorted(
        [p for p in root.rglob("*.mp4") if not p.name.endswith("_vertical_9x16.mp4")],
        key=lambda p: p.stat().st_mtime,
    )
    deleted: List[str] = []
    for mp4 in mp4s:
        try:
            rel = str(mp4.relative_to(root))
            if rel in protected:
                continue
            age = now - mp4.stat().st_mtime
            if age < max_age_sec:
                continue
            meta_path = mp4.parent / f"{mp4.stem}_meta.json"
            if meta_path.exists():
                try:
                    if json.loads(meta_path.read_text(encoding="utf-8")).get("kept"):
                        continue
                except Exception:
                    pass
            stem = mp4.stem
            parent = mp4.parent
            for sibling in (
                mp4,
                parent / f"{stem}_thumb.jpg",
                meta_path,
                parent / f"{stem}_vertical_9x16.mp4",
            ):
                if sibling.exists():
                    try:
                        sibling.unlink()
                    except OSError:
                        pass
            remove_from_edit_queue(root, rel)
            deleted.append(rel)
            # Clean empty date folder if all clips inside expired
            try:
                if parent != root and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
        except OSError:
            continue
    return deleted


def _probe_duration(path: Path) -> float:
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return round(float(proc.stdout.strip()), 2)
    except Exception:
        return 0.0


def _safe_slug(text: str, max_len: int = 28) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:max_len] or "clip"


def enforce_disk_quota(clips_root: Path, max_gb: float = 25.0) -> int:
    """Delete oldest exported clips if clips_to_edit exceeds max_gb."""
    if max_gb <= 0 or not clips_root.exists():
        return 0
    limit_bytes = int(max_gb * (1024 ** 3))
    mp4s = sorted(
        [p for p in clips_root.rglob("*.mp4") if not p.name.endswith("_vertical_9x16.mp4")],
        key=lambda p: p.stat().st_mtime,
    )
    total = sum(p.stat().st_size for p in clips_root.rglob("*") if p.is_file())
    removed = 0
    while total > limit_bytes and mp4s:
        oldest = mp4s.pop(0)
        stem = oldest.stem
        parent = oldest.parent
        for sibling in (
            oldest,
            parent / f"{stem}_thumb.jpg",
            parent / f"{stem}_meta.json",
            parent / f"{stem}_vertical_9x16.mp4",
        ):
            if sibling.exists():
                total -= sibling.stat().st_size
                try:
                    sibling.unlink()
                except OSError:
                    pass
        try:
            remove_from_edit_queue(clips_root, str(oldest.relative_to(clips_root)))
        except Exception:
            pass
        removed += 1
    return removed


def _append_edit_queue_csv(clips_root: Path, clip: ExportedClip) -> None:
    csv_path = clips_root / "EDIT_QUEUE.csv"
    write_header = not csv_path.exists()
    fieldnames = [
        "created_at", "streamer", "platform", "viral_score", "trigger_reason",
        "duration_sec", "peak_marker_sec", "game", "stream_title",
        "mp4_relpath", "chat_hook", "top_chat_sample",
    ]
    if csv_path.exists():
        with csv_path.open("r", newline="", encoding="utf-8") as fh:
            existing = next(csv.reader(fh), [])
        if existing and existing != fieldnames:
            # Upgrade older queue files to the new column layout
            with csv_path.open("r", newline="", encoding="utf-8") as fh:
                old_rows = list(csv.DictReader(fh))
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                w.writerows(old_rows)
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "created_at": clip.created_at,
            "streamer": clip.streamer,
            "platform": clip.platform,
            "viral_score": clip.viral_score,
            "trigger_reason": clip.trigger_reason,
            "duration_sec": clip.duration_sec,
            "peak_marker_sec": clip.peak_marker_sec,
            "game": clip.game,
            "stream_title": clip.stream_title[:90],
            "mp4_relpath": clip.mp4_relpath,
            "chat_hook": clip.chat_hook,
            "top_chat_sample": " | ".join(clip.top_chat_messages[:3]),
        })


def export_viral_clip(
    segment_paths: List[Path],
    clips_root: Path,
    streamer: str,
    platform: str,
    stream_title: str,
    game: str,
    viral_score: float,
    chat_z: float,
    audio_z: float,
    visual_score: float,
    trigger_reason: str,
    peak_marker_sec: float,
    top_chat_messages: List[str],
    export_vertical_draft: bool = False,
    max_clips_gb: float = 25.0,
    trim_start_sec: Optional[float] = None,
    trim_duration_sec: Optional[float] = None,
    normalize_audio: bool = True,
    quality_check: bool = True,
) -> ExportedClip:
    """
    Snapshot ring-buffer segments, cut an exact peak-centred window, and encode an
    editor-ready CFR 16:9 MP4 (1 s keyframes, -14 LUFS audio), plus thumbnail +
    JSON metadata + EDIT_QUEUE.csv entry. Raises ClipRejected for frozen/black/silent clips.

    trim_start_sec / trim_duration_sec are relative to the start of the first segment,
    and peak_marker_sec must be relative to the *trimmed* clip.
    """
    if not segment_paths:
        raise ValueError("No video segments supplied for export.")

    now = datetime.now()
    date_folder = now.strftime("%Y-%m-%d")
    day_dir = clips_root / date_folder
    day_dir.mkdir(parents=True, exist_ok=True)

    stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    s_slug = _safe_slug(streamer, 18)
    r_slug = _safe_slug(trigger_reason, 22)
    base_name = f"{stamp}__{s_slug}__score{int(round(viral_score))}__{r_slug}"

    out_mp4 = day_dir / f"{base_name}.mp4"
    out_thumb = day_dir / f"{base_name}_thumb.jpg"
    out_meta = day_dir / f"{base_name}_meta.json"
    out_vert = day_dir / f"{base_name}_vertical_9x16.mp4"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        copied: List[Path] = []
        for idx, src in enumerate(segment_paths):
            if src.exists() and src.stat().st_size > 1024:
                dst = tmp / f"chunk_{idx:03d}{src.suffix or '.ts'}"
                shutil.copy2(src, dst)
                copied.append(dst)

        if not copied:
            raise RuntimeError("All ring-buffer segments vanished before snapshot.")

        concat_file = tmp / "concat.txt"
        concat_file.write_text("\n".join(f"file '{p}'" for p in copied), encoding="utf-8")

        trim_args: List[str] = []
        if trim_start_sec is not None and trim_start_sec > 0.05:
            trim_args += ["-ss", f"{trim_start_sec:.3f}"]
        if trim_duration_sec is not None and trim_duration_sec > 0:
            trim_args += ["-t", f"{trim_duration_sec:.3f}"]

        audio_filter = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"] if normalize_audio else []

        # Apple Silicon VideoToolbox hardware H.264 encodes 1080p60 @ 20 Mbps ~25x faster than CPU
        # with zero 180s timeouts, falling back cleanly to libx264 if unavailable.
        base_in = [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            *trim_args,
            "-fps_mode", "cfr",
        ]
        vt_video = [
            "-c:v", "h264_videotoolbox", "-profile:v", "high",
            "-b:v", "20M", "-maxrate", "28M", "-bufsize", "40M", "-g", "60",
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        ]
        sw_video = [
            "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast", "-crf", "16",
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-force_key_frames", "expr:gte(t,n_forced*1)",
        ]
        audio_out = [
            *audio_filter,
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
            "-movflags", "+faststart",
            str(out_mp4),
        ]
        try:
            proc_vt = subprocess.run(base_in + vt_video + audio_out, capture_output=True, text=True, timeout=120)
            if proc_vt.returncode != 0:
                out_mp4.unlink(missing_ok=True)
                subprocess.run(base_in + sw_video + audio_out, check=True, timeout=180)
        except Exception:
            out_mp4.unlink(missing_ok=True)
            raise

    duration = _probe_duration(out_mp4)

    quality = None
    if quality_check:
        quality = check_clip_quality(out_mp4, duration)
        problem = _quality_problem(quality, duration)
        if problem:
            out_mp4.unlink(missing_ok=True)
            raise ClipRejected(problem)

    clamped_peak = round(max(0.5, min(max(0.5, duration - 0.5), peak_marker_sec)), 2)

    # Extract peak-moment preview thumbnail JPG
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{clamped_peak:.2f}", "-i", str(out_mp4),
            "-frames:v", "1", "-q:v", "3", str(out_thumb),
        ],
        check=True,
        timeout=30,
    )

    vert_rel = None
    if export_vertical_draft:
        vf = (
            "[0:v]split=2[bg0][fg0];"
            "[bg0]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:4,eq=brightness=-0.18[bg];"
            "[fg0]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
        )
        subprocess.run(
            [
                FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(out_mp4),
                "-filter_complex", vf,
                "-map", "[vout]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
                str(out_vert),
            ],
            check=True,
            timeout=120,
        )
        vert_rel = f"{date_folder}/{out_vert.name}"

    clip = ExportedClip(
        clip_id=base_name,
        created_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        date_folder=date_folder,
        streamer=streamer,
        platform=platform,
        stream_title=stream_title,
        game=game,
        viral_score=round(viral_score, 1),
        chat_z=round(chat_z, 2),
        audio_z=round(audio_z, 2),
        visual_score=round(visual_score, 2),
        trigger_reason=trigger_reason,
        duration_sec=duration,
        peak_marker_sec=clamped_peak,
        mp4_relpath=f"{date_folder}/{out_mp4.name}",
        thumb_relpath=f"{date_folder}/{out_thumb.name}",
        vertical_relpath=vert_rel,
        top_chat_messages=top_chat_messages[:8],
        chat_hook=(top_chat_messages[0][:80] if top_chat_messages else ""),
        quality=quality,
    )

    out_meta.write_text(json.dumps(asdict(clip), indent=2), encoding="utf-8")
    _append_edit_queue_csv(clips_root, clip)
    enforce_disk_quota(clips_root, max_clips_gb)
    return clip


def list_exported_clips(clips_root: Path, limit: int = 50) -> List[Dict]:
    """Return newest exported clips across all date folders for the web dashboard."""
    if not clips_root.exists():
        return []
    metas = sorted(clips_root.rglob("*_meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for mf in metas[:limit]:
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            if (clips_root / data.get("mp4_relpath", "")).exists():
                out.append(data)
        except Exception:
            continue
    return out
