import os
import queue
import re
import secrets
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    abort,
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError, CSRFProtect
from pypdf import PdfReader, PdfWriter
from werkzeug.security import check_password_hash, generate_password_hash

from automation import (
    AUTOMATION_VERSION,
    PLAYWRIGHT_HEADLESS,
    PLAYWRIGHT_SLOW_MO_MS,
    PLAYWRIGHT_TIMEOUT_MS,
    ejecutar_trabajo,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get("SECRETARIO_DATA_DIR", str(BASE_DIR / "datos"))
).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "secretario.db"
app = Flask(__name__)


def _clave_sesion():
    configurada = os.environ.get("SECRETARIO_SECRET", "").strip()
    if configurada:
        return configurada
    archivo = DATA_DIR / ".secretario_secret"
    if archivo.exists():
        return archivo.read_text(encoding="utf-8").strip()
    generada = secrets.token_urlsafe(48)
    archivo.write_text(generada, encoding="utf-8")
    return generada


app.secret_key = _clave_sesion()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
if os.environ.get("SESSION_COOKIE_SECURE", "0").strip().lower() in {
    "1",
    "true",
    "si",
    "sí",
    "yes",
}:
    app.config["SESSION_COOKIE_SECURE"] = True
csrf = CSRFProtect(app)

APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "5010"))
MAX_AUTOMATIZACIONES = max(
    1, int(os.environ.get("MAX_AUTOMATIZACIONES", "1"))
)
MAX_TRABAJOS_POR_USUARIO = max(
    1, int(os.environ.get("MAX_TRABAJOS_POR_USUARIO", "10"))
)
CENTROS_ATENCION = ("SAAVEDRA", "POMBO")
COLA_TRABAJOS = queue.Queue()
_CONSUMIDORES_INICIADOS = False
_CONSUMIDORES_LOCK = threading.Lock()


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def _asegurar_columna(con, tabla, columna, definicion):
    columnas = {
        fila["name"]
        for fila in con.execute(f"PRAGMA table_info({tabla})").fetchall()
    }
    if columna not in columnas:
        try:
            con.execute(
                f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}"
            )
        except sqlite3.OperationalError as exc:
            # Dos procesos del servidor pueden iniciar al mismo tiempo.
            # Si el otro ya agregó la columna, la migración está resuelta.
            if "duplicate column name" not in str(exc).lower():
                raise


def _migrar_carpetas_descargas(con):
    trabajos = con.execute(
        "SELECT id, carpeta FROM trabajos WHERE carpeta IS NOT NULL"
    ).fetchall()
    for trabajo in trabajos:
        ruta = Path(trabajo["carpeta"])
        if ruta.is_dir():
            continue
        partes = ruta.parts
        indice = next(
            (
                posicion
                for posicion, parte in enumerate(partes)
                if parte.casefold() == "descargas"
            ),
            None,
        )
        if indice is None:
            continue
        candidata = DATA_DIR.joinpath(*partes[indice:])
        if candidata.is_dir():
            con.execute(
                "UPDATE trabajos SET carpeta=? WHERE id=?",
                (str(candidata.resolve()), trabajo["id"]),
            )


