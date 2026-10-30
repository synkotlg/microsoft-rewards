@echo off
rem Configure les notifications Telegram : cree un bot et enregistre .env (a ne faire qu'une fois).
cd /d "%~dp0"
".venv\Scripts\python.exe" setup_telegram.py %*
pause
