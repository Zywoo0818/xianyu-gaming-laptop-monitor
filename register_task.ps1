# 可选：注册 / 卸载 Windows 定时任务
# 日常建议需要时手动运行 scan.bat；自动轮询会增加闲鱼风控概率。
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\register_task.ps1            # 每 60 分钟跑一次（可选）
#   powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -Minutes 90
#   powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -Unregister

param(
    [int]$Minutes = 60,
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

if ($Minutes -lt 30) {
    Write-Host "注意：间隔低于 30 分钟会明显增加风控概率，建议按需手动运行 scan.bat。" -ForegroundColor Yellow
}
Write-Host "提示：日常推荐需要时手动运行 scan.bat；定时任务仅作可选兜底。" -ForegroundColor Yellow

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "闲鱼笔记本低价监控（可选定时，建议手动运行）" -Force | Out-Null

Write-Host "已注册定时任务：$TaskName（每 $Minutes 分钟）" -ForegroundColor Green
Write-Host "日志：$root\logs\scan-YYYY-MM-DD.log（按天切分）"
Write-Host "查看任务：Get-ScheduledTask -TaskName $TaskName"
Write-Host "手动运行：$root\scan.bat"
