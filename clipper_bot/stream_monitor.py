#!/usr/bin/env python3
"""
Multi-Platform Live Stream Monitor (Twitch, YouTube Live, Kick)
===============================================================
Polls live status, game category, title, and viewer count with zero paid API keys.
Uses fast public Twitch GQL metadata lookup for Twitch channels (falling back to
yt-dlp --dump-single-json for YouTube, Kick, or on any GQL error), and resolves
live HLS media URLs via yt-dlp when a streamer matches the GTA 6 filter.
"""

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import requests


def _find_ytdlp() -> str:
    venv_dir = Path(sys.executable).resolve().parent
    for cand in (venv_dir / "yt-dlp", venv_dir / "yt-dlp.exe", Path("/opt/homebrew/bin/yt-dlp")):
        if cand.exists():
            return str(cand)
    return shutil.which("yt-dlp") or "yt-dlp"


YTDLP_BIN = _find_ytdlp()
TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"


@dataclass
class StreamStatus:
    name: str
    platform: str
    url: str
    is_live: bool
    title: str = ""
    game: str = ""
    viewers: int = 0
    matches_target: bool = False
    error: str = ""


def matches_gta6_filter(
    title: str,
    game: str,
    keywords: List[str],
    require_filter: bool = True,
    watch_any_game: bool = False,
) -> bool:
    """Return True if the stream matches GTA 6 keywords (or if filtering is disabled)."""
    if not require_filter or watch_any_game:
        return True
    haystack = f"{game} {title}".lower()
    return any(kw.lower() in haystack for kw in keywords if kw.strip())


def _check_twitch_gql(channel_name: str, timeout: float = 8.0) -> Optional[Dict]:
    """Fast zero-key Twitch web GQL live status + title + game category query."""
    query = """
    query($login: String!) {
      user(login: $login) {
        login
        stream {
          id
          title
          viewersCount
          game {
            name
          }
        }
      }
    }
    """
    try:
        resp = requests.post(
            TWITCH_GQL_URL,
            headers={"Client-ID": TWITCH_WEB_CLIENT_ID, "Content-Type": "application/json"},
            json={"query": query, "variables": {"login": channel_name.lower()}},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        user = (resp.json().get("data") or {}).get("user")
        if not user:
            return {"is_live": False, "title": "", "game": "", "viewers": 0}
        stream = user.get("stream")
        if not stream:
            return {"is_live": False, "title": "", "game": "", "viewers": 0}
        return {
            "is_live": True,
            "title": str(stream.get("title") or ""),
            "game": str((stream.get("game") or {}).get("name") or ""),
            "viewers": int(stream.get("viewersCount") or 0),
        }
    except Exception:
        return None


def _check_via_ytdlp(url: str, timeout: float = 20.0) -> Dict:
    """Platform-agnostic live check using yt-dlp --dump-single-json."""
    cmd = [YTDLP_BIN, "--no-playlist", "--dump-single-json", "--skip-download", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"is_live": False, "title": "", "game": "", "viewers": 0}
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        is_live = bool(info.get("is_live")) or str(info.get("live_status") or "") == "is_live"
        cats = info.get("categories") or []
        game = str(info.get("game") or (cats[0] if cats else "") or "")
        return {
            "is_live": is_live,
            "title": str(info.get("title") or info.get("fulltitle") or ""),
            "game": game,
            "viewers": int(info.get("concurrent_view_count") or info.get("view_count") or 0),
        }
    except Exception as exc:
        return {"is_live": False, "title": "", "game": "", "viewers": 0, "error": str(exc)}


def check_streamer_live(
    streamer_cfg: Dict,
    keywords: List[str],
    require_filter: bool = True,
) -> StreamStatus:
    """Check live status and whether this streamer should be actively clipped right now."""
    name = str(streamer_cfg.get("name", "")).strip()
    platform = str(streamer_cfg.get("platform", "twitch")).strip().lower()
    url = str(streamer_cfg.get("url", "")).strip()
    watch_any = bool(streamer_cfg.get("watch_any_game", False))

    info = None
    if platform == "twitch" and name:
        info = _check_twitch_gql(name)
    if info is None and url:
        info = _check_via_ytdlp(url)
    if info is None:
        info = {"is_live": False, "title": "", "game": "", "viewers": 0, "error": "Check failed"}

    is_live = bool(info.get("is_live"))
    title = str(info.get("title") or "")
    game = str(info.get("game") or "")
    viewers = int(info.get("viewers") or 0)
    matched = is_live and matches_gta6_filter(title, game, keywords, require_filter, watch_any)

    return StreamStatus(
        name=name,
        platform=platform,
        url=url,
        is_live=is_live,
        title=title,
        game=game,
        viewers=viewers,
        matches_target=matched,
        error=str(info.get("error") or ""),
    )


def resolve_live_hls_url(stream_url: str, max_height: int = 1080, timeout: float = 25.0) -> Optional[str]:
    """Resolve a live HLS stream URL suitable for FFmpeg -c copy ring buffering."""
    fmt = f"best[height<={max_height}]/best"
    cmd = [YTDLP_BIN, "--no-playlist", "-f", fmt, "-g", stream_url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return None
