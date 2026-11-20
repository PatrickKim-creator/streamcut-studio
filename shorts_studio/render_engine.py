#!/usr/bin/env python3
"""
Pro YouTube Shorts FFmpeg 8.0.1 Render Engine
=============================================
Compiles a full visual NLE timeline project JSON into a broadcast-ready
1080x1920 YouTube Short:
  - 9:16 Framing: Full-Bleed Fill, Dual-Split Facecam+Gameplay, or Blurred Cinema
  - Per-segment trim, pitch-preserved speed ramping (0.5x-2.5x), dynamic keyframed
    zooms (Punch, Push-In, Zoom Hold), and mixed Hard Cuts + Flash/Dissolve/Wipe xfade
  - Primary Color Correction + Creative LUT-style Look Presets (Teal-Orange, Vice Neon,
    Gaming HDR, Vintage Film, Clean Daylight) + Sharpen + Vignette
  - Pro Voice Engineering Chain (High-Pass, FFT DeNoise, Presence EQ, 3.5:1 Compressor)
    + Timed Studio SFX + Optional Sidechain-Ducked BGM + -14 LUFS Loudness Normalization
  - Multi-layered Pillow RGBA Animated Karaoke Captions + 3s Retention Hook Banner
  - High-CTR 1080x1920 Cover Thumbnail, .srt subtitles, and YouTube SEO JSON bundle
"""

import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from analyzer import FFMPEG_BIN, probe_media
from caption_renderer import (
    build_timed_overlay_plates,
    render_cover_thumbnail,
    write_srt_file,
)
from music_synth import ensure_bgm_assets
from sfx_synth import ensure_sfx_assets

W, H = 1080, 1920

RESOLUTION_MAP = {
    "1080p": (1080, 1920),
    "1440p": (1440, 2560),
    "2160p": (2160, 3840),
}

SCALE_FLAGS = "flags=lanczos+accurate_rnd+full_chroma_int"

COLOR_PRESETS: Dict[str, Dict] = {
    "neutral": {
        "name": "Neutral Rec.709",
        "brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0,
        "cb": "", "vignette": False,
    },
    "gaming_hdr": {
        "name": "Punchy Gaming HDR",
        "brightness": 0.01, "contrast": 1.08, "saturation": 1.18, "gamma": 0.98,
        "cb": "rs=-0.02:bs=0.03:rh=0.03:bh=-0.01", "vignette": False,
    },
    "teal_orange": {
        "name": "Cinema Teal & Orange",
        "brightness": -0.01, "contrast": 1.10, "saturation": 1.08, "gamma": 0.98,
        "cb": "rs=-0.07:gs=0.01:bs=0.08:rh=0.08:gh=0.02:bh=-0.06", "vignette": True,
    },
    "vice_neon": {
        "name": "Vice City Neon",
        "brightness": 0.0, "contrast": 1.13, "saturation": 1.26, "gamma": 0.97,
        "cb": "rs=-0.04:gs=0.03:bs=0.10:rh=0.09:gh=-0.02:bh=0.05", "vignette": True,
    },
    "vintage_film": {
        "name": "Vintage 35mm Film",
        "brightness": 0.02, "contrast": 0.96, "saturation": 0.86, "gamma": 1.05,
        "cb": "rs=0.06:gs=0.02:bs=-0.06:rm=0.02:bm=-0.03", "vignette": True,
    },
    "clean_daylight": {
        "name": "Clean Daylight",
        "brightness": 0.04, "contrast": 1.02, "saturation": 1.06, "gamma": 1.02,
        "cb": "rs=-0.02:gs=0.02:bs=0.03", "vignette": False,
    },
}

TRANSITION_MAP = {
    "flash": "fadewhite",
    "dissolve": "fade",
    "wipe": "wipeleft",
    "zoom": "circleopen",
}


def _safe_slug(text: str, max_len: int = 28) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return slug[:max_len] or "short"


def _atempo_chain(speed: float) -> str:
    """Build valid FFmpeg atempo chain for any speed in [0.5, 2.5]."""
    sp = max(0.5, min(2.5, float(speed)))
    if abs(sp - 1.0) < 0.005:
        return ""
    parts = []
    while sp > 2.0:
        parts.append("atempo=2.0")
        sp /= 2.0
    while sp < 0.5:
        parts.append("atempo=0.5")
        sp /= 0.5
    parts.append(f"atempo={sp:.4f}")
    return ",".join(parts)