def init_db():
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT UNIQUE NOT NULL,
                clave_hash TEXT NOT NULL,
                administrador INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS trabajos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                centro_atencion TEXT NOT NULL DEFAULT 'SAAVEDRA',
                internacion TEXT NOT NULL,
                dni TEXT NOT NULL,
                estado_paciente TEXT NOT NULL,
                rango_tipo TEXT NOT NULL,
                fecha_desde TEXT,
                fecha_hasta TEXT,
                estado TEXT NOT NULL DEFAULT 'PENDIENTE',
                mensaje TEXT NOT NULL DEFAULT '',
                progreso INTEGER NOT NULL DEFAULT 0,
                etapa TEXT NOT NULL DEFAULT 'Pendiente',
                carpeta TEXT,
                creado TEXT NOT NULL,
                actualizado TEXT NOT NULL,
                FOREIGN KEY(usuario_id) REFERENCES usuarios(id)
            );
            """
        )

        # Migra instalaciones antiguas con esquemas incompletos.
        _asegurar_columna(con, "usuarios", "administrador", "INTEGER NOT NULL DEFAULT 0")
        _asegurar_columna(con, "trabajos", "centro_atencion", "TEXT NOT NULL DEFAULT 'SAAVEDRA'")
        _asegurar_columna(con, "trabajos", "estado", "TEXT NOT NULL DEFAULT 'PENDIENTE'")
        _asegurar_columna(con, "trabajos", "mensaje", "TEXT NOT NULL DEFAULT ''")
        _asegurar_columna(con, "trabajos", "progreso", "INTEGER NOT NULL DEFAULT 0")
        _asegurar_columna(con, "trabajos", "etapa", "TEXT NOT NULL DEFAULT 'Pendiente'")
        _asegurar_columna(con, "trabajos", "carpeta", "TEXT")
        _asegurar_columna(con, "trabajos", "creado", "TEXT")
        _asegurar_columna(con, "trabajos", "actualizado", "TEXT")
        _migrar_carpetas_descargas(con)

        # Completa valores obligatorios en bases previas a las migraciones.
        ahora = datetime.now().isoformat(timespec="seconds")
        con.execute(
            "UPDATE trabajos SET creado = COALESCE(creado, ?) WHERE creado IS NULL",
            (ahora,),
        )
        con.execute(
            "UPDATE trabajos SET actualizado = COALESCE(actualizado, creado, ?) WHERE actualizado IS NULL",
            (ahora,),
        )
        con.execute(
            "UPDATE trabajos SET estado_paciente = 'ALTA' WHERE estado_paciente = 'ACTIVO'"
        )
        con.execute(
            """UPDATE trabajos
               SET progreso = CASE
                   WHEN estado='COMPLETADO' THEN 100
                   WHEN estado='EN PROCESO' AND progreso=0 THEN 3
                   ELSE COALESCE(progreso, 0)
               END,
               etapa = CASE
                   WHEN estado='COMPLETADO' THEN 'Finalizado'
                   WHEN estado='ERROR' THEN 'Error'
                   WHEN estado='EN PROCESO' AND etapa='Pendiente'
                       THEN 'Procesando'
                   ELSE COALESCE(etapa, 'Pendiente')
               END"""
        )
        existe = con.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone()
        if not existe:
            usuario_admin = os.environ.get(
                "ADMIN_INICIAL_USUARIO", "admin"
            ).strip()
            clave_admin = os.environ.get("ADMIN_INICIAL_CLAVE", "").strip()
            clave_generada = not clave_admin
            if clave_generada:
                clave_admin = secrets.token_urlsafe(16)
            con.execute(
                "INSERT INTO usuarios(usuario, clave_hash, administrador) VALUES (?, ?, 1)",
                (usuario_admin, generate_password_hash(clave_admin)),
            )
            if clave_generada:
                print(
                    "PRIMER ACCESO - usuario: "
                    f"{usuario_admin} | contraseña: {clave_admin}",
                    flush=True,
                )


def login_requerido(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "usuario_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


@app.errorhandler(CSRFError)
def error_csrf(error):
    flash("La sesión venció o el formulario ya no es válido. Intentá nuevamente.")
    destino = "inicio" if "usuario_id" in session else "login"
    return redirect(url_for(destino))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        with db() as con:
            usuario = con.execute(
                "SELECT * FROM usuarios WHERE usuario = ?", (request.form["usuario"].strip(),)
            ).fetchone()
        if usuario and check_password_hash(usuario["clave_hash"], request.form["clave"]):
            session.clear()
            session["usuario_id"] = usuario["id"]
            session["usuario"] = usuario["usuario"]
            session["administrador"] = bool(usuario["administrador"])
            return redirect(url_for("inicio"))
        flash("Usuario o contraseña incorrectos.")
    return render_template("login.html")


@app.post("/salir")
def salir():
    session.clear()
    return redirect(url_for("login"))


@app.route("/mi-clave", methods=["GET", "POST"])
@login_requerido
def mi_clave():
    if request.method == "POST":
        clave_actual = request.form.get("clave_actual", "")
        clave_nueva = request.form.get("clave_nueva", "")
        clave_confirmacion = request.form.get("clave_confirmacion", "")
        with db() as con:
            usuario = con.execute(
                "SELECT clave_hash FROM usuarios WHERE id = ?",
                (session["usuario_id"],),
            ).fetchone()
            if not usuario or not check_password_hash(
                usuario["clave_hash"], clave_actual
            ):
                flash("La contraseña actual no es correcta.")
                return redirect(url_for("mi_clave"))
            if len(clave_nueva) < 8:
                flash("La contraseña nueva debe tener al menos 8 caracteres.")
                return redirect(url_for("mi_clave"))
            if clave_nueva != clave_confirmacion:
                flash("La confirmación no coincide con la contraseña nueva.")
                return redirect(url_for("mi_clave"))
            con.execute(
                "UPDATE usuarios SET clave_hash=? WHERE id=?",
                (generate_password_hash(clave_nueva), session["usuario_id"]),
            )
        flash("Contraseña actualizada.")
        return redirect(url_for("inicio"))
    return render_template("mi_clave.html")


@app.route("/")
@login_requerido
def inicio():
    with db() as con:
        filas = con.execute(
            "SELECT * FROM trabajos WHERE usuario_id=? ORDER BY id DESC",
            (session["usuario_id"],),
        ).fetchall()
    trabajos = []
    for fila in filas:
        trabajo = dict(fila)
        trabajo["archivos_count"] = len(
            _archivos_clinicos(trabajo.get("carpeta"))
        )
        trabajos.append(trabajo)
    return render_template(
        "inicio.html",
        trabajos=trabajos,
        max_automatizaciones=MAX_AUTOMATIZACIONES,
        centros_atencion=CENTROS_ATENCION,
    )


@app.post("/trabajos")
@login_requerido
def crear_trabajo():
    centro_atencion = request.form.get("centro_atencion", "SAAVEDRA").strip().upper()
    internacion = request.form["internacion"].strip()
    dni = request.form["dni"].strip()
    if centro_atencion not in CENTROS_ATENCION:
        flash("El centro de atención seleccionado no es válido.")
        return redirect(url_for("inicio"))
    if not re.fullmatch(r"\d{4,20}", internacion):
        flash("El número de internación debe contener solo dígitos.")
        return redirect(url_for("inicio"))
    if not re.fullmatch(r"\d{6,20}", dni):
        flash("El DNI debe contener solo dígitos.")
        return redirect(url_for("inicio"))

    rango = request.form["rango_tipo"]
    desde = request.form.get("fecha_desde") or None
    hasta = request.form.get("fecha_hasta") or None
    if rango == "RANGO" and (not desde or not hasta):
        flash("Para un rango debés indicar fecha desde y hasta.")
        return redirect(url_for("inicio"))
    if rango == "RANGO" and desde > hasta:
        flash("La fecha desde no puede ser posterior a la fecha hasta.")
        return redirect(url_for("inicio"))

    estado_paciente = "ALTA"
    ahora = datetime.now().isoformat(timespec="seconds")
    with db() as con:
        cur = con.execute(
            """INSERT INTO trabajos
               (usuario_id, centro_atencion, internacion, dni, estado_paciente, rango_tipo,
                fecha_desde, fecha_hasta, creado, actualizado)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session["usuario_id"],
                centro_atencion,
                internacion,
                dni,
                estado_paciente,
                rango,
                desde,
                hasta,
                ahora,
                ahora,
            ),
        )
        trabajo_id = cur.lastrowid
    flash(f"Paciente #{trabajo_id} agregado.")
    return redirect(url_for("inicio"))


