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

echo 将打开浏览器，请用闲鱼 App 扫码登录。
echo 登录成功后脚本自动继续（最多等 5 分钟，可用 --timeout 改）。
echo 登录态保存在 data/browser_profile，之后 scan / loop 都带着它。
echo.
%PY% -m xianyu login %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo [X] login failed, exit code %CODE%
)
echo.
pause
exit /b %CODE%
