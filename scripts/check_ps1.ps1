# PowerShell 语法检查 —— 解析但不执行
#
# 为什么需要这个：.ps1 的语法错误在运行时才暴露，而且是那种
# 「Missing closing '}'」指向一个完全无关的行号的迷惑性报错。
# 用 Parser::ParseFile 可以只解析不执行，秒级定位真正的语法问题。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check_ps1.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$files = Get-ChildItem -Path $root -Filter "*.ps1" -Recurse -File |
         Where-Object { $_.FullName -notmatch '\\\.venv\\' }

if (-not $files) {
    Write-Host "没找到 .ps1 文件" -ForegroundColor Yellow
    exit 1
}

$totalErrors = 0

foreach ($f in $files) {
    $rel = $f.FullName.Substring($root.Length + 1)
    $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile(
        $f.FullName, [ref]$null, [ref]$parseErrors)

    if ($parseErrors -and $parseErrors.Count -gt 0) {
        Write-Host "FAIL  $rel" -ForegroundColor Red
        foreach ($e in $parseErrors) {
            Write-Host ("        第 $($e.Extent.StartLineNumber) 行: $($e.Message)" -replace '\s+', ' ')
            $src = $e.Extent.StartLineNumber
            $line = (Get-Content $f.FullName)[$src - 1]
            if ($line) { Write-Host "        > $($line.Trim())" -ForegroundColor DarkGray }
        }
        $totalErrors += $parseErrors.Count
    } else {
        Write-Host "PASS  $rel" -ForegroundColor Green
    }
}

Write-Host ""
if ($totalErrors -gt 0) {
    Write-Host "$totalErrors 个语法错误。修完再跑。" -ForegroundColor Red
    exit 1
}
Write-Host "全部通过。" -ForegroundColor Green
exit 0
