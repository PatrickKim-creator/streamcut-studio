#!/usr/bin/env python3
"""
GTA 6 24/7 Multi-Streamer Viral Clipping Bot Orchestrator
=========================================================
Watches multiple Twitch / YouTube Live / Kick streamers simultaneously 24/7:
  1. Polls live status & GTA 6 game/title filter across your streamer roster
  2. Spawns a low-CPU rolling FFmpeg DVR ring buffer + live Twitch Chat IRC watcher
  3. Scores every closed segment with Tri-Signal Fusion (Chat reactions + Audio + Visual),
     aligning chat to the video with an audience reaction delay
  4. Extends the capture while the hype keeps going, merges back-to-back spikes,
     and cuts an exact peak-centred clip (pre-roll before the moment, post-roll after)
  5. Exports editor-ready CFR MP4s (-14 LUFS, 1 s keyframes), rejects frozen/black/silent
     clips, and writes thumbnails, metadata JSON, and EDIT_QUEUE.csv into ./clips_to_edit/

CLI Usage:
  ../.venv/bin/python bot.py                # Run 24/7 bot + Web Control Panel (:8787)
  ../.venv/bin/python bot.py --headless     # Run 24/7 bot without web server
  ../.venv/bin/python bot.py --simulate     # End-to-end test: generate & detect a viral clip
"""

import argparse
import json
import logging
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont
import requests

from chat_watcher import ChatSpikeTracker, TwitchChatWatcher
from clip_exporter import (
    ClipRejected,
    ExportedClip,
    enforce_clip_expiry,
    export_viral_clip,
    list_exported_clips,
)
from ring_buffer import FFMPEG_BIN, StreamRingBuffer
from stream_monitor import StreamStatus, check_streamer_live, resolve_live_hls_url
from viral_detector import SegmentEvaluation, StreamViralityDetector

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_DIR / "config.json"

# Run 1 clip export at a time so CPU/RAM stay cool on laptops running 24/7
EXPORT_SEMAPHORE = threading.Semaphore(1)

DEFAULTS = {
    "pre_roll_sec": 15,
    "post_roll_sec": 10,
    "max_clip_sec": 60,
    "cooldown_sec": 25,
    "max_clips_per_hour": 18,
    "exceptional_score": 80.0,
    "warmup_sec": 35,
    "chat_delay_sec": 4.0,
    "auto_delete_minutes": 10,
    "normalize_audio": True,
    "quality_check": True,
}


def setup_logger(logs_dir: Path) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"gta6_clipper_bot:{logs_dir.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = RotatingFileHandler(logs_dir / "bot.log", maxBytes=2_000_000, backupCount=4, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


