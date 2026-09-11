"""
Veglia Context Fusion Layer (v1).
Deterministic multi-device context perception and fusion rules.
Pure-function implementation with zero external side-effects (no network, no threads, no I/O).
"""
from __future__ import annotations

import time
from typing import Any, Optional

# Fusion specification version
FUSION_VERSION = "context-v1"

# Windows device heuristics (in seconds)
WINDOWS_ACTIVE_IDLE_SECONDS = 15      # idle <= 15s -> active
WINDOWS_RECENT_IDLE_SECONDS = 60      # 15s < idle <= 60s -> recent
WINDOWS_INACTIVE_IDLE_SECONDS = 180   # 60s < idle <= 180s -> idle; >180s -> inactive
WINDOWS_OFFLINE_AGE_SECONDS = 10.0    # age > 10s -> offline

# Phone heuristics (in seconds)
# Android CompanionService sends heartbeats every 30s when screen is interactive.
PHONE_FRESH_THRESHOLD = 45.0          # <= 45s: fresh heartbeat
PHONE_STALE_THRESHOLD = 120.0         # 45s ~ 120s: delayed/stale; >120s: expired/offline
PHONE_OFFLINE_THRESHOLD = 300.0       # >300s total silence: offline

# Primary device disambiguation margin
PRIMARY_SCORE_DELTA = 0.25


def normalize_windows_device(raw: dict[str, Any], now_ms: int) -> dict[str, Any]:
    """
    Normalize a raw Windows Device Registry entry into a unified device state.
    """
    device_id = str(raw.get("device_id", "unknown")).strip()
    device_name = str(raw.get("device_name", device_id)).strip()
    device_type = str(raw.get("device_type", "windows_pc")).strip()

    # Calculate freshness (age in seconds)
    if "age_seconds" in raw and raw["age_seconds"] is not None:
        age_seconds = float(raw["age_seconds"])
    elif "last_seen" in raw and raw["last_seen"]:
        ls = raw["last_seen"]
        # last_seen may be seconds or ms
        ls_ms = ls * 1000 if ls < 1e11 else ls
        age_seconds = max(0.0, (now_ms - ls_ms) / 1000.0)
    else:
        age_seconds = 9999.0

    raw_online = raw.get("online", True)
    online = bool(raw_online) and (age_seconds <= WINDOWS_OFFLINE_AGE_SECONDS)

    idle_seconds = int(raw.get("idle_seconds", 0))

    signals: list[str] = []
    reason: str = ""

    if not online:
        activity_state = "offline"
        activity_score = 0.00
        if age_seconds > WINDOWS_OFFLINE_AGE_SECONDS:
            signals.append("DEVICE_STALE")
            signals.append("DEVICE_OFFLINE")
            reason = f"{device_id} is stale/offline (last reported {age_seconds:.1f}s ago)"
        else:
            signals.append("DEVICE_OFFLINE")
            reason = f"{device_id} is offline"
    elif idle_seconds <= WINDOWS_ACTIVE_IDLE_SECONDS:
        activity_state = "active"
        activity_score = 1.00
        signals.extend(["DEVICE_ONLINE", "WINDOWS_RECENT_INPUT"])
        reason = f"{device_id} has active keyboard/mouse input ({idle_seconds}s idle)"
    elif idle_seconds <= WINDOWS_RECENT_IDLE_SECONDS:
        activity_state = "recent"
        activity_score = 0.70
        signals.extend(["DEVICE_ONLINE", "WINDOWS_RECENTLY_ACTIVE"])
        reason = f"{device_id} was recently active ({idle_seconds}s idle)"
    elif idle_seconds <= WINDOWS_INACTIVE_IDLE_SECONDS:
        activity_state = "idle"
        activity_score = 0.35
        signals.extend(["DEVICE_ONLINE", "WINDOWS_IDLE"])
        reason = f"{device_id} is idle ({idle_seconds}s idle)"
    else:
        activity_state = "inactive"
        activity_score = 0.10
        signals.extend(["DEVICE_ONLINE", "WINDOWS_INACTIVE"])
        reason = f"{device_id} is inactive ({idle_seconds}s idle)"

    # Foreground summary (lightweight without heavy history)
    fg_raw = raw.get("foreground")
    foreground = None
    if isinstance(fg_raw, dict):
        foreground = {
            "app": fg_raw.get("app") or fg_raw.get("process_name", "unknown"),
            "label": fg_raw.get("label") or fg_raw.get("app") or fg_raw.get("process_name", "unknown"),
            "title": fg_raw.get("title") or fg_raw.get("window_title", ""),
        }

    return {
        "device_id": device_id,
        "device_name": device_name,
        "device_type": device_type,
        "online": online,
        "activity_state": activity_state,
        "activity_score": activity_score,
        "idle_seconds": idle_seconds,
        "freshness_seconds": round(age_seconds, 1),
        "foreground": foreground,
        "signals": signals,
        "_reason": reason,
    }


