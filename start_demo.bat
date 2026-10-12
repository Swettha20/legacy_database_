@echo off
rem Double-click to start everything for a demo (containers, databases, AI, API, page).
cd /d "%~dp0"
call venv\Scripts\activate.bat
python start_demo.py
pause
