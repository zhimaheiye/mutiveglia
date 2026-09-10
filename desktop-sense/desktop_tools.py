#!/usr/bin/env python3
"""
Desktop Tools: Formal business and state perception layer for Windows 11.
Provides robust foreground active window detection, privacy-aware redaction,
system idle duration measurement, on-demand screenshot capabilities, and
pluggable device identity metadata for multi-device presence hub integration.
Outputs pure JSON in UTF-8. Safe for direct import by MCP servers and Agents.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageGrab
import win32gui
import win32process

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9

SCREENSHOT_DIR = Path(__file__).resolve().parent / "data" / "screenshots"

# Pluggable Device Identity (configurable via environment, defaults to main-pc)
DEVICE_ID = os.environ.get("DESKTOP_DEVICE_ID", "main-pc")
DEVICE_NAME = os.environ.get("DESKTOP_DEVICE_NAME", "主力电脑")
DEVICE_TYPE = os.environ.get("DESKTOP_DEVICE_TYPE", "windows")

APP_LABELS = {
    "obsidian.exe": "Obsidian",
    "code.exe": "VS Code",
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "explorer.exe": "Windows 资源管理器",
    "windowsterminal.exe": "Windows Terminal",
    "cmd.exe": "命令提示符",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell 7",
    "antigravity.exe": "Antigravity",
    "cursor.exe": "Cursor",
    "wechat.exe": "微信",
    "qq.exe": "QQ",
    "clash-verge.exe": "Clash Verge",
    "taskmgr.exe": "任务管理器",
    "chatgpt.exe": "ChatGPT",
}


def get_device_identity() -> dict[str, str]:
    """Return standard device identity structure."""
    return {
        "id": DEVICE_ID,
        "name": DEVICE_NAME,
        "type": DEVICE_TYPE,
    }


def _attach_interactive_desktop(errors: list[dict[str, str]]) -> None:
    """Ensure the calling thread attaches to the interactive Default desktop."""
    try:
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
        else:
            errors.append({
                "component": "interactive_desktop",
                "code": "open_desktop_failed",
                "message": "OpenDesktopW('Default') returned NULL"
            })
    except Exception as e:
        errors.append({
            "component": "interactive_desktop",
            "code": "attach_exception",
            "message": str(e)
        })


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()


def _get_process_image_path(pid: int) -> Optional[str]:
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h_proc)


def _resolve_friendly_label(process_name: Optional[str]) -> str:
    if not process_name:
        return "Unknown"
    lower = process_name.lower()
    if lower in APP_LABELS:
        return APP_LABELS[lower]
    if lower.endswith(".exe"):
        return process_name[:-4]
    return process_name


def _query_active_window_raw(errors: list[dict[str, str]]) -> dict[str, Any]:
    _attach_interactive_desktop(errors)

    hwnd = 0
    title = ""
    pid = 0
    process_name: Optional[str] = None
    executable_path: Optional[str] = None

    try:
        hwnd = win32gui.GetForegroundWindow()
    except Exception as e:
        errors.append({
            "component": "foreground_window",
            "code": "get_foreground_failed",
            "message": str(e)
        })

    if hwnd:
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception as e:
            errors.append({
                "component": "window_title",
                "code": "get_title_failed",
                "message": str(e)
            })

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception as e:
            errors.append({
                "component": "process_id",
                "code": "get_pid_failed",
                "message": str(e)
            })

    if pid > 0:
        try:
            executable_path = _get_process_image_path(pid)
            if executable_path:
                process_name = Path(executable_path).name
            else:
                errors.append({
                    "component": "process_path",
                    "code": "access_denied_or_exited",
                    "message": "QueryFullProcessImageNameW returned NULL"
                })
        except Exception as e:
            errors.append({
                "component": "process_path",
                "code": "query_path_exception",
                "message": str(e)
            })

    friendly_label = _resolve_friendly_label(process_name)

    return {
        "hwnd": hwnd,
        "title": title,
        "pid": pid,
        "process_name": process_name,
        "executable_path": executable_path,
        "friendly_label": friendly_label,
    }


def _query_idle_raw(errors: list[dict[str, str]]) -> dict[str, Any]:
    _attach_interactive_desktop(errors)

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)

    idle_seconds = 0.0
    activity_state = "active"

    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        errors.append({
            "component": "idle_time",
            "code": "get_last_input_failed",
            "message": "GetLastInputInfo call failed"
        })
        return {
            "idle_seconds": None,
            "activity_state": "unknown",
            "classification": {
                "active_under_seconds": 15,
                "away_from_seconds": 120,
            },
        }

    try:
        tick = kernel32.GetTickCount64()
        diff = (tick & 0xFFFFFFFF) - lii.dwTime
        if diff < 0:
            diff += 0x100000000
        idle_seconds = round(diff / 1000.0, 2)

        if idle_seconds < 15.0:
            activity_state = "active"
        elif idle_seconds < 120.0:
            activity_state = "thinking"
        else:
            activity_state = "away"
    except Exception as e:
        errors.append({
            "component": "idle_time",
            "code": "calculation_exception",
            "message": str(e)
        })

    return {
        "idle_seconds": idle_seconds,
        "activity_state": activity_state,
        "classification": {
            "active_under_seconds": 15,
            "away_from_seconds": 120,
        },
    }


def get_active_window_result(detail: bool = False) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    raw = _query_active_window_raw(errors)

    if not detail:
        active_window = {
            "hwnd": raw["hwnd"],
            "pid": raw["pid"],
            "process_name": raw["process_name"],
            "friendly_label": raw["friendly_label"],
            "title": None,
            "executable_path": None,
        }
        privacy = {
            "mode": "safe",
            "redacted_fields": [
                "active_window.title",
                "active_window.executable_path",
            ],
        }
    else:
        active_window = {
            "hwnd": raw["hwnd"],
            "pid": raw["pid"],
            "process_name": raw["process_name"],
            "friendly_label": raw["friendly_label"],
            "title": raw["title"],
            "executable_path": raw["executable_path"],
        }
        privacy = {
            "mode": "detail",
            "redacted_fields": [],
        }

    return {
        "ok": True,
        "device": get_device_identity(),
        "captured_at": _now_iso(),
        "active_window": active_window,
        "privacy": privacy,
        "errors": errors,
    }


def get_idle_status_result() -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    idle_data = _query_idle_raw(errors)

    return {
        "ok": True,
        "device": get_device_identity(),
        "captured_at": _now_iso(),
        "idle": idle_data,
        "errors": errors,
    }


def get_desktop_status_result(detail: bool = False) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    now_str = _now_iso()

    raw_win = _query_active_window_raw(errors)
    idle_data = _query_idle_raw(errors)

    if not detail:
        active_window = {
            "hwnd": raw_win["hwnd"],
            "pid": raw_win["pid"],
            "process_name": raw_win["process_name"],
            "friendly_label": raw_win["friendly_label"],
            "title": None,
            "executable_path": None,
        }
        privacy = {
            "mode": "safe",
            "redacted_fields": [
                "active_window.title",
                "active_window.executable_path",
            ],
        }
    else:
        active_window = {
            "hwnd": raw_win["hwnd"],
            "pid": raw_win["pid"],
            "process_name": raw_win["process_name"],
            "friendly_label": raw_win["friendly_label"],
            "title": raw_win["title"],
            "executable_path": raw_win["executable_path"],
        }
        privacy = {
            "mode": "detail",
            "redacted_fields": [],
        }

    return {
        "ok": True,
        "device": get_device_identity(),
        "captured_at": now_str,
        "active_window": active_window,
        "idle": idle_data,
        "session": {
            "session_locked": None,
            "display_state": "unknown",
        },
        "privacy": privacy,
        "errors": errors,
    }


def get_desktop_screen_result(
    target: str = "active_window",
    image_format: str = "jpeg",
    quality: int = 90,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    _attach_interactive_desktop(errors)

    target_clean = target.lower().strip()
    if target_clean not in ("active_window", "primary", "virtual"):
        return {
            "ok": False,
            "device": get_device_identity(),
            "tool": "get_desktop_screen",
            "target": target,
            "error": {
                "component": "screen_capture",
                "code": "invalid_target",
                "message": f"Target '{target}' must be one of: active_window, primary, virtual"
            },
            "errors": errors,
        }

    fmt_clean = image_format.lower().strip()
    if fmt_clean not in ("jpeg", "jpg", "png"):
        return {
            "ok": False,
            "device": get_device_identity(),
            "tool": "get_desktop_screen",
            "target": target,
            "error": {
                "component": "screen_capture",
                "code": "invalid_format",
                "message": f"Format '{image_format}' must be jpeg or png"
            },
            "errors": errors,
        }

    source_info: Optional[dict[str, Any]] = None

    try:
        if target_clean == "active_window":
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return {
                    "ok": False,
                    "device": get_device_identity(),
                    "tool": "get_desktop_screen",
                    "target": target,
                    "error": {
                        "component": "screen_capture",
                        "code": "no_foreground_window",
                        "message": "No active foreground window detected"
                    },
                    "errors": errors,
                }

            rect = RECT()
            res = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
            if res != 0:
                rect_tuple = win32gui.GetWindowRect(hwnd)
                rect.left, rect.top, rect.right, rect.bottom = rect_tuple

            if rect.right <= rect.left or rect.bottom <= rect.top:
                return {
                    "ok": False,
                    "device": get_device_identity(),
                    "tool": "get_desktop_screen",
                    "target": target,
                    "error": {
                        "component": "screen_capture",
                        "code": "invalid_window_bounds",
                        "message": f"Window rectangle invalid: ({rect.left}, {rect.top}, {rect.right}, {rect.bottom})"
                    },
                    "errors": errors,
                }

            bbox = (rect.left, rect.top, rect.right, rect.bottom)
            img = ImageGrab.grab(bbox=bbox, all_screens=True)

            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_path = _get_process_image_path(pid) if pid > 0 else None
            proc_name = Path(proc_path).name if proc_path else None
            friendly_lbl = _resolve_friendly_label(proc_name)
            source_info = {
                "hwnd": hwnd,
                "pid": pid,
                "process_name": proc_name,
                "friendly_label": friendly_lbl,
            }

        elif target_clean == "primary":
            w = user32.GetSystemMetrics(0)
            h = user32.GetSystemMetrics(1)
            img = ImageGrab.grab(bbox=(0, 0, w, h))

        elif target_clean == "virtual":
            vx = user32.GetSystemMetrics(76)
            vy = user32.GetSystemMetrics(77)
            vw = user32.GetSystemMetrics(78)
            vh = user32.GetSystemMetrics(79)
            img = ImageGrab.grab(bbox=(vx, vy, vx + vw, vy + vh), all_screens=True)

    except Exception as e:
        return {
            "ok": False,
            "device": get_device_identity(),
            "tool": "get_desktop_screen",
            "target": target,
            "error": {
                "component": "screen_capture",
                "code": "grab_exception",
                "message": str(e)
            },
            "errors": errors,
        }

    # Save to disk
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        now_dt = datetime.datetime.now(datetime.timezone.utc).astimezone()
        ts_str = now_dt.strftime("%Y%m%d_%H%M%S_%f")[:19]
        ext = "jpg" if fmt_clean in ("jpeg", "jpg") else "png"
        filename = f"desktop_{ts_str}.{ext}"
        filepath = SCREENSHOT_DIR / filename

        if ext == "jpg":
            img.convert("RGB").save(filepath, format="JPEG", quality=quality, optimize=True)
            mime_type = "image/jpeg"
        else:
            img.save(filepath, format="PNG", optimize=True)
            mime_type = "image/png"

        return {
            "ok": True,
            "device": get_device_identity(),
            "tool": "get_desktop_screen",
            "captured_at": now_dt.isoformat(),
            "target": target_clean,
            "image_path": str(filepath.resolve()),
            "mime_type": mime_type,
            "width": img.width,
            "height": img.height,
            "file_size_bytes": filepath.stat().st_size,
            "source": source_info,
            "privacy": {
                "window_title_included": False,
                "executable_path_included": False,
            },
            "errors": errors,
        }
    except Exception as e:
        return {
            "ok": False,
            "device": get_device_identity(),
            "tool": "get_desktop_screen",
            "target": target,
            "error": {
                "component": "screen_save",
                "code": "save_exception",
                "message": str(e)
            },
            "errors": errors,
        }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Desktop Tools - Windows 11 State & Activity Layer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # active
    p_active = subparsers.add_parser("active", help="Get foreground active window info")
    p_active.add_argument("--detail", action="store_true", help="Include full window title and executable path")

    # idle
    p_idle = subparsers.add_parser("idle", help="Get system keyboard/mouse idle time and activity state")

    # status
    p_status = subparsers.add_parser("status", help="Get aggregated desktop active window and idle status")
    p_status.add_argument("--detail", action="store_true", help="Include full window title and executable path")

    # screen
    p_screen = subparsers.add_parser("screen", help="Capture desktop screenshot")
    p_screen.add_argument("--target", default="active_window", choices=["active_window", "primary", "virtual"], help="Capture target")
    p_screen.add_argument("--format", default="jpeg", choices=["jpeg", "jpg", "png"], help="Image format")
    p_screen.add_argument("--quality", type=int, default=90, help="JPEG quality (1-100)")

    args = parser.parse_args()

    if args.command == "active":
        res = get_active_window_result(detail=args.detail)
    elif args.command == "idle":
        res = get_idle_status_result()
    elif args.command == "status":
        res = get_desktop_status_result(detail=args.detail)
    elif args.command == "screen":
        res = get_desktop_screen_result(target=args.target, image_format=args.format, quality=args.quality)
    else:
        res = {"ok": False, "error": f"Unknown command: {args.command}"}

    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
