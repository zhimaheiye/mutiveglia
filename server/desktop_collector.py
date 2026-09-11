#!/usr/bin/env python3
# Veglia · desktop_collector.py
# Copyright (c) 2026 Evelyn & River — CC BY-NC-SA 4.0.
#
# Windows desktop state collector.
# Gathers foreground window, process name, window title, and user idle time.
#
# Design contract:
#   - Zero import side effects. No threads start on import.
#   - Call collector.start() explicitly (from veglia_mcp.py __main__).
#   - Call collector.stop() on shutdown (via atexit or explicit call).
#   - All public methods are thread-safe.
#
# Dependencies (already in .venv-mcp):
#   pywin32  — win32gui, win32process, win32api  (window/process info)
#   ctypes   — stdlib, for GetLastInputInfo       (idle time)
"""Veglia Windows desktop state collector."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import sys
import threading
import time
from typing import Any

# Guard: this module is Windows-only.
if sys.platform != "win32":
    raise ImportError("desktop_collector is Windows-only")

import win32api
import win32gui
import win32process

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HISTORY_SIZE = 15          # max recent window-switch entries to keep
_POLL_INTERVAL = 1.0        # seconds between foreground-window checks


# ---------------------------------------------------------------------------
# Low-level Windows API helpers
# ---------------------------------------------------------------------------

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("dwTime", ctypes.wintypes.DWORD),
    ]


def _get_idle_ms() -> int:
    """Return milliseconds since last keyboard/mouse input (GetLastInputInfo)."""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    tick_now = ctypes.windll.kernel32.GetTickCount()
    elapsed = tick_now - info.dwTime
    # GetTickCount wraps every ~49.7 days; handle the rare rollover gracefully.
    if elapsed < 0:
        elapsed = 0
    return int(elapsed)


def _query_process_name(pid: int) -> str:
    """
    Get the executable name for a given PID.

    Strategy:
      1. QueryFullProcessImageNameW via ctypes (needs PROCESS_QUERY_LIMITED_INFORMATION=0x1000)
      2. GetModuleFileNameEx via win32process (needs PROCESS_QUERY_INFORMATION|VM_READ=0x0410)
      3. Fallback: "pid:<pid>"
    """
    # Strategy 1: QueryFullProcessImageNameW (lowest privilege requirement)
    try:
        _kernel32 = ctypes.windll.kernel32
        h = _kernel32.OpenProcess(0x1000, False, pid)
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(1024)
            ok = _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            _kernel32.CloseHandle(h)
            if ok and buf.value:
                return os.path.basename(buf.value)
    except Exception:
        pass

    # Strategy 2: win32process.GetModuleFileNameEx (may fail on protected processes)
    try:
        handle = win32api.OpenProcess(0x0410, False, pid)
        exe_path: str = win32process.GetModuleFileNameEx(handle, 0)
        win32api.CloseHandle(handle)
        return os.path.basename(exe_path)
    except Exception:
        pass

    return f"pid:{pid}"


def _is_thread_on_default_desktop() -> bool:
    """Return True if the current thread is already attached to the 'Default' desktop."""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        h_desk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        if not h_desk:
            return False
        buf = ctypes.create_unicode_buffer(256)
        needed = ctypes.wintypes.DWORD()
        # UOI_NAME = 2
        if user32.GetUserObjectInformationW(h_desk, 2, buf, 256, ctypes.byref(needed)):
            return buf.value.lower() == "default"
    except Exception:
        pass
    return False


def _ensure_default_desktop() -> None:
    """Ensure the calling thread is attached to the user interactive desktop (WinSta0\\Default)."""
    if _is_thread_on_default_desktop():
        return
    try:
        user32 = ctypes.windll.user32
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            ok = user32.SetThreadDesktop(h_desk)
            if not ok:
                user32.CloseDesktop(h_desk)
    except Exception:
        pass


def _get_foreground_info() -> dict[str, Any] | None:
    """
    Return info about the current foreground window, or None on failure.

    Uses pywin32 (win32gui / win32process) for window/thread queries,
    and ctypes QueryFullProcessImageNameW for process name resolution.
    Returns a dict with keys: app, title, pid
    """
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            _ensure_default_desktop()
            hwnd = win32gui.GetForegroundWindow()

        if not hwnd:
            return None

        title = win32gui.GetWindowText(hwnd) or ""

        # Retrieve PID from the window's thread.
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return None

        app = _query_process_name(pid)
        return {"app": app, "title": title, "pid": pid}

    except Exception:
        return None


# ---------------------------------------------------------------------------
# DesktopCollector
# ---------------------------------------------------------------------------

def _format_ago(ts_ms: int) -> str:
    s = max(0, int(time.time() - ts_ms / 1000))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h{s % 3600 // 60}m ago"


class DesktopCollector:
    """
    Collects Windows desktop activity state.

    Lifecycle (must be managed by the caller — no auto-start on import):
        collector.start()   — begin background polling
        collector.stop()    — stop background polling
        collector.snapshot()— thread-safe point-in-time snapshot
    """

    def __init__(
        self,
        history_size: int = _HISTORY_SIZE,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._history_size = history_size
        self._poll_interval = poll_interval

        self._lock = threading.Lock()
        self._history: list[dict[str, Any]] = []   # chronological, oldest first
        self._current: dict[str, Any] | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background polling thread. Safe to call multiple times."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="veglia-desktop-collector",
            daemon=True,      # won't prevent interpreter exit
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Public query
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """
        Return a thread-safe point-in-time snapshot of desktop state.

        Structure is aligned with phone activity (app/label/ts/ago fields),
        with desktop-specific extensions (title, idle_seconds).
        """
        now_ms = int(time.time() * 1000)
        idle_ms = _get_idle_ms()
        idle_seconds = idle_ms // 1000

        with self._lock:
            current = dict(self._current) if self._current else None
            history = list(self._history)

        # Decorate current with fresh ts/ago if available.
        foreground: dict[str, Any] | None = None
        if current:
            ts = current.get("ts", now_ms)
            foreground = {
                "app": current["app"],
                "label": current["app"],          # desktop has no label table yet
                "title": current.get("title", ""),
                "pid": current.get("pid"),
                "ts": ts,
                "ago": _format_ago(ts),
            }

        # Build recent_activity in reverse-chron order (newest first),
        # matching the phone activity events ordering.
        recent: list[dict[str, Any]] = []
        for ev in reversed(history):
            ts = ev.get("ts", 0)
            recent.append({
                "app": ev["app"],
                "label": ev["app"],
                "title": ev.get("title", ""),
                "ts": ts,
                "ago": _format_ago(ts),
            })

        return {
            "ok": True,
            "tool": "get_desktop_activity",
            "device": "desktop",
            "device_type": "desktop",
            "online": True,
            "foreground": foreground,
            "idle_seconds": idle_seconds,
            "recent_activity": recent,
        }

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        _ensure_default_desktop()
        last_app: str | None = None

        while not self._stop_event.is_set():
            info = _get_foreground_info()

            if info:
                app = info["app"]
                if app != last_app:
                    # Foreground app changed — record the switch.
                    entry = {
                        "app": app,
                        "title": info.get("title", ""),
                        "pid": info.get("pid"),
                        "ts": int(time.time() * 1000),
                    }
                    with self._lock:
                        self._current = entry
                        self._history.append(entry)
                        if len(self._history) > self._history_size:
                            self._history = self._history[-self._history_size:]
                    last_app = app

            self._stop_event.wait(self._poll_interval)


# ---------------------------------------------------------------------------
# Module-level singleton — NOT auto-started. Caller must call .start().
# ---------------------------------------------------------------------------

collector = DesktopCollector()


# ---------------------------------------------------------------------------
# Smoke test (python desktop_collector.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    print("Starting collector for 3 seconds...")
    collector.start()
    time.sleep(3)

    snap = collector.snapshot()
    print(json.dumps(snap, ensure_ascii=False, indent=2))

    collector.stop()
    print("Done.")
