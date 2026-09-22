@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem The independent opener waits for this run's fresh snapshot and a healthy server.
start "" /b powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0open_dashboard_when_ready.ps1" -BaseUrl "http://127.0.0.1:8765/" -SnapshotPath "%~dp0market_sentiment_intraday.csv" -ChromeLauncherPath "%~dp0open_dashboard_chrome.ps1" -FailureSignalPath "%~dp0.dashboard_startup_failed" -TimeoutSeconds 600 -SuccessSignalPath "%~dp0.dashboard_opened"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_dashboard.ps1"
if errorlevel 1 (
    echo.
    echo Dashboard launcher failed. Review the error shown above.
    pause
)
