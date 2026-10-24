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

echo Rebuilding the latest report and opening it in your browser...
%PY% -m xianyu report --open %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo [X] report failed, exit code %CODE%
  pause
)
exit /b %CODE%
