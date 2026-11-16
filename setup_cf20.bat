@echo off
rem GeoBox one-click setup for a Windows workstation (CF-20).
rem Finds Python (system or Houdini's), installs the Houdini package, checks the
rem sample project, runs the unit tests and writes setup_report.txt.
setlocal
cd /d "%~dp0"
title GeoBox setup
call "%~dp0tools\find_python.bat"
if not defined GEOBOX_PY goto :nopython
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
chcp 65001 >nul
"%GEOBOX_PY%" "%~dp0tools\setup_cf20.py" %*
set "RC=%ERRORLEVEL%"
echo.
if /i not "%~1"=="--quick" pause
exit /b %RC%

:nopython
echo [GeoBox] Python 3.9+ was not found.
echo          Install Houdini (its bundled Python is used automatically)
echo          or Python from https://www.python.org/downloads/ ("Add python.exe to PATH"), then run this again.
pause
exit /b 1
