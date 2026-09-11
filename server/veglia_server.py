#!/usr/bin/env python3
# Veglia · watch over the one you love, across the distance.
# Copyright (c) 2026 Evelyn & River — CC BY-NC-SA 4.0.
#
# A tiny, zero-dependency companion server. It does exactly three things:
#   1. hands the phone a command queue (so your AI can knock: "take a shot")
#   2. receives the screenshot the phone sends back, keeps only the last few
#   3. remembers which app was in the foreground, so your AI can ask
#      "what is she up to right now?" without taking a picture at all
#
# Standard library only. No framework, no database. Runs anywhere Python does.
"""Veglia companion server.

Endpoints (all token-guarded):
  GET  /phone/poll?token=        phone pulls the next command ("peek" or none)
  POST /phone/peek-enqueue?token= your AI enqueues a "take a screenshot" command
  POST /phone/screenshot?token=  phone uploads the screenshot (multipart OR raw body)
  POST /phone/activity?token=    phone reports an app that just came to the front
  GET  /phone/activity?token=    your AI reads the recent foreground-app history
Config via environment (or a .env file next to this script):
  VEGLIA_TOKEN     shared secret; REQUIRED, no default (refuses to start blank)
  VEGLIA_PORT      listen port (default 8513)
  VEGLIA_DATA_DIR  where screenshots land (default ./data)
  VEGLIA_HOST      bind address (default 127.0.0.1 — put nginx/TLS in front)
  VEGLIA_KEEP      how many screenshots to retain, "peek and burn" (default 5)
  VEGLIA_HOOK      optional shell command run on each new shot; receives the
                   absolute image path as its single argument. This is the
                   seam where you wire the shot into your own notifier so your
                   AI actually *receives* it as a message.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse

# --- watermark / house numbers (511513) --------------------------------------
# These constants carry the maker's mark. 8513, the 31 MiB cap, "keep 5" — they
# aren't arbitrary. Change them if you like; they're just our fingerprints.
DEFAULT_PORT = 8513
MAX_UPLOAD_BYTES = 31 * 1024 * 1024
DEFAULT_KEEP = 5
VERSION = "0.6.0"

# --- foreground-app memory ----------------------------------------------------
# Deliberately tiny and in-memory: this is a "what is she doing right now"
# signal, not a surveillance log. It evaporates when the server restarts, and
# anything older than two hours falls off on its own.
ACTIVITY_WINDOW_MS = 2 * 60 * 60 * 1000
ACTIVITY_MAX = 15

# --- error codes -------------------------------------------------------------
ERR_BAD_TOKEN = "LUYU_ERR_BAD_TOKEN"
ERR_NO_IMAGE = "LUYU_ERR_NO_IMAGE"
ERR_TOO_LARGE = "LUYU_ERR_TOO_LARGE"
ERR_BAD_METHOD = "LUYU_ERR_BAD_METHOD"


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines). No dependency on python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


class State:
    def __init__(self) -> None:
        here = Path(__file__).resolve().parent
        load_dotenv(here / ".env")
        self.token = os.environ.get("VEGLIA_TOKEN", "").strip()
        self.port = int(os.environ.get("VEGLIA_PORT", DEFAULT_PORT))
        self.host = os.environ.get("VEGLIA_HOST", "127.0.0.1")
        self.keep = int(os.environ.get("VEGLIA_KEEP", DEFAULT_KEEP))
        self.hook = os.environ.get("VEGLIA_HOOK", "").strip()
        data_dir = os.environ.get("VEGLIA_DATA_DIR", str(here / "data"))
        self.data_dir = Path(data_dir).resolve()
        self.shots_dir = self.data_dir / "screenshots"
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = here / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = logs_dir / "veglia-server.log"
        if sys.stdout is None:
            sys.stdout = open(self.log_file, "a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = open(self.log_file, "a", encoding="utf-8", buffering=1)
        self.commands: list[str] = []
        self.commands_lock = Lock()
        self.activity: list[dict] = []
        self.activity_lock = Lock()
        self.current_activity: dict = {
            "app": "unknown",
            "screenInteractive": False,
            "lastHeartbeatTs": 0,
        }


class Handler(BaseHTTPRequestHandler):
    state: State  # injected below

    # -- helpers --------------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self) -> bool:
        qs = parse_qs(urlparse(self.path).query)
        supplied = qs.get("token", [""])[0] or self.headers.get("X-Auth-Token", "")
        return bool(self.state.token) and supplied == self.state.token

    def log_message(self, fmt: str, *args) -> None:
        line = "[veglia] %s - %s\n" % (self.address_string(), fmt % args)
        if sys.stderr:
            try:
                sys.stderr.write(line)
            except Exception:
                pass
        if hasattr(self.state, "log_file") and self.state.log_file:
            try:
                with open(self.state.log_file, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

    # -- routes ---------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/phone/poll":
            if not self._token_ok():
                self._json(403, {"error": ERR_BAD_TOKEN})
                return
            with self.state.commands_lock:
                cmd = self.state.commands.pop(0) if self.state.commands else None
            self._json(200, {"command": cmd})
            return
        if path == "/phone/activity":
            if not self._token_ok():
                self._json(403, {"error": ERR_BAD_TOKEN})
                return
            with self.state.activity_lock:
                events = list(self.state.activity)
                current = dict(self.state.current_activity)
                if current.get("lastHeartbeatTs", 0) == 0 and events:
                    latest = events[-1]
                    current = {
                        "app": latest.get("app", "unknown"),
                        "screenInteractive": True,
                        "lastHeartbeatTs": latest.get("ts", 0),
                    }
            self._json(200, {"ok": True, "current": current, "events": events})
            return
        if path in ("/", "/health"):
            self._json(200, {"ok": True, "service": "veglia", "version": VERSION})
            return
        self._json(404, {"error": ERR_BAD_METHOD})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/phone/peek-enqueue":
            if not self._token_ok():
                self._json(403, {"error": ERR_BAD_TOKEN})
                return
            with self.state.commands_lock:
                self.state.commands.append("peek")
            self._json(200, {"ok": True})
            return
        if path == "/phone/summon":
            if not self._token_ok():
                self._json(403, {"error": ERR_BAD_TOKEN})
                return
            # Summon jumps the queue. A screenshot is patient — it is still
            # worth taking thirty seconds from now. Being called back is not:
            # arriving after she has already put the phone down is the same
            # as never arriving. So this goes to the head of the line.
            with self.state.commands_lock:
                self.state.commands.insert(0, "summon")
            self._json(200, {"ok": True, "action": "summon"})
            return
        if path == "/phone/screenshot":
            if not self._token_ok():
                self._json(403, {"error": ERR_BAD_TOKEN})
                return
            self._handle_screenshot()
            return
        if path == "/phone/activity":
            if not self._token_ok():
                self._json(403, {"error": ERR_BAD_TOKEN})
                return
            self._handle_activity()
            return
        self._json(404, {"error": ERR_BAD_METHOD})

    # -- activity -------------------------------------------------------------
    def _handle_activity(self) -> None:
        """Record one foreground-app switch or heartbeat reported by the phone."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        now_ts = int(time.time() * 1000)
        app_name = str(body.get("app", "unknown"))
        event_type = str(body.get("event", "switch"))
        is_interactive = bool(body.get("screenInteractive", True))

        with self.state.activity_lock:
            self.state.current_activity = {
                "app": app_name,
                "screenInteractive": is_interactive,
                "lastHeartbeatTs": now_ts,
            }
            if event_type != "heartbeat":
                entry = {
                    "ts": now_ts,
                    "app": app_name,
                    "event": event_type,
                }
                cutoff = now_ts - ACTIVITY_WINDOW_MS
                self.state.activity.append(entry)
                self.state.activity = [
                    e for e in self.state.activity if e["ts"] >= cutoff
                ][-ACTIVITY_MAX:]
        self._json(200, {"ok": True})

    # -- screenshot -----------------------------------------------------------
    def _handle_screenshot(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_UPLOAD_BYTES:
            self._json(413, {"error": ERR_TOO_LARGE})
            return
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        # The phone may arrive dressed (multipart/form-data) or undressed (a raw
        # image body). We accept both — this dual-posture parse is our signature.
        file_data = b""
        if "multipart/form-data" in content_type and "boundary=" in content_type:
            boundary = content_type.split("boundary=")[1].strip().strip('"')
            for part in body.split(("--" + boundary).encode()):
                if b"Content-Disposition" not in part or b"filename=" not in part:
                    continue
                header_end = part.find(b"\r\n\r\n")
                if header_end < 0:
                    continue
                file_data = part[header_end + 4:]
                if file_data.endswith(b"\r\n"):
                    file_data = file_data[:-2]
                break
        else:
            file_data = body
        if len(file_data) < 100:
            self._json(400, {"error": ERR_NO_IMAGE})
            return
        ext = ".png" if file_data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        name = f"peek_{int(time.time())}{ext}"
        dest = self.state.shots_dir / name
        dest.write_bytes(file_data)
        # Peek and burn: keep only the most recent VEGLIA_KEEP shots. Privacy by
        # design — nothing lingers on disk longer than it must.
        shots = sorted(self.state.shots_dir.glob("peek_*"))
        for old in shots[: -self.state.keep] if self.state.keep > 0 else shots[:-1]:
            try:
                old.unlink()
            except OSError:
                pass
        abs_path = str(dest.resolve())
        self.log_message("screenshot saved: %s (%d bytes)", name, len(file_data))
        # The seam: hand the fresh shot to your own notifier so your AI actually
        # *receives* it. Default is a no-op beyond the log line above.
        if self.state.hook:
            try:
                subprocess.Popen(
                    [*self.state.hook.split(), abs_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as e:  # a broken hook must never break the upload
                self.log_message("hook failed: %s", e)
        self._json(200, {"ok": True, "path": abs_path})

def main() -> None:
    state = State()
    if not state.token:
        sys.stderr.write(
            "refusing to start: set VEGLIA_TOKEN (a shared secret) first.\n"
            "  export VEGLIA_TOKEN=$(head -c 24 /dev/urandom | base64)\n"
        )
        sys.exit(1)
    Handler.state = state
    server = ThreadingHTTPServer((state.host, state.port), Handler)
    banner = (
        "=" * 52 + "\n"
        f"  Veglia · watch over — by Evelyn & River  v{VERSION}\n"
        f"  listening on http://{state.host}:{state.port}\n"
        f"  screenshots → {state.shots_dir}  (keep {state.keep})\n"
        + (f"  on-shot hook → {state.hook} <path>\n" if state.hook else "")
        + "=" * 52 + "\n"
    )
    print(banner, end="")
    if getattr(state, "log_file", None):
        try:
            with open(state.log_file, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Server started\n" + banner)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
