@echo off
rem Airplane alarm launcher for Windows: starts the server and opens the browser.
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 app.py %*
) else (
    python app.py %*
)
pause
