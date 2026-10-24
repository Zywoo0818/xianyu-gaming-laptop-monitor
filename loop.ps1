# 手动循环控制台：start 跑第一轮，next 跑下一轮，stop 立即停止（本轮不出报表），report 打开最新报表，exit 退出。
# 相邻两轮默认至少间隔 300 秒；提前 next 需再确认一次。每日预算落盘累计。
# 用法：powershell -ExecutionPolicy Bypass -File .\loop.ps1
# 可选：.\loop.ps1 --headed   或   .\loop.ps1 --rounds 3
Set-Location $PSScriptRoot
& (Join-Path $PSScriptRoot "loop.bat") @args
exit $LASTEXITCODE
