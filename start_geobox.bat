@echo off
rem Start the GeoBox web preview and open the browser.  Ctrl+C (or close the window) to stop.
setlocal
cd /d "%~dp0"
title GeoBox web preview
if not exist "%~dp0tools\geobox_env.bat" call "%~dp0setup_cf20.bat" --quick
if not exist "%~dp0tools\geobox_env.bat" goto :nosetup
call "%~dp0tools\geobox_env.bat"
chcp 65001 >nul
"%GEOBOX_PY%" "%~dp0tools\start_geobox.py" %*
echo.
pause
exit /b 0

:nosetup
echo [GeoBox] setup did not finish - run setup_cf20.bat and read setup_report.txt
pause
exit /b 1
