#!/usr/bin/env python3
"""
Veglia Streamable HTTP MCP Server.
Provides remote MCP access over HTTP via standard Model Context Protocol (MCP SDK v2).
Default endpoint: http://0.0.0.0:8514/mcp
"""
from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
import uvicorn
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from mcp.server.streamable_http import TransportSecuritySettings

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

LOGS_DIR = HERE / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "veglia-mcp.log"

# Protect against pythonw where sys.stdout / sys.stderr is None
if sys.stdout is None:
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp"):
    _l = logging.getLogger(logger_name)
    _l.addHandler(file_handler)
    _l.setLevel(logging.INFO)

# Ensure default upstream Veglia Core URL points to local port 8513
os.environ.setdefault("VEGLIA_URL", "http://127.0.0.1:8513")

# Reuse the exact same tools and handlers from veglia_mcp
from veglia_mcp import mcp

HOST = os.environ.get("VEGLIA_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("VEGLIA_MCP_PORT", "8514"))
ENDPOINT_PATH = os.environ.get("VEGLIA_MCP_PATH", "/mcp")


def create_app():
    # Build starlette app with DNS rebinding protection disabled for LAN / proxy access
    app = mcp.streamable_http_app(
        streamable_http_path=ENDPOINT_PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=HOST,
    )

    # Health check probe
    app.add_route("/health", lambda r: JSONResponse({"ok": True, "service": "veglia-mcp-http", "endpoint": ENDPOINT_PATH}))

    # Enable standard CORS for browser-type clients and local proxies
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    return app


def main() -> None:
    if sys.platform == "win32":
        import atexit
        from desktop_collector import collector as _desktop_collector
        _desktop_collector.start()
        atexit.register(_desktop_collector.stop)

    app = create_app()
    print("=" * 52)
    print(f"  Veglia Streamable HTTP MCP Server")
    print(f"  listening on http://{HOST}:{PORT}{ENDPOINT_PATH}")
    print(f"  upstream Veglia Core: {os.environ.get('VEGLIA_URL')}")
    print("=" * 52)
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    finally:
        if sys.platform == "win32":
            from desktop_collector import collector as _desktop_collector
            _desktop_collector.stop()


if __name__ == "__main__":
    main()
