#!/usr/bin/env python3
"""
CUTLAB PRO — YouTube Shorts Studio Backend Server (:8790)
=========================================================
Serves the desktop NLE web studio, streams clips with HTTP 206 byte-range
scrubbing, stays live-synced with ../gta6_stream_clipper/clips_to_edit/ (via
both direct webhook /api/sync/new-clip and live polling), runs local OpenAI
Whisper spoken auto-captions + vibe-matched viral BGM & SFX, and renders
broadcast-grade 1080x1920 YouTube Shorts with live progress tracking.
"""

import argparse
import json
import mimetypes
import queue
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests

from analyzer import analyze_video_file
from caption_renderer import CAPTION_STYLES
from music_synth import BGM_CATALOG, ensure_bgm_assets
from render_engine import COLOR_PRESETS, render_short_project
from sfx_synth import SFX_CATALOG, ensure_sfx_assets
from transcriber import map_words_to_timeline, transcribe_video

PROJECT_DIR = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_DIR / "static"
_BOT_ROOT = (
    PROJECT_DIR.parent / "clipper_bot"
    if (PROJECT_DIR.parent / "clipper_bot").exists()
    else PROJECT_DIR.parent / "gta6_stream_clipper"
)
DEFAULT_BOT_CLIPS_DIR = _BOT_ROOT / "clips_to_edit"
DEFAULT_BOT_API_URL = "http://127.0.0.1:8787"

DEFAULT_SYNC_CFG = {
    "auto_edit_enabled": False,
    "min_viral_score": 58.0,
    "preset": "viral_gaming",
    "auto_delete_minutes": 10.0,
}


