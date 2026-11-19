@echo off
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 goto nopython

python "wechat_group_sync.py"
if errorlevel 1 (
    echo.
    echo [!] Program exited with an error. Check the message above.
    pause
)
exit /b 0

:nopython
echo.
echo [!] Python not found on this computer.
echo     Please install Python 3.8 or newer: https://www.python.org/downloads/
echo     During installation, tick "Add Python to PATH".
echo.
pause
exit /b 1
