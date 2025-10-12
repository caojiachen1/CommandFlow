@echo off
setlocal ENABLEEXTENSIONS

chcp 65001 >nul

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

echo Launching screenshot automation GUI...
python main.py

set EXITCODE=%errorlevel%
if not %EXITCODE%==0 (
    echo.
    echo [WARNING] script exited with code %EXITCODE%.
    pause
    exit /b %EXITCODE%
)

echo.
echo Application closed normally.
pause
