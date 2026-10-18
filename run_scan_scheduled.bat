@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "logs" mkdir "logs"
if not exist ".venv\Scripts\python.exe" exit /b 1

rem scheduled task only: hidden, no pause, append to log
".venv\Scripts\python.exe" -m xianyu scan --no-notify %* >> "logs\scan.log" 2>&1
exit /b %ERRORLEVEL%
