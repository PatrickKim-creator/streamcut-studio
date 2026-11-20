#!/usr/bin/env python3
"""
Pro Shorts Caption, Hook Banner, SRT & High-CTR Cover Thumbnail Renderer
========================================================================
Renders 1080x1920 transparent RGBA PNG caption plates with Pillow + native macOS
display fonts (zero FFmpeg drawtext/libfreetype dependency), enforcing YouTube
Shorts safe zones (top 15%, bottom 22%, right 16% HUD clear):

  - capcut_pop    : CapCut / Hormozi active-word karaoke pop (bold uppercase,
                    heavy stroke, drop shadow, active word highlighted in yellow/cyan)
  - mrbeast_bold  : Impact white uppercase + deep 3D hard drop shadow + keyword pop
  - variety_pill  : High-contrast rounded dark badge + vivid accent word highlight
  - clean_minimal : Sleek creator lower-third with crisp soft shadow
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920

FONT_MAP = {
    "impact": [
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/ariblk.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "arial_black": [
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "C:/Windows/Fonts/ariblk.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "din": [
        "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
        "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf",
        "C:/Windows/Fonts/bahnschrift.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    ],
    "avenir": [
        "/System/Library/Fonts/Avenir Next Condensed.ttc",
        "/System/Library/Fonts/Avenir Next.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
}

CAPTION_STYLES = [
    {"id": "capcut_pop", "name": "CapCut Karaoke Pop", "desc": "Active-word highlight pill + heavy stroke"},
    {"id": "mrbeast_bold", "name": "MrBeast Bold 3D", "desc": "Giant Impact uppercase + hard drop shadow"},
    {"id": "variety_pill", "name": "Variety Show Badge", "desc": "Dark glass pill + punchy accent colors"},
    {"id": "clean_minimal", "name": "Clean Creator", "desc": "Crisp legible modern typography"},
]


def _load_font(font_key: str, size: int) -> ImageFont.FreeTypeFont:
    candidates = FONT_MAP.get(font_key, FONT_MAP["arial_black"]) + [
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> Tuple[int, int, int, int]:
    h = str(hex_color or "#FFE600").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (r, g, b, alpha)
    except Exception:
        return (255, 230, 0, alpha)


def _wrap_words(draw: ImageDraw.ImageDraw, words: List[str], font: ImageFont.FreeTypeFont, max_w: int) -> List[List[Tuple[int, str]]]:
    """Wrap words into max 2 balanced lines, preserving original word indices."""
    lines: List[List[Tuple[int, str]]] = []
    cur: List[Tuple[int, str]] = []
    for idx, word in enumerate(words):
        trial = " ".join([w for _, w in cur] + [word])
        bbox = draw.textbbox((0, 0), trial, font=font, stroke_width=6)
        if cur and (bbox[2] - bbox[0]) > max_w:
            lines.append(cur)
            cur = [(idx, word)]
        else:
            cur.append((idx, word))
    if cur:
        lines.append(cur)
    return lines[:3]


def render_caption_plate(
    text: str,
    style: str = "capcut_pop",
    font_key: str = "arial_black",
    font_size: int = 68,
    accent_hex: str = "#FFE600",
    y_pct: float = 70.0,
    active_word_idx: Optional[int] = None,
) -> Image.Image:
    """Render a single 1080x1920 RGBA transparent caption overlay plate."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    clean = " ".join(str(text or "").strip().split())
    if not clean:
        return img

    uppercase = style in ("capcut_pop", "mrbeast_bold", "variety_pill")
    display_text = clean.upper() if uppercase else clean
    words = display_text.split(" ")

    # Clamp size & position inside YouTube Shorts safe zone
    sz = max(36, min(104, int(font_size)))
    font = _load_font(font_key, sz)
    accent = _hex_to_rgba(accent_hex, 255)
    white = (255, 255, 255, 255)
    black = (10, 12, 16, 255)
    stroke_w = max(5, sz // 10) if style != "clean_minimal" else max(3, sz // 16)

    # Safe width keeps right 16% (YT Like/Comment/Share buttons) and left 8% clear
    safe_left = 72
    safe_right = W - 156
    max_w = safe_right - safe_left
    center_x = safe_left + max_w // 2
    center_y = int(H * max(20.0, min(77.0, float(y_pct))) / 100.0)

    tmp_draw = ImageDraw.Draw(img)
    lines = _wrap_words(tmp_draw, words, font, max_w)
    line_h = int(sz * 1.24)
    total_h = len(lines) * line_h
    top_y = center_y - total_h // 2

    # Compute overall bounding box for optional pill backdrop
    line_widths: List[int] = []
    space_w = int(tmp_draw.textlength(" ", font=font))
    for line in lines:
        w_sum = sum(int(tmp_draw.textlength(w, font=font)) for _, w in line) + space_w * max(0, len(line) - 1)
        line_widths.append(w_sum)

    if style == "variety_pill" and line_widths:
        pad_x, pad_y = 34, 20
        box_w = max(line_widths) + pad_x * 2
        x0 = center_x - box_w // 2
        y0 = top_y - pad_y
        x1 = center_x + box_w // 2
        y1 = top_y + total_h + pad_y - int(sz * 0.12)
        bg_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bg_layer)
        bd.rounded_rectangle([x0, y0, x1, y1], radius=26, fill=(10, 12, 18, 212), outline=(*accent[:3], 190), width=4)
        img = Image.alpha_composite(img, bg_layer)

    # Soft shadow layer for readable separation over bright GTA 6 skies / explosions
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(text_layer)

    shadow_off = 9 if style == "mrbeast_bold" else 6
    for li, line in enumerate(lines):
        lw = line_widths[li]
        cur_x = center_x - lw // 2
        cur_y = top_y + li * line_h
        for w_idx, w_txt in line:
            ww = int(td.textlength(w_txt, font=font))
            is_active = active_word_idx is not None and w_idx == active_word_idx

            if style == "capcut_pop" and is_active:
                # Vivid glowing highlight pill behind the active spoken word
                td.rounded_rectangle(
                    [cur_x - 14, cur_y - 6, cur_x + ww + 14, cur_y + int(sz * 1.08)],
                    radius=16,
                    fill=(*accent[:3], 235),
                )
                td.text((cur_x, cur_y), w_txt, font=font, fill=black, stroke_width=2, stroke_fill=black)
            else:
                fill_col = accent if (is_active or (style == "variety_pill" and w_idx == len(words) - 1)) else white
                if style == "mrbeast_bold" and w_txt.rstrip("!?.,") in ("NO", "WAY", "WHAT", "OMG", "INSANE", "CRAZY", "CLIP", "DEAD", "WAIT"):
                    fill_col = accent
                sd.text(
                    (cur_x + shadow_off, cur_y + shadow_off),
                    w_txt,
                    font=font,
                    fill=(0, 0, 0, 215),
                    stroke_width=stroke_w,
                    stroke_fill=(0, 0, 0, 215),
                )
                td.text(
                    (cur_x, cur_y),
                    w_txt,
                    font=font,
                    fill=fill_col,
                    stroke_width=stroke_w,
                    stroke_fill=black,
                )
            cur_x += ww + space_w

    if style != "mrbeast_bold":
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=4))
    img = Image.alpha_composite(img, shadow_layer)
    img = Image.alpha_composite(img, text_layer)
    return img


