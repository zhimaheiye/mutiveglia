@echo off
title Stop Veglia Services
cd /d "%~dp0"
echo ========================================================
echo   Veglia - deliberate stop (maintenance mode ON)
echo ========================================================
REM Tell the watchdog this is intentional, otherwise the 5-minute patrol
REM will immediately start everything back up again.
REM Clear it again with:  ops\veglia_maintenance.ps1 -Off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\ops\veglia_maintenance.ps1" -On -Reason "stop_server.bat" -Hours 8
echo.
echo Stopping Veglia Server, MCP and Sensor services...
schtasks /End /TN "VegliaDesktopSensor" >nul 2>&1
schtasks /End /TN "VegliaServer" >nul 2>&1
schtasks /End /TN "VegliaMCP" >nul 2>&1
wmic process where "name='pythonw.exe' and commandline like '%%device_sensor.py%%'" call terminate >nul 2>&1
wmic process where "name='python.exe' and commandline like '%%device_sensor.py%%'" call terminate >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8513" ^| findstr "LISTENING"') do (
    echo Stopping PID %%a on port 8513...
    taskkill /F /PID %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8514" ^| findstr "LISTENING"') do (
    echo Stopping PID %%a on port 8514...
    taskkill /F /PID %%a >nul 2>&1
)
echo.
echo Veglia Server (8513), MCP (8514) and Sensor stopped cleanly.
echo Maintenance flag stays ON for 8h. To come back to normal operation:
echo   1) start the services (scheduled tasks VegliaServer then VegliaMCP, or this folder's start_server.bat)
echo   2) clear the flag:  powershell -ExecutionPolicy Bypass -File "%~dp0..\ops\veglia_maintenance.ps1" -Off
pause
