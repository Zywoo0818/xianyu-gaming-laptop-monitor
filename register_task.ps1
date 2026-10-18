# 注册 / 卸载 Windows 定时任务
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\register_task.ps1            # 每 20 分钟跑一次
#   powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -Minutes 30
#   powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -Unregister

param(
    [int]$Minutes = 20,
    [string]$TaskName = "XianyuScan",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $root "run_scan_scheduled.bat"

if (-not (Test-Path $bat)) {
    Write-Host "找不到 $bat" -ForegroundColor Red
    exit 1
}

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除定时任务：$TaskName" -ForegroundColor Green
    exit 0
}

if ($Minutes -lt 10) {
    Write-Host "间隔太短容易触发风控，建议 >= 15 分钟。" -ForegroundColor Yellow
}

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "闲鱼笔记本低价监控" -Force | Out-Null

Write-Host "已注册定时任务：$TaskName（每 $Minutes 分钟）" -ForegroundColor Green
Write-Host "日志：$root\logs\scan.log"
Write-Host "查看任务：Get-ScheduledTask -TaskName $TaskName"
