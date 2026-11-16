@echo off
rem Run the GeoBox self-test inside real Houdini (hython) and open the report.
rem Send selftest\selftest_report.txt back if anything says FAIL.
setlocal
cd /d "%~dp0"
title GeoBox Houdini self-test
if not exist "%~dp0tools\geobox_env.bat" call "%~dp0setup_cf20.bat" --quick
if not exist "%~dp0tools\geobox_env.bat" goto :nosetup
call "%~dp0tools\geobox_env.bat"
chcp 65001 >nul
"%GEOBOX_PY%" "%~dp0tools\run_selftest.py" %*
set "RC=%ERRORLEVEL%"
if exist "%~dp0selftest\selftest_report.txt" start "" notepad "%~dp0selftest\selftest_report.txt"
echo.
pause
exit /b %RC%

:nosetup
echo [GeoBox] setup did not finish - run setup_cf20.bat and read setup_report.txt
pause
exit /b 1
