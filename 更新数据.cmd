@echo off
REM ============================================================
REM  LifeLogging - refresh today's data (double-click me)
REM
REM  Why this file is ASCII-only:
REM    A .cmd containing Chinese text gets mangled by cmd.exe on
REM    a GBK console. Same class of bug that once broke the .ps1
REM    files (see README "实测修掉的 5 个真实缺陷"). All Chinese
REM    output comes from the steps this launches -- Python and
REM    PowerShell both set UTF-8 for themselves.
REM
REM  What it runs:
REM    scripts\run_daily.ps1 -Date today -SkipAI
REM  i.e. collect -> classify -> side notes for TODAY, without
REM  paying for an AI journal call. The 23:00 scheduled task
REM  still does the journal.
REM
REM  Want the journal here too? Delete the -SkipAI below.
REM  Costs roughly CNY 0.05 per run.
REM ============================================================

setlocal
cd /d "%~dp0"

echo ============================================================
echo   LifeLogging - refresh data for today
echo ============================================================
echo.
echo   collect -^> classify -^> write side notes
echo   no AI call (the journal stays with the 23:00 task)
echo   takes 1-3 minutes; this window stays open when done
echo.

powershell -NoProfile -File "scripts\run_daily.ps1" -Date today -SkipAI
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" goto failed
echo   [OK] Done. Reload http://localhost:3001
goto done

:failed
echo   [FAIL] exit %RC%
echo          Details: state\logs\ - newest run_*.log
echo          One failing step does not stop the others,
echo          so the data may still be partly refreshed.

:done
echo.
pause
endlocal
