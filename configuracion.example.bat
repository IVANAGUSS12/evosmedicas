@echo off
rem Copiar este archivo como configuracion.bat.

set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUNBUFFERED=1"

rem Red
set "APP_HOST=127.0.0.1"
set "APP_PORT=5010"
set "APP_URL=http://127.0.0.1:5010"

rem Datos persistentes: base, PDFs, secreto local y logs.
set "SECRETARIO_DATA_DIR=%~dp0datos"

rem Capacidad local. Usar CONFIGURAR_PC.bat para elegir 2, 3 o 4.
set "MAX_AUTOMATIZACIONES=2"
set "MAX_TRABAJOS_POR_USUARIO=20"
set "HTTP_THREADS=16"

rem Playwright
set "HOSPITAL_LOGIN_URL=http://saavedra-ts.cemic1.edu.ar/HOSPITAL/pages/login.faces"
set "HOSPITAL_ADMISION_URL=http://saavedra-ts.cemic1.edu.ar/HOSPITAL/pages/admisionInternados/modificacionAdmisionInternado/inicio.faces"
set "PLAYWRIGHT_HEADLESS=0"
set "PLAYWRIGHT_RECORD_VIDEO=0"
set "PLAYWRIGHT_SLOW_MO_MS=220"
set "PLAYWRIGHT_TIMEOUT_MS=35000"
set "EVOLUCIONES_PAGINAS_POR_ARCHIVO=5"

rem Seguridad
rem En producción, generar una clave larga y aleatoria.
rem Si queda vacío, la aplicación crea una clave persistente en datos.
set "SECRETARIO_SECRET="
set "SESSION_COOKIE_SECURE=0"

rem Se usan únicamente cuando la base se crea por primera vez.
set "ADMIN_INICIAL_USUARIO=admin"
set "ADMIN_INICIAL_CLAVE="