def normalize_phone_device(phone_state: Optional[dict[str, Any]], now_ms: int) -> dict[str, Any]:
    """
    Normalize raw Android phone state into a unified device state.
    """
    device_id = "phone"
    device_name = "手机"
    device_type = "android_phone"

    if not phone_state or not isinstance(phone_state, dict):
        return {
            "device_id": device_id,
            "device_name": device_name,
            "device_type": device_type,
            "online": False,
            "activity_state": "unknown",
            "activity_score": 0.00,
            "screen_interactive": False,
            "freshness_seconds": None,
            "foreground": None,
            "signals": ["PHONE_NO_DATA"],
            "_reason": "no phone telemetry available",
        }

    current = phone_state.get("current")
    if not isinstance(current, dict):
        current = {}

    last_ts = int(current.get("lastHeartbeatTs", 0))
    # Fallback to events if current lastHeartbeatTs is zero
    if last_ts == 0 and phone_state.get("events"):
        last_ts = int(phone_state["events"][0].get("ts", 0))

    if last_ts <= 0:
        return {
            "device_id": device_id,
            "device_name": device_name,
            "device_type": device_type,
            "online": False,
            "activity_state": "unknown",
            "activity_score": 0.00,
            "screen_interactive": False,
            "freshness_seconds": None,
            "foreground": None,
            "signals": ["PHONE_NO_HEARTBEAT"],
            "_reason": "phone has never sent a heartbeat",
        }

    freshness_seconds = max(0.0, round((now_ms - last_ts) / 1000.0, 1))
    screen_interactive = bool(current.get("screenInteractive", False))

    signals: list[str] = []
    reason: str = ""

    if screen_interactive:
        signals.append("PHONE_SCREEN_INTERACTIVE")
        if freshness_seconds <= PHONE_FRESH_THRESHOLD:
            online = True
            activity_state = "active"
            activity_score = 1.00
            signals.append("PHONE_HEARTBEAT_FRESH")
            reason = f"phone screen is on with fresh heartbeat ({freshness_seconds}s ago)"
        elif freshness_seconds <= PHONE_STALE_THRESHOLD:
            online = True
            activity_state = "recent"
            activity_score = 0.70
            signals.append("PHONE_HEARTBEAT_STALE")
            reason = f"phone screen was interactive but heartbeat is delayed ({freshness_seconds}s ago)"
        else:
            online = False
            activity_state = "offline"
            activity_score = 0.00
            signals.append("PHONE_HEARTBEAT_STALE")
            signals.append("DEVICE_OFFLINE")
            reason = f"phone screen was on but heartbeat expired ({freshness_seconds}s ago)"
    else:
        signals.append("PHONE_SCREEN_OFF")
        if freshness_seconds <= PHONE_OFFLINE_THRESHOLD:
            online = True
            activity_state = "inactive"
            activity_score = 0.10
            reason = "phone screen is not interactive (screen off/locked)"
        else:
            online = False
            activity_state = "offline"
            activity_score = 0.00
            signals.append("DEVICE_OFFLINE")
            reason = f"phone screen is off and no heartbeat for {freshness_seconds}s"

    # Foreground
    cur_app = current.get("app") or "unknown"
    cur_label = current.get("label") or cur_app
    foreground = {
        "app": cur_app,
        "label": cur_label,
        "title": "",
    } if cur_app and cur_app != "unknown" else None

    return {
        "device_id": device_id,
        "device_name": device_name,
        "device_type": device_type,
        "online": online,
        "activity_state": activity_state,
        "activity_score": activity_score,
        "screen_interactive": screen_interactive,
        "freshness_seconds": freshness_seconds,
        "foreground": foreground,
        "signals": signals,
        "_reason": reason,
    }


