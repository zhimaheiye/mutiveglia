#!/usr/bin/env python3
"""
Regression tests: Veglia's internal requests to the loopback Veglia Core
must succeed even when the process environment contains a poisoned
(HTTP(S)_PROXY) proxy configuration.

The test spins up a fake Veglia Core on 127.0.0.1, points veglia_tools at
it, sets HTTP_PROXY/HTTPS_PROXY to a guaranteed-dead proxy
(http://127.0.0.1:1), and asserts that get_veglia_status / 
get_devices_activity / get_context still work via DIRECT connection.

This test does NOT depend on any real Clash/proxy state on the machine.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import veglia_tools  # noqa: E402

DEAD_PROXY = "http://127.0.0.1:1"


class _FakeCoreHandler(http.server.BaseHTTPRequestHandler):
    """Minimal stand-in for the Veglia Core HTTP API."""

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json({"ok": True, "service": "veglia", "version": "test"})
        elif self.path == "/devices":
            self._json({
                "devices": [
                    {
                        "device_id": "fake-pc",
                        "device_name": "FakePC",
                        "device_type": "windows_pc",
                        "online": True,
                        "idle_seconds": 3,
                    }
                ]
            })
        elif self.path == "/context":
            self._json({
                "ok": True,
                "fusion_version": "context-v1",
                "active_devices": ["fake-pc"],
                "primary_device": "fake-pc",
                "simultaneous_usage": False,
                "ambiguous": False,
            })
        else:
            self._json({"error": "not_found"}, code=404)

    def log_message(self, *args) -> None:  # silence
        pass


class LoopbackProxyBypassTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeCoreHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        # Point veglia_tools at the fake core.
        veglia_tools.BASE_URL = f"http://127.0.0.1:{self.port}"
        # Poison the environment with a proxy that can never connect.
        self._old = {k: os.environ.get(k) for k in
                     ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                      "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[k] = DEAD_PROXY
        for k in ("NO_PROXY", "no_proxy"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_get_veglia_status_bypasses_proxy(self) -> None:
        r = veglia_tools.get_veglia_status_result()
        self.assertTrue(r.get("ok") or r.get("server_reachable"), r)
        self.assertEqual(r.get("base_url"), veglia_tools.BASE_URL)

    def test_get_devices_activity_bypasses_proxy(self) -> None:
        r = veglia_tools.get_devices_activity_result()
        self.assertTrue(r.get("ok"), r)
        ids = [d["device_id"] for d in r.get("devices", [])]
        self.assertIn("fake-pc", ids)

    def test_get_context_bypasses_proxy(self) -> None:
        r = veglia_tools.get_context_result()
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r.get("primary_device"), "fake-pc")
        self.assertEqual(r.get("fusion_version"), "context-v1")

    def test_poisoned_env_actually_would_break_urlopen(self) -> None:
        """Sanity check: bare urlopen under the poisoned env must fail,
        proving the tests above exercise the bypass rather than a no-op."""
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/health", method="GET")
        with self.assertRaises(Exception):
            urllib.request.urlopen(req, timeout=2)


if __name__ == "__main__":
    unittest.main()