@app.post("/trabajos/<int:trabajo_id>/ejecutar")
@login_requerido
def ejecutar(trabajo_id):
    hospital_usuario = request.form["hospital_usuario"].strip()
    hospital_clave = request.form["hospital_clave"]
    if not hospital_usuario or not hospital_clave:
        flash("Ingresá el usuario y la contraseña del Hospital.")
        return redirect(url_for("inicio"))

    with db() as con:
        pendientes_usuario = con.execute(
            """SELECT COUNT(*) AS cantidad FROM trabajos
               WHERE usuario_id=? AND estado IN ('EN COLA', 'EN PROCESO')""",
            (session["usuario_id"],),
        ).fetchone()["cantidad"]
        if pendientes_usuario >= MAX_TRABAJOS_POR_USUARIO:
            flash(
                "Alcanzaste el máximo de trabajos pendientes. "
                "Esperá a que termine alguno antes de agregar otro."
            )
            return redirect(url_for("inicio"))

        trabajo = con.execute(
            "SELECT * FROM trabajos WHERE id=? AND usuario_id=?",
            (trabajo_id, session["usuario_id"]),
        ).fetchone()
        if not trabajo:
            return ("No encontrado", 404)

        actualizado_filas = con.execute(
            """UPDATE trabajos
               SET estado='EN COLA', mensaje=?, progreso=1,
                   etapa='Esperando turno', actualizado=?
               WHERE id=? AND estado NOT IN ('EN COLA', 'EN PROCESO')""",
            (
                "Solicitud recibida. Se iniciará automáticamente cuando haya un lugar disponible.",
                datetime.now().isoformat(timespec="microseconds"),
                trabajo_id,
            ),
        ).rowcount
        if not actualizado_filas:
            flash("Ese paciente ya está en cola o procesándose.")
            return redirect(url_for("inicio"))

    datos = dict(trabajo)
    COLA_TRABAJOS.put(
        (trabajo_id, datos, hospital_usuario, hospital_clave)
    )
    iniciar_consumidores()
    flash(
        "Solicitud agregada a la cola. Podés cargar y ejecutar otro paciente mientras espera."
    )
    return redirect(url_for("inicio"))