def render_hook_plate(
    text: str,
    accent_hex: str = "#FFE600",
    font_key: str = "impact",
    y_px: int = 250,
) -> Image.Image:
    """Render a punchy 3-second retention Hook Banner in the safe zone."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    clean = " ".join(str(text or "").strip().split())
    if not clean:
        return img
    upper = clean.upper()
    font = _load_font(font_key, 54)
    accent = _hex_to_rgba(accent_hex, 255)
    td = ImageDraw.Draw(img)

    max_w = W - 180
    lines = _wrap_words(td, upper.split(" "), font, max_w)[:2]
    line_strs = [" ".join(w for _, w in ln) for ln in lines]
    widths = [int(td.textlength(s, font=font)) for s in line_strs]
    line_h = 66
    box_w = min(W - 100, max(widths) + 72)
    box_h = len(line_strs) * line_h + 32
    x0 = (W - box_w) // 2
    y0 = max(90, min(H - box_h - 320, int(y_px)))
    x1 = x0 + box_w
    y1 = y0 + box_h

    # High-contrast pill card with accent top stripe
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([x0 + 6, y0 + 10, x1 + 6, y1 + 10], radius=24, fill=(0, 0, 0, 170))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=10))
    img = Image.alpha_composite(img, shadow)

    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle([x0, y0, x1, y1], radius=24, fill=(14, 16, 24, 232), outline=(*accent[:3], 255), width=5)
    for i, s in enumerate(line_strs):
        sw = widths[i]
        tx = (W - sw) // 2
        ty = y0 + 18 + i * line_h
        col = accent if i == 0 else (255, 255, 255, 255)
        cd.text((tx, ty), s, font=font, fill=col, stroke_width=4, stroke_fill=(0, 0, 0, 255))
    return Image.alpha_composite(img, card)


def build_timed_overlay_plates(
    out_dir: Path,
    hook_cfg: Dict,
    captions: List[Dict],
    caption_style_cfg: Dict,
    framing: Optional[Dict] = None,
) -> List[Dict]:
    """
    Generate all RGBA PNG overlay files for FFmpeg compositing.
    Returns list of {"path": Path, "start": float, "end": float}.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    plates: List[Dict] = []

    # 1. 3-second Retention Hook Banner
    if hook_cfg.get("enabled") and str(hook_cfg.get("text") or "").strip():
        h_start = max(0.0, float(hook_cfg.get("start", 0.0)))
        h_end = max(h_start + 0.4, float(hook_cfg.get("end", 3.2)))
        # In streamer_top / split_cam mode, seat the hook banner right on the seam
        # between the streamer's facecam and the gameplay so it never covers their eyes!
        frm = framing or {}
        if frm.get("mode") == "streamer_top":
            top_pad_px = int(H * float(frm.get("top_pad", 0.05)))
            cam_h_px = int(H * float(frm.get("split_ratio", 0.31)))
            hook_y = top_pad_px + cam_h_px - 52
        elif frm.get("mode") == "split_cam":
            hook_y = int(H * float(frm.get("split_ratio", 0.34))) - 52
        else:
            hook_y = 250
        hook_img = render_hook_plate(
            hook_cfg["text"],
            accent_hex=str(hook_cfg.get("accent") or caption_style_cfg.get("accent") or "#FFE600"),
            font_key=str(hook_cfg.get("font") or "impact"),
            y_px=hook_y,
        )
        p = out_dir / "hook_banner.png"
        hook_img.save(p)
        plates.append({"path": p, "start": round(h_start, 3), "end": round(h_end, 3)})

    # 2. Timed Captions (with karaoke active-word plates for capcut_pop!)
    style = str(caption_style_cfg.get("style") or "capcut_pop")
    font_key = str(caption_style_cfg.get("font") or "arial_black")
    font_size = int(caption_style_cfg.get("size") or 66)
    accent = str(caption_style_cfg.get("accent") or "#FFE600")
    y_pct = float(caption_style_cfg.get("y_pct") or 70.0)

    idx_counter = 0
    for cue in captions:
        txt = " ".join(str(cue.get("text") or "").strip().split())
        if not txt:
            continue
        start = max(0.0, float(cue.get("start", 0.0)))
        end = float(cue.get("end", start + 1.5))
        if end <= start + 0.08:
            continue

        words = txt.split(" ")
        word_timings = cue.get("words") or []
        if style == "capcut_pop" and len(words) > 1 and (end - start) >= 0.35:
            # Cap word highlight splits at 12 steps per cue; use exact Whisper word
            # timestamps when available so every syllable highlights in lip-sync!
            step_words = words[:12]
            use_exact = len(word_timings) == len(step_words)
            step_dur = (end - start) / len(step_words)
            for wi in range(len(step_words)):
                if use_exact:
                    w_s = max(start, float(word_timings[wi].get("start", start + wi * step_dur)))
                    if wi < len(step_words) - 1:
                        w_e = max(w_s + 0.05, float(word_timings[wi + 1].get("start", word_timings[wi].get("end", w_s + step_dur))))
                    else:
                        w_e = max(w_s + 0.06, end)
                else:
                    w_s = start + wi * step_dur
                    w_e = end if wi == len(step_words) - 1 else (start + (wi + 1) * step_dur)
                plate_img = render_caption_plate(
                    " ".join(step_words),
                    style=style,
                    font_key=font_key,
                    font_size=font_size,
                    accent_hex=accent,
                    y_pct=y_pct,
                    active_word_idx=wi,
                )
                p = out_dir / f"cap_{idx_counter:04d}.png"
                plate_img.save(p)
                plates.append({"path": p, "start": round(w_s, 3), "end": round(w_e, 3)})
                idx_counter += 1
        else:
            plate_img = render_caption_plate(
                txt,
                style=style,
                font_key=font_key,
                font_size=font_size,
                accent_hex=accent,
                y_pct=y_pct,
                active_word_idx=None,
            )
            p = out_dir / f"cap_{idx_counter:04d}.png"
            plate_img.save(p)
            plates.append({"path": p, "start": round(start, 3), "end": round(end, 3)})
            idx_counter += 1

    return plates


