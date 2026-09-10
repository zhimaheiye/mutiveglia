"""
Quick validation: run this directly in a terminal (not via AGY tool runner)
to verify that desktop_collector can see real foreground windows.

Usage:
  D:\\mutiveglia\\.venv-mcp\\Scripts\\python.exe D:\\mutiveglia\\server\\test_desktop_real.py

Note: this script is Windows-only and must be run in an interactive desktop
session (not via SSH or a headless CI runner).
"""
import sys
import json
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from desktop_collector import collector, _get_foreground_info, _get_idle_ms

print("=== Veglia Desktop Collector — Real Environment Test ===")
print()

# 1. Single-shot foreground check
info = _get_foreground_info()
idle_ms = _get_idle_ms()
print("[1] GetForegroundWindow snapshot:")
print(f"    foreground : {info}")
print(f"    idle_ms    : {idle_ms}  ({idle_ms // 1000}s)")
print()

# 2. Start collector, wait 5s, show history
print("[2] Starting collector for 5 seconds. Switch windows now...")
collector.start()
time.sleep(5)
snap = collector.snapshot()
collector.stop()
print(json.dumps(snap, ensure_ascii=False, indent=2))