@app.post("/trabajos/<int:trabajo_id>/borrar")
@login_requerido
def borrar_trabajo(trabajo_id):
    with db() as con:
        trabajo = con.execute(
            "SELECT estado FROM trabajos WHERE id=? AND usuario_id=?",
            (trabajo_id, session["usuario_id"]),
        ).fetchone()
        if not trabajo:
            return ("No encontrado", 404)
        if trabajo["estado"] in ("EN COLA", "EN PROCESO"):
            flash("No se puede borrar un intento mientras está en cola o en proceso.")
        else:
            con.execute(
                "DELETE FROM trabajos WHERE id=? AND usuario_id=?",
                (trabajo_id, session["usuario_id"]),
            )
            flash("Intento eliminado.")
    return redirect(url_for("inicio"))


PROGRESO_ETAPAS = (
    ("Iniciando sesión", 5, "Ingresando al sistema"),
    ("Admisión ya validada", 12, "Admisión validada"),
    ("Buscando admisión", 8, "Buscando admisión"),
    ("Informe de hospitalización ya", 22, "Informe de hospitalización"),
    ("Informe de hospitalización descargado", 22, "Informe de hospitalización"),
    ("Procesando informe", 15, "Informe de hospitalización"),
    ("Documentación adicional ya", 40, "Documentación adicional"),
    ("Documentación adicional descargada", 40, "Documentación adicional"),
    ("Procesando documentación", 25, "Documentación adicional"),
    ("Cargando internación", 45, "Abriendo historia clínica"),
    ("Evoluciones ya", 70, "Evoluciones médicas"),
    ("Evoluciones médicas descargadas", 70, "Evoluciones médicas"),
    ("Procesando evoluciones", 50, "Evoluciones médicas"),
    ("Epicrisis ya", 82, "Epicrisis"),
    ("Epicrisis descargada", 82, "Epicrisis"),
    ("Procesando epicrisis", 75, "Epicrisis"),
    ("Indicaciones ya", 98, "Indicaciones y prescripciones"),
    ("Indicaciones descargadas", 98, "Indicaciones y prescripciones"),
    ("Procesando indicaciones", 86, "Indicaciones y prescripciones"),
)