def render_cover_thumbnail(frame_jpg: Path, out_jpg: Path, headline: str, accent_hex: str = "#FFE600") -> Path:
    """Create a 1080x1920 High-CTR Shorts cover thumbnail with mobile-readable headline."""
    base = Image.open(frame_jpg).convert("RGBA")
    if base.size != (W, H):
        base = base.resize((W, H), Image.Resampling.LANCZOS)

    # Subtle top & bottom gradient scrim so thumbnail text pops on mobile feeds
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(0, 480):
        a = int(175 * (1.0 - y / 480.0))
        gd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    for y in range(H - 520, H):
        a = int(195 * ((y - (H - 520)) / 520.0))
        gd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    comp = Image.alpha_composite(base, grad)

    clean_hl = " ".join(str(headline or "WAIT FOR IT").strip().split()[:5])
    text_plate = render_caption_plate(
        clean_hl,
        style="mrbeast_bold",
        font_key="impact",
        font_size=94,
        accent_hex=accent_hex,
        y_pct=24.0,
    )
    comp = Image.alpha_composite(comp, text_plate).convert("RGB")
    comp.save(out_jpg, quality=93)
    return out_jpg


def _fmt_srt_time(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h = ms // 3_600_000
    ms %= 3_600_000
    m = ms // 60_000
    ms %= 60_000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt_file(captions: List[Dict], out_path: Path) -> Path:
    """Write standard .srt subtitle file for YouTube Studio upload."""
    blocks = []
    idx = 1
    for cue in sorted(captions, key=lambda c: float(c.get("start", 0.0))):
        txt = " ".join(str(cue.get("text") or "").strip().split())
        if not txt:
            continue
        s = float(cue.get("start", 0.0))
        e = float(cue.get("end", s + 1.5))
        if e <= s + 0.05:
            continue
        blocks.append(f"{idx}\n{_fmt_srt_time(s)} --> {_fmt_srt_time(e)}\n{txt}\n")
        idx += 1
    out_path.write_text("\n".join(blocks), encoding="utf-8")
    return out_path
