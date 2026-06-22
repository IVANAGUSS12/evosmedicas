@echo off
setlocal EnableExtensions
cd /d "%~dp0"

for /f "usebackq delims=" %%R in (`powershell -NoProfile -Command "$m=Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue; if($m){[math]::Round($m.TotalPhysicalMemory/1GB)}else{0}"`) do set "RAM_GB=%%R"

echo.
echo ==========================================
echo   CONFIGURACION DE SECRETARIO DE SALA
echo ==========================================
if not "%RAM_GB%"=="0" echo Memoria detectada: %RAM_GB% GB
echo.
echo 2 - Procesar hasta 2 pacientes simultaneos
echo 3 - Procesar hasta 3 pacientes simultaneos
echo 4 - Procesar hasta 4 pacientes simultaneos
echo.
echo Recomendacion:
echo   2 pacientes: 16 GB de RAM
echo   3 pacientes: 24 GB de RAM
echo   4 pacientes: 32 GB de RAM
echo.

choice /C 234 /N /M "Elegir capacidad [2/3/4]: "
if errorlevel 3 (
  set "CAPACIDAD=4"
) else if errorlevel 2 (
  set "CAPACIDAD=3"
) else (
  set "CAPACIDAD=2"
)

set /a HTTP_THREADS=CAPACIDAD*4+8

(
  echo @echo off
  echo set "PYTHONDONTWRITEBYTECODE=1"
  echo set "PYTHONUNBUFFERED=1"
  echo set "APP_HOST=127.0.0.1"
  echo set "APP_PORT=5010"
  echo set "APP_URL=http://127.0.0.1:5010"
  echo set "SECRETARIO_DATA_DIR=%%~dp0datos"
  echo set "MAX_AUTOMATIZACIONES=%CAPACIDAD%"
  echo set "MAX_TRABAJOS_POR_USUARIO=20"
  echo set "HTTP_THREADS=%HTTP_THREADS%"
  echo set "HOSPITAL_LOGIN_URL=http://saavedra-ts.cemic1.edu.ar/HOSPITAL/pages/login.faces"
  echo set "HOSPITAL_ADMISION_URL=http://saavedra-ts.cemic1.edu.ar/HOSPITAL/pages/admisionInternados/modificacionAdmisionInternado/inicio.faces"
  echo set "PLAYWRIGHT_HEADLESS=0"
  echo set "PLAYWRIGHT_RECORD_VIDEO=0"
  echo set "PLAYWRIGHT_SLOW_MO_MS=220"
  echo set "PLAYWRIGHT_TIMEOUT_MS=35000"
  echo set "EVOLUCIONES_PAGINAS_POR_ARCHIVO=5"
  echo set "SECRETARIO_SECRET="
  echo set "SESSION_COOKIE_SECURE=0"
  echo set "ADMIN_INICIAL_USUARIO=admin"
  echo set "ADMIN_INICIAL_CLAVE="
) > "configuracion.bat"

echo.
echo Configuracion guardada: %CAPACIDAD% pacientes simultaneos.
echo Los pacientes adicionales quedaran en cola y comenzaran solos.
echo.
pause
