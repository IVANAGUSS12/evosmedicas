@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo La aplicacion no esta instalada. Ejecutar INSTALAR.bat
  exit /b 1
)

if not exist "configuracion.bat" (
  echo Falta configuracion.bat
  echo Copiar configuracion.example.bat como configuracion.bat y revisarlo.
  exit /b 1
)

call "configuracion.bat"
if not defined APP_URL set "APP_URL=http://127.0.0.1:%APP_PORT%"
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%APP_URL%'"
".venv\Scripts\python.exe" server.py