def load_config() -> Dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(cfg: Dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


class StreamerWorker:
    """24/7 self-healing worker thread watching one live streamer."""

    def __init__(
        self,
        streamer_cfg: Dict,
        status: StreamStatus,
        bot_cfg: Dict,
        clips_dir: Path,
        buffers_dir: Path,
        logger: logging.Logger,
        on_clip_exported=None,
        on_event=None,
    ):
        self.streamer_cfg = streamer_cfg
        self.status = status
        self.bot_cfg = bot_cfg
        self.clips_dir = clips_dir
        self.logger = logger
        self.on_clip_exported = on_clip_exported
        self.on_event = on_event

        self.name = status.name
        self.platform = status.platform
        self.seg_sec = int(bot_cfg.get("segment_duration_sec", 10))
        self.ring_size = int(bot_cfg.get("ring_buffer_segments", 18))

        self.ring = StreamRingBuffer(self.name, buffers_dir, self.seg_sec, self.ring_size)
        self.chat: Optional[TwitchChatWatcher] = TwitchChatWatcher(self.name) if self.platform == "twitch" else None
        self.detector = StreamViralityDetector(
            score_threshold=float(bot_cfg.get("viral_score_threshold", 68.0)),
            chat_z_threshold=float(bot_cfg.get("chat_z_threshold", 2.4)),
            audio_z_threshold=float(bot_cfg.get("audio_z_threshold", 2.2)),
        )

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seg_info: Dict[str, Dict] = {}            # seg name -> {mtime, duration, ev}
        self._queue: Dict[str, Tuple[Path, float]] = {}  # closed segs waiting for chat reactions
        self._pending: Optional[Dict] = None
        self._last_clip_ts: float = 0.0
        self._last_manual_ts: float = 0.0
        self._clip_times: Deque[float] = deque(maxlen=100)
        self.watch_started_at: float = 0.0

        # Live telemetry for dashboard
        self.last_eval: Optional[SegmentEvaluation] = None
        self.clips_captured: int = 0
        self.clips_rejected: int = 0
        self.exports_in_progress: int = 0
        self.segments_evaluated: int = 0
        self.state_label: str = "STARTING"

    # ------------------------------------------------------------- lifecycle
    def _c(self, key: str):
        return self.bot_cfg.get(key, DEFAULTS.get(key))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        if self.chat:
            self.chat.start()
        self._thread = threading.Thread(target=self._run, name=f"worker-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.ring.stop()
        if self.chat:
            self.chat.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4.0)
        self.ring.clear_segments()
        self.state_label = "STOPPED"

    def update_status(self, status: StreamStatus, bot_cfg: Dict) -> None:
        self.status = status
        self.bot_cfg = bot_cfg
        self.detector.score_threshold = float(bot_cfg.get("viral_score_threshold", 68.0))
        self.detector.chat_z_threshold = float(bot_cfg.get("chat_z_threshold", 2.4))
        self.detector.audio_z_threshold = float(bot_cfg.get("audio_z_threshold", 2.2))

    def is_warming_up(self) -> bool:
        if not self.watch_started_at:
            return True
        return (time.time() - self.watch_started_at) < float(self._c("warmup_sec")) or not self.detector.audio_warmed_up

    def _emit(self, msg: str) -> None:
        if self.on_event:
            self.on_event(msg)

    # ------------------------------------------------------------- main loop
    def _run(self) -> None:
        backoff = 3.0
        while not self._stop_event.is_set():
            if not self.ring.is_running():
                self.state_label = "RESOLVING_HLS"
                hls_url = resolve_live_hls_url(self.status.url)
                if not hls_url:
                    self.state_label = "HLS_RETRY"
                    self.logger.warning("[%s] Could not resolve HLS URL; retrying in %.0fs", self.name, backoff)
                    self._stop_event.wait(backoff)
                    backoff = min(60.0, backoff * 1.6)
                    continue
                if not self.watch_started_at:
                    self.logger.info("[%s] Attached rolling DVR ring buffer (%ds x %d)", self.name, self.seg_sec, self.ring_size)
                    self._seg_info.clear()
                    self._queue.clear()
                    self.watch_started_at = time.time()
                self.ring.start(hls_url)
                self.state_label = "WARMING_UP" if self.is_warming_up() else "WATCHING_LIVE"
                backoff = 3.0

            # Queue newly closed segments; evaluate once the audience has had time to react in chat
            for seg in self.ring.get_completed_segments():
                try:
                    mtime = seg.stat().st_mtime
                except OSError:
                    continue
                info = self._seg_info.get(seg.name)
                if info and abs(info["mtime"] - mtime) < 0.01:
                    continue
                q = self._queue.get(seg.name)
                if q and abs(q[1] - mtime) < 0.01:
                    continue
                self._queue[seg.name] = (seg, mtime)

            delay = float(self._c("chat_delay_sec")) if self.chat else 0.0
            now = time.time()
            ready = sorted((v for v in self._queue.values() if now >= v[1] + delay), key=lambda v: v[1])
            for seg, mtime in ready:
                self._queue.pop(seg.name, None)
                self._evaluate(seg, mtime, delay)

            if self._pending and self._pending_ready(self._pending):
                trig, self._pending = self._pending, None
                self._finalize_pending_clip(trig)

            if self._pending:
                self.state_label = "CAPTURING_CLIP"
            elif self.is_warming_up():
                self.state_label = "WARMING_UP"
            else:
                self.state_label = "WATCHING_LIVE"
            self._stop_event.wait(1.0)

        if self._pending:
            trig, self._pending = self._pending, None
            self._finalize_pending_clip(trig)

    def _evaluate(self, seg: Path, mtime: float, delay: float) -> None:
        chat_snap = None
        if self.chat:
            chat_snap = self.chat.tracker.window_snapshot(mtime - self.seg_sec + delay, mtime + delay)
        ev = self.detector.evaluate_segment(seg, chat_snap)
        self._seg_info[seg.name] = {"mtime": mtime, "duration": ev.duration_sec or float(self.seg_sec), "ev": ev}
        self.last_eval = ev
        self.segments_evaluated += 1
        self.logger.info(
            "[%s] Seg %s | Viral=%.1f (ChatZ=%.1f reactors=%d%s | AudioZ=%.1f %.0fdB | Vis=%.2f)%s",
            self.name, seg.name, ev.viral_score, ev.chat_z, ev.chat_reactors,
            " SUPPRESSED(subs/raid)" if ev.chat_suppressed else "",
            ev.audio_z, ev.audio_peak_db, ev.visual_score,
            " [warm-up]" if self.is_warming_up() else "",
        )
        self._handle_trigger(seg.name, mtime, ev)

    # ---------------------------------------------------------- trigger logic
    def _dur(self, name: str) -> float:
        info = self._seg_info.get(name)
        return float(info["duration"]) if info else float(self.seg_sec)

    def _handle_trigger(self, name: str, mtime: float, ev: SegmentEvaluation) -> None:
        now = time.time()
        threshold = self.detector.score_threshold
        pre = float(self._c("pre_roll_sec"))
        post = float(self._c("post_roll_sec"))
        max_len = float(self._c("max_clip_sec"))

        p = self._pending
        if p is not None:
            # Keep rolling while the moment is still going (chat still losing it / streamer still yelling)
            if ev.should_clip or ev.viral_score >= threshold * 0.85:
                start_wall = p["first_mtime"] - self._dur(p["first_name"]) + p["first_peak"] - pre
                end_wall = mtime - self._dur(name) + ev.peak_offset_sec + post
                if end_wall - start_wall <= max_len:
                    p.update(last_name=name, last_mtime=mtime, last_peak=ev.peak_offset_sec)
                    p["extensions"] += 1
                    if ev.viral_score > p["best_ev"].viral_score:
                        p.update(best_ev=ev, best_name=name, best_mtime=mtime)
                    self.logger.info("[%s] Hype continues (%.1f) -> extending clip", self.name, ev.viral_score)
            return

        if not ev.should_clip:
            return
        if self.is_warming_up():
            self.logger.info("[%s] Spike %.1f ignored: still learning this stream's normal levels", self.name, ev.viral_score)
            return
        if now - self._last_clip_ts < float(self._c("cooldown_sec")):
            return
        recent = [t for t in self._clip_times if now - t < 3600]
        if len(recent) >= int(self._c("max_clips_per_hour")) and ev.viral_score < float(self._c("exceptional_score")):
            self.logger.info("[%s] Spike %.1f skipped: hourly clip cap reached (%d/h)", self.name, ev.viral_score, len(recent))
            return

        self.logger.info("[%s] VIRAL SPIKE DETECTED (%.1f — %s)! Capturing aftermath...", self.name, ev.viral_score, ev.trigger_reason)
        self._pending = {
            "first_name": name, "first_mtime": mtime, "first_peak": ev.peak_offset_sec,
            "last_name": name, "last_mtime": mtime, "last_peak": ev.peak_offset_sec,
            "best_ev": ev, "best_name": name, "best_mtime": mtime,
            "created": now, "extensions": 0,
        }

    def _pending_ready(self, p: Dict) -> bool:
        post = float(self._c("post_roll_sec"))
        covered = self._dur(p["last_name"]) - p["last_peak"]
        for info in self._seg_info.values():
            if info["mtime"] > p["last_mtime"] + 0.01:
                covered += float(info["duration"])
        if covered >= post:
            return True
        # Safety valve: stream stalled / ended — don't hold the clip forever
        return time.time() > p["created"] + post + 3 * self.seg_sec + self.seg_sec * p["extensions"] + 20

    def _segment_items(self) -> List[Tuple[Path, float, float]]:
        items = []
        for path in self.ring.get_completed_segments():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            info = self._seg_info.get(path.name)
            dur = float(info["duration"]) if info and abs(info["mtime"] - mtime) < 0.01 else float(self.seg_sec)
            items.append((path, mtime, dur))
        return items

    @staticmethod
    def _find(items: List[Tuple[Path, float, float]], name: str, mtime: float) -> Optional[int]:
        for i, (path, mt, _) in enumerate(items):
            if path.name == name and abs(mt - mtime) < 0.01:
                return i
        return None

    def _finalize_pending_clip(self, p: Dict) -> None:
        items = self._segment_items()
        i_first = self._find(items, p["first_name"], p["first_mtime"])
        i_last = self._find(items, p["last_name"], p["last_mtime"])
        i_best = self._find(items, p["best_name"], p["best_mtime"])
        if i_first is None or i_last is None:
            self.logger.warning("[%s] Peak segments already rotated out of the buffer; clip skipped", self.name)
            return
        if i_best is None:
            i_best = i_first
        best_ev: SegmentEvaluation = p["best_ev"]

        pre = float(self._c("pre_roll_sec"))
        post = float(self._c("post_roll_sec"))
        max_len = float(self._c("max_clip_sec"))

        lo, acc = i_first, 0.0
        while lo > 0 and acc < pre - p["first_peak"]:
            lo -= 1
            acc += items[lo][2]
        hi, acc = i_last, 0.0
        while hi < len(items) - 1 and acc < post - (items[i_last][2] - p["last_peak"]):
            hi += 1
            acc += items[hi][2]
        chosen = items[lo:hi + 1]

        offsets, t = [], 0.0
        for _, _, d in chosen:
            offsets.append(t)
            t += d
        total = t
        first_abs = offsets[i_first - lo] + p["first_peak"]
        last_abs = offsets[i_last - lo] + p["last_peak"]
        best_abs = offsets[i_best - lo] + best_ev.peak_offset_sec

        start = max(0.0, first_abs - pre)
        end = min(total, last_abs + post)
        if end - start > max_len:
            start = max(start, best_abs - pre)
            end = min(end, start + max_len)

        self._submit_export(
            [c[0] for c in chosen],
            viral_score=best_ev.viral_score,
            chat_z=best_ev.chat_z,
            audio_z=best_ev.audio_z,
            visual_score=best_ev.visual_score,
            reason=best_ev.trigger_reason + (f"+ext{p['extensions']}" if p["extensions"] else ""),
            peak_marker_sec=best_abs - start,
            top_chat=best_ev.top_chat_messages,
            trim_start=start,
            trim_duration=end - start,
        )
        self._last_clip_ts = time.time()
        self._clip_times.append(self._last_clip_ts)

    # ----------------------------------------------------------------- export
    def _submit_export(self, seg_paths: List[Path], **kw) -> bool:
        """Snapshot segments immediately (before the ring overwrites them), then encode in background."""
        snap_dir = Path(tempfile.mkdtemp(prefix=f"clip_{self.name}_"))
        copied = []
        for i, src in enumerate(seg_paths):
            try:
                dst = snap_dir / f"seg_{i:03d}.ts"
                shutil.copy2(src, dst)
                copied.append(dst)
            except OSError:
                continue
        if not copied:
            shutil.rmtree(snap_dir, ignore_errors=True)
            return False
        self.exports_in_progress += 1
        threading.Thread(target=self._export_job, args=(copied, snap_dir, kw), daemon=True, name=f"export-{self.name}").start()
        return True

    def _export_job(self, seg_paths: List[Path], snap_dir: Path, kw: Dict) -> None:
        try:
            with EXPORT_SEMAPHORE:
                clip = export_viral_clip(
                    segment_paths=seg_paths,
                    clips_root=self.clips_dir,
                    streamer=self.name,
                    platform=self.platform,
                    stream_title=self.status.title,
                    game=self.status.game or "Grand Theft Auto VI",
                    viral_score=kw["viral_score"],
                    chat_z=kw["chat_z"],
                    audio_z=kw["audio_z"],
                    visual_score=kw["visual_score"],
                    trigger_reason=kw["reason"],
                    peak_marker_sec=kw["peak_marker_sec"],
                    top_chat_messages=kw["top_chat"],
                    export_vertical_draft=bool(self.bot_cfg.get("export_vertical_draft", False)),
                    max_clips_gb=float(self.bot_cfg.get("max_clips_folder_gb", 25.0)),
                    trim_start_sec=kw.get("trim_start"),
                    trim_duration_sec=kw.get("trim_duration"),
                    normalize_audio=bool(self._c("normalize_audio")),
                    quality_check=bool(self._c("quality_check")),
                )
            self.clips_captured += 1
            self.logger.info("[%s] SAVED CLIP -> %s (%.1fs, peak @ %.1fs)", self.name, clip.mp4_relpath, clip.duration_sec, clip.peak_marker_sec)
            if self.on_clip_exported:
                self.on_clip_exported(clip)
            # Push real-time sync webhook to CUTLAB PRO Shorts Editor (:8790) if running
            editor_url = str(self.bot_cfg.get("editor_sync_url") or "http://127.0.0.1:8790").rstrip("/")
            if editor_url and kw.get("reason") != "manual_clip":
                try:
                    requests.post(
                        f"{editor_url}/api/sync/new-clip",
                        json={"mp4_relpath": clip.mp4_relpath, "viral_score": clip.viral_score},
                        timeout=2.0,
                    )
                except Exception:
                    pass
        except ClipRejected as exc:
            self.clips_rejected += 1
            self.logger.info("[%s] REJECTED low-quality clip: %s", self.name, exc)
            self._emit(f"REJECTED {self.name} clip: {exc}")
        except Exception as exc:
            self.logger.error("[%s] Failed exporting clip: %s", self.name, exc)
        finally:
            self.exports_in_progress -= 1
            shutil.rmtree(snap_dir, ignore_errors=True)

    def force_clip_now(self) -> Dict:
        """Queue a clip of the last (pre_roll + post_roll) seconds. Returns immediately."""
        now = time.time()
        if now - self._last_manual_ts < 15.0:
            return {"ok": False, "error": f"Already saving a clip of {self.name} — wait a few seconds."}
        items = self._segment_items()
        if not items:
            return {"ok": False, "error": f"No buffered video for '{self.name}' yet."}
        want = float(self._c("pre_roll_sec")) + float(self._c("post_roll_sec"))
        lo, acc = len(items), 0.0
        while lo > 0 and acc < want:
            lo -= 1
            acc += items[lo][2]
        chosen = items[lo:]
        total = sum(d for _, _, d in chosen)
        start = max(0.0, total - want)
        ev = self.last_eval
        chat_snap = self.chat.tracker.snapshot() if self.chat else None
        ok = self._submit_export(
            [c[0] for c in chosen],
            viral_score=ev.viral_score if ev else 0.0,
            chat_z=chat_snap.chat_z if chat_snap else 0.0,
            audio_z=ev.audio_z if ev else 0.0,
            visual_score=ev.visual_score if ev else 0.0,
            reason="manual_clip",
            peak_marker_sec=float(self._c("pre_roll_sec")),
            top_chat=chat_snap.top_messages if chat_snap else [],
            trim_start=start,
            trim_duration=total - start,
        )
        if not ok:
            return {"ok": False, "error": "Buffered segments vanished before snapshot."}
        self._last_manual_ts = now
        return {"ok": True, "message": f"Saving last {int(min(want, total))}s of {self.name} to clips_to_edit/ ..."}

    def telemetry(self) -> Dict:
        chat_snap = self.chat.tracker.snapshot() if self.chat else None
        return {
            "state": self.state_label,
            "ring_active": self.ring.is_running(),
            "warming_up": self.is_warming_up(),
            "chat_connected": bool(chat_snap and chat_snap.connected),
            "chat_mps": chat_snap.current_mps if chat_snap else 0.0,
            "chat_z": self.last_eval.chat_z if self.last_eval else 0.0,
            "chat_reactors": self.last_eval.chat_reactors if self.last_eval else 0,
            "chat_filtered": chat_snap.filtered_messages if chat_snap else 0,
            "audio_z": self.last_eval.audio_z if self.last_eval else 0.0,
            "last_viral_score": self.last_eval.viral_score if self.last_eval else 0.0,
            "clips_captured": self.clips_captured,
            "clips_rejected": self.clips_rejected,
            "exports_in_progress": self.exports_in_progress,
        }


class ClipperBotEngine:
    """24/7 Multi-Streamer Orchestrator managing all StreamerWorkers."""

    def __init__(self, project_dir: Path = PROJECT_DIR):
        self.project_dir = project_dir
        self.config_file = project_dir / "config.json"
        self.cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.clips_dir = self.project_dir / self.cfg.get("clips_dir", "clips_to_edit")
        self.buffers_dir = self.project_dir / self.cfg.get("buffers_dir", "buffers")
        self.logs_dir = self.project_dir / self.cfg.get("logs_dir", "logs")
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.buffers_dir.mkdir(parents=True, exist_ok=True)
        self.logger = setup_logger(self.logs_dir)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_poll_at = "Not polled yet"
        self.statuses: Dict[str, StreamStatus] = {}
        self.workers: Dict[str, StreamerWorker] = {}
        self._offline_strikes: Dict[str, int] = {}
        self.recent_events: List[str] = []

    def _log_event(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.recent_events.insert(0, f"[{stamp}] {msg}")
        self.recent_events = self.recent_events[:40]

    def reload_config(self) -> Dict:
        with self._lock:
            self.cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
            self.clips_dir = self.project_dir / self.cfg.get("clips_dir", "clips_to_edit")
            self.clips_dir.mkdir(parents=True, exist_ok=True)
            return self.cfg

    def update_config(self, new_cfg: Dict) -> Dict:
        with self._lock:
            self.config_file.write_text(json.dumps(new_cfg, indent=2), encoding="utf-8")
            self.cfg = new_cfg
            self.clips_dir = self.project_dir / self.cfg.get("clips_dir", "clips_to_edit")
            self.clips_dir.mkdir(parents=True, exist_ok=True)
            for w in self.workers.values():
                w.update_status(w.status, new_cfg)
        self._log_event("Updated bot configuration & streamer roster.")
        return self.cfg

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            subprocess.run(["pkill", "-f", str(self.buffers_dir.resolve())], capture_output=True, timeout=3)
        except Exception:
            pass
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="clipper-coordinator", daemon=True)
        self._thread.start()
        self._log_event("24/7 GTA 6 Multi-Streamer Clipping Bot started.")

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            workers = list(self.workers.values())
            self.workers.clear()
        for w in workers:
            w.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4.0)
        self.logger.info("ClipperBotEngine stopped cleanly.")

    def poll_once(self) -> None:
        cfg = self.reload_config()
        keywords = list(cfg.get("gta6_keywords", []))
        require_filter = bool(cfg.get("require_gta6_filter", True))
        streamers = [s for s in cfg.get("streamers", []) if s.get("enabled", True)]

        active_names = {str(s["name"]).lower() for s in streamers}
        with self._lock:
            for nm in list(self.workers.keys()):
                if nm not in active_names:
                    self.logger.info("[%s] Removed/disabled in config; stopping worker.", nm)
                    self.workers.pop(nm).stop()

        for scfg in streamers:
            if self._stop_event.is_set():
                break
            st = check_streamer_live(scfg, keywords, require_filter)
            key = st.name.lower()
            with self._lock:
                self.statuses[key] = st
                existing = self.workers.get(key)
                if st.matches_target:
                    self._offline_strikes.pop(key, None)
                    if existing is None:
                        w = StreamerWorker(
                            scfg, st, cfg, self.clips_dir, self.buffers_dir, self.logger,
                            on_clip_exported=lambda clip: self._log_event(
                                f"SAVED VIRAL CLIP: {clip.streamer} (Score {clip.viral_score}, {clip.duration_sec}s) -> {clip.mp4_relpath}"
                            ),
                            on_event=self._log_event,
                        )
                        self.workers[key] = w
                        w.start()
                        self._log_event(f"ATTACHED LIVE: {st.name} playing '{st.game or 'Live'}' ({st.viewers:,} viewers)")
                    else:
                        existing.update_status(st, cfg)
                else:
                    if existing is not None:
                        strikes = self._offline_strikes.get(key, 0) + 1
                        self._offline_strikes[key] = strikes
                        if strikes < 2 and existing.ring.is_running() and not st.is_live:
                            self.logger.info("[%s] Transient offline/timeout check (strike %d/2) while stream ring is active; keeping worker.", st.name, strikes)
                        else:
                            self._offline_strikes.pop(key, None)
                            self.logger.info("[%s] Offline or switched off GTA 6; detaching worker.", st.name)
                            self.workers.pop(key).stop()
                            self._log_event(f"DETACHED: {st.name} (offline or non-GTA 6)")

        self.last_poll_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Auto-delete unchosen clips older than auto_delete_minutes in oldest-first order
        max_age = float(cfg.get("auto_delete_minutes", DEFAULTS["auto_delete_minutes"]))
        if max_age > 0:
            expired = enforce_clip_expiry(self.clips_dir, max_age_minutes=max_age)
            if expired:
                self.logger.info("Auto-expired %d unchosen clip(s) (>%.0f min): oldest=%s", len(expired), max_age, expired[0])
                self._log_event(f"AUTO-EXPIRED {len(expired)} unchosen clip(s) (>{max_age:.0f}m old)")

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self.logger.error("Poll loop error: %s", exc)
            interval = max(15, int(self.cfg.get("poll_interval_sec", 45)))
            self._stop_event.wait(interval)

    def force_clip_streamer(self, streamer_name: str) -> Dict:
        with self._lock:
            w = self.workers.get(streamer_name.lower())
        if not w:
            return {"ok": False, "error": f"'{streamer_name}' is not being watched right now."}
        res = w.force_clip_now()
        if res.get("ok"):
            self._log_event(f"MANUAL CLIP queued for {w.name}")
        return res

    def run_simulated_viral_event(self, streamer_name: str = "kaicenat_sim") -> ExportedClip:
        """
        End-to-end offline verification: generates 4 synthetic 5 s MPEG-TS chunks
        with a calm baseline followed by a loud "heli crash + scream" and a simulated
        Twitch chat surge, runs them through the real detector and saves an
        editor-ready MP4 + thumbnail + metadata + EDIT_QUEUE.csv.
        """
        self._log_event(f"Running live pipeline simulation for {streamer_name}...")
        chat = ChatSpikeTracker(window_sec=6.0)
        chat.connected = True
        detector = StreamViralityDetector(
            score_threshold=float(self.cfg.get("viral_score_threshold", 68.0)),
            chat_z_threshold=float(self.cfg.get("chat_z_threshold", 2.4)),
            audio_z_threshold=float(self.cfg.get("audio_z_threshold", 2.2)),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            segs: List[Path] = []
            # FFmpeg's lavfi sine source defaults to -18 dBFS (amp ~0.125): volume 0.55 ~ calm talk
            # (~-26 dBFS), volume 5.8 ~ a scream/explosion (~-6 dBFS).
            specs = [
                ("0:00-0:05 // CRUISING OCEAN DRIVE (CALM)", (14, 24, 38), (56, 189, 248), 0.55, 190),
                ("0:05-0:10 // ENTERING VICE CITY BANK (SETUP)", (18, 28, 22), (74, 222, 128), 0.65, 210),
                ("0:10-0:15 // 5-STAR HELI CRASH + STREAMER SCREAM!", (64, 14, 22), (245, 183, 0), 5.8, 440),
                ("0:15-0:20 // CHAT GOING CRAZY (AFTERMATH)", (28, 18, 44), (244, 63, 94), 1.8, 290),
            ]
            font_path = "/System/Library/Fonts/Helvetica.ttc"
            f_big = ImageFont.truetype(font_path, 44) if Path(font_path).exists() else ImageFont.load_default()
            f_sub = ImageFont.truetype(font_path, 26) if Path(font_path).exists() else ImageFont.load_default()

            for i, (label, bg, accent, vol, freq) in enumerate(specs):
                png = tmp / f"frame_{i}.png"
                ts_path = tmp / f"seg_{i:03d}.ts"
                img = Image.new("RGB", (1280, 720), bg)
                dr = ImageDraw.Draw(img)
                dr.rectangle([48, 48, 1232, 672], outline=accent, width=5)
                dr.text((90, 120), f"LIVE STREAM: {streamer_name.upper()} — GRAND THEFT AUTO VI", fill=accent, font=f_sub)
                dr.text((90, 300), label, fill=(248, 250, 252), font=f_big)
                dr.text((90, 400), "24/7 AUTO-CLIPPER RING BUFFER TEST STREAM (16:9 EDITOR MASTER)", fill=(180, 195, 210), font=f_sub)
                img.save(png)

                subprocess.run(
                    [
                        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                        "-loop", "1", "-t", "5", "-i", str(png),
                        "-f", "lavfi", "-t", "5", "-i", f"sine=frequency={freq}:sample_rate=48000",
                        "-filter_complex", f"[1:a]volume={vol}[a]",
                        "-map", "0:v", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-f", "mpegts", str(ts_path),
                    ],
                    check=True,
                    timeout=30,
                )
                segs.append(ts_path)

            # Calm baseline (4 evaluations warm up the audio baseline), then the viral surge on seg 2
            base_t = time.time() - 36.0
            for b in range(4):
                for m in range(2):
                    chat.record_message(f"chilling in vice city {b}{m}", ts=base_t + b * 6.0 + m * 1.5)
            for k, idx in enumerate((0, 1, 0, 1)):
                detector.evaluate_segment(segs[idx], chat.snapshot(base_t + 6.0 + k * 4.0), run_visual=False)

            spike_t = base_t + 26.0
            hype_lines = [
                "NO WAYYYYY CLIP THAT!!!", "LMAOOOOO WHAT JUST HAPPENED", "OMG THE PHYSICS ARE INSANE",
                "CLIP CLIP CLIP", "KEKW BRO FLEW OUT THE WINDSHIELD", "AINT NO WAY LUCIA SURVIVED THAT",
                "WTF????", "CLIP IT RIGHT NOW!!!", "DEAD LMAOOO", "W STREAMER", "INSANE CRASH!!",
                "CLIP THAT IMMEDIATELY", "NO WAY BRO", "OMG???", "HAHAHA CLIP THAT",
            ]
            for idx, line in enumerate(hype_lines):
                chat.record_message(line, ts=spike_t + idx * 0.25, user=f"viewer{idx}")

            ev = detector.evaluate_segment(segs[2], chat.snapshot(spike_t + 4.0), run_visual=True)
            pre = float(self.cfg.get("pre_roll_sec", DEFAULTS["pre_roll_sec"]))
            post = float(self.cfg.get("post_roll_sec", DEFAULTS["post_roll_sec"]))
            peak_abs = 10.0 + ev.peak_offset_sec
            start = max(0.0, peak_abs - pre)
            end = min(20.0, peak_abs + post)
            clip = export_viral_clip(
                segment_paths=segs,
                clips_root=self.clips_dir,
                streamer=streamer_name,
                platform="twitch",
                stream_title="FIRST EVER GTA 6 MARATHON STREAM!! VICE CITY CHAOS",
                game="Grand Theft Auto VI",
                viral_score=ev.viral_score,
                chat_z=ev.chat_z,
                audio_z=ev.audio_z,
                visual_score=ev.visual_score,
                trigger_reason=ev.trigger_reason,
                peak_marker_sec=peak_abs - start,
                top_chat_messages=ev.top_chat_messages,
                export_vertical_draft=bool(self.cfg.get("export_vertical_draft", False)),
                max_clips_gb=float(self.cfg.get("max_clips_folder_gb", 25.0)),
                trim_start_sec=start,
                trim_duration_sec=end - start,
                normalize_audio=bool(self.cfg.get("normalize_audio", True)),
                quality_check=False,  # synthetic slideshow frames are intentionally static
            )
            self._log_event(f"SIMULATED VIRAL CLIP SAVED (Score {clip.viral_score}) -> {clip.mp4_relpath}")
            return clip

    def get_status_snapshot(self) -> Dict:
        with self._lock:
            streamers_out = []
            for scfg in self.cfg.get("streamers", []):
                key = str(scfg.get("name", "")).lower()
                st = self.statuses.get(key)
                w = self.workers.get(key)
                streamers_out.append({
                    **scfg,
                    "is_live": st.is_live if st else False,
                    "title": st.title if st else "",
                    "game": st.game if st else "",
                    "viewers": st.viewers if st else 0,
                    "matches_target": st.matches_target if st else False,
                    "worker": w.telemetry() if w else None,
                })
            cfg_out = {**DEFAULTS, **self.cfg}
            return {
                "bot_running": bool(self._thread and self._thread.is_alive()),
                "started_at": self.started_at,
                "last_poll_at": self.last_poll_at,
                "clips_dir_abs": str(self.clips_dir.resolve()),
                "active_workers": len(self.workers),
                "config": cfg_out,
                "streamers": streamers_out,
                "clips": list_exported_clips(self.clips_dir, limit=40),
                "events": list(self.recent_events),
            }


def main() -> None:
    parser = argparse.ArgumentParser(description="GTA 6 24/7 Multi-Streamer Viral Clipping Bot")
    parser.add_argument("--simulate", action="store_true", help="Run an end-to-end simulated viral clip test and exit")
    parser.add_argument("--headless", action="store_true", help="Run 24/7 bot in terminal without web dashboard")
    parser.add_argument("--port", type=int, default=8787, help="Control panel port (default 8787)")
    args = parser.parse_args()

    engine = ClipperBotEngine()
    if args.simulate:
        clip = engine.run_simulated_viral_event("kaicenat_demo")
        print(json.dumps(asdict(clip), indent=2))
        return

    def _handle_sig(_sig, _frame):
        engine.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    engine.start()
    if args.headless:
        print(f"GTA 6 24/7 Clipping Bot running headless. Clips folder: {engine.clips_dir.resolve()}")
        while True:
            time.sleep(5)
    else:
        from dashboard import run_dashboard
        run_dashboard(engine, port=args.port)


if __name__ == "__main__":
    main()
