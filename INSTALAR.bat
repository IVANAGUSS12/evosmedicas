@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creando entorno virtual...
  py -3 -m venv .venv || goto :error
)

echo Instalando dependencias Python...
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error

echo Instalando Chromium de Playwright...
".venv\Scripts\python.exe" -m playwright install chromium || goto :error

if not exist "configuracion.bat" call "CONFIGURAR_PC.bat"

echo.
echo Instalacion terminada. Ejecutar INICIAR.bat
exit /b 0

:error
echo.
echo La instalacion fallo. Revisar el mensaje anterior.
exit /b 1
