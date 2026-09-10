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
        "Tools for reading the owner's Android phone context through Veglia. "
        "Prefer get_phone_activity before taking a screenshot. "
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
    name="get_desktop_activity",
    description=(
        "Read current Windows desktop state: foreground process name, window title, "
        "user idle time in seconds (keyboard/mouse inactivity), and recent window-switch history. "
        "Use this to understand what the owner is currently doing on their computer. "
        "Returns ok=false on non-Windows platforms."
    )
)
def get_desktop_activity() -> dict:
    """Return desktop foreground app, idle seconds, and recent window switch history."""
    return veglia_tools.get_desktop_activity_result()


if __name__ == "__main__":
    # Desktop collector lifecycle:
    # MCPServer (mcp SDK v2) has no on_startup/on_shutdown hooks.
    # We start the collector explicitly here, and register stop via atexit.
    # This runs only when the MCP server is launched as a process (not on import).
    import atexit

    if sys.platform == "win32":
        from desktop_collector import collector as _desktop_collector
        _desktop_collector.start()
        atexit.register(_desktop_collector.stop)

    mcp.run(transport="stdio")
