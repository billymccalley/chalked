@echo off
cd /d "%~dp0"
echo Stopping anything already using port 8080...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do taskkill /PID %%a /F >nul 2>nul
echo Starting Chalked at http://127.0.0.1:8080/
python -m backend.chalked_backend.server
pause

