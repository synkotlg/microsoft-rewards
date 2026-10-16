@echo off
rem Premiere connexion au compte Microsoft (a refaire seulement si la session expire).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Installation de l'environnement Python...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :error
)
".venv\Scripts\python.exe" rewards.py --setup
pause
exit /b 0

:error
echo Echec de l'installation. Verifie que Python est installe.
pause
exit /b 1
