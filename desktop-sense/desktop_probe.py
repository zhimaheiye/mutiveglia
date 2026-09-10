#!/usr/bin/env python3
"""
Desktop Probe (Step 1): Raw Windows 11 system state and activity probe.
Queries foreground active window, PID, executable path, and system idle duration.
Outputs structured JSON exclusively in UTF-8.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import win32gui
import win32process

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

FRIENDLY_LABELS = {
    "Obsidian.exe": "Obsidian",
    "Code.exe": "VS Code",
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "explorer.exe": "Windows 资源管理器",
    "WindowsTerminal.exe": "Windows Terminal",
    "cmd.exe": "命令提示符",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell 7",
    "Antigravity.exe": "Antigravity",
    "cursor.exe": "Cursor",
    "WeChat.exe": "微信",
    "QQ.exe": "QQ",
    "clash-verge.exe": "Clash Verge",
    "Taskmgr.exe": "任务管理器",
}


def attach_interactive_desktop() -> None:
    """Ensure the thread is attached to the interactive Default desktop."""
    try:
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
    except Exception:
        pass


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def get_process_image_path(pid: int) -> Optional[str]:
    """Retrieve full executable path for a given PID using Win32 API."""
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


def get_active_window_probe(errors: list[dict[str, str]]) -> dict[str, Any]:
    attach_interactive_desktop()

    hwnd = 0
    title = ""
    pid = 0
    process_name: Optional[str] = None
    executable_path: Optional[str] = None
    friendly_label = "Unknown"

    try:
        hwnd = win32gui.GetForegroundWindow()
    except Exception as e:
        errors.append({"component": "GetForegroundWindow", "error": str(e)})

    if hwnd:
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception as e:
            errors.append({"component": "GetWindowText", "error": str(e)})

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception as e:
            errors.append({"component": "GetWindowThreadProcessId", "error": str(e)})

    if pid > 0:
        try:
            executable_path = get_process_image_path(pid)
            if executable_path:
                process_name = Path(executable_path).name
                friendly_label = FRIENDLY_LABELS.get(process_name, process_name)
            else:
                errors.append({"component": "QueryFullProcessImageNameW", "error": "access_denied_or_exited"})
        except Exception as e:
            errors.append({"component": "process_path", "error": str(e)})

    return {
        "hwnd": hwnd,
        "title": title,
        "pid": pid,
        "process_name": process_name,
        "executable_path": executable_path,
        "friendly_label": friendly_label,
    }


def get_idle_probe(errors: list[dict[str, str]]) -> dict[str, Any]:
    attach_interactive_desktop()

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)

    idle_seconds = 0.0
    activity_state = "active"

    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        errors.append({"component": "GetLastInputInfo", "error": "call_failed"})
        return {
            "idle_seconds": None,
            "activity_state": "unknown",
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
        errors.append({"component": "idle_calculation", "error": str(e)})

    return {
        "idle_seconds": idle_seconds,
        "activity_state": activity_state,
    }


def probe() -> dict[str, Any]:
    errors: list[dict[str, str]] = []

    active_win = get_active_window_probe(errors)
    idle_info = get_idle_probe(errors)

    return {
        "ok": True,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(),
        "active_window": active_win,
        "idle": idle_info,
        "session": {
            "session_locked": None,
            "display_state": "unknown",
        },
        "errors": errors,
    }


def main() -> None:
    data = probe()
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