def _framing_filter(in_label: str, out_label: str, framing: Dict, tag: str, out_w: int = W, out_h: int = H) -> str:
    """Convert any source frame into razor-sharp vertical geometry using Lanczos + CAS."""
    mode = str(framing.get("mode") or "streamer_top")
    pan_x = max(-1.0, min(1.0, float(framing.get("pan_x") or 0.0)))
    pan_y = max(-1.0, min(1.0, float(framing.get("pan_y") or 0.0)))
    zoom = max(1.0, min(2.2, float(framing.get("zoom") or 1.0)))
    div_h = max(4, int(round(out_h / 320)))

    if mode == "streamer_top":
        # Viral Gaming Short layout:
        #   - Top panel: Streamer Facecam deblocked + Lanczos upscaled + FidelityFX CAS sharpened
        #   - Middle panel: Wide, native-sharp Gameplay directly below the streamer
        #   - Subtle blurred gameplay backdrop fills top safe notch & bottom YouTube HUD zone
        top_pad = int((out_h * max(0.0, min(0.14, float(framing.get("top_pad", 0.05))))) // 2) * 2
        split_ratio = max(0.20, min(0.42, float(framing.get("split_ratio") or 0.31)))
        game_ratio = max(0.32, min(0.58, float(framing.get("game_height") or 0.42)))
        cam_h = int((out_h * split_ratio) // 2) * 2
        game_h = int((out_h * game_ratio) // 2) * 2
        margin_guard = int(out_h * 0.02)
        if top_pad + cam_h + game_h > out_h - margin_guard:
            game_h = int(((out_h - margin_guard - top_pad - cam_h)) // 2) * 2
        game_y = top_pad + cam_h

        cx = max(0.0, min(0.88, float(framing.get("cam_x") or 0.0)))
        cy = max(0.0, min(0.88, float(framing.get("cam_y") or 0.40)))
        cw = max(0.08, min(1.0 - cx, float(framing.get("cam_w") or 0.21)))
        ch = max(0.08, min(1.0 - cy, float(framing.get("cam_h") or 0.24)))
        px_norm = (pan_x + 1.0) / 2.0
        py_norm = (pan_y + 1.0) / 2.0
        blur_r = max(22, int(round(24 * (out_w / 1080.0))))

        return (
            f"[{in_label}]split=3[{tag}_bg0][{tag}_cam0][{tag}_game0];"
            f"[{tag}_bg0]scale={out_w}:{out_h}:force_original_aspect_ratio=increase:{SCALE_FLAGS},"
            f"crop={out_w}:{out_h}:(iw-{out_w})/2:(ih-{out_h})/2,boxblur={blur_r}:5,eq=brightness=-0.22,setsar=1[{tag}_bg];"
            f"[{tag}_cam0]crop=w=iw*{cw:.4f}:h=ih*{ch:.4f}:x=iw*{cx:.4f}:y=ih*{cy:.4f},"
            f"hqdn3d=1.4:1.4:3.5:3.5,"
            f"scale={out_w}:{cam_h}:force_original_aspect_ratio=increase:{SCALE_FLAGS},"
            f"crop={out_w}:{cam_h}:(iw-{out_w})/2:(ih-{cam_h})/2,cas=0.45,setsar=1[{tag}_cam];"
            f"[{tag}_game0]scale=w='trunc({out_w}*{zoom:.3f}/2)*2':h='trunc({game_h}*{zoom:.3f}/2)*2':force_original_aspect_ratio=increase:{SCALE_FLAGS},"
            f"crop={out_w}:{game_h}:(iw-{out_w})*{px_norm:.3f}:(ih-{game_h})*{py_norm:.3f},cas=0.25,setsar=1[{tag}_game];"
            f"[{tag}_bg][{tag}_cam]overlay=0:{top_pad}[{tag}_m1];"
            f"[{tag}_m1][{tag}_game]overlay=0:{game_y},"
            f"drawbox=x=0:y={game_y - div_h // 2}:w={out_w}:h={div_h}:color=black@0.95:t=fill,"
            f"drawbox=x=0:y={game_y + game_h - div_h // 2}:w={out_w}:h={max(4, div_h - 2)}:color=black@0.85:t=fill,"
            f"setsar=1[{out_label}]"
        )

    if mode == "split_cam":
        split_ratio = max(0.22, min(0.52, float(framing.get("split_ratio") or 0.34)))
        top_h = int((out_h * split_ratio) // 2) * 2
        bot_h = out_h - top_h
        cx = max(0.0, min(0.85, float(framing.get("cam_x") or 0.0)))
        cy = max(0.0, min(0.85, float(framing.get("cam_y") or 0.40)))
        cw = max(0.10, min(1.0 - cx, float(framing.get("cam_w") or 0.21)))
        ch = max(0.10, min(1.0 - cy, float(framing.get("cam_h") or 0.24)))
        px_norm = (pan_x + 1.0) / 2.0
        return (
            f"[{in_label}]split=2[{tag}_top0][{tag}_bot0];"
            f"[{tag}_top0]crop=w=iw*{cw:.4f}:h=ih*{ch:.4f}:x=iw*{cx:.4f}:y=ih*{cy:.4f},"
            f"hqdn3d=1.4:1.4:3.5:3.5,"
            f"scale={out_w}:{top_h}:force_original_aspect_ratio=increase:{SCALE_FLAGS},"
            f"crop={out_w}:{top_h}:(iw-{out_w})/2:(ih-{top_h})/2,cas=0.45,setsar=1[{tag}_top];"
            f"[{tag}_bot0]scale=w='trunc({out_w}*{zoom:.3f}/2)*2':h='trunc({bot_h}*{zoom:.3f}/2)*2':force_original_aspect_ratio=increase:{SCALE_FLAGS},"
            f"crop={out_w}:{bot_h}:(iw-{out_w})*{px_norm:.3f}:(ih-{bot_h})/2,cas=0.25,setsar=1[{tag}_bot];"
            f"[{tag}_top][{tag}_bot]vstack=inputs=2,"
            f"drawbox=x=0:y={top_h - div_h // 2}:w={out_w}:h={div_h}:color=black@0.9:t=fill[{out_label}]"
        )

    if mode == "blur_bg":
        fg_w = int((out_w * zoom) // 2) * 2
        oy = int(pan_y * int(out_h * 0.135))
        blur_r = max(22, int(round(24 * (out_w / 1080.0))))
        return (
            f"[{in_label}]split=2[{tag}_bg0][{tag}_fg0];"
            f"[{tag}_bg0]scale={out_w}:{out_h}:force_original_aspect_ratio=increase:{SCALE_FLAGS},"
            f"crop={out_w}:{out_h}:(iw-{out_w})/2:(ih-{out_h})/2,boxblur={blur_r}:4,eq=brightness=-0.18,setsar=1[{tag}_bg];"
            f"[{tag}_fg0]scale={fg_w}:-2:{SCALE_FLAGS},crop=w='min(iw,{out_w})':h='min(ih,{out_h})':x='(iw-min(iw,{out_w}))/2':y='(ih-min(ih,{out_h}))/2',cas=0.25,setsar=1[{tag}_fg];"
            f"[{tag}_bg][{tag}_fg]overlay=(W-w)/2:(H-h)/2+({oy}),setsar=1[{out_label}]"
        )

    # Default: 9:16 Full-Bleed Fill
    sw = int((out_w * zoom) // 2) * 2
    sh = int((out_h * zoom) // 2) * 2
    px_norm = (pan_x + 1.0) / 2.0
    py_norm = (pan_y + 1.0) / 2.0
    return (
        f"[{in_label}]scale={sw}:{sh}:force_original_aspect_ratio=increase:{SCALE_FLAGS},"
        f"crop={out_w}:{out_h}:(iw-{out_w})*{px_norm:.3f}:(ih-{out_h})*{py_norm:.3f},cas=0.28,setsar=1[{out_label}]"
    )


def _zoom_motion_filter(seg: Dict, seg_out_dur: float, out_w: int = W, out_h: int = H) -> str:
    """Return dynamic zoom animation filter using sharp Lanczos resampling."""
    mode = str(seg.get("zoom_mode") or "none")
    amt = max(1.0, min(1.35, float(seg.get("zoom_amount") or 1.0)))
    delta = amt - 1.0
    if mode == "none" or delta < 0.01:
        return ""

    d = max(0.3, float(seg_out_dur))
    if mode == "punch":
        expr = f"1+{delta:.3f}*sin(PI*min(t/0.45\\,1))"
    elif mode == "push_in":
        expr = f"1+{delta:.3f}*min(t/{d:.3f}\\,1)"
    elif mode == "zoom_hold":
        expr = f"1+{delta:.3f}*(1-0.45*min(t/{d:.3f}\\,1))"
    else:
        return ""

    return (
        f"scale=w='trunc({out_w}*({expr})/2)*2':h='trunc({out_h}*({expr})/2)*2':eval=frame:{SCALE_FLAGS},"
        f"crop={out_w}:{out_h}:(iw-{out_w})/2:(ih-{out_h})/2"
    )


def _color_filter(color: Dict) -> str:
    """Build combined primary color correction + creative look filter chain."""
    preset = COLOR_PRESETS.get(str(color.get("preset") or "neutral"), COLOR_PRESETS["neutral"])
    exp = max(-0.35, min(0.35, float(color.get("exposure") or 0.0)))
    con = max(0.6, min(1.6, float(color.get("contrast") or 1.0)))
    sat = max(0.0, min(2.0, float(color.get("saturation") or 1.0)))
    warm = max(-0.25, min(0.25, float(color.get("warmth") or 0.0)))
    sharp = max(0.0, min(1.5, float(color.get("sharpen") or 0.0)))
    vig = bool(color.get("vignette")) or bool(preset.get("vignette"))

    b = round(max(-0.5, min(0.5, preset["brightness"] + exp)), 3)
    c = round(max(0.5, min(2.0, preset["contrast"] * con)), 3)
    s = round(max(0.0, min(2.5, preset["saturation"] * sat)), 3)
    g = round(preset["gamma"], 3)

    chain = [f"eq=brightness={b}:contrast={c}:saturation={s}:gamma={g}"]

    # Combine preset colorbalance with warmth slider
    cb_vals = {"rs": 0.0, "gs": 0.0, "bs": 0.0, "rm": 0.0, "gm": 0.0, "bm": 0.0, "rh": 0.0, "gh": 0.0, "bh": 0.0}
    if preset.get("cb"):
        for part in preset["cb"].split(":"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k in cb_vals:
                    cb_vals[k] += float(v)
    if abs(warm) >= 0.005:
        cb_vals["rm"] += warm * 0.45
        cb_vals["rh"] += warm * 0.35
        cb_vals["bm"] -= warm * 0.45
        cb_vals["bh"] -= warm * 0.35
    active_cb = [f"{k}={max(-0.5, min(0.5, v)):.3f}" for k, v in cb_vals.items() if abs(v) >= 0.005]
    if active_cb:
        chain.append("colorbalance=" + ":".join(active_cb))

    if sharp >= 0.05:
        chain.append(f"unsharp=5:5:{sharp:.2f}:5:5:0.0")
    if vig:
        chain.append("vignette=angle=PI/5")
    return ",".join(chain)


def render_short_project(
    source_path: Path,
    project: Dict,
    exports_dir: Path,
    sfx_dir: Path,
    bgm_dir: Optional[Path] = None,
    builtin_bgm_dir: Optional[Path] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> Dict:
    """
    Execute a full Pro YouTube Shorts render and return export metadata.
    Calls on_progress(pct: 0..100, stage: str) during rendering.
    """
    def _prog(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(round(max(0.0, min(100.0, pct)), 1), msg)

    _prog(3.0, "Probing source footage & preparing studio assets...")
    probe = probe_media(source_path)
    src_dur = max(0.5, float(probe["duration"]))
    has_audio = bool(probe["has_audio"])

    exports_dir.mkdir(parents=True, exist_ok=True)
    sfx_paths = ensure_sfx_assets(sfx_dir)
    builtin_bgm_paths = ensure_bgm_assets(builtin_bgm_dir or (sfx_dir.parent / "bgm"))

    raw_segs = project.get("segments") or [{"start": 0.0, "end": src_dur, "speed": 1.0}]
    segs: List[Dict] = []
    for seg in raw_segs:
        s = max(0.0, min(src_dur - 0.1, float(seg.get("start", 0.0))))
        e = max(s + 0.15, min(src_dur, float(seg.get("end", src_dur))))
        sp = max(0.5, min(2.5, float(seg.get("speed") or 1.0)))
        out_d = (e - s) / sp
        if out_d < 0.12:
            continue
        segs.append({
            **seg,
            "start": round(s, 3),
            "end": round(e, 3),
            "speed": round(sp, 3),
            "out_dur": out_d,
        })
    if not segs:
        raise ValueError("Timeline has no valid segments to render.")

    framing = project.get("framing") or {}
    color = project.get("color") or {}
    audio_cfg = project.get("audio") or {}
    hook_cfg = project.get("hook") or {}
    cap_style = project.get("caption_style") or {}
    captions = project.get("captions") or []
    sfx_events = project.get("sfx") or []
    export_cfg = project.get("export") or {}
    seo_cfg = project.get("seo") or {}

    res_key = str(export_cfg.get("resolution") or "1440p").lower()
    out_w, out_h = RESOLUTION_MAP.get(res_key, (1440, 2560))
    target_fps = 60 if int(export_cfg.get("fps") or 60) >= 50 else 30
    crf = max(11, min(26, int(export_cfg.get("crf") or 14)))
    enc_preset = "medium" if crf <= 16 else "fast"

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    title_slug = _safe_slug(project.get("title") or source_path.stem, 24)
    base_name = f"{stamp}__{title_slug}__SHORT_{out_w}x{out_h}"
    out_mp4 = exports_dir / f"{base_name}.mp4"
    out_cover = exports_dir / f"{base_name}_cover.jpg"
    out_srt = exports_dir / f"{base_name}.srt"
    out_meta = exports_dir / f"{base_name}_meta.json"

    with tempfile.TemporaryDirectory(prefix="short_render_") as tmpdir:
        tmp = Path(tmpdir)
        _prog(10.0, "Rendering animated caption & hook plates (1080x1920 RGBA)...")
        plates = build_timed_overlay_plates(tmp / "plates", hook_cfg, captions, cap_style, framing=framing)

        # Build FFmpeg inputs list:
        #   0: main source video
        #   1..K: overlay PNG plates
        #   K+1..: used SFX WAV inputs
        #   last (optional): BGM audio file
        cmd: List[str] = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source_path)]
        input_idx = 1

        plate_inputs: List[Dict] = []
        for pl in plates:
            cmd += ["-i", str(pl["path"])]
            plate_inputs.append({**pl, "idx": input_idx})
            input_idx += 1

        sfx_inputs: List[Dict] = []
        for ev in sfx_events:
            sfx_id = str(ev.get("sfx") or "")
            wav = sfx_paths.get(sfx_id)
            if wav and wav.exists():
                cmd += ["-i", str(wav)]
                sfx_inputs.append({
                    "idx": input_idx,
                    "time": max(0.0, float(ev.get("time") or 0.0)),
                    "volume": max(0.05, min(1.5, float(ev.get("volume") or 0.45))),
                })
                input_idx += 1

        bgm_idx: Optional[int] = None
        bgm_name = str(audio_cfg.get("bgm_file") or "").strip()
        if bgm_name:
            bgm_path: Optional[Path] = None
            clean_bgm = Path(bgm_name).name
            if clean_bgm in builtin_bgm_paths and builtin_bgm_paths[clean_bgm].exists():
                bgm_path = builtin_bgm_paths[clean_bgm]
            elif bgm_dir:
                cand = (bgm_dir / clean_bgm).resolve()
                if bgm_dir.resolve() in cand.parents and cand.exists():
                    bgm_path = cand
            if bgm_path is not None:
                cmd += ["-stream_loop", "-1", "-i", str(bgm_path)]
                bgm_idx = input_idx
                input_idx += 1

        # Build filter_complex graph
        fc: List[str] = []
        seg_v_labels: List[str] = []
        seg_a_labels: List[str] = []

        for i, seg in enumerate(segs):
            s, e, sp, out_d = seg["start"], seg["end"], seg["speed"], seg["out_dur"]
            # Trim & speed-ramp video segment
            fc.append(
                f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=(PTS-STARTPTS)/{sp:.4f},"
                f"fps={target_fps},format=yuv420p[sv_{i}_raw]"
            )
            fc.append(_framing_filter(f"sv_{i}_raw", f"sv_{i}_frm", framing, f"s{i}", out_w=out_w, out_h=out_h))
            z_flt = _zoom_motion_filter(seg, out_d, out_w=out_w, out_h=out_h)
            if z_flt:
                fc.append(f"[sv_{i}_frm]{z_flt},settb=1/{target_fps},fps={target_fps}[sv_{i}]")
            else:
                fc.append(f"[sv_{i}_frm]settb=1/{target_fps},fps={target_fps}[sv_{i}]")
            seg_v_labels.append(f"sv_{i}")

            # Trim & pitch-preserved speed-ramp audio segment (or silence if video has no audio)
            if has_audio:
                at_chain = _atempo_chain(sp)
                at_part = f",{at_chain}" if at_chain else ""
                fc.append(
                    f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS{at_part},"
                    f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[sa_{i}]"
                )
            else:
                fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{out_d:.3f}[sa_{i}]")
            seg_a_labels.append(f"sa_{i}")

        # Stitch segments respecting mixed Hard Cuts vs Flash/Dissolve/Wipe transitions
        cur_v = seg_v_labels[0]
        cur_a = seg_a_labels[0]
        cur_dur = segs[0]["out_dur"]

        for i in range(1, len(segs)):
            nxt_v = seg_v_labels[i]
            nxt_a = seg_a_labels[i]
            nxt_dur = segs[i]["out_dur"]
            tr_key = str(segs[i].get("transition") or "none")
            xf_name = TRANSITION_MAP.get(tr_key)
            tdur = float(segs[i].get("transition_dur") or 0.22)

            if xf_name and tdur >= 0.06 and cur_dur > 0.35 and nxt_dur > 0.35:
                td = round(min(tdur, cur_dur * 0.35, nxt_dur * 0.35), 3)
                off = round(max(0.01, cur_dur - td), 3)
                fc.append(f"[{cur_v}][{nxt_v}]xfade=transition={xf_name}:duration={td:.3f}:offset={off:.3f}[mv_{i}]")
                fc.append(f"[{cur_a}][{nxt_a}]acrossfade=d={td:.3f}:c1=tri:c2=tri[ma_{i}]")
                cur_dur = cur_dur + nxt_dur - td
            else:
                fc.append(f"[{cur_v}][{cur_a}][{nxt_v}][{nxt_a}]concat=n=2:v=1:a=1[mv_{i}][ma_{i}]")
                cur_dur = cur_dur + nxt_dur
            cur_v, cur_a = f"mv_{i}", f"ma_{i}"

        total_dur = max(0.3, cur_dur)

        # Apply Studio Color Grading & Look Preset globally for uniform look across all cuts
        grade = _color_filter(color)
        fc.append(f"[{cur_v}]{grade}[v_graded]")
        cur_v = "v_graded"

        # Composite timed RGBA hook banner + animated karaoke caption plates (Lanczos-scaled to output res)
        for pi, pl in enumerate(plate_inputs):
            p_start = max(0.0, min(total_dur, pl["start"]))
            p_end = max(p_start + 0.04, min(total_dur, pl["end"]))
            nxt_lbl = f"v_ov_{pi}"
            if out_w != W or out_h != H:
                fc.append(f"[{pl['idx']}:v]scale={out_w}:{out_h}:{SCALE_FLAGS}[pl_s_{pi}]")
                pl_ref = f"pl_s_{pi}"
            else:
                pl_ref = f"{pl['idx']}:v"
            fc.append(
                f"[{cur_v}][{pl_ref}]overlay=0:0:shortest=0:repeatlast=1:"
                f"enable='between(t,{p_start:.3f},{p_end:.3f})'[{nxt_lbl}]"
            )
            cur_v = nxt_lbl

        # Optional crisp 0.12 s fade-in/out for seamless loop feel
        if export_cfg.get("seamless_loop") and total_dur >= 2.0:
            fade_out_t = max(0.1, total_dur - 0.14)
            fc.append(f"[{cur_v}]fade=t=in:st=0:d=0.10,fade=t=out:st={fade_out_t:.3f}:d=0.14[v_loop]")
            cur_v = "v_loop"

        fc.append(f"[{cur_v}]format=yuv420p[v_final]")

        # Pro Voice Engineering Chain (High-Pass -> DeNoise -> Presence EQ -> Compressor)
        voice_chain: List[str] = []
        if audio_cfg.get("voice_polish", True):
            if audio_cfg.get("highpass", True):
                voice_chain.append("highpass=f=90")
            if audio_cfg.get("denoise", True):
                voice_chain.append("afftdn=nr=10:nf=-35")
            if audio_cfg.get("presence_eq", True):
                voice_chain.append("equalizer=f=3200:t=q:w=1.4:g=2.4")
            if audio_cfg.get("compressor", True):
                voice_chain.append("acompressor=threshold=-18dB:ratio=3.5:attack=10:release=120:makeup=2")

        master_vol = max(0.0, min(2.0, float(audio_cfg.get("master_volume", 1.0))))
        if abs(master_vol - 1.0) >= 0.02:
            voice_chain.append(f"volume={master_vol:.2f}")

        if voice_chain:
            fc.append(f"[{cur_a}]" + ",".join(voice_chain) + "[a_voice]")
        else:
            fc.append(f"[{cur_a}]acopy[a_voice]")
        cur_a = "a_voice"

        # Mix optional BGM with sidechain ducking under voice
        mix_inputs: List[str] = []
        if bgm_idx is not None:
            bgm_vol = max(0.02, min(0.8, float(audio_cfg.get("bgm_volume", 0.18))))
            if audio_cfg.get("bgm_ducking", True):
                fc.append(f"[{cur_a}]asplit=2[a_vmain][a_vkey]")
                fc.append(
                    f"[{bgm_idx}:a]atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS,"
                    f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"volume={bgm_vol:.2f},afade=t=out:st={max(0.0, total_dur - 0.6):.3f}:d=0.6[bgm_raw]"
                )
                fc.append("[bgm_raw][a_vkey]sidechaincompress=threshold=0.04:ratio=6:attack=25:release=350[bgm_duck]")
                cur_a = "a_vmain"
                mix_inputs = [cur_a, "bgm_duck"]
            else:
                fc.append(
                    f"[{bgm_idx}:a]atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS,"
                    f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"volume={bgm_vol:.2f}[bgm_duck]"
                )
                mix_inputs = [cur_a, "bgm_duck"]
        else:
            mix_inputs = [cur_a]

        # Mix timed Studio SFX hits cleanly (normalize=0 preserves full voice loudness!)
        for si, sfx in enumerate(sfx_inputs):
            delay_ms = int(round(sfx["time"] * 1000))
            fc.append(
                f"[{sfx['idx']}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={sfx['volume']:.2f},adelay={delay_ms}|{delay_ms}[sfx_{si}]"
            )
            mix_inputs.append(f"sfx_{si}")

        if len(mix_inputs) > 1:
            joined = "".join(f"[{m}]" for m in mix_inputs)
            fc.append(f"{joined}amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=0:normalize=0[a_mixed]")
            cur_a = "a_mixed"

        # Final -14 LUFS broadcast loudness normalization + loop fade
        final_a_chain: List[str] = []
        if audio_cfg.get("loudnorm", True):
            final_a_chain.append("loudnorm=I=-14:TP=-1.5:LRA=11")
        if export_cfg.get("seamless_loop") and total_dur >= 2.0:
            final_a_chain.append(f"afade=t=in:st=0:d=0.08,afade=t=out:st={max(0.1, total_dur - 0.12):.3f}:d=0.12")

        if final_a_chain:
            fc.append(f"[{cur_a}]" + ",".join(final_a_chain) + "[a_final]")
        else:
            fc.append(f"[{cur_a}]acopy[a_final]")

        filter_script = tmp / "graph.ffscript"
        filter_script.write_text(";\n".join(fc), encoding="utf-8")

        # Hardware-accelerated Apple Silicon VideoToolbox encoding (5-10x faster @ 20-48 Mbps master!)
        # with clean libx264 fallback on non-macOS systems.
        base_mbps = {"1080p": 20, "1440p": 30, "2160p": 46}.get(res_key, 30)
        crf_scale = 1.25 if crf <= 13 else (1.0 if crf <= 15 else (0.78 if crf <= 17 else 0.58))
        target_mbps = max(10, int(round(base_mbps * crf_scale)))
        max_mbps = int(round(target_mbps * 1.4))

        use_vt = shutil.which("sw_vers") is not None
        if use_vt:
            video_enc = [
                "-c:v", "h264_videotoolbox", "-profile:v", "high", "-level:v", "5.1",
                "-b:v", f"{target_mbps}M", "-maxrate", f"{max_mbps}M", "-bufsize", f"{max_mbps * 2}M",
                "-g", str(target_fps),
                "-pix_fmt", "yuv420p",
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            ]
        else:
            video_enc = [
                "-c:v", "libx264", "-profile:v", "high", "-level:v", "5.1",
                "-preset", enc_preset, "-crf", str(crf),
                "-x264-params", "aq-mode=3:deblock=-1,-1",
                "-pix_fmt", "yuv420p",
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
                "-force_key_frames", "expr:gte(t,n_forced*1)",
            ]

        tmp_mp4 = out_mp4.with_name(f".tmp_{out_mp4.name}")
        cmd += [
            "-threads", "4",
            "-filter_complex_script", str(filter_script),
            "-map", "[v_final]", "-map", "[a_final]",
            "-fps_mode", "cfr", "-r", str(target_fps),
            *video_enc,
            "-c:a", "aac", "-b:a", "320k", "-ar", "48000",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            str(tmp_mp4),
        ]

        _prog(20.0, f"Hardware encoding {out_w}x{out_h} {target_fps}fps Master ({target_mbps} Mbps)...")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                    ratio = min(1.0, max(0.0, (us / 1_000_000.0) / max(0.5, total_dur)))
                    _prog(20.0 + ratio * 68.0, f"Encoding {out_w}x{out_h} {target_fps}fps Master ({int(ratio * 100)}%)...")
                except Exception:
                    pass
        _, err = proc.communicate(timeout=300)
        if proc.returncode != 0 or not tmp_mp4.exists():
            tmp_mp4.unlink(missing_ok=True)
            raise RuntimeError(f"FFmpeg render failed: {(err or '').strip()[-600:]}")
        tmp_mp4.replace(out_mp4)

        _prog(92.0, "Generating High-CTR Shorts cover thumbnail & .srt subtitles...")
        out_probe = probe_media(out_mp4)
        real_dur = out_probe["duration"]
        peak_t = float(project.get("peak_out_time") or (real_dur * 0.45))
        peak_t = max(0.2, min(max(0.2, real_dur - 0.2), peak_t))

        raw_frame = tmp / "peak_frame.jpg"
        subprocess.run(
            [
                FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{peak_t:.2f}", "-i", str(out_mp4),
                "-frames:v", "1", "-q:v", "2", str(raw_frame),
            ],
            check=True,
            timeout=25,
        )
        first_spoken = str(captions[0].get("text") or "").strip() if captions else ""
        cover_headline = str(
            (hook_cfg.get("text") if hook_cfg.get("enabled") else "")
            or first_spoken
            or project.get("title")
            or "GTA 6 MOMENT"
        )
        render_cover_thumbnail(
            raw_frame,
            out_cover,
            headline=cover_headline,
            accent_hex=str(hook_cfg.get("accent") or cap_style.get("accent") or "#FFE600"),
        )
        write_srt_file(captions, out_srt)

        meta = {
            "id": base_name,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": str(project.get("title") or source_path.stem),
            "source_name": source_path.name,
            "mp4_name": out_mp4.name,
            "cover_name": out_cover.name,
            "srt_name": out_srt.name,
            "width": out_probe["width"],
            "height": out_probe["height"],
            "fps": out_probe["fps"],
            "duration_sec": round(real_dur, 2),
            "size_mb": out_probe["size_mb"],
            "bitrate_mbps": round((out_mp4.stat().st_size * 8.0) / max(0.5, real_dur) / 1_000_000.0, 1),
            "resolution": res_key,
            "crf": crf,
            "preset": str(project.get("preset") or "custom"),
            "vibe": str(project.get("vibe_label") or project.get("vibe") or ""),
            "bgm_file": str(audio_cfg.get("bgm_file") or ""),
            "look": str(color.get("preset") or "neutral"),
            "framing_mode": str(framing.get("mode") or "fill"),
            "segments_count": len(segs),
            "captions_count": len(captions),
            "sfx_count": len(sfx_inputs),
            "loudnorm": bool(audio_cfg.get("loudnorm", True)),
            "seo": seo_cfg,
        }
        out_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        _prog(100.0, "Done! 1080x1920 YouTube Short ready to upload.")
        return meta
