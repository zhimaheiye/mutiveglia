@echo off
title Stop Veglia Services
cd /d "%~dp0"
echo Stopping Veglia Server and MCP services...
schtasks /End /TN "VegliaServer" >nul 2>&1
schtasks /End /TN "VegliaMCP" >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8513" ^| findstr "LISTENING"') do (
    echo Stopping PID %%a on port 8513...
    taskkill /F /PID %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8514" ^| findstr "LISTENING"') do (
    echo Stopping PID %%a on port 8514...
    taskkill /F /PID %%a >nul 2>&1
)
echo Veglia Server (8513) and MCP (8514) stopped cleanly.
pause
