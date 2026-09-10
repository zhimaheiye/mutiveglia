@echo off
title Veglia Server
cd /d "%~dp0"
echo ========================================================
echo   Starting Veglia Companion Server on port 8513...
echo ========================================================
"%~dp0..\.venv-mcp\Scripts\python.exe" veglia_server.py
pause
