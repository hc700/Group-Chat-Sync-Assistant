@echo off
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 goto nopython

python "wechat_group_relay.py"
if errorlevel 1 (
    echo.
    echo [!] 程序出错退出，请查看上方提示。
    pause
)
exit /b 0

:nopython
echo.
echo [!] 未检测到 Python。
echo     请先安装 Python 3.8 或更高版本：https://www.python.org/downloads/
echo     安装时勾选 "Add Python to PATH"。
echo.
pause
exit /b 1
