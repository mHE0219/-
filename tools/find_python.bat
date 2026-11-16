@echo off
rem Sets GEOBOX_PY to a Python >= 3.9 (no setlocal on purpose: the caller keeps the variable).
rem Order: GEOBOX_PY already set > Python launcher (py -3) > Houdini's bundled python > python on PATH.
if defined GEOBOX_PY if exist "%GEOBOX_PY%" exit /b 0
set "GEOBOX_PY="

where py >nul 2>nul
if errorlevel 1 goto :houdini
for /f "usebackq delims=" %%P in (`py -3 -c "import sys;print(sys.executable if sys.version_info>=(3,9) else '')" 2^>nul`) do set "GEOBOX_PY=%%P"
if defined GEOBOX_PY exit /b 0

:houdini
if defined HFS for /d %%Q in ("%HFS%\python3*") do if exist "%%~Q\python.exe" set "GEOBOX_PY=%%~Q\python.exe"
if defined GEOBOX_PY exit /b 0
for /d %%D in ("%ProgramFiles%\Side Effects Software\Houdini *") do for /d %%Q in ("%%~D\python3*") do if exist "%%~Q\python.exe" set "GEOBOX_PY=%%~Q\python.exe"
if defined GEOBOX_PY exit /b 0

where python >nul 2>nul
if errorlevel 1 exit /b 1
for /f "usebackq delims=" %%P in (`python -c "import sys;print(sys.executable if sys.version_info>=(3,9) else '')" 2^>nul`) do set "GEOBOX_PY=%%P"
if defined GEOBOX_PY exit /b 0
exit /b 1
