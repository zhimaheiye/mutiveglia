#!/usr/bin/env python3
"""
Regression tests: Veglia Windows Sensor must reach a local/LAN Veglia Hub
DIRECT, never through a proxy, regardless of the process environment.

Covers:
  1. hostname classification (loopback + RFC1918 -> DIRECT; public -> not)
  2. proxy poison test (HTTP(S)_PROXY pointed at a dead proxy)
  3. non-private Hub URLs keep default urllib proxy behavior

Does not depend on any Clash / system proxy state on the machine.
"""
from __future__ import annotations

import http.server
import ipaddress
import json
import os
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import device_sensor  # noqa: E402  (requires pywin32 on Windows)
except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
    print(f"SKIP: cannot import device_sensor ({exc}); needs pywin32")
    raise SystemExit(0)

DEAD_PROXY = "http://127.0.0.1:1"
PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
    "NO_PROXY", "no_proxy",
)


class _FakeHubHandler(http.server.BaseHTTPRequestHandler):
    """Minimal stand-in for POST /devices/report."""

    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            _FakeHubHandler.received.append(json.loads(body.decode("utf-8")))
        except Exception:
            pass
        out = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args) -> None:  # silence
        pass


class HubHostClassificationTestCase(unittest.TestCase):
    """1. hostname classification."""

    def test_direct_hosts(self) -> None:
        for host in (
            "localhost", "LOCALHOST", "127.0.0.1", "127.1.2.3", "::1",
            "10.0.0.1", "10.255.255.254",
            "172.16.0.1", "172.31.255.254",
            "192.168.0.1", "192.168.101.84",
        ):
            with self.subTest(host=host):
                self.assertTrue(device_sensor._is_direct_hub_host(host))

    def test_non_direct_hosts(self) -> None:
        for host in (
            "8.8.8.8", "1.1.1.1", "example.com", "mcp.zhimaheiye.dpdns.org",
            "172.15.0.1", "172.32.0.1", "11.0.0.1", "192.169.0.1",
        ):
            with self.subTest(host=host):
                self.assertFalse(device_sensor._is_direct_hub_host(host))

    def test_empty_and_none(self) -> None:
        self.assertFalse(device_sensor._is_direct_hub_host(None))
        self.assertFalse(device_sensor._is_direct_hub_host(""))

    def test_no_private_range_blowup(self) -> None:
        """Guard: we must not silently widen to ipaddress.is_private
        (e.g. 169.254/16 link-local or 100.64/10 CGNAT)."""
        for host in ("169.254.1.1", "100.64.0.1", "fd00::1"):
            with self.subTest(host=host):
                self.assertFalse(device_sensor._is_direct_hub_host(host))


class SensorProxyPoisonTestCase(unittest.TestCase):
    """2. Hub reporting works with a poisoned proxy environment."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeHubHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _FakeHubHandler.received = []
        self._old = {k: os.environ.get(k) for k in PROXY_ENV_KEYS}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            os.environ[k] = DEAD_PROXY
        for k in ("NO_PROXY", "no_proxy"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _sensor(self) -> device_sensor.DeviceSensor:
        return device_sensor.DeviceSensor(
            device_id="proxy-bypass-test",
            device_name="ProxyBypassTest",
            device_type="windows_pc",
            hub_url=f"http://127.0.0.1:{self.port}",
            token="",
            interval=2.0,
        )

    def test_send_report_survives_poisoned_proxy(self) -> None:
        s = self._sensor()
        payload = s.build_payload({
            "foreground": {"app": "test.exe", "title": "test", "pid": 1},
            "idle_seconds": 3,
            "recent_activity": [],
        })
        self.assertTrue(s.send_report(payload), "send_report must succeed DIRECT")
        self.assertEqual(len(_FakeHubHandler.received), 1)
        self.assertEqual(_FakeHubHandler.received[0]["device_id"], "proxy-bypass-test")

    def test_lan_hub_is_also_direct(self) -> None:
        """192.168.x.x style Hub URLs must be classified DIRECT as well."""
        self.assertTrue(device_sensor._is_direct_hub_host("192.168.101.84"))

    def test_bare_urlopen_would_fail_under_same_env(self) -> None:
        """Sanity: proving the poisoned env is actually harmful."""
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/devices/report",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(Exception):
            urllib.request.urlopen(req, timeout=2)


class NonPrivateHubKeepsProxySemanticsTestCase(unittest.TestCase):
    """3. Public/non-private Hub URLs must NOT be forced through
    ProxyHandler({}); they keep default urllib proxy behavior."""

    def test_public_host_uses_default_urlopen(self) -> None:
        import urllib.request
        calls = {"default": 0}

        def fake_urlopen(req, timeout=None):
            calls["default"] += 1
            raise RuntimeError("stop-here")

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            req = urllib.request.Request(
                "https://example.com/devices/report",
                data=b"{}", method="POST")
            with self.assertRaises(RuntimeError):
                device_sensor._urlopen_hub(req, timeout=1)
        finally:
            urllib.request.urlopen = original
        self.assertEqual(calls["default"], 1)

    def test_public_ip_uses_default_urlopen(self) -> None:
        import urllib.request
        calls = {"default": 0}

        def fake_urlopen(req, timeout=None):
            calls["default"] += 1
            raise RuntimeError("stop-here")

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            req = urllib.request.Request(
                "http://8.8.8.8:8513/devices/report", data=b"{}", method="POST")
            with self.assertRaises(RuntimeError):
                device_sensor._urlopen_hub(req, timeout=1)
        finally:
            urllib.request.urlopen = original
        self.assertEqual(calls["default"], 1)

    def test_loopback_does_not_touch_default_urlopen(self) -> None:
        """Loopback must use the no-proxy opener, never the global one."""
        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not use default urlopen for loopback"))
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:9/devices/report", data=b"{}", method="POST")
            with self.assertRaises(OSError):  # connection refused, not AssertionError
                device_sensor._urlopen_hub(req, timeout=1)
        finally:
            urllib.request.urlopen = original


if __name__ == "__main__":
    unittest.main()