def _progreso_para(estado, mensaje, progreso_actual=0):
    if estado == "COMPLETADO":
        return 100, "Finalizado"
    if estado == "ERROR":
        return progreso_actual, "Error"
    mensaje_normalizado = mensaje.casefold()
    for texto, porcentaje, etapa in PROGRESO_ETAPAS:
        if texto.casefold() in mensaje_normalizado:
            return max(progreso_actual, porcentaje), etapa
    return max(progreso_actual, 3), "Procesando"


@app.get("/api/trabajos/estado")
@login_requerido
def estado_trabajos():
    posiciones = _posiciones_cola()
    with db() as con:
        trabajos = con.execute(
            """SELECT id, estado, mensaje, progreso, etapa, actualizado
               FROM trabajos WHERE usuario_id=? ORDER BY id DESC""",
            (session["usuario_id"],),
        ).fetchall()
    respuesta = []
    for fila in trabajos:
        trabajo = dict(fila)
        posicion = posiciones.get(trabajo["id"])
        if trabajo["estado"] == "EN COLA" and posicion:
            trabajo["etapa"] = f"En cola · posición {posicion}"
            if posicion == 1:
                trabajo["mensaje"] = "Es el próximo trabajo en comenzar."
            else:
                cantidad = posicion - 1
                sustantivo = "trabajo" if cantidad == 1 else "trabajos"
                trabajo["mensaje"] = (
                    f"Esperando turno. Hay {cantidad} {sustantivo} antes."
                )
        respuesta.append(trabajo)
    return jsonify(respuesta)


@app.get("/salud")
def salud():
    with db() as con:
        cantidades = {
            fila["estado"]: fila["cantidad"]
            for fila in con.execute(
                """SELECT estado, COUNT(*) AS cantidad
                   FROM trabajos
                   WHERE estado IN ('EN COLA', 'EN PROCESO')
                   GROUP BY estado"""
            ).fetchall()
        }
    return jsonify(
        {
            "estado": "ok",
            "automation_version": AUTOMATION_VERSION,
            "concurrencia": MAX_AUTOMATIZACIONES,
            "en_proceso": cantidades.get("EN PROCESO", 0),
            "en_cola": cantidades.get("EN COLA", 0),
        }
    )


def _trabajo_del_usuario(trabajo_id):
    with db() as con:
        trabajo = con.execute(
            "SELECT * FROM trabajos WHERE id=? AND usuario_id=?",
            (trabajo_id, session["usuario_id"]),
        ).fetchone()
    if not trabajo:
        abort(404)
    return dict(trabajo)


def _carpeta_valida(ruta):
    if not ruta:
        return None
    carpeta = Path(ruta)
    try:
        carpeta = carpeta.resolve()
    except OSError:
        return None
    return carpeta if carpeta.is_dir() else None


def _normalizar_nombre_archivo(nombre):
    texto = nombre.casefold()
    texto = (
        texto.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ü", "u")
        .replace("ñ", "n")
    )
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def _orden_archivo_clinico(archivo):
    nombre = _normalizar_nombre_archivo(archivo.stem)
    reglas = (
        (10, ("informe", "hospitalizacion")),
        (20, ("autorizacion",)),
        (30, ("orden", "internacion")),
        (40, ("consentimiento", "informado")),
        (50, ("epicrisis",)),
        (60, ("evoluciones",)),
        (70, ("indicaciones",)),
        (70, ("prescripciones",)),
    )
    for orden, palabras in reglas:
        if all(palabra in nombre for palabra in palabras):
            return orden, nombre
    return 900, nombre


