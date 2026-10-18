@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [X] .venv not found. Follow README "install" section first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m xianyu review %*
