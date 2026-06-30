# Secretario de Sala

Aplicación local para preparar documentación de pacientes internados. Cada
usuario la instala en su propia PC. Automatiza el sistema Hospital con
Playwright/Chromium, organiza los PDFs y permite procesar 2, 3 o 4 pacientes al
mismo tiempo según la capacidad elegida.

## Funciones

- Instalación independiente por cada PC.
- Usuarios locales y administración de accesos.
- Carga por número de internación y DNI.
- Pacientes activos o dados de alta.
- Toda la internación o rango de fechas.
- Descarga de informe de hospitalización, documentación, evoluciones,
  epicrisis, indicaciones y prescripciones.
- Evoluciones divididas en archivos de hasta 5 páginas del sistema para
  reducir bloqueos durante la generación.
- Barra de progreso y posición en cola.
- Dos, tres o cuatro Chromium simultáneos según la capacidad de la PC.
- Archivos y trabajos visibles únicamente para el usuario que los creó.
- Apertura, descarga e impresión individual de cada PDF.
- Descarga ZIP e impresión conjunta de toda la documentación del paciente.

## Requisitos

- Windows 10/11 o Windows Server de 64 bits.
- Python 3.12, 3.13 o 3.14.
- Acceso de red al sistema Hospital de CEMIC.
- Git, si se instala clonando este repositorio.

## Instalación desde GitHub

### Descargando el ZIP

1. En GitHub, seleccionar `Code` → `Download ZIP`.
2. Extraer completamente el ZIP en una carpeta local.
3. Abrir esa carpeta y ejecutar `INSTALAR.bat`.

No ejecutar la aplicación directamente dentro del archivo ZIP.

### Clonando con Git

```powershell
git clone URL_DEL_REPOSITORIO secretario-sala
cd secretario-sala
.\INSTALAR.bat
```

El instalador:

1. crea `.venv`;
2. instala todo lo indicado en `requirements.txt`;
3. instala Chromium de Playwright;
4. pregunta si la PC procesará 2, 3 o 4 pacientes simultáneos;
5. crea la configuración local.

Después, ejecutar:

```powershell
.\INICIAR.bat
```

La aplicación abre automáticamente el navegador en
`http://127.0.0.1:5010`.

En la primera instalación, si `ADMIN_INICIAL_CLAVE` está vacío, la contraseña
inicial aleatoria se muestra una sola vez en la consola.

## Procesamiento simultáneo

Cada PC tiene su propia cola:

- Si se configura en 2, procesa dos pacientes y deja el resto esperando.
- Si se configura en 3, procesa tres pacientes.
- Si se configura en 4, procesa cuatro pacientes.
- Cuando termina uno, el siguiente comienza automáticamente.

Capacidad orientativa:

| Pacientes simultáneos | Memoria recomendada |
| ---: | ---: |
| 2 | 16 GB |
| 3 | 24 GB |
| 4 | 32 GB |

Para cambiar la capacidad posteriormente, cerrar la aplicación y ejecutar:

```powershell
.\CONFIGURAR_PC.bat
```

No se deben abrir varias copias de `INICIAR.bat`: una sola instancia maneja
todos los Chromium y la cola.

## Configuración

La configuración local está en `configuracion.bat`, archivo excluido de Git.
Las opciones principales son:

- `APP_HOST` y `APP_PORT`.
- `SECRETARIO_DATA_DIR`: base, PDFs y logs fuera del código.
- `MAX_AUTOMATIZACIONES`: 2, 3 o 4 Chromium simultáneos en esa PC.
- `MAX_TRABAJOS_POR_USUARIO`: solicitudes simultáneas por usuario.
- `PLAYWRIGHT_HEADLESS=1`: ejecución invisible en servidor.
- `EVOLUCIONES_PAGINAS_POR_ARCHIVO=5`: cantidad máxima de páginas del sistema
  seleccionadas para generar cada PDF de evoluciones.
- `SECRETARIO_SECRET`: clave de sesión obligatoria en producción.
- `SESSION_COOKIE_SECURE=1`: activar cuando se publique exclusivamente por
  HTTPS.

Cada instalación guarda su información en su propia carpeta `datos`. Los
pacientes y PDFs de una PC no se comparten con las demás.

Para una futura instalación central en infraestructura institucional,
consultar [DESPLIEGUE_CEMIC.md](DESPLIEGUE_CEMIC.md).

## Datos que no se suben

`.gitignore` excluye:

- entorno virtual;
- configuración y secretos;
- base SQLite;
- PDFs descargados;
- logs;
- cachés de Python.

Las credenciales del Hospital usadas para cada trabajo permanecen en memoria y
no se guardan en la base ni en archivos.

## Comprobación

Con el servidor iniciado:

```text
http://servidor:5010/salud
```

Debe responder con `estado: ok` y mostrar la concurrencia configurada.
