r"""
MCP Protocol E2E test for Veglia Desktop Collector.

Runs from the server/ directory using the local .venv-mcp Python.
Usage:
    ..\.venv-mcp\Scripts\python.exe test_mcp_e2e.py

What it tests:
  1. Start veglia_mcp.py as a stdio subprocess
  2. MCP initialize handshake
  3. tools/list — confirm all 5 tools registered
  4. tools/call get_desktop_activity — validate response structure

Note: foreground/recent_activity may be null/empty when run from a non-interactive
desktop (e.g. CI, headless, sandboxed agent session). That is expected — the
MCP protocol layer is being tested here, not the interactive desktop observation.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
PYTHON_EXE = Path(sys.executable)          # use the same Python that runs this script
SERVER_SCRIPT = HERE / "veglia_mcp.py"


def _get_expected_device_id() -> str:
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("VEGLIA_DEVICE_ID=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("VEGLIA_DEVICE_ID", "desktop-pc").strip()

EXPECTED_TOOLS = {
    "get_veglia_status",
    "get_phone_activity",
    "get_phone_screen",
    "summon_phone_ai",
    "get_devices_activity",
    "get_device_activity",
    "get_desktop_activity",
    "get_context",
}


async def run_mcp_e2e() -> tuple[bool, dict | None]:
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp import ClientSession

    params = StdioServerParameters(
        command=str(PYTHON_EXE),
        args=[str(SERVER_SCRIPT)],
        cwd=str(HERE),
        env=None,
    )

    print("=== MCP Protocol E2E Test ===")
    print(f"Server : {SERVER_SCRIPT}")
    print(f"Python : {PYTHON_EXE}")
    print()

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:

            # 1. Initialize
            print("[1] initialize ...")
            init_result = await session.initialize()
            srv = init_result.server_info
            print(f"    server name    : {srv.name if srv else 'n/a'}")
            print(f"    server version : {srv.version if srv else 'n/a'}")
            print("    PASS")
            print()

            # 2. tools/list
            print("[2] tools/list ...")
            tools_result = await session.list_tools()
            found = {t.name for t in tools_result.tools}
            print(f"    found : {sorted(found)}")
            missing = EXPECTED_TOOLS - found
            if missing:
                print(f"    FAIL  : missing tools: {missing}")
                return False, None
            print(f"    PASS  : all {len(EXPECTED_TOOLS)} tools present")
            print()
            for t in tools_result.tools:
                print(f"    [{t.name}] {(t.description or '')[:80]}")
            print()

            # 3. tools/call get_devices_activity
            print("[3] tools/call get_devices_activity ...")
            call_devices = await session.call_tool("get_devices_activity", arguments={})
            raw_devices = None
            for item in (call_devices.content or []):
                if hasattr(item, "text"):
                    try:
                        raw_devices = json.loads(item.text)
                    except Exception:
                        raw_devices = item.text
                    break
            print(f"    devices response: {raw_devices}")
            if not isinstance(raw_devices, dict) or not raw_devices.get("ok"):
                print(f"    FAIL: get_devices_activity failed: {raw_devices}")
                return False, None
            print(f"    PASS  : devices count = {raw_devices.get('count', 0)}")
            print()

            # 4. tools/call get_desktop_activity (compat alias)
            print("[4] tools/call get_desktop_activity ...")
            call_result = await session.call_tool("get_desktop_activity", arguments={})
            raw = None
            for item in (call_result.content or []):
                if hasattr(item, "text"):
                    try:
                        raw = json.loads(item.text)
                    except Exception:
                        raw = item.text
                    break

            print(f"    response:\n{json.dumps(raw, ensure_ascii=False, indent=6) if isinstance(raw, dict) else raw}")
            print()

            if not isinstance(raw, dict):
                print("    FAIL: response not a dict")
                return False, None

            expected_device_id = _get_expected_device_id()
            assert raw.get("ok") is True
            assert raw.get("tool") == "get_desktop_activity"
            assert raw.get("device_id") == expected_device_id
            assert isinstance(raw.get("idle_seconds"), (int, float))
            assert raw.get("idle_seconds", -1) >= 0
            assert isinstance(raw.get("recent_activity"), list)
            print(f"    device_id      : {raw['device_id']}")
            print(f"    online         : {raw.get('online')}")
            print(f"    idle_seconds   : {raw['idle_seconds']}")
            print(f"    foreground     : {raw.get('foreground')}")
            print(f"    recent_activity: {len(raw['recent_activity'])} entries")
            print("    PASS")
            print()

            # 5. tools/call get_device_activity (expected_device_id)
            print(f"[5] tools/call get_device_activity ({expected_device_id}) ...")
            call_single = await session.call_tool("get_device_activity", arguments={"device_id": expected_device_id})
            raw_single = None
            for item in (call_single.content or []):
                if hasattr(item, "text"):
                    try:
                        raw_single = json.loads(item.text)
                    except Exception:
                        raw_single = item.text
                    break
            if not isinstance(raw_single, dict) or not raw_single.get("ok"):
                print(f"    FAIL: get_device_activity failed: {raw_single}")
                return False, None
            print(f"    PASS  : device {raw_single.get('device_id')} online={raw_single.get('online')}")

            # Also check desktop-pc if expected_device_id != desktop-pc
            if expected_device_id != "desktop-pc":
                print("[5b] tools/call get_device_activity (desktop-pc) ...")
                call_desktop = await session.call_tool("get_device_activity", arguments={"device_id": "desktop-pc"})
                raw_desk = None
                for item in (call_desktop.content or []):
                    if hasattr(item, "text"):
                        try:
                            raw_desk = json.loads(item.text)
                        except Exception:
                            raw_desk = item.text
                        break
                if not isinstance(raw_desk, dict) or not raw_desk.get("ok"):
                    print(f"    FAIL: get_device_activity (desktop-pc) failed: {raw_desk}")
                    return False, None
                print(f"    PASS  : device {raw_desk.get('device_id')} online={raw_desk.get('online')}")

            # 6. tools/call get_context
            print("[6] tools/call get_context ...")
            call_ctx = await session.call_tool("get_context", arguments={})
            raw_ctx = None
            for item in (call_ctx.content or []):
                if hasattr(item, "text"):
                    try:
                        raw_ctx = json.loads(item.text)
                    except Exception:
                        raw_ctx = item.text
                    break
            print(f"    context response:\n{json.dumps(raw_ctx, ensure_ascii=False, indent=6) if isinstance(raw_ctx, dict) else raw_ctx}")
            if not isinstance(raw_ctx, dict) or not raw_ctx.get("ok"):
                print(f"    FAIL: get_context failed: {raw_ctx}")
                return False, None
            assert raw_ctx.get("fusion_version") == "context-v1"
            assert "active_devices" in raw_ctx
            assert "primary_device" in raw_ctx
            assert "simultaneous_usage" in raw_ctx
            assert "devices" in raw_ctx
            assert "reason_codes" in raw_ctx
            print(f"    PASS  : get_context fusion_version={raw_ctx.get('fusion_version')}, active={raw_ctx.get('active_devices')}, primary={raw_ctx.get('primary_device')}")

            return True, raw

    return False, None


async def main() -> int:
    try:
        passed, snap = await run_mcp_e2e()
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    print()
    print("=" * 50)
    print(f"MCP Protocol E2E: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
