@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "tools\start_chrome.py" %*
) else (
    python "tools\start_chrome.py" %*
)
if errorlevel 1 pause
endlocal
