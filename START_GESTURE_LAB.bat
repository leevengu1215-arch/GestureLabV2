@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  py -3 start_windows.py
  goto :done
)

where python >nul 2>nul
if not errorlevel 1 (
  python start_windows.py
  goto :done
)

echo [Gesture Lab] Python 3 was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo During installation, enable "Add python.exe to PATH".
pause
exit /b 1

:done
if errorlevel 1 pause
