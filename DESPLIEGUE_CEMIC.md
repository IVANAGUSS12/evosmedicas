# Despliegue en servidores CEMIC

> El modo operativo actual previsto es una instalación local por cada usuario.
> Este documento queda como alternativa para una futura centralización.

## Arquitectura

- Una instancia web servida por Waitress.
- Una cola común para todos los usuarios.
- Varios Chromium simultáneos, controlados por `MAX_AUTOMATIZACIONES`.
- Una carpeta independiente por usuario, paciente y solicitud.
- Base SQLite en modo WAL para accesos concurrentes.
- Credenciales del Hospital únicamente en memoria durante la espera y
  ejecución.

Los usuarios pueden cargar y enviar varios pacientes sin esperar. Cuando todos
los cupos están ocupados, las solicitudes muestran `EN COLA` y arrancan
automáticamente.

La aplicación debe ejecutarse como un único proceso de `server.py`. La
concurrencia se aumenta mediante la configuración, no iniciando varias copias
del servidor.

## Capacidad inicial sugerida

| Memoria | CPU sugerida | Chromium simultáneos |
| --- | ---: | ---: |
| 16 GB | 4 núcleos | 2 |
| 32 GB | 8 núcleos | 4 |
| 48-64 GB | 12 núcleos | 6 |

Son valores iniciales. Sistemas debe observar RAM, CPU, respuesta del Hospital
y tasa de errores antes de subir el límite.

## Instalación

1. Instalar Python 3.12, 3.13 o 3.14 de 64 bits.
2. Clonar o copiar el repositorio.
3. Ejecutar `INSTALAR.bat`.
4. Editar `configuracion.bat`.
5. Ejecutar `INICIAR.bat`.
6. Verificar `http://servidor:5010/salud`.

Para inicio automático, registrar `INICIAR.bat` como tarea programada al
iniciar Windows, usando una cuenta de servicio.

## Configuración recomendada

Partir de `configuracion.example.bat` y ajustar:

```bat
set "APP_HOST=0.0.0.0"
set "APP_PORT=5010"
set "SECRETARIO_DATA_DIR=D:\SecretarioSalaDatos"
set "MAX_AUTOMATIZACIONES=4"
set "MAX_TRABAJOS_POR_USUARIO=10"
set "HTTP_THREADS=24"
set "PLAYWRIGHT_HEADLESS=1"
set "PLAYWRIGHT_RECORD_VIDEO=0"
set "SECRETARIO_SECRET=UNA_CLAVE_ALEATORIA_LARGA"
set "SESSION_COOKIE_SECURE=1"
set "ADMIN_INICIAL_USUARIO=admin"
set "ADMIN_INICIAL_CLAVE=UNA_CLAVE_INICIAL_SEGURA"
```

`SESSION_COOKIE_SECURE=1` debe usarse cuando la publicación sea exclusivamente
HTTPS.

## Red y seguridad

- El servidor debe resolver y acceder a
  `saavedra-ts.cemic1.edu.ar` desde la red interna.
- Los puestos deben poder acceder al puerto publicado.
- Publicar detrás del proxy HTTPS institucional.
- No exponer Waitress directamente a Internet.
- Limitar los permisos NTFS de `SECRETARIO_DATA_DIR` a la cuenta del servicio
  y administradores autorizados.
- Definir una política institucional de retención y borrado de PDFs.
- Respaldar `secretario.db` y `descargas`.
- Mantener `configuracion.bat` fuera de Git.

## Reinicios

Si el servicio se reinicia, los trabajos en cola o ejecución quedan marcados
como interrumpidos y pueden relanzarse. Las credenciales no se recuperan porque
deliberadamente no se guardan en disco.

## Actualización

1. Esperar a que `/salud` indique `en_proceso: 0`.
2. Detener el servicio.
3. Respaldar `SECRETARIO_DATA_DIR`.
4. Actualizar el repositorio.
5. Ejecutar nuevamente `INSTALAR.bat`.
6. Iniciar y verificar `/salud`.
