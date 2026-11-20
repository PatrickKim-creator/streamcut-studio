#!/usr/bin/env python3
"""
Automated Unit & End-to-End Integration Test Suite for CUTLAB PRO
(YouTube Shorts Pro Video Editor + Whisper Auto-Captions + Viral BGM + Bot Sync).
"""

import subprocess
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import requests
from PIL import Image

from analyzer import (
    FFMPEG_BIN,
    analyze_video_file,
    classify_clip_vibe,
    detect_streamer_cam_box,
)
from caption_renderer import (
    build_timed_overlay_plates,
    render_caption_plate,
    render_cover_thumbnail,
    write_srt_file,
)
from music_synth import BGM_CATALOG, ensure_bgm_assets
from render_engine import render_short_project
from server import ShortsEditorApp, create_server
from sfx_synth import SFX_CATALOG, ensure_sfx_assets
from transcriber import map_words_to_timeline


def _make_video_with_dead_air(path: Path) -> Path:
    """Create a 9 s 1280x720 MP4: 2.5 s tone -> 2.5 s silence -> 4 s loud climax."""
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=9",
            "-f", "lavfi", "-i", "sine=frequency=240:sample_rate=48000:duration=2.5",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:duration=2.5",
            "-f", "lavfi", "-i", "sine=frequency=520:sample_rate=48000:duration=4.0",
            "-filter_complex",
            "[1:a]volume=1.2[a1];[3:a]volume=5.5[a3];[a1][2:a][a3]concat=n=3:v=0:a=1,aformat=channel_layouts=stereo[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path),
        ],
        check=True,
        timeout=30,
    )
    return path


