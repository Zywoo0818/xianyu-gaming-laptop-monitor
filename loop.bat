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

echo 控制台命令：start 跑第一轮，next 跑下一轮（纯手动），stop 立即停止（本轮不出报表），report 打开报表，exit 退出。
echo 每轮默认搜索 2 页（可用 --pages 改）；默认不打开卖家主页；没有任何自动续跑。
echo 相邻两轮默认至少间隔 300 秒；提前输入 next 只提示，再输一次才放行。每日预算落盘累计。
echo.
%PY% -m xianyu loop %*
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" echo [X] loop failed, exit code %CODE%
pause
exit /b %CODE%
