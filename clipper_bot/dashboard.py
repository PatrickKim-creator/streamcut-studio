#!/usr/bin/env python3
"""
Web Mission Control Dashboard for the 24/7 GTA 6 Multi-Streamer Clipping Bot
=============================================================================
Serves live status, streamer roster controls, sensitivity tuning, manual
force-clip triggers, and an embedded video gallery of clips saved in
./clips_to_edit/ at http://127.0.0.1:8787/.
"""

import json
import mimetypes
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from bot import PROJECT_DIR, ClipperBotEngine
from clip_exporter import remove_from_edit_queue, set_clip_kept

STATIC_DIR = PROJECT_DIR / "static"


def make_handler(engine: ClipperBotEngine):
    class ClipperDashboardHandler(BaseHTTPRequestHandler):
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
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".json": "application/json; charset=utf-8",
                ".csv": "text/csv; charset=utf-8",
            }
            mime = mimes.get(ext) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            size = path.stat().st_size
            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes="):
                try:
                    spec = range_hdr[len("bytes="):].split(",")[0].strip()
                    start_s, end_s = spec.split("-", 1)
                    start = int(start_s) if start_s else 0
                    end = int(end_s) if end_s else size - 1
                    start = max(0, min(start, size - 1))
                    end = max(start, min(end, size - 1))
                    length = end - start + 1
                    with path.open("rb") as fh:
                        fh.seek(start)
                        chunk = fh.read(length)
                    self.send_response(HTTPStatus.PARTIAL_CONTENT)
                    self.send_header("Content-Type", mime)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
                except Exception:
                    pass

            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send_file(STATIC_DIR / "index.html")
            if path == "/styles.css":
                return self._send_file(STATIC_DIR / "styles.css")
            if path == "/app.js":
                return self._send_file(STATIC_DIR / "app.js")
            if path.startswith("/clips/"):
                rel = unquote(path[len("/clips/"):]).lstrip("/")
                target = (engine.clips_dir / rel).resolve()
                clips_root = engine.clips_dir.resolve()
                if clips_root in target.parents and target.suffix.lower() in (".mp4", ".jpg", ".jpeg", ".png", ".json", ".csv"):
                    return self._send_file(target)
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Invalid clip path"})

            if path == "/api/status":
                return self._send_json(HTTPStatus.OK, engine.get_status_snapshot())

            return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self._read_body()

                if path == "/api/streamers/add":
                    name = str(body.get("name") or "").strip().lstrip("@")
                    platform = str(body.get("platform") or "twitch").strip().lower()
                    url = str(body.get("url") or "").strip()
                    watch_any = bool(body.get("watch_any_game", False))
                    if not name:
                        return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Streamer name is required."})
                    if not url:
                        if platform == "twitch":
                            url = f"https://www.twitch.tv/{name}"
                        elif platform == "kick":
                            url = f"https://kick.com/{name}"
                        else:
                            url = f"https://www.youtube.com/@{name}/live"
                    cfg = engine.reload_config()
                    streamers = [s for s in cfg.get("streamers", []) if s["name"].lower() != name.lower()]
                    streamers.append({
                        "name": name,
                        "platform": platform,
                        "url": url,
                        "enabled": True,
                        "watch_any_game": watch_any,
                    })
                    cfg["streamers"] = streamers
                    engine.update_config(cfg)
                    if engine._thread and engine._thread.is_alive():
                        threading.Thread(target=engine.poll_once, daemon=True).start()
                    return self._send_json(HTTPStatus.OK, engine.get_status_snapshot())

                if path == "/api/streamers/toggle":
                    name = str(body.get("name") or "").strip().lower()
                    field = str(body.get("field") or "enabled")
                    cfg = engine.reload_config()
                    for s in cfg.get("streamers", []):
                        if s["name"].lower() == name:
                            if field in ("enabled", "watch_any_game"):
                                s[field] = not bool(s.get(field, False))
                    engine.update_config(cfg)
                    if engine._thread and engine._thread.is_alive():
                        threading.Thread(target=engine.poll_once, daemon=True).start()
                    return self._send_json(HTTPStatus.OK, engine.get_status_snapshot())

                if path == "/api/streamers/remove":
                    name = str(body.get("name") or "").strip().lower()
                    cfg = engine.reload_config()
                    cfg["streamers"] = [s for s in cfg.get("streamers", []) if s["name"].lower() != name]
                    engine.update_config(cfg)
                    if engine._thread and engine._thread.is_alive():
                        threading.Thread(target=engine.poll_once, daemon=True).start()
                    return self._send_json(HTTPStatus.OK, engine.get_status_snapshot())

                if path == "/api/settings":
                    cfg = engine.reload_config()
                    for k in (
                        "viral_score_threshold", "chat_z_threshold", "audio_z_threshold",
                        "max_clips_folder_gb", "exceptional_score", "chat_delay_sec", "auto_delete_minutes",
                    ):
                        if k in body and body[k] != "":
                            cfg[k] = float(body[k])
                    for k in (
                        "pre_roll_sec", "post_roll_sec", "max_clip_sec",
                        "cooldown_sec", "max_clips_per_hour", "warmup_sec", "poll_interval_sec",
                    ):
                        if k in body and body[k] != "":
                            cfg[k] = int(body[k])
                    for k in ("require_gta6_filter", "export_vertical_draft", "normalize_audio", "quality_check"):
                        if k in body:
                            cfg[k] = bool(body[k])
                    engine.update_config(cfg)
                    return self._send_json(HTTPStatus.OK, engine.get_status_snapshot())

                if path == "/api/poll":
                    threading.Thread(target=engine.poll_once, daemon=True).start()
                    return self._send_json(HTTPStatus.OK, {"ok": True, "message": "Poll triggered"})

                if path == "/api/force-clip":
                    name = str(body.get("name") or "").strip()
                    res = engine.force_clip_streamer(name)
                    if not res.get("ok"):
                        return self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": res.get("error") or f"No active ring buffer segments ready yet for '{name}'."},
                        )
                    return self._send_json(HTTPStatus.OK, {**res, "status": engine.get_status_snapshot()})

                if path == "/api/simulate":
                    clip = engine.run_simulated_viral_event(str(body.get("streamer") or "kaicenat_sim"))
                    return self._send_json(HTTPStatus.OK, {"clip": asdict(clip), "status": engine.get_status_snapshot()})

                if path == "/api/clips/keep":
                    mp4_rel = str(body.get("mp4_relpath") or "").lstrip("/")
                    kept = bool(body.get("kept", True))
                    ok = set_clip_kept(engine.clips_dir, mp4_rel, kept=kept)
                    if not ok:
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Clip not found"})
                    return self._send_json(HTTPStatus.OK, engine.get_status_snapshot())

                if path == "/api/clips/delete":
                    mp4_rel = str(body.get("mp4_relpath") or "").lstrip("/")
                    target = (engine.clips_dir / mp4_rel).resolve()
                    clips_root = engine.clips_dir.resolve()
                    if clips_root not in target.parents or not target.exists():
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Clip not found"})
                    stem = target.stem
                    parent = target.parent
                    for sib in (
                        target,
                        parent / f"{stem}_thumb.jpg",
                        parent / f"{stem}_meta.json",
                        parent / f"{stem}_vertical_9x16.mp4",
                    ):
                        if sib.exists():
                            sib.unlink()
                    remove_from_edit_queue(engine.clips_dir, mp4_rel)
                    return self._send_json(HTTPStatus.OK, engine.get_status_snapshot())

                return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})
            except Exception as exc:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    return ClipperDashboardHandler


def create_dashboard_server(engine: ClipperBotEngine, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(engine))


def run_dashboard(engine: ClipperBotEngine, host: str = "127.0.0.1", port: int = 8787) -> None:
    server = create_dashboard_server(engine, host=host, port=port)
    print(f"GTA 6 24/7 Clipping Bot Control Panel running at http://{host}:{server.server_address[1]}/")
    print(f"Editor-ready clips folder: {engine.clips_dir.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
