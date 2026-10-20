@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY set "PY=python"
set "PYTHONIOENCODING=utf-8"

if not exist "logs" mkdir "logs"

rem scheduled task only: hidden, no pause, append to log
%PY% -m xianyu scan --no-notify %* >> "logs\scan.log" 2>&1
exit /b %ERRORLEVEL%
