@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1" %*
set "stack_result=%ERRORLEVEL%"
if not "%stack_result%"=="0" pause
exit /b %stack_result%
