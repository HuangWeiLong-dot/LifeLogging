# 编译 LifeLoggingLive.exe（无控制台的 WinExe）。
#
#   powershell -ExecutionPolicy Bypass -File windows\live-daemon\build.ps1
#
# 产物放在仓库根：LifeLoggingLive.exe。用 Windows 自带的 .NET Framework
# csc，不需要装任何 SDK / pip 包。

param(
    [string]$Root = ""
)

$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
if (-not $Root) {
    $Root = Split-Path -Parent (Split-Path -Parent $here)
}
$Root = (Resolve-Path $Root).Path

$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe"
}
if (-not (Test-Path $csc)) {
    Write-Host "FAIL csc.exe not found (.NET Framework 4.x missing?)" -ForegroundColor Red
    exit 1
}

$source = Join-Path $here "Program.cs"
$out = Join-Path $Root "LifeLoggingLive.exe"

Write-Host "-> csc: $csc"
Write-Host "-> out: $out"
& $csc /nologo /target:winexe /optimize+ "/out:$out" $source
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAIL compile failed" -ForegroundColor Red
    exit 1
}

Write-Host "OK   $out" -ForegroundColor Green
