@echo off
title Build MyLauncher.exe

echo === Build MyLauncher.exe (PyInstaller) ===
echo Run this once on Windows with Python installed.
echo.

set "PYTHON_CMD="
where python >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    where py >nul 2>nul && set "PYTHON_CMD=py"
)
if not defined PYTHON_CMD (
    echo [ERROR] Python not found in PATH.
    echo Install it from https://www.python.org/downloads/
    echo and check "Add Python to PATH" during setup.
    pause
    exit /b 1
)

echo Installing dependencies...
%PYTHON_CMD% -m pip install -r requirements.txt --quiet || goto :error
%PYTHON_CMD% -m pip install pyinstaller --quiet || goto :error

echo.
echo Building exe (takes 1-3 minutes)...
%PYTHON_CMD% -m PyInstaller --onefile --noconsole --clean --name MyLauncher --collect-all customtkinter launcher.py || goto :error

echo.
echo ============================================
echo  Done! Your file is here:  dist\MyLauncher.exe
echo  You can move it anywhere and run it by
echo  double click - Python is no longer needed.
echo ============================================
pause
exit /b 0

:error
echo.
echo [ERROR] Build failed. Check the error message above.
pause
exit /b 1
