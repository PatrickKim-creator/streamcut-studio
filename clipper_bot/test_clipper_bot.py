#!/usr/bin/env python3
"""
Automated Unit & End-to-End Integration Test Suite
for the GTA 6 24/7 Multi-Streamer Viral Clipping Bot.
"""

import csv
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import requests

from bot import PROJECT_DIR, ClipperBotEngine
from chat_watcher import ChatSpikeTracker
from clip_exporter import (
    ClipRejected,
    enforce_clip_expiry,
    enforce_disk_quota,
    export_viral_clip,
    list_exported_clips,
    set_clip_kept,
)
from dashboard import create_dashboard_server
from ring_buffer import FFMPEG_BIN
from stream_monitor import matches_gta6_filter
from viral_detector import StreamViralityDetector


def _make_synthetic_ts(path: Path, freq: int, vol: float, dur: int = 3) -> Path:
    """Create a small patterned MPEG-TS video+audio segment via FFmpeg for testing."""
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=24:duration={dur}",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={dur}",
            "-filter_complex", f"[1:a]volume={vol}[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-f", "mpegts", str(path),
        ],
        check=True,
        timeout=25,
    )
    return path


def _make_frozen_silent_ts(path: Path, dur: int = 5) -> Path:
    """Create a frozen black silent segment (simulates Twitch ad slate / BRB screen)."""
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=black:s=320x180:r=24:d={dur}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={dur}",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-f", "mpegts", str(path),
        ],
        check=True,
        timeout=25,
    )
    return path


