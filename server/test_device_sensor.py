"""
Unit test suite for Veglia Device Sensor (device_sensor.py).
Tests:
- Payload construction matches the Device Hub schema
- Error throttling and tolerance when Hub is unreachable
- Successful report submission to a mock HTTP server
"""
import json
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from device_sensor import DeviceSensor


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class MockHubHandler(BaseHTTPRequestHandler):
    received_reports: list[dict] = []

    def do_POST(self):
        if self.path == "/devices/report":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            self.received_reports.append({
                "body": body,
                "token": self.headers.get("X-Auth-Token"),
            })
            resp = json.dumps({"ok": True, "device_id": body.get("device_id")}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


class DeviceSensorTestCase(unittest.TestCase):
    def test_build_payload(self):
        sensor = DeviceSensor(
            device_id="desktop-test",
            device_name="Test Desktop",
            device_type="windows_pc",
        )
        fake_snapshot = {
            "ok": True,
            "device": "desktop",
            "foreground": {
                "process_name": "notepad.exe",
                "window_title": "notes.txt - Notepad",
                "pid": 5678,
            },
            "idle_seconds": 15,
            "recent_activity": [
                {"ts": 1700000000000, "app": "notepad.exe", "title": "notes.txt"},
            ],
        }
        payload = sensor.build_payload(fake_snapshot)
        self.assertEqual(payload["device_id"], "desktop-test")
        self.assertEqual(payload["device_name"], "Test Desktop")
        self.assertEqual(payload["device_type"], "windows_pc")
        self.assertIn("reported_at", payload)
        self.assertEqual(payload["foreground"]["process_name"], "notepad.exe")
        self.assertEqual(payload["idle_seconds"], 15)
        self.assertEqual(len(payload["recent_activity"]), 1)

    def test_send_report_success(self):
        port = get_free_port()
        MockHubHandler.received_reports = []
        httpd = HTTPServer(("127.0.0.1", port), MockHubHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        try:
            hub_url = f"http://127.0.0.1:{port}"
            sensor = DeviceSensor(
                device_id="desktop-pc",
                device_name="台式机",
                hub_url=hub_url,
                token="mock-token-xyz",
            )
            payload = {
                "device_id": "desktop-pc",
                "device_name": "台式机",
                "device_type": "windows_pc",
                "foreground": {"process_name": "code.exe", "window_title": "Code", "pid": 123},
                "idle_seconds": 0,
                "recent_activity": [],
            }
            ok = sensor.send_report(payload)
            self.assertTrue(ok)
            self.assertEqual(len(MockHubHandler.received_reports), 1)
            received = MockHubHandler.received_reports[0]
            self.assertEqual(received["token"], "mock-token-xyz")
            self.assertEqual(received["body"]["device_id"], "desktop-pc")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_unreachable_hub_tolerated(self):
        # Point to unused port — sensor should return False and not raise exception
        dead_port = get_free_port()
        sensor = DeviceSensor(
            device_id="desktop-pc",
            hub_url=f"http://127.0.0.1:{dead_port}",
        )
        ok = sensor.send_report({"device_id": "desktop-pc"})
        self.assertFalse(ok)
        self.assertTrue(sensor._had_error)


if __name__ == "__main__":
    unittest.main()
