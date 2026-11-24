@echo off
title MyLauncher

echo === MyLauncher ===

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

echo Installing dependencies if missing...
%PYTHON_CMD% -m pip install -r requirements.txt --quiet

echo Starting launcher...
%PYTHON_CMD% launcher.py

if errorlevel 1 (
    echo.
    echo [ERROR] Launcher exited with an error. See messages above.
    pause
)
