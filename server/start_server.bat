@echo off
title Veglia Server
cd /d "%~dp0"
echo ========================================================
echo   Starting Veglia Companion Server on port 8513...
echo ========================================================
REM Leaving maintenance mode: this file is the operator starting things on purpose,
REM so the watchdog is allowed to resume automatic recovery. It starts the MCP
REM task itself if 8514 is still down (core first, then mcp).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\ops\veglia_maintenance.ps1" -Off -NoStart
"%~dp0..\.venv-mcp\Scripts\python.exe" veglia_server.py
pause