class TestGTA6ClipperBot(unittest.TestCase):
    def test_matches_gta6_filter(self):
        kws = ["gta 6", "gta vi", "grand theft auto vi", "vice city"]
        self.assertTrue(matches_gta6_filter("First heist!", "Grand Theft Auto VI", kws, True, False))
        self.assertTrue(matches_gta6_filter("PLAYING GTA 6 ALL NIGHT", "Just Chatting", kws, True, False))
        self.assertFalse(matches_gta6_filter("Ranked Valorant grind", "Valorant", kws, True, False))
        self.assertTrue(matches_gta6_filter("Ranked Valorant grind", "Valorant", kws, True, True))
        self.assertTrue(matches_gta6_filter("Cooking stream", "IRL", kws, False, False))

    def test_chat_filters_spam_raids_and_detects_real_reactions(self):
        tracker = ChatSpikeTracker(window_sec=6.0)
        tracker.connected = True
        t0 = 1_700_000_000.0

        # Bot accounts, commands, and links must be filtered out
        self.assertFalse(tracker.record_message("Subscribe at https://twitch.tv/subs/kai", ts=t0, user="nightbot"))
        self.assertFalse(tracker.record_message("!giveaway enter now", ts=t0 + 0.1, user="viewer1"))
        self.assertFalse(tracker.record_message("Check out https://drops-register.ubi.com/link", ts=t0 + 0.2, user="moduser"))
        self.assertFalse(tracker.record_message("free coins", ts=t0 + 0.3, user="custom_helper_bot"))
        self.assertEqual(tracker.filtered_messages, 4)

        # Build 4 calm baseline windows (~0.5 msgs/sec)
        for b in range(4):
            tracker.record_message(f"nice driving {b}", ts=t0 + b * 6.0 + 1.0, user=f"u{b}a")
            tracker.record_message(f"chill stream {b}", ts=t0 + b * 6.0 + 3.0, user=f"u{b}b")
            tracker.record_message(f"graphics look good {b}", ts=t0 + b * 6.0 + 4.5, user=f"u{b}c")

        calm = tracker.snapshot(ts=t0 + 23.0)
        self.assertLess(calm.chat_z, 1.5)

        # Giveaway copypasta flood (everyone typing "Vice City" with invisible chars)
        # must be discounted via spam_share penalty!
        giveaway_t = t0 + 30.0
        for i in range(16):
            invis = "\u034f" if i % 2 else ""
            tracker.record_message(f"Vice City{invis}", ts=giveaway_t + i * 0.25, user=f"gw{i}")
        gw_snap = tracker.snapshot(ts=giveaway_t + 4.2)
        self.assertGreaterEqual(gw_snap.spam_share, 0.8)
        self.assertLess(gw_snap.chat_z, 2.0)

        # Gift-sub bomb / raid suppression must cap chat_z <= 1.0
        raid_t = t0 + 45.0
        tracker.record_event("submysterygift", ts=raid_t)
        for i in range(15):
            tracker.record_message("OMG WWWW LMAO!!", ts=raid_t + 1.0 + i * 0.2, user=f"sub{i}")
        raid_snap = tracker.snapshot(ts=raid_t + 4.5)
        self.assertTrue(raid_snap.suppressed)
        self.assertLessEqual(raid_snap.chat_z, 1.0)

        # Genuine reaction explosion after suppression expires -> high chat_z + distinct hooks
        burst_t = t0 + 80.0
        for i in range(14):
            tracker.record_message(f"NO WAY CLIP THAT LMAO {i}!!! 💀", ts=burst_t + i * 0.25, user=f"fan{i}")
        spike = tracker.snapshot(ts=burst_t + 3.8)
        self.assertFalse(spike.suppressed)
        self.assertGreater(spike.chat_z, 3.0)
        self.assertGreater(spike.hype_ratio, 0.8)
        self.assertGreaterEqual(spike.reaction_users, 12)
        self.assertTrue(any("CLIP THAT" in m for m in spike.top_messages))

        # Verify segment-aligned window_snapshot builds baseline and scores peaks cleanly
        win_tracker = ChatSpikeTracker()
        win_tracker.connected = True
        wt0 = 1_700_001_000.0
        for w in range(5):
            ws = wt0 + w * 10.0
            for j in range(4):
                win_tracker.record_message(f"normal chat {w}-{j}", ts=ws + j * 2.0, user=f"cw{j}")
            snap = win_tracker.window_snapshot(ws, ws + 10.0)
            self.assertLess(snap.chat_z, 2.0)

        spike_ws = wt0 + 50.0
        for j in range(20):
            win_tracker.record_message("OMG NO WAY CLIP IT LMAO!!", ts=spike_ws + 4.0 + j * 0.15, user=f"react{j}")
        win_spike = win_tracker.window_snapshot(spike_ws, spike_ws + 10.0)
        self.assertGreater(win_spike.chat_z, 3.0)
        self.assertGreaterEqual(win_spike.peak_offset_sec, 3.0)
        self.assertLessEqual(win_spike.peak_offset_sec, 9.0)

    def test_detector_exporter_trimming_quality_gate_and_quota(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            clips_root = tmp / "clips_to_edit"
            clips_root.mkdir()

            # Build 4 calm segments (for 4-seg dB warm-up) + 1 loud scream/crash peak segment
            s0 = _make_synthetic_ts(tmp / "s0.ts", 200, 0.50, dur=3)
            s1 = _make_synthetic_ts(tmp / "s1.ts", 210, 0.55, dur=3)
            s2 = _make_synthetic_ts(tmp / "s2.ts", 220, 0.52, dur=3)
            s3 = _make_synthetic_ts(tmp / "s3.ts", 215, 0.58, dur=3)
            s_peak = _make_synthetic_ts(tmp / "s4.ts", 520, 5.8, dur=3)
            s_bad = _make_frozen_silent_ts(tmp / "s_bad.ts", dur=5)

            chat = ChatSpikeTracker(window_sec=6.0)
            chat.connected = True
            t0 = 1_700_000_000.0
            for b in range(4):
                chat.record_message(f"driving around {b}", ts=t0 + b * 6.0, user=f"u{b}1")
                chat.record_message(f"hello {b}", ts=t0 + b * 6.0 + 2.0, user=f"u{b}2")

            detector = StreamViralityDetector(score_threshold=68.0, chat_z_threshold=2.4, audio_z_threshold=2.2)
            for idx, seg in enumerate((s0, s1, s2, s3)):
                ev = detector.evaluate_segment(seg, chat.snapshot(t0 + 6.0 + idx * 4.0), run_visual=False)
                self.assertFalse(ev.should_clip)
            self.assertTrue(detector.audio_warmed_up)

            # Inject genuine multi-viewer chat burst on peak segment
            burst_t = t0 + 26.0
            for i in range(15):
                chat.record_message(f"OMG CLIP IT INSANE CRASH #{i}!!", ts=burst_t + i * 0.2, user=f"fan{i}")

            ev_peak = detector.evaluate_segment(s_peak, chat.snapshot(burst_t + 3.5), run_visual=True)
            self.assertTrue(ev_peak.should_clip)
            self.assertGreaterEqual(ev_peak.viral_score, 68.0)
            self.assertGreater(ev_peak.audio_z, 2.2)
            self.assertGreater(ev_peak.chat_z, 2.4)

            # Quality gate must reject frozen black silent clips and leave no MP4 behind
            with self.assertRaises(ClipRejected):
                export_viral_clip(
                    segment_paths=[s_bad],
                    clips_root=clips_root,
                    streamer="AdBreakStreamer",
                    platform="twitch",
                    stream_title="BRB Screen",
                    game="Grand Theft Auto VI",
                    viral_score=80.0,
                    chat_z=4.0,
                    audio_z=0.0,
                    visual_score=0.2,
                    trigger_reason="test_bad",
                    peak_marker_sec=2.0,
                    top_chat_messages=[],
                    quality_check=True,
                )
            self.assertEqual(list(clips_root.rglob("*.mp4")), [])

            # Export with exact peak-centered trimming (5 s out of 9 s total) + loudnorm + 9:16 draft
            clip = export_viral_clip(
                segment_paths=[s2, s3, s_peak],
                clips_root=clips_root,
                streamer="TestStreamer",
                platform="twitch",
                stream_title="GTA 6 Launch Chaos",
                game="Grand Theft Auto VI",
                viral_score=ev_peak.viral_score,
                chat_z=ev_peak.chat_z,
                audio_z=ev_peak.audio_z,
                visual_score=ev_peak.visual_score,
                trigger_reason=ev_peak.trigger_reason,
                peak_marker_sec=4.0,
                top_chat_messages=ev_peak.top_chat_messages,
                export_vertical_draft=True,
                max_clips_gb=5.0,
                trim_start_sec=2.0,
                trim_duration_sec=5.0,
                normalize_audio=True,
                quality_check=True,
            )

            mp4_path = clips_root / clip.mp4_relpath
            thumb_path = clips_root / clip.thumb_relpath
            vert_path = clips_root / str(clip.vertical_relpath)
            csv_path = clips_root / "EDIT_QUEUE.csv"

            self.assertTrue(mp4_path.exists() and mp4_path.stat().st_size > 10_000)
            self.assertTrue(thumb_path.exists() and thumb_path.stat().st_size > 1_000)
            self.assertTrue(vert_path.exists() and vert_path.stat().st_size > 10_000)
            self.assertTrue(csv_path.exists())
            self.assertGreaterEqual(clip.duration_sec, 4.5)
            self.assertLessEqual(clip.duration_sec, 5.6)
            self.assertTrue(bool(clip.chat_hook))

            with csv_path.open("r", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["streamer"], "TestStreamer")
            self.assertEqual(rows[0]["chat_hook"], clip.chat_hook)

            listed = list_exported_clips(clips_root)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["clip_id"], clip.clip_id)

            # Disk quota enforcement should prune oldest clips AND clean EDIT_QUEUE.csv
            removed = enforce_disk_quota(clips_root, max_gb=0.000001)
            self.assertEqual(removed, 1)
            self.assertFalse(mp4_path.exists())
            self.assertFalse(thumb_path.exists())
            self.assertFalse(vert_path.exists())
            with csv_path.open("r", encoding="utf-8") as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 0)

    def test_auto_delete_10min_in_order_and_keep_protection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            day = root / "2026-11-18"
            day.mkdir()
            now = time.time()

            def _mk(name: str, age_sec: float, kept: bool = False) -> Path:
                mp4 = day / f"{name}.mp4"
                mp4.write_bytes(b"0" * 5000)
                (day / f"{name}_thumb.jpg").write_bytes(b"jpg")
                (day / f"{name}_meta.json").write_text(
                    f'{{"mp4_relpath": "2026-11-18/{name}.mp4", "kept": {"true" if kept else "false"}}}',
                    encoding="utf-8",
                )
                ts = now - age_sec
                os.utime(mp4, (ts, ts))
                return mp4

            c_oldest = _mk("clip_1_oldest", 900)        # 15m old, unchosen -> deleted 1st
            c_kept = _mk("clip_2_chosen", 800, kept=False)
            c_second = _mk("clip_3_older", 650)         # 10.8m old, unchosen -> deleted 2nd
            c_fresh = _mk("clip_4_fresh", 120)          # 2m old, unchosen -> stays!

            # Mark clip_2 as chosen/kept via set_clip_kept
            self.assertTrue(set_clip_kept(root, "2026-11-18/clip_2_chosen.mp4", kept=True))

            deleted = enforce_clip_expiry(root, max_age_minutes=10.0, now_ts=now)
            self.assertEqual(deleted, ["2026-11-18/clip_1_oldest.mp4", "2026-11-18/clip_3_older.mp4"])
            self.assertFalse(c_oldest.exists())
            self.assertFalse(c_second.exists())
            self.assertTrue(c_kept.exists())
            self.assertTrue(c_fresh.exists())

    def test_dashboard_http_api_and_security(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shutil.copy2(PROJECT_DIR / "config.json", tmp / "config.json")
            engine = ClipperBotEngine(project_dir=tmp)
            server = create_dashboard_server(engine, host="127.0.0.1", port=0)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{port}"

            try:
                for route in ("/", "/styles.css", "/app.js"):
                    r = requests.get(f"{base}{route}", timeout=5)
                    self.assertEqual(r.status_code, 200, route)
                    self.assertGreater(len(r.text), 100)

                st = requests.get(f"{base}/api/status", timeout=5)
                self.assertEqual(st.status_code, 200)
                data = st.json()
                self.assertIn("streamers", data)
                self.assertIn("clips", data)

                # Add, toggle, and remove a streamer
                r_add = requests.post(
                    f"{base}/api/streamers/add",
                    json={"name": "test_creator", "platform": "youtube", "watch_any_game": True},
                    timeout=5,
                )
                self.assertEqual(r_add.status_code, 200)
                names = [s["name"].lower() for s in r_add.json()["streamers"]]
                self.assertIn("test_creator", names)

                r_tog = requests.post(
                    f"{base}/api/streamers/toggle",
                    json={"name": "test_creator", "field": "watch_any_game"},
                    timeout=5,
                )
                self.assertEqual(r_tog.status_code, 200)
                creator = [s for s in r_tog.json()["streamers"] if s["name"].lower() == "test_creator"][0]
                self.assertFalse(creator["watch_any_game"])

                # Update both classic and new pacing/quality settings
                r_set = requests.post(
                    f"{base}/api/settings",
                    json={
                        "viral_score_threshold": 72.5,
                        "pre_roll_sec": 18,
                        "post_roll_sec": 12,
                        "max_clip_sec": 55,
                        "max_clips_per_hour": 6,
                        "warmup_sec": 60,
                        "normalize_audio": True,
                        "quality_check": True,
                        "export_vertical_draft": True,
                    },
                    timeout=5,
                )
                self.assertEqual(r_set.status_code, 200)
                cfg_out = r_set.json()["config"]
                self.assertEqual(cfg_out["viral_score_threshold"], 72.5)
                self.assertEqual(cfg_out["pre_roll_sec"], 18)
                self.assertEqual(cfg_out["max_clip_sec"], 55)
                self.assertEqual(cfg_out["max_clips_per_hour"], 6)
                self.assertEqual(cfg_out["warmup_sec"], 60)
                self.assertTrue(cfg_out["normalize_audio"])
                self.assertTrue(cfg_out["quality_check"])
                self.assertTrue(cfg_out["export_vertical_draft"])

                # Force-clip on an unwatched streamer returns 400 with clean JSON error
                r_fc = requests.post(f"{base}/api/force-clip", json={"name": "not_watched"}, timeout=5)
                self.assertEqual(r_fc.status_code, 400)
                self.assertIn("error", r_fc.json())

                # Run offline viral simulation via API
                r_sim = requests.post(f"{base}/api/simulate", json={"streamer": "sim_tester"}, timeout=90)
                self.assertEqual(r_sim.status_code, 200)
                sim_clip = r_sim.json()["clip"]
                mp4_rel = sim_clip["mp4_relpath"]
                self.assertGreaterEqual(sim_clip["viral_score"], 68.0)

                # Stream clip via HTTP 200 & HTTP 206 Byte-Range
                r_mp4 = requests.get(f"{base}/clips/{mp4_rel}", timeout=10)
                self.assertEqual(r_mp4.status_code, 200)
                self.assertEqual(r_mp4.headers.get("Accept-Ranges"), "bytes")
                self.assertGreater(len(r_mp4.content), 20_000)

                r_range = requests.get(f"{base}/clips/{mp4_rel}", headers={"Range": "bytes=0-1023"}, timeout=10)
                self.assertEqual(r_range.status_code, 206)
                self.assertEqual(len(r_range.content), 1024)
                self.assertIn("bytes 0-1023/", r_range.headers.get("Content-Range", ""))

                # Path traversal must be rejected with 404
                r_trav = requests.get(f"{base}/clips/../../config.json", timeout=5)
                self.assertEqual(r_trav.status_code, 404)

                # Delete clip cleanly via API AND verify EDIT_QUEUE.csv syncs
                r_del = requests.post(f"{base}/api/clips/delete", json={"mp4_relpath": mp4_rel}, timeout=5)
                self.assertEqual(r_del.status_code, 200)
                self.assertEqual(len(r_del.json()["clips"]), 0)
                with (tmp / "clips_to_edit" / "EDIT_QUEUE.csv").open("r", encoding="utf-8") as fh:
                    self.assertEqual(len(list(csv.DictReader(fh))), 0)

                # Remove added streamer
                r_rem = requests.post(f"{base}/api/streamers/remove", json={"name": "test_creator"}, timeout=5)
                self.assertEqual(r_rem.status_code, 200)
                self.assertNotIn("test_creator", [s["name"].lower() for s in r_rem.json()["streamers"]])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
