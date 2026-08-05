@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 goto :use_py

where python >nul 2>nul
if not errorlevel 1 goto :use_python

echo [Gesture Lab] Python 3 was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo During installation, enable "Add python.exe to PATH".
pause
exit /b 1

:use_py
if "%~1"=="" (
  py -3 update_windows.py
) else (
  py -3 update_windows.py "%~1"
)
goto :result

:use_python
if "%~1"=="" (
  python update_windows.py
) else (
  python update_windows.py "%~1"
)

:result
if errorlevel 1 goto :failed
echo.
echo [Gesture Lab] Update completed. Starting the console...
start "" start_windows.bat
exit /b 0

:failed
echo.
echo [Gesture Lab] Update was not completed. Existing experiment data is unchanged.
pause
exit /b 1
