@echo off
REM Telegram Toolbox launcher - double-click to open the menu
chcp 65001 >NUL
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

where python >NUL 2>&1
if %ERRORLEVEL%==0 (
    python telegram_tool.py
) else if exist "C:\Python313\python.exe" (
    "C:\Python313\python.exe" telegram_tool.py
) else (
    echo Python not found on PATH. Install Python or edit this file with the full path.
)

echo.
pause
