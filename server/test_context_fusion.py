"""
Unit tests for Context Fusion Layer (server/context_fusion.py).
Validates all deterministic rules, heuristic thresholds, and edge cases.
"""
import copy
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from context_fusion import (
    fuse_context,
    normalize_phone_device,
    normalize_windows_device,
    WINDOWS_ACTIVE_IDLE_SECONDS,
    WINDOWS_RECENT_IDLE_SECONDS,
    WINDOWS_INACTIVE_IDLE_SECONDS,
    WINDOWS_OFFLINE_AGE_SECONDS,
    PHONE_FRESH_THRESHOLD,
    PHONE_STALE_THRESHOLD,
    PRIMARY_SCORE_DELTA,
)


class ContextFusionTestCase(unittest.TestCase):
    def setUp(self):
        self.fixed_now_ms = 1789136000000

    def test_case_1_main_pc_active_desktop_idle_phone_off(self):
        """Case 1: main-pc idle 2s, desktop-pc idle 100s, phone screen off"""
        phone_state = {
            "current": {
                "app": "com.tencent.mm",
                "screenInteractive": False,
                "lastHeartbeatTs": self.fixed_now_ms - 20000,
            }
        }
        devices = [
            {
                "device_id": "desktop-pc",
                "device_name": "台式机",
                "device_type": "windows_pc",
                "online": True,
                "age_seconds": 0.5,
                "idle_seconds": 100,
                "foreground": {"process_name": "code.exe", "window_title": "Editor"},
            },
            {
                "device_id": "main-pc",
                "device_name": "主力机",
                "device_type": "windows_pc",
                "online": True,
                "age_seconds": 0.8,
                "idle_seconds": 2,
                "foreground": {"process_name": "msedge.exe", "window_title": "Browser"},
            },
        ]

        res = fuse_context(phone_state, devices, now_ms=self.fixed_now_ms)
        self.assertTrue(res["ok"])
        self.assertEqual(res["active_devices"], ["main-pc"])
        self.assertEqual(res["primary_device"], "main-pc")
        self.assertAlmostEqual(res["primary_device_confidence"], 0.90)
        self.assertFalse(res["simultaneous_usage"])
        self.assertFalse(res["ambiguous"])
        self.assertIn("SINGLE_ACTIVE_DEVICE", res["reason_codes"])

        # Check per-device normalized states
        dev_map = {d["device_id"]: d for d in res["devices"]}
        self.assertEqual(dev_map["main-pc"]["activity_state"], "active")
        self.assertEqual(dev_map["main-pc"]["activity_score"], 1.0)
        self.assertEqual(dev_map["desktop-pc"]["activity_state"], "idle")
        self.assertEqual(dev_map["desktop-pc"]["activity_score"], 0.35)
        self.assertEqual(dev_map["phone"]["activity_state"], "inactive")
        self.assertEqual(dev_map["phone"]["activity_score"], 0.10)

    def test_case_2_desktop_active_main_pc_offline_phone_off(self):
        """Case 2: desktop-pc idle 3s, main-pc offline, phone screen off"""
        phone_state = {
            "current": {
                "screenInteractive": False,
                "lastHeartbeatTs": self.fixed_now_ms - 10000,
            }
        }
        devices = [
            {
                "device_id": "desktop-pc",
                "device_name": "台式机",
                "online": True,
                "age_seconds": 1.0,
                "idle_seconds": 3,
            },
            {
                "device_id": "main-pc",
                "device_name": "主力机",
                "online": False,
                "age_seconds": 25.0,
                "idle_seconds": 0,
            },
        ]

        res = fuse_context(phone_state, devices, now_ms=self.fixed_now_ms)
        self.assertEqual(res["active_devices"], ["desktop-pc"])
        self.assertEqual(res["primary_device"], "desktop-pc")
        self.assertFalse(res["simultaneous_usage"])
        self.assertFalse(res["ambiguous"])
        dev_map = {d["device_id"]: d for d in res["devices"]}
        self.assertEqual(dev_map["main-pc"]["activity_state"], "offline")
        self.assertEqual(dev_map["main-pc"]["activity_score"], 0.0)

    def test_case_3_phone_active_pcs_idle(self):
        """Case 3: phone screenInteractive=true (fresh), both PCs idle > 180s"""
        phone_state = {
            "current": {
                "app": "com.tencent.mm",
                "label": "微信",
                "screenInteractive": True,
                "lastHeartbeatTs": self.fixed_now_ms - 5000,  # 5s ago (fresh)
            }
        }
        devices = [
            {"device_id": "desktop-pc", "online": True, "age_seconds": 1.0, "idle_seconds": 240},
            {"device_id": "main-pc", "online": True, "age_seconds": 1.0, "idle_seconds": 300},
        ]

        res = fuse_context(phone_state, devices, now_ms=self.fixed_now_ms)
        self.assertEqual(res["active_devices"], ["phone"])
        self.assertEqual(res["primary_device"], "phone")
        self.assertFalse(res["simultaneous_usage"])
        self.assertFalse(res["ambiguous"])
        dev_map = {d["device_id"]: d for d in res["devices"]}
        self.assertEqual(dev_map["phone"]["activity_state"], "active")
        self.assertEqual(dev_map["phone"]["activity_score"], 1.0)
        self.assertEqual(dev_map["desktop-pc"]["activity_state"], "inactive")
        self.assertEqual(dev_map["main-pc"]["activity_state"], "inactive")

    def test_case_4_phone_and_main_pc_simultaneous(self):
        """Case 4: phone active, main-pc active, score delta < 0.25 -> ambiguous, simultaneous"""
        phone_state = {
            "current": {
                "screenInteractive": True,
                "lastHeartbeatTs": self.fixed_now_ms - 2000,
            }
        }
        devices = [
            {"device_id": "main-pc", "online": True, "age_seconds": 0.5, "idle_seconds": 1},
            {"device_id": "desktop-pc", "online": True, "age_seconds": 1.0, "idle_seconds": 120},
        ]

        res = fuse_context(phone_state, devices, now_ms=self.fixed_now_ms)
        self.assertEqual(set(res["active_devices"]), {"main-pc", "phone"})
        self.assertTrue(res["simultaneous_usage"])
        self.assertTrue(res["ambiguous"])
        self.assertIsNone(res["primary_device"])
        self.assertEqual(res["primary_device_confidence"], 0.0)
        self.assertIn("SIMULTANEOUS_USAGE_DETECTED", res["reason_codes"])
        self.assertIn("PRIMARY_SCORE_AMBIGUOUS", res["reason_codes"])

    def test_case_5_two_pcs_simultaneous(self):
        """Case 5: main-pc active, desktop-pc active, phone off -> ambiguous, simultaneous"""
        phone_state = {
            "current": {
                "screenInteractive": False,
                "lastHeartbeatTs": self.fixed_now_ms - 10000,
            }
        }
        devices = [
            {"device_id": "desktop-pc", "online": True, "age_seconds": 0.5, "idle_seconds": 0},
            {"device_id": "main-pc", "online": True, "age_seconds": 0.5, "idle_seconds": 0},
        ]

        res = fuse_context(phone_state, devices, now_ms=self.fixed_now_ms)
        self.assertEqual(set(res["active_devices"]), {"desktop-pc", "main-pc"})
        self.assertTrue(res["simultaneous_usage"])
        self.assertTrue(res["ambiguous"])
        self.assertIsNone(res["primary_device"])

    def test_case_6_all_inactive_or_offline(self):
        """Case 6: all devices inactive or offline -> active_devices=[], primary=None"""
        phone_state = {
            "current": {
                "screenInteractive": False,
                "lastHeartbeatTs": self.fixed_now_ms - 60000,
            }
        }
        devices = [
            {"device_id": "desktop-pc", "online": True, "age_seconds": 2.0, "idle_seconds": 500},
            {"device_id": "main-pc", "online": False, "age_seconds": 50.0, "idle_seconds": 500},
        ]

        res = fuse_context(phone_state, devices, now_ms=self.fixed_now_ms)
        self.assertEqual(res["active_devices"], [])
        self.assertIsNone(res["primary_device"])
        self.assertEqual(res["primary_device_confidence"], 0.0)
        self.assertFalse(res["simultaneous_usage"])
        self.assertFalse(res["ambiguous"])
        self.assertIn("NO_ACTIVE_DEVICE", res["reason_codes"])

    def test_case_7_stale_pc_never_active(self):
        """Case 7: PC reported idle_seconds=0 but age_seconds > 10.0 -> must be offline!"""
        devices = [
            {
                "device_id": "stale-pc",
                "online": True,
                "age_seconds": 15.0,  # exceeds 10s
                "idle_seconds": 0,
            }
        ]
        res = fuse_context(None, devices, now_ms=self.fixed_now_ms)
        self.assertEqual(res["active_devices"], [])
        dev = res["devices"][1]  # index 1 after phone
        self.assertEqual(dev["activity_state"], "offline")
        self.assertEqual(dev["activity_score"], 0.0)
        self.assertIn("DEVICE_STALE", dev["signals"])

    def test_case_8_stale_phone_never_active(self):
        """Case 8: phone screenInteractive=true but heartbeat > 120s ago -> must not be active!"""
        phone_state = {
            "current": {
                "screenInteractive": True,
                "lastHeartbeatTs": self.fixed_now_ms - 150000,  # 150s ago
            }
        }
        res = fuse_context(phone_state, [], now_ms=self.fixed_now_ms)
        self.assertEqual(res["active_devices"], [])
        phone_dev = res["devices"][0]
        self.assertEqual(phone_dev["activity_state"], "offline")
        self.assertEqual(phone_dev["activity_score"], 0.0)

    def test_case_9_determinism(self):
        """Case 9: 100 identical calls produce exact same dictionary results"""
        phone_state = {
            "current": {
                "screenInteractive": True,
                "lastHeartbeatTs": self.fixed_now_ms - 10000,
            }
        }
        devices = [
            {"device_id": "desktop-pc", "online": True, "age_seconds": 1.0, "idle_seconds": 5},
            {"device_id": "main-pc", "online": True, "age_seconds": 0.5, "idle_seconds": 45},
        ]

        first = fuse_context(phone_state, devices, now_ms=self.fixed_now_ms)
        for _ in range(100):
            nxt = fuse_context(phone_state, devices, now_ms=self.fixed_now_ms)
            self.assertEqual(first, nxt)

    def test_case_10_graceful_handling_of_empty_and_corrupt_data(self):
        """Case 10: Empty, None, or unexpected payloads never raise exceptions"""
        res1 = fuse_context(None, None, now_ms=self.fixed_now_ms)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["active_devices"], [])

        res2 = fuse_context({}, {}, now_ms=self.fixed_now_ms)
        self.assertTrue(res2["ok"])

        res3 = fuse_context({"current": "not_a_dict"}, ["not_a_dict", 123], now_ms=self.fixed_now_ms)
        self.assertTrue(res3["ok"])


if __name__ == "__main__":
    unittest.main()