def fuse_context(
    phone_state: Optional[dict[str, Any]],
    devices_state: Any,
    now_ms: Optional[int] = None,
) -> dict[str, Any]:
    """
    Deterministic context fusion pure function.
    Fuses raw phone telemetry and Windows device states into a single context snapshot.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    # 1. Normalize phone
    norm_phone = normalize_phone_device(phone_state, now_ms)

    # 2. Normalize Windows devices
    raw_devices: list[dict[str, Any]] = []
    if isinstance(devices_state, list):
        raw_devices = [d for d in devices_state if isinstance(d, dict)]
    elif isinstance(devices_state, dict):
        # Handle dict format: {"devices": [...]} or {device_id: device_data}
        if "devices" in devices_state and isinstance(devices_state["devices"], list):
            raw_devices = [d for d in devices_state["devices"] if isinstance(d, dict)]
        else:
            raw_devices = [v for v in devices_state.values() if isinstance(v, dict)]

    norm_devices: list[dict[str, Any]] = []
    for raw_dev in raw_devices:
        norm_devices.append(normalize_windows_device(raw_dev, now_ms))

    # Keep deterministic ordering: sorted by device_id
    norm_devices.sort(key=lambda d: d["device_id"])

    # Combine all normalized devices
    all_devices = [norm_phone] + norm_devices

    # 3. Identify active devices (only activity_state == "active")
    active_devices = [d["device_id"] for d in all_devices if d["activity_state"] == "active"]
    simultaneous_usage = len(active_devices) >= 2

    # 4. Determine Primary Device
    primary_device: Optional[str] = None
    confidence: float = 0.0
    ambiguous: bool = False
    decision_code: str = ""
    decision_reason: str = ""

    if len(active_devices) == 0:
        primary_device = None
        confidence = 0.0
        ambiguous = False
        decision_code = "NO_ACTIVE_DEVICE"
        decision_reason = "No active devices detected across phone and computers"
    elif len(active_devices) == 1:
        primary_device = active_devices[0]
        confidence = 0.90
        ambiguous = False
        decision_code = "SINGLE_ACTIVE_DEVICE"
        decision_reason = f"Only {primary_device} is currently active"
    else:
        # Multiple active devices: sort active devices by activity_score descending
        active_objs = [d for d in all_devices if d["device_id"] in active_devices]
        # Secondary sort key by device_id for determinism
        active_objs.sort(key=lambda d: (-d["activity_score"], d["device_id"]))
        top = active_objs[0]
        second = active_objs[1]
        delta = top["activity_score"] - second["activity_score"]

        if delta >= PRIMARY_SCORE_DELTA:
            primary_device = top["device_id"]
            confidence = 0.75
            ambiguous = False
            decision_code = "PRIMARY_SCORE_CLEAR"
            decision_reason = f"{top['device_id']} score ({top['activity_score']:.2f}) is significantly higher than {second['device_id']} ({second['activity_score']:.2f})"
        else:
            primary_device = None
            confidence = 0.0
            ambiguous = True
            decision_code = "PRIMARY_SCORE_AMBIGUOUS"
            decision_reason = f"Multiple devices ({', '.join(active_devices)}) are concurrently active with close scores"

    # 5. Build reason_codes and explanations
    reason_codes: list[str] = []
    reasons: list[str] = []

    # Per-device explanation lines
    for dev in all_devices:
        if dev.get("_reason"):
            reasons.append(dev["_reason"])
        for sig in dev.get("signals", []):
            if sig not in reason_codes:
                reason_codes.append(sig)

    # Decision-level codes & reasons
    if simultaneous_usage and "SIMULTANEOUS_USAGE_DETECTED" not in reason_codes:
        reason_codes.append("SIMULTANEOUS_USAGE_DETECTED")
    if decision_code and decision_code not in reason_codes:
        reason_codes.append(decision_code)
    if decision_reason:
        reasons.append(decision_reason)

    # Clean internal fields from output devices
    public_devices = []
    for d in all_devices:
        dev_copy = dict(d)
        dev_copy.pop("_reason", None)
        public_devices.append(dev_copy)

    return {
        "ok": True,
        "fusion_version": FUSION_VERSION,
        "generated_at": now_ms,
        "active_devices": active_devices,
        "primary_device": primary_device,
        "primary_device_confidence": confidence,
        "simultaneous_usage": simultaneous_usage,
        "ambiguous": ambiguous,
        "devices": public_devices,
        "reason_codes": reason_codes,
        "reasons": reasons,
    }
