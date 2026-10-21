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

rem optional scheduled task only: hidden, no pause, append to daily log
rem daily use: run scan.bat manually
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "TODAY=%%i"
if not defined TODAY set "TODAY=unknown"
%PY% -m xianyu scan %* >> "logs\scan-%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%