class ShortsEditorApp:
    """Application state, media resolver, and Clipper-Bot Auto-Sync engine."""

    def __init__(
        self,
        root_dir: Path = PROJECT_DIR,
        bot_clips_dir: Path = DEFAULT_BOT_CLIPS_DIR,
        bot_api_url: str = DEFAULT_BOT_API_URL,
        enable_sync_worker: bool = True,
    ):
        self.root_dir = root_dir
        self.bot_clips_dir = bot_clips_dir
        self.bot_api_url = bot_api_url.rstrip("/")
        self.uploads_dir = root_dir / "uploads"
        self.bgm_dir = self.uploads_dir / "bgm"
        self.builtin_bgm_dir = root_dir / "assets" / "bgm"
        self.exports_dir = root_dir / "exports"
        self.projects_dir = root_dir / "projects"
        self.sfx_dir = root_dir / "assets" / "sfx"
        self.transcript_cache_dir = root_dir / "cache" / "transcripts"
        self.sync_cfg_file = root_dir / "sync_config.json"

        for d in (
            self.uploads_dir, self.bgm_dir, self.builtin_bgm_dir,
            self.exports_dir, self.projects_dir, self.sfx_dir, self.transcript_cache_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        ensure_sfx_assets(self.sfx_dir)
        ensure_bgm_assets(self.builtin_bgm_dir)

        self._jobs_lock = threading.Lock()
        self.jobs: Dict[str, Dict] = {}

        # Clipper <-> Editor Live Auto-Sync state
        self._sync_lock = threading.Lock()
        self.sync_cfg = self._load_sync_config()
        self._auto_queue: "queue.Queue[str]" = queue.Queue()
        self._queued_or_done: set[str] = set()
        self.auto_rendering_clip: str = ""
        self.auto_rendered_count: int = 0
        self.last_sync_event: str = "Live sync ready"
        self._stop_event = threading.Event()

        # Mark pre-existing bot clips as already seen so boot doesn't flood CPU re-rendering old history
        for c in self.list_bot_clips(limit=200):
            self._queued_or_done.add(c["source_id"])

        # Embedded Clipper Bot fallback: if standalone :8787 bot is NOT running, boot it inside this app!
        self.embedded_bot = None
        if enable_sync_worker:
            self._maybe_start_embedded_bot()
            threading.Thread(target=self._auto_sync_worker, name="auto-sync-render", daemon=True).start()
            threading.Thread(target=self._bot_folder_watcher, name="auto-sync-watch", daemon=True).start()

    def _bot_port_reachable(self) -> bool:
        try:
            r = requests.get(f"{self.bot_api_url}/api/status", timeout=0.9)
            return r.status_code == 200
        except Exception:
            return False

    def _maybe_start_embedded_bot(self) -> None:
        if self._bot_port_reachable():
            return
        bot_dir = (
            self.root_dir.parent / "clipper_bot"
            if (self.root_dir.parent / "clipper_bot" / "bot.py").exists()
            else self.root_dir.parent / "gta6_stream_clipper"
        )
        if not (bot_dir / "bot.py").exists() or not (bot_dir / "config.json").exists():
            return
        try:
            if str(bot_dir) not in sys.path:
                sys.path.insert(0, str(bot_dir))
            from bot import ClipperBotEngine
            eng = ClipperBotEngine(project_dir=bot_dir)
            eng.start()
            self.embedded_bot = eng
            self.last_sync_event = "Merged 24/7 Clipper Bot running in-process!"
        except Exception:
            self.embedded_bot = None

    def proxy_bot_get(self, subpath: str) -> tuple[int, Any]:
        if self.embedded_bot is not None:
            if subpath == "status":
                return HTTPStatus.OK, self.embedded_bot.get_status_snapshot()
            return HTTPStatus.NOT_FOUND, {"error": f"Unknown bot route: {subpath}"}
        try:
            r = requests.get(f"{self.bot_api_url}/api/{subpath}", timeout=6.0)
            return r.status_code, r.json()
        except Exception as exc:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"Clipper bot offline: {exc}"}

    def proxy_bot_post(self, subpath: str, body: Dict) -> tuple[int, Any]:
        if self.embedded_bot is not None:
            eng = self.embedded_bot
            if subpath == "poll":
                threading.Thread(target=eng.poll_once, daemon=True).start()
                return HTTPStatus.OK, {"ok": True, "message": "Poll triggered"}
            if subpath == "force-clip":
                res = eng.force_clip_streamer(str(body.get("name") or "").strip())
                return (HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_REQUEST), {**res, "status": eng.get_status_snapshot()}
            if subpath == "streamers/add":
                name = str(body.get("name") or "").strip().lstrip("@")
                platform = str(body.get("platform") or "twitch").strip().lower()
                url = str(body.get("url") or "").strip()
                if not name:
                    return HTTPStatus.BAD_REQUEST, {"error": "Streamer name is required."}
                if not url:
                    url = f"https://www.twitch.tv/{name}" if platform == "twitch" else (f"https://kick.com/{name}" if platform == "kick" else f"https://www.youtube.com/@{name}/live")
                cfg = eng.reload_config()
                streamers = [s for s in cfg.get("streamers", []) if s["name"].lower() != name.lower()]
                streamers.append({"name": name, "platform": platform, "url": url, "enabled": True, "watch_any_game": bool(body.get("watch_any_game", False))})
                cfg["streamers"] = streamers
                eng.update_config(cfg)
                threading.Thread(target=eng.poll_once, daemon=True).start()
                return HTTPStatus.OK, eng.get_status_snapshot()
            if subpath == "streamers/toggle":
                name = str(body.get("name") or "").strip().lower()
                field = str(body.get("field") or "watch_any_game")
                cfg = eng.reload_config()
                for s in cfg.get("streamers", []):
                    if s["name"].lower() == name and field in ("enabled", "watch_any_game"):
                        s[field] = not bool(s.get(field, False))
                eng.update_config(cfg)
                threading.Thread(target=eng.poll_once, daemon=True).start()
                return HTTPStatus.OK, eng.get_status_snapshot()
            if subpath == "streamers/remove":
                name = str(body.get("name") or "").strip().lower()
                cfg = eng.reload_config()
                cfg["streamers"] = [s for s in cfg.get("streamers", []) if s["name"].lower() != name]
                eng.update_config(cfg)
                threading.Thread(target=eng.poll_once, daemon=True).start()
                return HTTPStatus.OK, eng.get_status_snapshot()
            if subpath == "settings":
                cfg = eng.reload_config()
                for k in ("viral_score_threshold", "max_clips_folder_gb"):
                    if k in body and body[k] != "":
                        cfg[k] = float(body[k])
                for k in ("pre_roll_sec", "post_roll_sec", "max_clip_sec", "cooldown_sec", "max_clips_per_hour", "warmup_sec"):
                    if k in body and body[k] != "":
                        cfg[k] = int(body[k])
                for k in ("require_gta6_filter", "normalize_audio", "quality_check", "export_vertical_draft"):
                    if k in body:
                        cfg[k] = bool(body[k])
                eng.update_config(cfg)
                return HTTPStatus.OK, eng.get_status_snapshot()
            return HTTPStatus.NOT_FOUND, {"error": f"Unknown bot route: {subpath}"}
        try:
            r = requests.post(f"{self.bot_api_url}/api/{subpath}", json=body, timeout=10.0)
            return r.status_code, r.json()
        except Exception as exc:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"Clipper bot offline: {exc}"}

    def _load_sync_config(self) -> Dict:
        if self.sync_cfg_file.exists():
            try:
                return {**DEFAULT_SYNC_CFG, **json.loads(self.sync_cfg_file.read_text(encoding="utf-8"))}
            except Exception:
                pass
        return dict(DEFAULT_SYNC_CFG)

    def update_sync_config(self, updates: Dict) -> Dict:
        with self._sync_lock:
            if "auto_edit_enabled" in updates:
                self.sync_cfg["auto_edit_enabled"] = bool(updates["auto_edit_enabled"])
            if "min_viral_score" in updates and updates["min_viral_score"] != "":
                self.sync_cfg["min_viral_score"] = max(0.0, min(100.0, float(updates["min_viral_score"])))
            if "preset" in updates and updates["preset"]:
                self.sync_cfg["preset"] = str(updates["preset"])
            if "auto_delete_minutes" in updates and updates["auto_delete_minutes"] != "":
                mins = max(0.0, min(1440.0, float(updates["auto_delete_minutes"])))
                self.sync_cfg["auto_delete_minutes"] = mins
                # Sync auto_delete_minutes into clipper bot config as well
                try:
                    self.proxy_bot_post("settings", {"auto_delete_minutes": mins})
                except Exception:
                    pass
            self.sync_cfg_file.write_text(json.dumps(self.sync_cfg, indent=2), encoding="utf-8")
            self.last_sync_event = (
                f"Auto-Edit {'ON' if self.sync_cfg['auto_edit_enabled'] else 'OFF'} • "
                f"Auto-Delete: {self.sync_cfg.get('auto_delete_minutes', 10):.0f}m"
            )
            return dict(self.sync_cfg)

    def mark_bot_clip_kept(self, mp4_relpath: str, kept: bool = True) -> bool:
        """Mark/unmark a bot clip as chosen/kept in its _meta.json so 10-min auto-delete skips it."""
        rel = unquote(str(mp4_relpath or "")).lstrip("/")
        root = self.bot_clips_dir.resolve()
        mp4 = (root / rel).resolve()
        if root not in mp4.parents or not mp4.exists():
            return False
        meta_path = mp4.parent / f"{mp4.stem}_meta.json"
        if not meta_path.exists():
            return False
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data["kept"] = bool(kept)
            if kept:
                data["kept_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def expire_old_bot_clips(self, now_ts: Optional[float] = None) -> List[str]:
        """Auto-delete unchosen bot clips older than auto_delete_minutes in oldest-first order."""
        with self._sync_lock:
            max_age_min = float(self.sync_cfg.get("auto_delete_minutes", 10.0))
            active_render = self.auto_rendering_clip
        if max_age_min <= 0 or not self.bot_clips_dir.exists():
            return []
        now = now_ts if now_ts is not None else time.time()
        max_age_sec = max_age_min * 60.0
        root = self.bot_clips_dir.resolve()
        mp4s = sorted(
            [p for p in root.rglob("*.mp4") if not p.name.endswith("_vertical_9x16.mp4")],
            key=lambda p: p.stat().st_mtime,
        )
        deleted: List[str] = []
        for mp4 in mp4s:
            try:
                if active_render and mp4.name == active_render:
                    continue
                if (now - mp4.stat().st_mtime) < max_age_sec:
                    continue
                meta_path = mp4.parent / f"{mp4.stem}_meta.json"
                if meta_path.exists():
                    try:
                        if json.loads(meta_path.read_text(encoding="utf-8")).get("kept"):
                            continue
                    except Exception:
                        pass
                rel = str(mp4.relative_to(root))
                stem = mp4.stem
                parent = mp4.parent
                for sib in (mp4, parent / f"{stem}_thumb.jpg", meta_path, parent / f"{stem}_vertical_9x16.mp4"):
                    if sib.exists():
                        sib.unlink(missing_ok=True)
                # Keep EDIT_QUEUE.csv cleanly synced too
                csv_path = root / "EDIT_QUEUE.csv"
                if csv_path.exists():
                    import csv as _csv
                    with csv_path.open("r", newline="", encoding="utf-8") as fh:
                        rd = _csv.DictReader(fh)
                        fns = rd.fieldnames or []
                        rows = [r for r in rd if r.get("mp4_relpath") != rel]
                    with csv_path.open("w", newline="", encoding="utf-8") as fh:
                        wr = _csv.DictWriter(fh, fieldnames=fns)
                        wr.writeheader()
                        wr.writerows(rows)
                deleted.append(rel)
                if parent != root and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                continue
        if deleted:
            with self._sync_lock:
                self.last_sync_event = f"Auto-deleted {len(deleted)} unchosen clip(s) (>{max_age_min:.0f}m old)"
        return deleted

    def enqueue_synced_bot_clip(self, mp4_relpath: str, viral_score: float = 0.0, force: bool = False) -> Dict:
        """Called by clipper bot webhook (/api/sync/new-clip) or folder watcher."""
        rel = unquote(str(mp4_relpath or "")).lstrip("/")
        if not rel:
            return {"queued": False, "reason": "Empty clip path"}
        with self._sync_lock:
            cfg = dict(self.sync_cfg)
            if not force and not cfg.get("auto_edit_enabled", True):
                self._queued_or_done.add(rel)
                self.last_sync_event = f"Synced new clip {Path(rel).name} (Auto-Edit paused)"
                return {"queued": False, "reason": "Auto-Edit Sync is paused"}
            if not force and viral_score > 0 and viral_score < float(cfg.get("min_viral_score", 70.0)):
                self._queued_or_done.add(rel)
                self.last_sync_event = f"Synced {Path(rel).name} (score {viral_score:.0f} < {cfg['min_viral_score']:.0f})"
                return {"queued": False, "reason": "Below auto-render score threshold"}
            if not force and rel in self._queued_or_done:
                return {"queued": False, "reason": "Already synced"}
            self._queued_or_done.add(rel)
            self._auto_queue.put(rel)
            self.last_sync_event = f"Queued auto-Short edit for {Path(rel).name}"
            return {"queued": True, "mp4_relpath": rel}

    def _auto_sync_worker(self) -> None:
        """Sequentially auto-edits & renders newly captured viral bot clips into 1080x1920 Shorts."""
        while not self._stop_event.is_set():
            try:
                rel = self._auto_queue.get(timeout=1.5)
            except queue.Empty:
                continue
            try:
                src_path, meta = self.resolve_source("bot", rel)
                with self._sync_lock:
                    self.auto_rendering_clip = src_path.name
                    preset = str(self.sync_cfg.get("preset") or "viral_gaming")
                    self.last_sync_event = f"Auto-editing {src_path.name} (Whisper captions + BGM + Cam Top)..."
                analysis = analyze_video_file(
                    src_path,
                    meta=meta,
                    preset=preset,
                    auto_caption=True,
                    transcript_cache_dir=self.transcript_cache_dir,
                )
                plan = {
                    "title": f"{(meta or {}).get('streamer', 'clip')}_auto_short",
                    **analysis["plan"],
                }
                res = render_short_project(
                    source_path=src_path,
                    project=plan,
                    exports_dir=self.exports_dir,
                    sfx_dir=self.sfx_dir,
                    bgm_dir=self.bgm_dir,
                    builtin_bgm_dir=self.builtin_bgm_dir,
                )
                with self._sync_lock:
                    self.auto_rendered_count += 1
                    self.last_sync_event = f"Auto-rendered Short -> {res['mp4_name']} ({res['duration_sec']}s)"
            except Exception as exc:
                with self._sync_lock:
                    self.last_sync_event = f"Auto-edit skipped ({Path(rel).name}): {str(exc)[:90]}"
            finally:
                with self._sync_lock:
                    self.auto_rendering_clip = ""

    def _bot_folder_watcher(self) -> None:
        """Fallback watcher catching new bot clips & sweeping expired unchosen clips every 15s."""
        while not self._stop_event.is_set():
            self._stop_event.wait(15.0)
            if self._stop_event.is_set():
                break
            try:
                self.expire_old_bot_clips()
                for c in self.list_bot_clips(limit=25):
                    rel = c["source_id"]
                    with self._sync_lock:
                        seen = rel in self._queued_or_done
                    if not seen:
                        self.enqueue_synced_bot_clip(rel, viral_score=float(c.get("viral_score") or 0.0))
            except Exception:
                pass

    def get_sync_status(self) -> Dict:
        bot_online = False
        active_workers = 0
        bot_last_poll = ""
        bot_mode = "external"
        if self.embedded_bot is not None:
            bd = self.embedded_bot.get_status_snapshot()
            bot_online = bool(bd.get("bot_running"))
            active_workers = int(bd.get("active_workers") or 0)
            bot_last_poll = str(bd.get("last_poll_at") or "")
            bot_mode = "merged"
        else:
            try:
                r = requests.get(f"{self.bot_api_url}/api/status", timeout=1.2)
                if r.status_code == 200:
                    bd = r.json()
                    bot_online = bool(bd.get("bot_running"))
                    active_workers = int(bd.get("active_workers") or 0)
                    bot_last_poll = str(bd.get("last_poll_at") or "")
                    bot_mode = "synced"
            except Exception:
                pass
        with self._sync_lock:
            return {
                "bot_online": bot_online,
                "bot_mode": bot_mode,
                "bot_url": self.bot_api_url,
                "active_workers": active_workers,
                "bot_last_poll": bot_last_poll,
                "sync_config": dict(self.sync_cfg),
                "auto_rendering_clip": self.auto_rendering_clip,
                "auto_queue_size": self._auto_queue.qsize(),
                "auto_rendered_count": self.auto_rendered_count,
                "last_sync_event": self.last_sync_event,
            }

    def resolve_source(self, source_type: str, source_id: str) -> tuple[Path, Optional[Dict]]:
        """Safely resolve a bot clip or uploaded clip without allowing path traversal."""
        rel = unquote(str(source_id or "")).lstrip("/")
        if source_type == "bot":
            root = self.bot_clips_dir.resolve()
            target = (root / rel).resolve()
            if root not in target.parents or not target.exists():
                raise FileNotFoundError("Bot clip not found.")
            meta_path = target.parent / f"{target.stem}_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
            return target, meta

        if source_type == "upload":
            root = self.uploads_dir.resolve()
            target = (root / Path(rel).name).resolve()
            if root not in target.parents or not target.exists():
                raise FileNotFoundError("Uploaded clip not found.")
            return target, None

        raise ValueError(f"Unknown source_type: {source_type}")

    def list_bot_clips(self, limit: int = 60) -> List[Dict]:
        if not self.bot_clips_dir.exists():
            return []
        with self._sync_lock:
            max_age_min = float(self.sync_cfg.get("auto_delete_minutes", 10.0))
        max_age_sec = max_age_min * 60.0
        now = time.time()
        metas = sorted(self.bot_clips_dir.rglob("*_meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for mf in metas[:limit]:
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                mp4_rel = data.get("mp4_relpath", "")
                mp4_file = self.bot_clips_dir / mp4_rel
                if mp4_file.exists():
                    kept = bool(data.get("kept", False))
                    age_sec = max(0.0, now - mp4_file.stat().st_mtime)
                    expires_in = None if (kept or max_age_sec <= 0) else int(max(0, round(max_age_sec - age_sec)))
                    out.append({
                        "source_type": "bot",
                        "source_id": mp4_rel,
                        "name": f"{data.get('streamer', 'clip')} ({data.get('viral_score', 0)})",
                        "streamer": data.get("streamer", ""),
                        "viral_score": data.get("viral_score", 0),
                        "duration_sec": data.get("duration_sec", 0),
                        "peak_marker_sec": data.get("peak_marker_sec", 0),
                        "created_at": data.get("created_at", ""),
                        "kept": kept,
                        "expires_in_sec": expires_in,
                        "media_url": f"/media/bot/{mp4_rel}",
                        "thumb_url": f"/media/bot/{data.get('thumb_relpath', '')}",
                    })
            except Exception:
                continue
        return out

    def list_uploads(self) -> List[Dict]:
        out = []
        for p in sorted(self.uploads_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".m4v"):
                out.append({
                    "source_type": "upload",
                    "source_id": p.name,
                    "name": p.name,
                    "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
                    "media_url": f"/media/uploads/{p.name}",
                })
        return out

    def list_bgm(self) -> List[Dict]:
        out: List[Dict] = []
        for item in BGM_CATALOG:
            if (self.builtin_bgm_dir / item["file"]).exists():
                out.append({
                    "name": item["file"],
                    "label": f"[Built-In] {item['name']} ({item['bpm']} BPM)",
                    "vibe": item["vibe"],
                    "builtin": True,
                    "url": f"/media/builtin-bgm/{item['file']}",
                })
        for p in sorted(self.bgm_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file() and p.suffix.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg"):
                out.append({
                    "name": p.name,
                    "label": f"[Custom] {p.name}",
                    "vibe": "custom",
                    "builtin": False,
                    "url": f"/media/bgm/{p.name}",
                })
        return out

    def list_exports(self, limit: int = 50) -> List[Dict]:
        metas = sorted(self.exports_dir.glob("*_meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for mf in metas[:limit]:
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                if (self.exports_dir / data.get("mp4_name", "")).exists():
                    out.append({
                        **data,
                        "mp4_url": f"/media/exports/{data['mp4_name']}",
                        "cover_url": f"/media/exports/{data['cover_name']}",
                        "srt_url": f"/media/exports/{data['srt_name']}",
                    })
            except Exception:
                continue
        return out

    def start_render_job(self, source_type: str, source_id: str, project: Dict) -> Dict:
        src_path, _ = self.resolve_source(source_type, source_id)
        job_id = uuid.uuid4().hex[:10]
        job = {
            "job_id": job_id,
            "status": "running",
            "progress": 1.0,
            "stage": "Starting render...",
            "result": None,
            "error": "",
        }
        with self._jobs_lock:
            self.jobs[job_id] = job

        def _update(pct: float, stage: str) -> None:
            with self._jobs_lock:
                self.jobs[job_id]["progress"] = pct
                self.jobs[job_id]["stage"] = stage

        def _worker() -> None:
            try:
                res = render_short_project(
                    source_path=src_path,
                    project=project,
                    exports_dir=self.exports_dir,
                    sfx_dir=self.sfx_dir,
                    bgm_dir=self.bgm_dir,
                    builtin_bgm_dir=self.builtin_bgm_dir,
                    on_progress=_update,
                )
                with self._jobs_lock:
                    self.jobs[job_id].update(
                        status="done",
                        progress=100.0,
                        stage="Export complete!",
                        result={
                            **res,
                            "mp4_url": f"/media/exports/{res['mp4_name']}",
                            "cover_url": f"/media/exports/{res['cover_name']}",
                            "srt_url": f"/media/exports/{res['srt_name']}",
                        },
                    )
            except Exception as exc:
                with self._jobs_lock:
                    self.jobs[job_id].update(status="error", error=str(exc), stage="Render failed")

        threading.Thread(target=_worker, name=f"render-{job_id}", daemon=True).start()
        return job

    def get_job(self, job_id: str) -> Optional[Dict]:
        with self._jobs_lock:
            j = self.jobs.get(job_id)
            return dict(j) if j else None

    def shutdown(self) -> None:
        self._stop_event.set()
        if self.embedded_bot is not None:
            try:
                self.embedded_bot.stop()
            except Exception:
                pass


def _sanitize_filename(name: str, default_ext: str = ".mp4") -> str:
    base = Path(name or f"clip{default_ext}").name
    ext = Path(base).suffix.lower() or default_ext
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(base).stem).strip("._") or "media"
    return f"{stem[:48]}{ext}"


def make_handler(app: ShortsEditorApp):
    class StudioHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, body: Any) -> None:
            raw = json.dumps(body, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "File not found"})
            ext = path.suffix.lower()
            mimes = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".webm": "video/webm",
                ".wav": "audio/wav",
                ".mp3": "audio/mpeg",
                ".m4a": "audio/mp4",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".srt": "text/plain; charset=utf-8",
                ".json": "application/json; charset=utf-8",
            }
            mime = mimes.get(ext) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            size = path.stat().st_size
            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes="):
                try:
                    spec = range_hdr[len("bytes="):].split(",")[0].strip()
                    s_str, e_str = spec.split("-", 1)
                    start = int(s_str) if s_str else 0
                    end = int(e_str) if e_str else size - 1
                    start = max(0, min(start, size - 1))
                    end = max(start, min(end, size - 1))
                    chunk_len = end - start + 1
                    self.send_response(HTTPStatus.PARTIAL_CONTENT)
                    self.send_header("Content-Type", mime)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Content-Length", str(chunk_len))
                    self.end_headers()
                    with path.open("rb") as fh:
                        fh.seek(start)
                        rem = chunk_len
                        while rem > 0:
                            buf = fh.read(min(262_144, rem))
                            if not buf:
                                break
                            self.wfile.write(buf)
                            rem -= len(buf)
                    return
                except Exception:
                    pass

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with path.open("rb") as fh:
                while True:
                    buf = fh.read(262_144)
                    if not buf:
                        break
                    self.wfile.write(buf)

        def _read_json(self) -> Dict:
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _safe_media(self, root: Path, rel: str, allowed_exts: tuple[str, ...]) -> None:
            clean = unquote(rel).lstrip("/")
            resolved_root = root.resolve()
            target = (resolved_root / clean).resolve()
            if resolved_root in target.parents and target.suffix.lower() in allowed_exts:
                return self._send_file(target)
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Invalid media path"})

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                return self._send_file(STATIC_DIR / "index.html")
            if path == "/styles.css":
                return self._send_file(STATIC_DIR / "styles.css")
            if path == "/app.js":
                return self._send_file(STATIC_DIR / "app.js")

            if path.startswith("/media/bot/"):
                return self._safe_media(
                    app.bot_clips_dir, path[len("/media/bot/"):],
                    (".mp4", ".jpg", ".jpeg", ".png", ".json"),
                )
            if path.startswith("/media/uploads/"):
                return self._safe_media(
                    app.uploads_dir, path[len("/media/uploads/"):],
                    (".mp4", ".mov", ".mkv", ".webm", ".m4v"),
                )
            if path.startswith("/media/bgm/"):
                return self._safe_media(
                    app.bgm_dir, path[len("/media/bgm/"):],
                    (".mp3", ".wav", ".m4a", ".aac", ".ogg"),
                )
            if path.startswith("/media/builtin-bgm/"):
                return self._safe_media(app.builtin_bgm_dir, path[len("/media/builtin-bgm/"):], (".wav",))
            if path.startswith("/media/sfx/"):
                return self._safe_media(app.sfx_dir, path[len("/media/sfx/"):], (".wav",))
            if path.startswith("/media/exports/"):
                return self._safe_media(
                    app.exports_dir, path[len("/media/exports/"):],
                    (".mp4", ".jpg", ".jpeg", ".srt", ".json"),
                )

            if path == "/api/library":
                return self._send_json(HTTPStatus.OK, {
                    "bot_clips": app.list_bot_clips(),
                    "uploads": app.list_uploads(),
                    "bgm": app.list_bgm(),
                    "exports": app.list_exports(),
                    "exports_dir_abs": str(app.exports_dir.resolve()),
                    "sfx": [{**s, "url": f"/media/sfx/{s['id']}.wav"} for s in SFX_CATALOG],
                    "look_presets": [{"id": k, "name": v["name"]} for k, v in COLOR_PRESETS.items()],
                    "caption_styles": CAPTION_STYLES,
                    "sync": app.get_sync_status(),
                })

            if path == "/api/sync/status":
                return self._send_json(HTTPStatus.OK, app.get_sync_status())

            if path.startswith("/api/bot/"):
                sub = path[len("/api/bot/"):].strip("/")
                code, payload = app.proxy_bot_get(sub)
                return self._send_json(code, payload)

            if path.startswith("/api/render/"):
                job_id = path[len("/api/render/"):].strip("/")
                job = app.get_job(job_id)
                if not job:
                    return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
                return self._send_json(HTTPStatus.OK, job)

            return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            try:
                if path == "/api/upload":
                    kind = (qs.get("kind") or ["video"])[0]
                    raw_name = (qs.get("filename") or [self.headers.get("X-Filename") or "upload.mp4"])[0]
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    if length <= 0 or length > 800 * 1024 * 1024:
                        return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid upload size (max 800 MB)."})
                    if kind == "bgm":
                        fname = _sanitize_filename(raw_name, ".mp3")
                        dst = app.bgm_dir / fname
                    else:
                        fname = _sanitize_filename(raw_name, ".mp4")
                        dst = app.uploads_dir / fname
                    if dst.exists():
                        dst = dst.with_name(f"{dst.stem}_{uuid.uuid4().hex[:5]}{dst.suffix}")
                    rem = length
                    with dst.open("wb") as fh:
                        while rem > 0:
                            buf = self.rfile.read(min(262_144, rem))
                            if not buf:
                                break
                            fh.write(buf)
                            rem -= len(buf)
                    return self._send_json(HTTPStatus.OK, {
                        "ok": True,
                        "kind": kind,
                        "source_type": "upload",
                        "source_id": dst.name,
                        "name": dst.name,
                        "media_url": f"/media/{'bgm' if kind == 'bgm' else 'uploads'}/{dst.name}",
                    })

                body = self._read_json()

                if path == "/api/analyze":
                    source_type = str(body.get("source_type") or "bot")
                    source_id = str(body.get("source_id") or "")
                    preset = str(body.get("preset") or "viral_gaming")
                    auto_caption = bool(body.get("auto_caption", True))
                    mark_kept = bool(body.get("mark_kept", False))
                    if source_type == "bot" and mark_kept:
                        app.mark_bot_clip_kept(source_id, kept=True)
                    src_path, meta = app.resolve_source(source_type, source_id)
                    analysis = analyze_video_file(
                        src_path,
                        meta=meta,
                        preset=preset,
                        auto_caption=auto_caption,
                        transcript_cache_dir=app.transcript_cache_dir,
                    )
                    media_url = f"/media/bot/{source_id}" if source_type == "bot" else f"/media/uploads/{src_path.name}"
                    return self._send_json(HTTPStatus.OK, {
                        "source_type": source_type,
                        "source_id": source_id,
                        "source_name": src_path.name,
                        "media_url": media_url,
                        "meta": meta,
                        **analysis,
                    })

                if path == "/api/clips/keep":
                    source_id = str(body.get("source_id") or "")
                    kept = bool(body.get("kept", True))
                    ok = app.mark_bot_clip_kept(source_id, kept=kept)
                    if not ok:
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Clip not found"})
                    return self._send_json(HTTPStatus.OK, {"ok": True, "bot_clips": app.list_bot_clips()})

                if path == "/api/transcribe":
                    source_type = str(body.get("source_type") or "bot")
                    source_id = str(body.get("source_id") or "")
                    segments = body.get("segments") or []
                    src_path, _ = app.resolve_source(source_type, source_id)
                    tr = transcribe_video(src_path, cache_dir=app.transcript_cache_dir)
                    cues = map_words_to_timeline(tr.get("words") or [], segments) if segments else (tr.get("cues") or [])
                    return self._send_json(HTTPStatus.OK, {**tr, "cues": cues})

                if path == "/api/sync/config":
                    cfg = app.update_sync_config(body)
                    return self._send_json(HTTPStatus.OK, {"ok": True, "sync": app.get_sync_status(), "config": cfg})

                if path == "/api/sync/new-clip":
                    rel = str(body.get("mp4_relpath") or "")
                    score = float(body.get("viral_score") or 0.0)
                    force = bool(body.get("force", False))
                    res = app.enqueue_synced_bot_clip(rel, viral_score=score, force=force)
                    return self._send_json(HTTPStatus.OK, {**res, "sync": app.get_sync_status()})

                if path.startswith("/api/bot/"):
                    sub = path[len("/api/bot/"):].strip("/")
                    code, payload = app.proxy_bot_post(sub, body)
                    return self._send_json(code, payload)

                if path == "/api/render":
                    source_type = str(body.get("source_type") or "bot")
                    source_id = str(body.get("source_id") or "")
                    project = body.get("project") or {}
                    if source_type == "bot":
                        app.mark_bot_clip_kept(source_id, kept=True)
                    job = app.start_render_job(source_type, source_id, project)
                    return self._send_json(HTTPStatus.OK, job)

                if path == "/api/exports/delete":
                    mp4_name = Path(str(body.get("mp4_name") or "")).name
                    target = (app.exports_dir / mp4_name).resolve()
                    if app.exports_dir.resolve() not in target.parents or not target.exists():
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Export not found"})
                    stem = target.stem
                    for sib in (
                        target,
                        app.exports_dir / f"{stem}_cover.jpg",
                        app.exports_dir / f"{stem}.srt",
                        app.exports_dir / f"{stem}_meta.json",
                    ):
                        sib.unlink(missing_ok=True)
                    return self._send_json(HTTPStatus.OK, {"ok": True, "exports": app.list_exports()})

                if path == "/api/project/save":
                    pid = _sanitize_filename(str(body.get("id") or "project"), ".json")
                    if not pid.endswith(".json"):
                        pid += ".json"
                    dst = app.projects_dir / pid
                    dst.write_text(json.dumps(body.get("project") or {}, indent=2), encoding="utf-8")
                    return self._send_json(HTTPStatus.OK, {"ok": True, "file": dst.name})

                return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})
            except FileNotFoundError as exc:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except Exception as exc:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    return StudioHandler


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def create_server(app: ShortsEditorApp, host: str = "127.0.0.1", port: int = 8790) -> ThreadingHTTPServer:
    return ReusableHTTPServer((host, port), make_handler(app))


def main() -> None:
    parser = argparse.ArgumentParser(description="CUTLAB PRO — YouTube Shorts Video Editor Studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()

    app = ShortsEditorApp()
    server = create_server(app, host=args.host, port=args.port)

    def _handle_sig(_sig, _frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    print(f"CUTLAB PRO (Merged 24/7 Clipper + Shorts Studio) running at http://{args.host}:{server.server_address[1]}/", flush=True)
    print(f"Exported Shorts folder: {app.exports_dir.resolve()}", flush=True)
    print("Press Ctrl+C anytime to stop cleanly.", flush=True)
    try:
        server.serve_forever()
    finally:
        app.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
