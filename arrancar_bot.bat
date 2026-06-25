@echo off
title Servidor Trading Bot - FTMO
echo ===================================================
echo   INICIANDO BOT DE TRADING EN ENTORNO VIRTUAL
echo ===================================================
echo.

:: 1. Ir a la carpeta del proyecto
cd /d "C:\Users\raul\source\repos\Python.TraidingBot.Service"

:: 2. Activar el entorno virtual nativo
call venv\Scripts\activate

:: 3. Ejecutar el script principal
python main.py

:: Si el bot se cae por algún motivo, la ventana no se cerrará sola y verás el error
echo.
echo [ALERTA] El bot se ha detenido de forma inesperada.
pause