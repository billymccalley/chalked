@echo off
echo Stopping Chalked on port 8080...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do taskkill /PID %%a /F
pause