def _archivos_clinicos(ruta):
    carpeta = _carpeta_valida(ruta)
    if not carpeta:
        return []
    archivos = []
    for archivo in carpeta.iterdir():
        if (
            archivo.is_file()
            and archivo.suffix.lower() == ".pdf"
            and not archivo.name.startswith("_")
        ):
            archivos.append(archivo)
    return sorted(archivos, key=_orden_archivo_clinico)


def _archivo_clinico(trabajo, nombre):
    carpeta = _carpeta_valida(trabajo.get("carpeta"))
    if not carpeta or Path(nombre).name != nombre:
        abort(404)
    archivo = (carpeta / nombre).resolve()
    if (
        archivo.parent != carpeta
        or not archivo.is_file()
        or archivo.suffix.lower() != ".pdf"
        or archivo.name.startswith("_")
    ):
        abort(404)
    return archivo


def _pdf_completo(trabajo):
    archivos = _archivos_clinicos(trabajo.get("carpeta"))
    if not archivos:
        return None
    memoria = tempfile.SpooledTemporaryFile(
        max_size=32 * 1024 * 1024,
        mode="w+b",
    )
    escritor = PdfWriter()
    for archivo in archivos:
        lector = PdfReader(str(archivo))
        for pagina in lector.pages:
            escritor.add_page(pagina)
    escritor.write(memoria)
    memoria.seek(0)
    return memoria


@app.get("/trabajos/<int:trabajo_id>/archivos")
@login_requerido
def archivos_trabajo(trabajo_id):
    trabajo = _trabajo_del_usuario(trabajo_id)
    archivos = [
        {
            "nombre": archivo.name,
            "bytes": archivo.stat().st_size,
        }
        for archivo in _archivos_clinicos(trabajo.get("carpeta"))
    ]
    return render_template(
        "archivos.html", trabajo=trabajo, archivos=archivos
    )


@app.get("/trabajos/<int:trabajo_id>/archivos/<path:nombre>")
@login_requerido
def ver_archivo(trabajo_id, nombre):
    trabajo = _trabajo_del_usuario(trabajo_id)
    archivo = _archivo_clinico(trabajo, nombre)
    return send_file(
        archivo,
        mimetype="application/pdf",
        download_name=archivo.name,
        max_age=0,
    )


@app.get("/trabajos/<int:trabajo_id>/archivos/<path:nombre>/descargar")
@login_requerido
def descargar_archivo(trabajo_id, nombre):
    trabajo = _trabajo_del_usuario(trabajo_id)
    archivo = _archivo_clinico(trabajo, nombre)
    return send_file(
        archivo,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=archivo.name,
        max_age=0,
    )


@app.get("/trabajos/<int:trabajo_id>/archivos/<path:nombre>/imprimir")
@login_requerido
def imprimir_archivo(trabajo_id, nombre):
    trabajo = _trabajo_del_usuario(trabajo_id)
    archivo = _archivo_clinico(trabajo, nombre)
    return render_template(
        "imprimir.html",
        titulo=archivo.name,
        pdf_url=url_for(
            "ver_archivo",
            trabajo_id=trabajo_id,
            nombre=archivo.name,
        ),
    )


@app.get("/trabajos/<int:trabajo_id>/documentacion.pdf")
@login_requerido
def ver_documentacion_completa(trabajo_id):
    trabajo = _trabajo_del_usuario(trabajo_id)
    memoria = _pdf_completo(trabajo)
    if memoria is None:
        abort(404)
    nombre = (
        f'{trabajo["internacion"]}_{trabajo["dni"]}_documentacion.pdf'
    )
    respuesta = send_file(
        memoria,
        mimetype="application/pdf",
        download_name=nombre,
        max_age=0,
    )
    respuesta.call_on_close(memoria.close)
    return respuesta


