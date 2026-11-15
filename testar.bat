@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
    echo Execute primeiro o iniciar.bat para instalar as dependencias.
    pause
    exit /b 1
)
.venv\Scripts\python -m pytest -v
pause
