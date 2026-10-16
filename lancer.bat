@echo off
rem Lance les quetes maintenant (Edge travaille sur un bureau virtuel cache).
cd /d "%~dp0"
".venv\Scripts\python.exe" rewards.py %*
pause