@app.get("/trabajos/<int:trabajo_id>/imprimir")
@login_requerido
def imprimir_trabajo(trabajo_id):
    trabajo = _trabajo_del_usuario(trabajo_id)
    if not _archivos_clinicos(trabajo.get("carpeta")):
        flash("Este paciente todavía no tiene archivos para imprimir.")
        return redirect(url_for("archivos_trabajo", trabajo_id=trabajo_id))
    return render_template(
        "imprimir.html",
        titulo=f'Documentación · Internación {trabajo["internacion"]}',
        pdf_url=url_for(
            "ver_documentacion_completa",
            trabajo_id=trabajo_id,
        ),
    )


@app.get("/trabajos/<int:trabajo_id>/descargar")
@login_requerido
def descargar_trabajo(trabajo_id):
    trabajo = _trabajo_del_usuario(trabajo_id)
    archivos = _archivos_clinicos(trabajo.get("carpeta"))
    if not archivos:
        flash("Este paciente todavía no tiene archivos para descargar.")
        return redirect(url_for("archivos_trabajo", trabajo_id=trabajo_id))
    memoria = tempfile.SpooledTemporaryFile(
        max_size=16 * 1024 * 1024,
        mode="w+b",
    )
    with zipfile.ZipFile(
        memoria, "w", compression=zipfile.ZIP_DEFLATED
    ) as comprimido:
        for archivo in archivos:
            comprimido.write(archivo, arcname=archivo.name)
    memoria.seek(0)
    nombre = (
        f'{trabajo["internacion"]}_{trabajo["dni"]}_documentacion.zip'
    )
    respuesta = send_file(
        memoria,
        mimetype="application/zip",
        as_attachment=True,
        download_name=nombre,
    )
    respuesta.call_on_close(memoria.close)
    return respuesta


def _worker(trabajo_id, datos, hospital_usuario, hospital_clave):
    def informar(estado, mensaje, carpeta=None):
        with db() as con:
            actual = con.execute(
                "SELECT progreso FROM trabajos WHERE id=?", (trabajo_id,)
            ).fetchone()
            progreso_actual = actual["progreso"] if actual else 0
            progreso, etapa = _progreso_para(
                estado, mensaje, progreso_actual
            )
            con.execute(
                """UPDATE trabajos
                   SET estado=?, mensaje=?, progreso=?, etapa=?,
                       carpeta=COALESCE(?, carpeta), actualizado=?
                   WHERE id=?""",
                (
                    estado,
                    mensaje,
                    progreso,
                    etapa,
                    str(carpeta) if carpeta else None,
                    datetime.now().isoformat(timespec="seconds"),
                    trabajo_id,
                ),
            )

    try:
        carpeta = ejecutar_trabajo(datos, hospital_usuario, hospital_clave, informar)
        informar("COMPLETADO", "Descargas finalizadas.", carpeta)
    except Exception as exc:
        app.logger.exception("Falló el trabajo %s", trabajo_id)
        detalle = f"{type(exc).__name__}: {exc}"
        if "Executable doesn't exist" in str(exc):
            detalle = (
                "Falta instalar Chromium de Playwright. "
                "Ejecutá: .venv\\Scripts\\python.exe -m playwright install chromium"
            )
        informar("ERROR", detalle)


def _posiciones_cola():
    with COLA_TRABAJOS.mutex:
        ids = [item[0] for item in list(COLA_TRABAJOS.queue)]
    return {
        trabajo_id: posicion
        for posicion, trabajo_id in enumerate(ids, start=1)
    }


