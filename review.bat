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

echo Review page: http://127.0.0.1:8765
echo Close this window or press Ctrl+C to stop the server.
echo.
%PY% -m xianyu review %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
exit /b %CODE%
