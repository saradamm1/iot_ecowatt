@echo off
chcp 65001 >nul
cd /d "%~dp0"
title EcoWatt - prototipo

if not exist .venv (
    echo Criando ambiente virtual Python...
    py -3.13 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Nao foi possivel criar o ambiente. Verifique se o Python esta instalado.
        pause
        exit /b 1
    )
    echo Instalando dependencias, aguarde...
    .venv\Scripts\python -m pip install --upgrade pip >nul
    .venv\Scripts\python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Falha ao instalar as dependencias.
        pause
        exit /b 1
    )
)

.venv\Scripts\python iniciar.py %*
pause