def _consumidor_cola(numero):
    while True:
        item = COLA_TRABAJOS.get()
        hospital_usuario = hospital_clave = None
        try:
            trabajo_id, datos, hospital_usuario, hospital_clave = item
            with db() as con:
                actualizado = con.execute(
                    """UPDATE trabajos
                       SET estado='EN PROCESO', mensaje=?, progreso=3,
                           etapa='Iniciando navegador', actualizado=?
                       WHERE id=? AND estado='EN COLA'""",
                    (
                        (
                            "Abriendo Chromium... "
                            f"(automation {AUTOMATION_VERSION}, "
                            f"headless={'sí' if PLAYWRIGHT_HEADLESS else 'no'}, "
                            f"slow={PLAYWRIGHT_SLOW_MO_MS}ms, "
                            f"timeout={PLAYWRIGHT_TIMEOUT_MS}ms)"
                        ),
                        datetime.now().isoformat(timespec="seconds"),
                        trabajo_id,
                    ),
                ).rowcount
            if actualizado:
                _worker(
                    trabajo_id,
                    datos,
                    hospital_usuario,
                    hospital_clave,
                )
        except Exception as exc:
            app.logger.exception(
                "Falló el consumidor %s con el trabajo %s",
                numero,
                locals().get("trabajo_id", "desconocido"),
            )
            trabajo_fallido = locals().get("trabajo_id")
            if trabajo_fallido:
                try:
                    with db() as con:
                        con.execute(
                            """UPDATE trabajos
                               SET estado='ERROR', mensaje=?, etapa='Error',
                                   actualizado=?
                               WHERE id=?""",
                            (
                                f"{type(exc).__name__}: {exc}",
                                datetime.now().isoformat(timespec="seconds"),
                                trabajo_fallido,
                            ),
                        )
                except Exception:
                    app.logger.exception(
                        "No se pudo registrar el error del trabajo %s",
                        trabajo_fallido,
                    )
        finally:
            # Elimina cuanto antes las referencias a las credenciales.
            item = hospital_usuario = hospital_clave = None
            COLA_TRABAJOS.task_done()


def iniciar_consumidores():
    global _CONSUMIDORES_INICIADOS
    with _CONSUMIDORES_LOCK:
        if _CONSUMIDORES_INICIADOS:
            return
        for numero in range(1, MAX_AUTOMATIZACIONES + 1):
            hilo = threading.Thread(
                target=_consumidor_cola,
                args=(numero,),
                name=f"automatizacion-{numero}",
                daemon=True,
            )
            hilo.start()
        _CONSUMIDORES_INICIADOS = True


def _recuperar_trabajos_interrumpidos():
    ahora = datetime.now().isoformat(timespec="seconds")
    with db() as con:
        con.execute(
            """UPDATE trabajos
               SET estado='ERROR',
                   mensaje='El servidor se reinició antes de completar este trabajo. Podés ejecutarlo nuevamente.',
                   etapa='Interrumpido por reinicio',
                   actualizado=?
               WHERE estado IN ('EN COLA', 'EN PROCESO')""",
            (ahora,),
        )


@app.route("/usuarios", methods=["GET", "POST"])
@login_requerido
def usuarios():
    if not session.get("administrador"):
        return ("Prohibido", 403)
    if request.method == "POST":
        nuevo_usuario = request.form["usuario"].strip()
        nueva_clave = request.form["clave"]
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,50}", nuevo_usuario):
            flash(
                "El usuario debe tener entre 3 y 50 caracteres y usar "
                "solamente letras, números, punto, guion o guion bajo."
            )
            return redirect(url_for("usuarios"))
        if len(nueva_clave) < 8:
            flash("La contraseña inicial debe tener al menos 8 caracteres.")
            return redirect(url_for("usuarios"))
        try:
            with db() as con:
                con.execute(
                    "INSERT INTO usuarios(usuario, clave_hash) VALUES (?, ?)",
                    (
                        nuevo_usuario,
                        generate_password_hash(nueva_clave),
                    ),
                )
            flash("Usuario creado.")
        except sqlite3.IntegrityError:
            flash("Ese usuario ya existe.")
    with db() as con:
        lista = con.execute(
            "SELECT id, usuario, administrador FROM usuarios ORDER BY usuario"
        ).fetchall()
    return render_template("usuarios.html", usuarios=lista)


init_db()


if __name__ == "__main__":
    _recuperar_trabajos_interrumpidos()
    iniciar_consumidores()
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
