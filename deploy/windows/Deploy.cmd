@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Deploy-Yuki.ps1"
set "YUKI_RESULT=%ERRORLEVEL%"
if not "%YUKI_RESULT%"=="0" echo Deployment paused or failed. Read the message above, then retry this file.
pause
exit /b %YUKI_RESULT%
