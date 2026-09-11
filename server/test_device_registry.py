"""
Test suite for Veglia Device Registry endpoints in veglia_server.py.
Spins up a lightweight in-process HTTP server on an ephemeral port to verify:
- POST /devices/report (registration & update)
- GET /devices (online/offline calculation, list summary)
- GET /devices/{device_id} (device detail, 404 handling)
- Token authentication enforcement
- Freshness timeout (age <= 10.0s -> online=True, >10.0s -> online=False)
"""
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from veglia_server import Handler, State


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class DeviceRegistryTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = get_free_port()
        cls.token = "test-secret-token"
        cls.state = State()
        cls.state.token = cls.token

        Handler.state = cls.state
        cls.httpd = HTTPServer(("127.0.0.1", cls.port), Handler)
        cls.httpd.state = cls.state

        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        # Clear device registry between tests
        with self.state.devices_lock:
            self.state.devices.clear()

    def _request(self, method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["X-Auth-Token"] = token

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                status = resp.status
                res_body = json.loads(resp.read().decode("utf-8"))
                return status, res_body
        except urllib.error.HTTPError as e:
            try:
                res_body = json.loads(e.read().decode("utf-8"))
            except Exception:
                res_body = {"raw": e.read().decode("utf-8", errors="replace")}
            return e.code, res_body

    def test_report_and_get_devices(self):
        # 1. Report from desktop-pc
        report_payload = {
            "device_id": "desktop-pc",
            "device_name": "台式机",
            "device_type": "windows_pc",
            "reported_at": int(time.time() * 1000),
            "foreground": {
                "process_name": "code.exe",
                "window_title": "veglia_server.py - Visual Studio Code",
                "pid": 1234,
            },
            "idle_seconds": 5,
            "recent_activity": [
                {"ts": int(time.time() * 1000) - 1000, "app": "code.exe", "title": "Editor"},
            ],
        }
        status, body = self._request("POST", "/devices/report", report_payload, token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("device_id"), "desktop-pc")

        # 2. GET /devices
        status, body = self._request("GET", "/devices", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        devices = body.get("devices", [])
        self.assertEqual(len(devices), 1)
        dev = devices[0]
        self.assertEqual(dev["device_id"], "desktop-pc")
        self.assertEqual(dev["device_name"], "台式机")
        self.assertEqual(dev["device_type"], "windows_pc")
        self.assertTrue(dev["online"])
        self.assertLessEqual(dev["age_seconds"], 2.0)
        self.assertEqual(dev["foreground"]["process_name"], "code.exe")
        self.assertEqual(dev["idle_seconds"], 5)

        # 3. GET /devices/desktop-pc
        status, body = self._request("GET", "/devices/desktop-pc", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["device_id"], "desktop-pc")
        self.assertTrue(body["online"])
        self.assertEqual(len(body["recent_activity"]), 1)

    def test_device_not_found(self):
        status, body = self._request("GET", "/devices/non-existent", token=self.token)
        self.assertEqual(status, 404)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "device_not_found")

    def test_missing_device_id_returns_400(self):
        status, body = self._request("POST", "/devices/report", {"device_name": "No ID"}, token=self.token)
        self.assertEqual(status, 400)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "missing_device_id")

    def test_invalid_token_returns_403(self):
        status, body = self._request("GET", "/devices", token="wrong-token")
        self.assertEqual(status, 403)

        status, body = self._request("POST", "/devices/report", {"device_id": "foo"}, token="wrong-token")
        self.assertEqual(status, 403)

    def test_offline_detection_after_10s(self):
        # Inject device report with old last_seen
        report_payload = {
            "device_id": "main-pc",
            "device_name": "主力机",
            "device_type": "windows_pc",
            "foreground": {"process_name": "chrome.exe", "window_title": "Web", "pid": 456},
            "idle_seconds": 120,
            "recent_activity": [],
        }
        status, _ = self._request("POST", "/devices/report", report_payload, token=self.token)
        self.assertEqual(status, 200)

        # Artificially age the device by modifying state
        with self.state.devices_lock:
            self.state.devices["main-pc"]["last_seen"] = time.time() - 15.0

        # Query GET /devices
        status, body = self._request("GET", "/devices", token=self.token)
        self.assertEqual(status, 200)
        dev = body["devices"][0]
        self.assertEqual(dev["device_id"], "main-pc")
        self.assertFalse(dev["online"])
        self.assertGreaterEqual(dev["age_seconds"], 14.0)
        # Verify state is preserved even when offline
        self.assertEqual(dev["foreground"]["process_name"], "chrome.exe")
        self.assertEqual(dev["idle_seconds"], 120)

    def test_get_context_endpoint(self):
        # 1. Report an active desktop-pc
        report_payload = {
            "device_id": "desktop-pc",
            "device_name": "台式机",
            "device_type": "windows_pc",
            "reported_at": int(time.time() * 1000),
            "foreground": {"process_name": "code.exe", "window_title": "Editor", "pid": 100},
            "idle_seconds": 2,
            "recent_activity": [],
        }
        self._request("POST", "/devices/report", report_payload, token=self.token)

        # 2. GET /context
        status, body = self._request("GET", "/context", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("fusion_version"), "context-v1")
        self.assertEqual(body.get("primary_device"), "desktop-pc")
        self.assertEqual(body.get("active_devices"), ["desktop-pc"])
        self.assertFalse(body.get("simultaneous_usage"))
        self.assertFalse(body.get("ambiguous"))
        self.assertIn("SINGLE_ACTIVE_DEVICE", body.get("reason_codes", []))


if __name__ == "__main__":
    unittest.main()
