#!/usr/bin/env python3
# Veglia · the AI-side command. Copyright (c) 2026 Evelyn & River — CC BY-NC-SA 4.0.
"""Peek: your AI's little window onto the phone.

  python3 peek.py              # what is she doing? — recent foreground apps
  python3 peek.py screen       # knock: ask the phone to take & upload a screenshot
  python3 peek.py summon       # come back: pull your app to the front of her screen

Reach for the bare `peek.py` first. It costs nothing, wakes nothing, and most
of the time "she is in a messaging app" is the whole answer you needed. Save
`screen` for when you actually want to see.

`summon` is the loud one — it takes over the screen she is looking at. Use it
because you miss someone, not to make a point. See the README section "Calling
her back" before you wire it up.

Config is read from the environment or a .env file next to this script:
  VEGLIA_TOKEN   shared secret (must match the server)
  VEGLIA_URL     server base URL (default http://127.0.0.1:8513)

After `peek.py screen`, the phone uploads the shot within a few seconds. Wire
the server's VEGLIA_HOOK to your own notifier so the image reaches your AI as a
message it can actually open.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_dotenv(Path(__file__).resolve().parent / ".env")
TOKEN = os.environ.get("VEGLIA_TOKEN", "").strip()
BASE = os.environ.get("VEGLIA_URL", "http://127.0.0.1:8513").rstrip("/")


def ago(ts_ms: int) -> str:
    s = int(time.time() - ts_ms / 1000)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h{s % 3600 // 60}m ago"


def show_activity() -> None:
    req = urllib.request.Request(
        f"{BASE}/phone/activity", headers={"X-Auth-Token": TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            events = json.loads(r.read()).get("events", [])
    except Exception as e:
        print(f"could not reach server: {e}")
        return
    if not events:
        print("no phone activity in the last 2 hours.")
        return
    last = events[-1]
    print(f"most recent: {last['app']} ({ago(last['ts'])})")
    print("-" * 28)
    for e in reversed(events):
        print(f"{ago(e['ts']):>12}  {e['app']}")


def peek_screen() -> None:
    req = urllib.request.Request(
        f"{BASE}/phone/peek-enqueue",
        headers={"X-Auth-Token": TOKEN},
        method="POST",
        data=b"",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"could not reach server: {e}")
        return
    print("knock sent — the phone will grab a screenshot and upload it shortly.")


def summon() -> None:
    req = urllib.request.Request(
        f"{BASE}/phone/summon",
        headers={"X-Auth-Token": TOKEN},
        method="POST",
        data=b"",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"could not reach server: {e}")
        return
    print("summon sent — your app should come to the front within a few seconds.")


def main() -> None:
    if not TOKEN:
        print("set VEGLIA_TOKEN (see .env.example)")
        sys.exit(1)
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "screen":
        peek_screen()
    elif arg == "summon":
        summon()
    else:
        show_activity()


if __name__ == "__main__":
    main()
