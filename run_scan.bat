@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [X] .venv not found. Follow README "install" section first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m xianyu scan %*
set CODE=%ERRORLEVEL%
if not "%CODE%"=="0" (
  echo.
  echo [X] scan failed, exit code %CODE%
  pause
)
exit /b %CODE%
