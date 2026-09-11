#!/usr/bin/env python3
"""
Veglia Windows Device Sensor.
Collects local desktop activity via DesktopCollector and reports
periodically to the central Veglia Core Server (:8513).
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

LOGS_DIR = HERE / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "device-sensor.log"

# Protect against pythonw where sys.stdout / sys.stderr is None
if sys.stdout is None:
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
logger = logging.getLogger("veglia-sensor")
logger.addHandler(file_handler)
logger.setLevel(logging.INFO)


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# Load configuration
load_dotenv(HERE / ".env")

DEVICE_ID = os.environ.get("VEGLIA_DEVICE_ID", "desktop-pc").strip()
DEVICE_NAME = os.environ.get("VEGLIA_DEVICE_NAME", "台式机").strip()
DEVICE_TYPE = os.environ.get("VEGLIA_DEVICE_TYPE", "windows_pc").strip()
HUB_URL = os.environ.get("VEGLIA_HUB_URL") or os.environ.get("VEGLIA_URL") or "http://127.0.0.1:8513"
HUB_URL = HUB_URL.rstrip("/")
TOKEN = os.environ.get("VEGLIA_TOKEN", "").strip()

REPORT_INTERVAL = float(os.environ.get("VEGLIA_SENSOR_INTERVAL", "2.0"))
HTTP_TIMEOUT = 3.0

from desktop_collector import collector


class DeviceSensor:
    def __init__(
        self,
        device_id: str = DEVICE_ID,
        device_name: str = DEVICE_NAME,
        device_type: str = DEVICE_TYPE,
        hub_url: str = HUB_URL,
        token: str = TOKEN,
        interval: float = REPORT_INTERVAL,
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.device_type = device_type
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.interval = interval

        self._running = False
        self._last_error_log_ts = 0.0
        self._last_error_msg = ""
        self._had_error = False

    def build_payload(self, snap: dict[str, Any]) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "device_type": self.device_type,
            "reported_at": int(time.time() * 1000),
            "foreground": snap.get("foreground"),
            "idle_seconds": int(snap.get("idle_seconds", 0)),
            "recent_activity": snap.get("recent_activity", []),
        }

    def send_report(self, payload: dict[str, Any]) -> bool:
        url = f"{self.hub_url}/devices/report"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(data)),
        }
        if self.token:
            headers["X-Auth-Token"] = self.token

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status == 200:
                    if self._had_error:
                        logger.info(f"Hub connection restored ({url})")
                        self._had_error = False
                        self._last_error_msg = ""
                    return True
        except Exception as e:
            now = time.time()
            err_str = str(e)
            # Throttle error logging: log if error changed or once every 60s
            if err_str != self._last_error_msg or (now - self._last_error_log_ts > 60.0):
                logger.warning(f"Failed to report to hub ({url}): {e}")
                self._last_error_log_ts = now
                self._last_error_msg = err_str
            self._had_error = True
            return False
        return False

    def start(self) -> None:
        """Start collector and begin reporting loop."""
        self._running = True
        logger.info(
            f"Starting DeviceSensor for [{self.device_id}] '{self.device_name}' "
            f"reporting to {self.hub_url} every {self.interval}s"
        )
        if sys.platform == "win32":
            collector.start()

        while self._running:
            try:
                snap = collector.snapshot() if sys.platform == "win32" else {}
                payload = self.build_payload(snap)
                self.send_report(payload)
            except Exception as e:
                logger.error(f"Unexpected error in sensor loop: {e}", exc_info=True)

            # Sleep in smaller increments for responsive shutdown
            slept = 0.0
            step = 0.2
            while self._running and slept < self.interval:
                time.sleep(min(step, self.interval - slept))
                slept += step

    def stop(self) -> None:
        """Stop sensor reporting and background collector."""
        if not self._running:
            return
        logger.info(f"Stopping DeviceSensor [{self.device_id}]...")
        self._running = False
        if sys.platform == "win32":
            collector.stop()
        logger.info(f"DeviceSensor [{self.device_id}] stopped cleanly.")


sensor = DeviceSensor()


def _handle_signal(signum: int, frame: Any) -> None:
    sensor.stop()
    sys.exit(0)


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    atexit.register(sensor.stop)

    sensor.start()


if __name__ == "__main__":
    main()
