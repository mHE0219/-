@echo off
rem Run a GeoBox farm worker on this machine (name = computer name, e.g. CF-20).
rem Farm root = shared folder used by all workers: set GEOBOX_FARM_ROOT, or type it when asked.
setlocal
cd /d "%~dp0"
title GeoBox farm worker %COMPUTERNAME%
if not exist "%~dp0tools\geobox_env.bat" call "%~dp0setup_cf20.bat" --quick
if not exist "%~dp0tools\geobox_env.bat" goto :nosetup
call "%~dp0tools\geobox_env.bat"
chcp 65001 >nul
if not defined GEOBOX_FARM_ROOT set /p "GEOBOX_FARM_ROOT=Farm root (shared folder, e.g. \\nas\geobox_farm; Enter = local .\farm): "
if not defined GEOBOX_FARM_ROOT set "GEOBOX_FARM_ROOT=%~dp0farm"
if "%GEOBOX_FARM_ROOT:~-1%"=="\" set "GEOBOX_FARM_ROOT=%GEOBOX_FARM_ROOT:~0,-1%"
echo Worker %COMPUTERNAME% on "%GEOBOX_FARM_ROOT%"  (Ctrl+C to stop)
"%GEOBOX_PY%" -m geobox farm --root "%GEOBOX_FARM_ROOT%" worker --name "%COMPUTERNAME%" %*
echo.
pause
exit /b 0

:nosetup
echo [GeoBox] setup did not finish - run setup_cf20.bat and read setup_report.txt
pause
exit /b 1
