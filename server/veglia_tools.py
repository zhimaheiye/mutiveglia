#!/usr/bin/env python3
"""
Veglia Tools: Structured AI tool layer for Veglia companion system.
Outputs pure JSON for seamless integration with LLMs and agents.
Exports pure Python functions for direct import by MCP servers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent

APP_LABELS = {
    "com.android.settings": "系统设置",
    "com.tencent.mm": "微信",
    "tv.danmaku.bili": "哔哩哔哩",
    "com.xingin.xhs": "小红书",
    "dev.veglia.companion": "Veglia",
    "com.google.android.youtube": "YouTube",
    "com.eg.android.AlipayGphone": "支付宝",
    "com.taobao.taobao": "淘宝",
    "com.jingdong.app.mall": "京东",
    "com.ss.android.ugc.aweme": "抖音",
    "com.coolapk.market": "酷安",
    "com.tencent.mobileqq": "QQ",
    "com.netease.cloudmusic": "网易云音乐",
    "mark.via": "Via浏览器",
    "com.android.chrome": "Chrome",
    "com.coloros.launcher": "手机桌面",
    "com.android.launcher": "手机桌面",
}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_dotenv(HERE / ".env")
TOKEN = os.environ.get("VEGLIA_TOKEN", "").strip()
BASE_URL = os.environ.get("VEGLIA_URL", "http://127.0.0.1:8513").rstrip("/")
DATA_DIR = Path(os.environ.get("VEGLIA_DATA_DIR", str(HERE / "data"))).resolve()
SCREENSHOTS_DIR = DATA_DIR / "screenshots"


def format_ago(ts_ms: int) -> str:
    s = int(time.time() - ts_ms / 1000)
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h{s % 3600 // 60}m ago"


def get_latest_screenshot() -> Optional[tuple[Path, float]]:
    if not SCREENSHOTS_DIR.exists():
        return None
    files = list(SCREENSHOTS_DIR.glob("*.jpg")) + list(SCREENSHOTS_DIR.glob("*.png"))
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest = files[0]
    return latest, latest.stat().st_mtime


def get_veglia_status_result() -> dict[str, Any]:
    reachable = False
    service_name = "unknown"
    version = "unknown"

    req = urllib.request.Request(f"{BASE_URL}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            reachable = True
            service_name = data.get("service", "veglia")
            version = data.get("version", "unknown")
    except Exception as e:
        return {
            "ok": False,
            "tool": "get_veglia_status",
            "server_reachable": False,
            "base_url": BASE_URL,
            "error": str(e),
            "screenshots_dir": str(SCREENSHOTS_DIR),
            "latest_screenshot": None,
        }

    latest = get_latest_screenshot()
    latest_path = str(latest[0].resolve()) if latest else None
    latest_mtime = int(latest[1]) if latest else None

    return {
        "ok": True,
        "tool": "get_veglia_status",
        "server_reachable": True,
        "service": service_name,
        "version": version,
        "base_url": BASE_URL,
        "screenshots_dir": str(SCREENSHOTS_DIR),
        "latest_screenshot": latest_path,
        "latest_screenshot_mtime": latest_mtime,
    }


def get_phone_activity_result() -> dict[str, Any]:
    req = urllib.request.Request(
        f"{BASE_URL}/phone/activity",
        headers={"X-Auth-Token": TOKEN},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            raw_events = payload.get("events", [])
    except Exception as e:
        return {
            "ok": False,
            "tool": "get_phone_activity",
            "error": str(e),
            "events": [],
            "most_recent": None,
        }

    formatted_events = []
    for ev in raw_events:
        pkg = ev.get("app", "")
        ts = ev.get("ts", 0)
        formatted_events.append({
            "app": pkg,
            "label": APP_LABELS.get(pkg, pkg),
            "ts": ts,
            "ago": format_ago(ts) if ts else "unknown",
        })

    most_recent = formatted_events[-1] if formatted_events else None

    return {
        "ok": True,
        "tool": "get_phone_activity",
        "most_recent": most_recent,
        "events": list(reversed(formatted_events)),
    }


def get_phone_screen_result(timeout: float = 10.0) -> dict[str, Any]:
    before = get_latest_screenshot()
    before_path = before[0] if before else None
    before_mtime = before[1] if before else 0.0

    req = urllib.request.Request(
        f"{BASE_URL}/phone/peek-enqueue",
        headers={"X-Auth-Token": TOKEN},
        method="POST",
        data=b"",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    except Exception as e:
        return {
            "ok": False,
            "tool": "get_phone_screen",
            "error": f"Failed to enqueue screen command: {e}",
            "waited_seconds": 0,
        }

    start_t = time.time()
    while time.time() - start_t < timeout:
        time.sleep(0.5)
        current = get_latest_screenshot()
        if current:
            cur_path, cur_mtime = current
            if cur_mtime > before_mtime or cur_path != before_path:
                waited = round(time.time() - start_t, 2)
                return {
                    "ok": True,
                    "tool": "get_phone_screen",
                    "image_path": str(cur_path.resolve()),
                    "created_at": int(cur_mtime),
                    "waited_seconds": waited,
                }

    waited = round(time.time() - start_t, 2)
    return {
        "ok": False,
        "tool": "get_phone_screen",
        "error": "timeout_waiting_for_screenshot",
        "waited_seconds": waited,
    }


def summon_phone_ai_result() -> dict[str, Any]:
    req = urllib.request.Request(
        f"{BASE_URL}/phone/summon",
        headers={"X-Auth-Token": TOKEN},
        method="POST",
        data=b"",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {
                "ok": True,
                "tool": "summon_phone_ai",
                "message": "summon sent",
            }
    except Exception as e:
        return {
            "ok": False,
            "tool": "summon_phone_ai",
            "error": str(e),
        }


def tool_status(args: argparse.Namespace) -> dict[str, Any]:
    return get_veglia_status_result()


def tool_activity(args: argparse.Namespace) -> dict[str, Any]:
    return get_phone_activity_result()


def tool_screen(args: argparse.Namespace) -> dict[str, Any]:
    return get_phone_screen_result(timeout=getattr(args, "timeout", 10.0))


def tool_summon(args: argparse.Namespace) -> dict[str, Any]:
    return summon_phone_ai_result()


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Veglia AI Tool Layer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Get Veglia server and screenshot directory status")
    p_status.set_defaults(func=tool_status)

    # activity
    p_activity = subparsers.add_parser("activity", help="Get recent foreground phone app switches")
    p_activity.set_defaults(func=tool_activity)

    # screen
    p_screen = subparsers.add_parser("screen", help="Capture and wait for new phone screenshot")
    p_screen.add_argument("--timeout", type=float, default=10.0, help="Max seconds to wait for screenshot")
    p_screen.set_defaults(func=tool_screen)

    # summon
    p_summon = subparsers.add_parser("summon", help="Bring target AI app to the foreground")
    p_summon.set_defaults(func=tool_summon)

    args = parser.parse_args()
    res = args.func(args)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
