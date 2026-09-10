#!/usr/bin/env python3
"""
Desktop Sense MCP Server: Official MCP SDK v2 stdio implementation.
Exposes privacy-safe Windows 11 desktop activity and on-demand screen perception tools.
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image

# Ensure local directory is on sys.path for desktop_tools import
sys.path.insert(0, str(Path(__file__).resolve().parent))

from desktop_tools import (
    get_active_window_result,
    get_idle_status_result,
    get_desktop_status_result,
    get_desktop_screen_result,
)

mcp = MCPServer(
    "desktop-sense",
    instructions=(
        "Read-only tools for understanding the user's current Windows desktop state. "
        "Prefer lightweight status tools before taking screenshots. "
        "Only capture the screen when textual application and idle information "
        "is insufficient to answer the user's request. "
        "Prefer active-window screenshots over full-desktop screenshots."
    ),
)


@mcp.tool(
    description=(
        "Get a privacy-safe summary of the currently focused Windows application. "
        "Use this before requesting a screenshot when only the active application name is needed. "
        "Window titles and executable paths are intentionally redacted for privacy."
    )
)
def get_active_window() -> dict[str, Any]:
    """Return privacy-safe foreground application information."""
    return get_active_window_result(detail=False)


@mcp.tool(
    description=(
        "Get system keyboard/mouse idle time in seconds and UX activity state. "
        "Note: idle_seconds is the raw measurement. activity_state ('active', 'thinking', 'away') "
        "is an inferred classification and does not prove mental state or display power state."
    )
)
def get_idle_status() -> dict[str, Any]:
    """Return keyboard/mouse idle measurement and classification."""
    return get_idle_status_result()


@mcp.tool(
    description=(
        "Preferred aggregate tool: get both the active window summary and system idle status. "
        "Call this first to determine what application is in use and whether the user is active. "
        "Only request a screen capture if this textual context is insufficient."
    )
)
def get_desktop_status() -> dict[str, Any]:
    """Return aggregated active window and idle status in safe mode."""
    return get_desktop_status_result(detail=False)


@mcp.tool(
    description=(
        "Capture an on-demand screenshot of the desktop and return native image content. "
        "Default target is 'active_window' (least intrusive). Only use 'primary' or 'virtual' "
        "if the user explicitly requests full desktop observation or cross-window context."
    )
)
def get_desktop_screen(
    target: Literal["active_window", "primary", "virtual"] = "active_window",
) -> Image:
    """Capture a screenshot and deliver it as native MCP ImageContent."""
    result = get_desktop_screen_result(
        target=target,
        image_format="jpeg",
        quality=90,
    )

    if not result.get("ok"):
        err = result.get("error") or {}
        msg = err.get("message") or err.get("code") or "Desktop screenshot capture failed"
        raise RuntimeError(f"get_desktop_screen failed: {msg}")

    img_path_str = result.get("image_path")
    if not img_path_str:
        raise RuntimeError("get_desktop_screen failed: no image_path returned")

    img_path = Path(img_path_str)
    if not img_path.is_file():
        raise RuntimeError(f"Screenshot file was not created on disk: {img_path}")

    return Image(str(img_path))


if __name__ == "__main__":
    mcp.run()
