#!/usr/bin/env python3
"""
Veglia MCP Server (stdio transport).
Bridges Android companion state to LLMs / AI agents via Model Context Protocol (MCP SDK v2).
"""
from __future__ import annotations

import sys
from pathlib import Path
from mcp.server.mcpserver import MCPServer, Image

# Ensure stdout is reserved exclusively for JSON-RPC MCP messages.
# Never write arbitrary logs to stdout.

import veglia_tools

mcp = MCPServer(
    "veglia-phone",
    instructions=(
        "Tools for perceiving the owner's context through Veglia. "
        "Provides fused multi-device context (get_context), Android companion phone state "
        "(foreground apps, on-demand screenshots), and Windows computers state across devices "
        "in the Veglia Device Hub (active window, process, title, user idle time). "
        "Prefer get_context first for overall awareness of what the owner is currently doing and "
        "which device is active. Use get_devices_activity or get_phone_activity for lower-level details. "
        "Only use summon_phone_ai when the user explicitly requests it "
        "or an established automation policy authorizes it."
    )
)


@mcp.tool(
    name="get_veglia_status",
    description="Check Veglia companion server reachability, version, and screenshot directory status. Useful for diagnostic health checks."
)
def get_veglia_status() -> dict:
    """Read Veglia server health and screenshot directory information."""
    return veglia_tools.get_veglia_status_result()


@mcp.tool(
    name="get_phone_activity",
    description="Read recent foreground application transitions on the owner's phone. Prefer this low-friction read to check what app the owner is currently using."
)
def get_phone_activity() -> dict:
    """Read recent foreground app events with friendly labels and elapsed time."""
    return veglia_tools.get_phone_activity_result()


@mcp.tool(
    name="get_phone_screen",
    description="Capture an on-demand screenshot of the phone screen via accessibility service and return the image content directly for visual inspection."
)
def get_phone_screen(timeout: float = 10.0) -> Image:
    """Trigger accessibility screenshot and return the image data directly to the agent."""
    res = veglia_tools.get_phone_screen_result(timeout=timeout)
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "timeout_waiting_for_screenshot"))
    return Image(path=res["image_path"], format="jpeg")


@mcp.tool(
    name="summon_phone_ai",
    description="Bring the target companion AI application to the foreground on the phone. This has noticeable side effects; call only when explicitly requested by the user or by an established automation rule."
)
def summon_phone_ai() -> dict:
    """Send summon signal to pull the companion AI app to the front."""
    return veglia_tools.summon_phone_ai_result()


@mcp.tool(
    name="get_devices_activity",
    description=(
        "Read activity status across all connected Windows computers in the Veglia Device Hub. "
        "Returns a list of known devices with online status, foreground process, window title, and idle seconds. "
        "Use this to see which computer the owner is using or check overall workspace activity."
    )
)
def get_devices_activity() -> dict:
    """Read summary activity for all registered devices."""
    return veglia_tools.get_devices_activity_result()


@mcp.tool(
    name="get_device_activity",
    description=(
        "Read detailed desktop activity for a specific computer by its device_id (e.g., 'desktop-pc', 'main-pc'). "
        "Returns foreground process, window title, user idle time in seconds, and recent window switch history. "
        "Also reports whether the device is currently online (reported within 10s) and elapsed time since last report."
    )
)
def get_device_activity(device_id: str) -> dict:
    """Read detailed activity and switch history for a specific device."""
    return veglia_tools.get_device_activity_result(device_id)


@mcp.tool(
    name="get_desktop_activity",
    description=(
        "Read current Windows desktop state for the default/local desktop PC: "
        "foreground process name, window title, user idle time in seconds, and recent window-switch history. "
        "Backwards-compatibility tool querying the central Device Hub for the primary desktop."
    )
)
def get_desktop_activity() -> dict:
    """Return default desktop PC foreground app, idle seconds, and recent window switch history."""
    return veglia_tools.get_desktop_activity_result()


@mcp.tool(
    name="get_context",
    description=(
        "Return a deterministic fused snapshot of the owner's current multi-device activity, "
        "including active devices, likely primary device, simultaneous usage and ambiguity. "
        "Use this before manually comparing individual devices when the question concerns the owner's overall current context."
    )
)
def get_context() -> dict:
    """Return fused multi-device context snapshot across phone and connected computers."""
    return veglia_tools.get_context_result()


if __name__ == "__main__":
    mcp.run(transport="stdio")
