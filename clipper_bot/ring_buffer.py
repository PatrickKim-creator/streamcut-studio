#!/usr/bin/env python3
"""
Low-CPU Rolling Ring-Buffer DVR Recorder (FFmpeg -c copy segment_wrap)
======================================================================
Records a live HLS stream into rolling 10-second MPEG-TS chunks using zero
video re-encoding (-c copy) and automatic circular overwrite (-segment_wrap),
keeping CPU <1% and disk usage capped (~60-100 MB per streamer) 24/7.
"""

import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional

FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg" if shutil.which("/opt/homebrew/bin/ffmpeg") else (shutil.which("ffmpeg") or "ffmpeg")


class StreamRingBuffer:
    """Manages one streamer's rolling MPEG-TS DVR ring buffer."""

    def __init__(
        self,
        streamer_name: str,
        buffer_root: Path,
        segment_duration_sec: int = 10,
        ring_size: int = 18,
    ):
        self.streamer_name = streamer_name
        self.segment_duration_sec = max(4, int(segment_duration_sec))
        self.ring_size = max(6, int(ring_size))
        safe = "".join(c for c in streamer_name.lower() if c.isalnum() or c in ("_", "-")) or "stream"
        self.buf_dir = buffer_root / safe
        self.buf_dir.mkdir(parents=True, exist_ok=True)
        self._proc: Optional[subprocess.Popen] = None
        self.started_at: float = 0.0

    def start(self, hls_url: str) -> None:
        self.stop()
        try:
            subprocess.run(["pkill", "-f", str(self.buf_dir.resolve())], capture_output=True, timeout=2)
        except Exception:
            pass

        now = time.time()
        # Purge only stale segments (>3 min old); keep recent rolling segments so pre-roll survives HLS reconnects!
        recent_segs = []
        for p in self.buf_dir.glob("seg_*.ts"):
            try:
                st = p.stat()
                if (now - st.st_mtime) > 180.0 or st.st_size < 65536:
                    p.unlink()
                else:
                    idx = int(p.stem.split("_")[1])
                    recent_segs.append((st.st_mtime, idx, st.st_size, p))
            except Exception:
                pass

        start_num = 0
        if recent_segs:
            recent_segs.sort(key=lambda x: x[0])
            _, last_idx, last_sz, last_p = recent_segs[-1]
            # If the newest segment was tiny/partial (<1.8 MB), overwrite it; otherwise advance to next slot
            if last_sz < 1_800_000:
                try:
                    last_p.unlink()
                except OSError:
                    pass
                start_num = last_idx % self.ring_size
            else:
                start_num = (last_idx + 1) % self.ring_size

        pattern = str(self.buf_dir / "seg_%03d.ts")
        cmd = [
            FFMPEG_BIN,
            "-hide_banner", "-loglevel", "error",
            "-rw_timeout", "12000000",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", hls_url,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self.segment_duration_sec),
            "-segment_wrap", str(self.ring_size),
            "-segment_start_number", str(start_num),
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            pattern,
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.started_at = time.time()

    def is_running(self) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            return False
        # Watchdog: if FFmpeg is alive but hasn't written any segment bytes in >18s
        # (common on YouTube Live HLS DVR playlist window ends), restart seamlessly at next segment number.
        now = time.time()
        stall_limit = max(18.0, self.segment_duration_sec * 1.8)
        if now - self.started_at > stall_limit:
            newest_mtime = 0.0
            for p in self.buf_dir.glob("seg_*.ts"):
                try:
                    mt = p.stat().st_mtime
                    if mt > newest_mtime:
                        newest_mtime = mt
                except OSError:
                    continue
            if newest_mtime == 0.0 or (now - newest_mtime) > stall_limit:
                self.stop()
                return False
        return True

    def stop(self) -> None:
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=4.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    def clear_segments(self) -> None:
        for p in self.buf_dir.glob("seg_*.ts"):
            try:
                p.unlink()
            except OSError:
                pass

    def get_completed_segments(self, min_settle_sec: float = 1.5) -> List[Path]:
        """
        Return all completed .ts chunks ordered oldest -> newest, excluding
        the chunk actively open and being written by FFmpeg right now.
        """
        files = []
        for p in self.buf_dir.glob("seg_*.ts"):
            try:
                st = p.stat()
                if st.st_size < 4096:
                    continue
                files.append((st.st_mtime, p))
            except OSError:
                continue

        if not files:
            return []
        files.sort(key=lambda item: item[0])

        # While FFmpeg -f segment is running, the newest file is ALWAYS the currently
        # open segment (even between 2-second HLS chunk arrivals), so exclude it until
        # FFmpeg rolls over to the next segment file.
        if self.is_running():
            files = files[:-1]
        return [p for _, p in files]
