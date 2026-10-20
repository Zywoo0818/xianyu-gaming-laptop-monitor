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

echo Opening Chrome. Scan the QR code with the Xianyu app.
echo.
%PY% -m xianyu login %*
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" echo [X] login failed, exit code %CODE%
pause
exit /b %CODE%