class TestShortsProEditor(unittest.TestCase):
    def test_sfx_and_viral_bgm_synthesizers(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sfx_paths = ensure_sfx_assets(tmp / "sfx")
            self.assertEqual(len(sfx_paths), len(SFX_CATALOG))
            self.assertIn("vine_boom", sfx_paths)
            self.assertIn("record_scratch", sfx_paths)
            for sfx_id, wav_path in sfx_paths.items():
                self.assertTrue(wav_path.exists(), sfx_id)
                with wave.open(str(wav_path), "rb") as wf:
                    self.assertEqual(wf.getnchannels(), 2)
                    self.assertEqual(wf.getframerate(), 48000)
                    self.assertGreater(wf.getnframes(), 4000)

            bgm_paths = ensure_bgm_assets(tmp / "bgm")
            self.assertEqual(len(bgm_paths), len(BGM_CATALOG))
            for fname, wav_path in bgm_paths.items():
                self.assertTrue(wav_path.exists(), fname)
                with wave.open(str(wav_path), "rb") as wf:
                    self.assertEqual(wf.getnchannels(), 2)
                    self.assertEqual(wf.getframerate(), 48000)
                    self.assertGreater(wf.getnframes(), 48000 * 5)

    def test_caption_hook_cover_and_srt(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for style in ("capcut_pop", "mrbeast_bold", "variety_pill", "clean_minimal"):
                img = render_caption_plate("CALL THE COPS NOW", style=style, active_word_idx=1)
                self.assertEqual(img.size, (1080, 1920))
                self.assertEqual(img.mode, "RGBA")

            plates = build_timed_overlay_plates(
                tmp / "plates",
                hook_cfg={"enabled": False, "text": ""},
                captions=[{
                    "start": 0.5,
                    "end": 2.3,
                    "text": "Call the cops",
                    "words": [
                        {"word": "Call", "start": 0.5, "end": 0.9},
                        {"word": "the", "start": 0.9, "end": 1.2},
                        {"word": "cops", "start": 1.2, "end": 2.3},
                    ],
                }],
                caption_style_cfg={"style": "capcut_pop", "font": "arial_black", "size": 64, "accent": "#FFE600", "y_pct": 76},
                framing={"mode": "streamer_top", "top_pad": 0.05, "split_ratio": 0.31},
            )
            # 0 hook plates (disabled) + 3 exact-timed karaoke word plates
            self.assertEqual(len(plates), 3)
            self.assertAlmostEqual(plates[0]["start"], 0.5, places=2)
            self.assertAlmostEqual(plates[2]["start"], 1.2, places=2)
            for p in plates:
                self.assertTrue(p["path"].exists())

            raw_jpg = tmp / "frame.jpg"
            Image.new("RGB", (1080, 1920), (30, 50, 90)).save(raw_jpg)
            cov = render_cover_thumbnail(raw_jpg, tmp / "cover.jpg", "CALL THE COPS")
            self.assertTrue(cov.exists())
            with Image.open(cov) as cov_im:
                self.assertEqual(cov_im.size, (1080, 1920))

            srt = write_srt_file([{"start": 0.5, "end": 2.4, "text": "Call the cops!"}], tmp / "out.srt")
            txt = srt.read_text(encoding="utf-8")
            self.assertIn("00:00:00,500 --> 00:00:02,400", txt)
            self.assertIn("Call the cops!", txt)

    def test_vibe_classifier_word_remap_and_auto_edit_no_lmao(self):
        # Verify clip-aware BGM + SFX selection by mood
        v_act = classify_clip_vibe("Call the cops! Shoot him! Crash the car!", [], {"audio_z": 3.2})
        self.assertEqual(v_act["bgm_file"], "phonk_drift.wav")
        self.assertEqual(v_act["peak_sfx"], "impact")

        v_com = classify_clip_vibe("Bro stop what the hell wait chill", [], {"audio_z": 1.1})
        self.assertEqual(v_com["bgm_file"], "comedy_bounce.wav")
        self.assertEqual(v_com["peak_sfx"], "vine_boom")

        v_sus = classify_clip_vibe("Be careful he is right behind you hide", [], {})
        self.assertEqual(v_sus["bgm_file"], "tension_dark.wav")

        # Word remapping across cut + speed-ramped timeline
        words = [
            {"word": "First", "start": 0.2, "end": 0.6},
            {"word": "cut", "start": 3.0, "end": 3.4},  # falls inside removed dead-air
            {"word": "Second", "start": 5.5, "end": 6.0},
        ]
        segs = [
            {"start": 0.0, "end": 2.0, "speed": 1.0},
            {"start": 5.0, "end": 7.0, "speed": 2.0},
        ]
        cues = map_words_to_timeline(words, segs)
        texts = " ".join(c["text"] for c in cues)
        self.assertIn("First", texts)
        self.assertIn("Second", texts)
        self.assertNotIn("cut", texts)

        with tempfile.TemporaryDirectory() as td:
            vid = _make_video_with_dead_air(Path(td) / "clip.mp4")
            res = analyze_video_file(
                vid,
                meta={"streamer": "kaicenat", "game": "Grand Theft Auto VI", "chat_hook": "Lmaoooooo", "top_chat_messages": ["LMAOOOOO", "DEAD"]},
                auto_caption=False,
            )
            plan = res["plan"]
            self.assertEqual(plan["framing"]["mode"], "streamer_top")
            self.assertAlmostEqual(plan["framing"]["cam_y"], 0.395, places=2)
            # Never auto-inject "LMAOOO" chat spam as hook or captions!
            self.assertFalse(plan["hook"]["enabled"])
            all_cap_txt = " ".join(c["text"] for c in plan["captions"]).lower()
            self.assertNotIn("lmao", all_cap_txt)
            # Always picks a built-in viral BGM track automatically
            self.assertTrue(plan["audio"]["bgm_file"].endswith(".wav"))
            self.assertTrue(plan["audio"]["bgm_ducking"])

            has_dv, box_dv = detect_streamer_cam_box("darkviperau")
            self.assertTrue(has_dv)
            self.assertGreater(box_dv["cam_x"], 0.75)
            has_lirik, _ = detect_streamer_cam_box("lirik")
            self.assertFalse(has_lirik)

    def test_full_pro_render_and_sync_webhook_api(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bot_dir = tmp / "fake_bot_clips"
            bot_dir.mkdir()
            app = ShortsEditorApp(root_dir=tmp / "studio", bot_clips_dir=bot_dir, enable_sync_worker=False)
            server = create_server(app, host="127.0.0.1", port=0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{port}"

            try:
                for route in ("/", "/styles.css", "/app.js", "/api/library", "/api/sync/status", "/media/builtin-bgm/phonk_drift.wav", "/media/sfx/vine_boom.wav"):
                    r = requests.get(f"{base}{route}", timeout=5)
                    self.assertEqual(r.status_code, 200, route)

                lib = requests.get(f"{base}/api/library", timeout=5).json()
                self.assertEqual(len(lib["sfx"]), len(SFX_CATALOG))
                self.assertEqual(len(lib["bgm"]), len(BGM_CATALOG))

                # Sync config & webhook test
                r_sc = requests.post(f"{base}/api/sync/config", json={"auto_edit_enabled": True, "min_viral_score": 65}, timeout=5)
                self.assertEqual(r_sc.status_code, 200)
                self.assertEqual(r_sc.json()["config"]["min_viral_score"], 65.0)

                r_wh = requests.post(f"{base}/api/sync/new-clip", json={"mp4_relpath": "2026-11-18/test.mp4", "viral_score": 82.0}, timeout=5)
                self.assertEqual(r_wh.status_code, 200)
                self.assertTrue(r_wh.json()["queued"])

                # Upload synthetic video & analyze (skip Whisper on pure sine test tone)
                src_vid = _make_video_with_dead_air(tmp / "input_clip.mp4")
                r_up = requests.post(
                    f"{base}/api/upload?kind=video&filename=gta_test.mp4",
                    data=src_vid.read_bytes(),
                    timeout=15,
                )
                self.assertEqual(r_up.status_code, 200)
                up_id = r_up.json()["source_id"]

                r_an = requests.post(
                    f"{base}/api/analyze",
                    json={"source_type": "upload", "source_id": up_id, "preset": "viral_gaming", "auto_caption": False},
                    timeout=30,
                )
                self.assertEqual(r_an.status_code, 200)
                plan = r_an.json()["plan"]

                # Render with built-in viral BGM (phonk_drift.wav), vine_boom SFX, streamer_top framing,
                # Flash White transition, and per-word spoken karaoke captions!
                plan["title"] = "test_pro_short"
                plan["framing"]["mode"] = "streamer_top"
                plan["color"]["preset"] = "vice_neon"
                plan["audio"]["bgm_file"] = "phonk_drift.wav"
                plan["audio"]["bgm_ducking"] = True
                plan["sfx"] = [{"id": "s1", "sfx": "vine_boom", "time": 1.6, "volume": 0.55}]
                plan["captions"] = [{
                    "id": "cap_1", "start": 0.4, "end": 2.0, "text": "CALL THE COPS!",
                    "words": [
                        {"word": "CALL", "start": 0.4, "end": 0.9},
                        {"word": "THE", "start": 0.9, "end": 1.3},
                        {"word": "COPS!", "start": 1.3, "end": 2.0},
                    ],
                }]
                plan["export"]["resolution"] = "1440p"
                plan["export"]["fps"] = 30
                plan["export"]["crf"] = 20
                plan["segments"] = [
                    {"id": "s1", "start": 0.0, "end": 2.2, "speed": 1.15, "zoom_mode": "push_in", "zoom_amount": 1.08, "transition": "none", "transition_dur": 0},
                    {"id": "s2", "start": 5.2, "end": 8.4, "speed": 1.0, "zoom_mode": "punch", "zoom_amount": 1.15, "transition": "flash", "transition_dur": 0.22},
                ]

                r_ren = requests.post(
                    f"{base}/api/render",
                    json={"source_type": "upload", "source_id": up_id, "project": plan},
                    timeout=10,
                )
                self.assertEqual(r_ren.status_code, 200)
                job_id = r_ren.json()["job_id"]

                job = {}
                for _ in range(90):
                    job = requests.get(f"{base}/api/render/{job_id}", timeout=5).json()
                    if job["status"] in ("done", "error"):
                        break
                    time.sleep(0.4)
                self.assertEqual(job.get("status"), "done", job.get("error"))
                res = job["result"]
                self.assertEqual(res["width"], 1440)
                self.assertEqual(res["height"], 2560)
                self.assertEqual(res["bgm_file"], "phonk_drift.wav")
                self.assertGreater(res["bitrate_mbps"], 0.5)

                # Verify HTTP 206 byte-range video streaming, Cover JPG, SRT, path-traversal guard & delete
                r_rng = requests.get(f"{base}{res['mp4_url']}", headers={"Range": "bytes=0-2047"}, timeout=10)
                self.assertEqual(r_rng.status_code, 206)
                self.assertEqual(len(r_rng.content), 2048)
                self.assertEqual(requests.get(f"{base}{res['cover_url']}", timeout=5).status_code, 200)
                self.assertEqual(requests.get(f"{base}{res['srt_url']}", timeout=5).status_code, 200)
                self.assertEqual(requests.get(f"{base}/media/exports/../../server.py", timeout=5).status_code, 404)

                r_del = requests.post(f"{base}/api/exports/delete", json={"mp4_name": res["mp4_name"]}, timeout=5)
                self.assertEqual(r_del.status_code, 200)
                self.assertFalse((app.exports_dir / res["mp4_name"]).exists())
            finally:
                app.shutdown()
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
