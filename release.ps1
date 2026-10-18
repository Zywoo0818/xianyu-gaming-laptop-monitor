<#
.SYNOPSIS
  发布一个新版本：改版本号 → 提交 → 打 tag → 推送。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\release.ps1 1.0.1 "修正 4060 中位价"
#>
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Note = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "版本号格式必须是 1.0.1 这样"
}
$current = (Get-Content VERSION -Raw).Trim()
if ($Version -eq $current) {
    throw "版本号没变（当前 $current）"
}

$utf8 = New-Object System.Text.UTF8Encoding($false)

# 1. 版本号
$initPath = Join-Path $root "xianyu\__init__.py"
$init = [System.IO.File]::ReadAllText($initPath, $utf8)
$init = [regex]::Replace($init, '__version__\s*=\s*".*?"', "__version__ = `"$Version`"")
[System.IO.File]::WriteAllText($initPath, $init, $utf8)
[System.IO.File]::WriteAllText((Join-Path $root "VERSION"), "$Version`n", $utf8)

# 2. 更新日志
$changelogPath = Join-Path $root "CHANGELOG.md"
$changelog = [System.IO.File]::ReadAllText($changelogPath, $utf8)
$entry = "## v$Version — $(Get-Date -Format 'yyyy-MM-dd')`n`n$Note`n`n"
$marker = "# 更新日志`n`n"
if ($changelog.StartsWith($marker)) {
    $changelog = $marker + $entry + $changelog.Substring($marker.Length)
} else {
    $changelog = $entry + $changelog
}
[System.IO.File]::WriteAllText($changelogPath, $changelog, $utf8)

# 3. 提交 + 打 tag
git add -A
git commit -m "release: v$Version"
git tag -a "v$Version" -m "v$Version"

# 4. 推送
git push origin main
git push origin "v$Version"

Write-Host "`n已发布 v$Version" -ForegroundColor Green
Write-Host "回退命令：git checkout v$Version"
