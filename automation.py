import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter


URL = os.environ.get(
    "HOSPITAL_LOGIN_URL",
    "http://saavedra-ts.cemic1.edu.ar/HOSPITAL/pages/login.faces",
)
URL_ADMISION_PACIENTES = os.environ.get(
    "HOSPITAL_ADMISION_URL",
    (
        "http://saavedra-ts.cemic1.edu.ar/HOSPITAL/pages/"
        "admisionInternados/modificacionAdmisionInternado/inicio.faces"
    ),
)
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get("SECRETARIO_DATA_DIR", str(BASE_DIR / "datos"))
).expanduser().resolve()
DESCARGAS_DIR = DATA_DIR / "descargas"
AUTOMATION_VERSION = "2026-06-19-g"
PLAYWRIGHT_SLOW_MO_MS = max(0, int(os.environ.get("PLAYWRIGHT_SLOW_MO_MS", "220")))
PLAYWRIGHT_TIMEOUT_MS = max(20000, int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "35000")))
PLAYWRIGHT_HEADLESS = os.environ.get(
    "PLAYWRIGHT_HEADLESS", "0"
).strip().lower() in {"1", "true", "si", "sí", "yes"}
PLAYWRIGHT_RECORD_VIDEO = os.environ.get(
    "PLAYWRIGHT_RECORD_VIDEO", "0"
).strip().lower() in {"1", "true", "si", "sí", "yes"}


def _captura_debug(page, carpeta, nombre):
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(carpeta / nombre), full_page=True)
    except Exception:
        pass


def _leer_progreso(carpeta):
    ruta = carpeta / "_progreso.json"
    if not ruta.exists():
        return {}
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _guardar_progreso(carpeta, progreso):
    ruta = carpeta / "_progreso.json"
    temporal = carpeta / "_progreso.tmp"
    temporal.write_text(
        json.dumps(progreso, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporal.replace(ruta)


def _completar_seccion(carpeta, progreso, seccion, **datos):
    progreso[seccion] = {
        "completada": True,
        "fecha": datetime.now().isoformat(timespec="seconds"),
        **datos,
    }
    _guardar_progreso(carpeta, progreso)


def _seccion_completa(progreso, seccion):
    return bool(progreso.get(seccion, {}).get("completada"))


def _esperar_capa_carga(page, timeout=60000):
    try:
        page.locator("#LoadingDialog_modal").wait_for(
            state="hidden", timeout=timeout
        )
    except Exception:
        pass
    try:
        page.locator(".ui-blockui:visible").wait_for(
            state="hidden", timeout=timeout
        )
    except Exception:
        pass


def _click_link_con_reintento(page, patron, timeout=20000, intentos=2):
    ultimo = None
    for _ in range(intentos):
        try:
            link = page.get_by_role("link", name=patron).first
            link.wait_for(state="visible", timeout=timeout)
            link.click()
            _esperar_capa_carga(page, timeout=timeout)
            return
        except Exception as exc:
            ultimo = exc
            _esperar_capa_carga(page, timeout=timeout)
            page.wait_for_timeout(1200)
    raise RuntimeError(f"No se pudo hacer click en el link {patron}: {ultimo}")


def _aceptar_paciente(page):
    modal = page.locator(".ui-dialog:visible").filter(
        has_text="Buscar Internación"
    )
    modal.wait_for(state="visible")
    botones = modal.locator("button:visible")
    posiciones = []
    for indice in range(botones.count()):
        caja = botones.nth(indice).bounding_box()
        if caja:
            posiciones.append(
                (
                    caja["x"],
                    indice,
                    botones.nth(indice).inner_text().strip(),
                )
            )
    if len(posiciones) < 2:
        raise RuntimeError(
            f"No se encontraron los botones inferiores: {posiciones}"
        )
    posiciones.sort()
    _, indice, texto = posiciones[0]
    if texto != "Aceptar":
        raise RuntimeError(
            f"El botón inferior izquierdo no es Aceptar: {posiciones}"
        )
    botones.nth(indice).click()
    modal.wait_for(state="hidden", timeout=60000)


def _cerrar_advertencias_opcionales(page, timeout=12000):
    enlace_evoluciones = page.get_by_role(
        "link", name="Evolución y Diagnóstico", exact=True
    )
    limite = time.monotonic() + timeout / 1000
    enlace_visible_desde = None

    while time.monotonic() < limite:
        if page.is_closed():
            raise RuntimeError(
                "Chromium se cerró mientras cargaba la historia clínica."
            )

        dialogos = page.locator(".ui-dialog:visible")
        for indice in range(dialogos.count()):
            dialogo = dialogos.nth(indice)
            cerrar = dialogo.get_by_role(
                "button", name="Cerrar", exact=True
            )
            if not cerrar.count():
                continue
            try:
                texto = dialogo.inner_text(timeout=1000).casefold()
            except Exception:
                texto = ""
            if (
                "advertenc" not in texto
                and "alerg" not in texto
                and dialogos.count() > 1
            ):
                continue
            cerrar.click()
            dialogo.wait_for(state="hidden", timeout=30000)
            return True

        try:
            enlace_visible = (
                enlace_evoluciones.count() > 0
                and enlace_evoluciones.is_visible()
            )
        except Exception:
            enlace_visible = False

        if enlace_visible:
            if enlace_visible_desde is None:
                enlace_visible_desde = time.monotonic()
            elif time.monotonic() - enlace_visible_desde >= 2:
                return False
        else:
            enlace_visible_desde = None

        page.wait_for_timeout(250)

    return False


def _seguro(nombre):
    return re.sub(r'[<>:"/\\|?*]+', "_", nombre).strip(" .") or "archivo"


def _carpeta_trabajo(trabajo):
    usuario_id = trabajo.get("usuario_id") or "pruebas"
    trabajo_id = trabajo.get("id") or "manual"
    paciente = _seguro(
        f'{trabajo["internacion"]}_{trabajo["dni"]}'
    )
    if trabajo.get("rango_tipo") == "RANGO":
        periodo = _seguro(
            f'{trabajo.get("fecha_desde")}_a_'
            f'{trabajo.get("fecha_hasta")}'
        )
    else:
        periodo = "toda_la_internacion"
    solicitud = _seguro(
        f"solicitud_{trabajo_id}_{periodo}"
    )
    return (
        DESCARGAS_DIR
        / f"usuario_{usuario_id}"
        / paciente
        / solicitud
    )


def _nombre_disponible(carpeta, nombre):
    destino = carpeta / _seguro(nombre)
    if not destino.exists():
        return destino
    i = 2
    while True:
        candidato = destino.with_name(f"{destino.stem} ({i}){destino.suffix}")
        if not candidato.exists():
            return candidato
        i += 1


def _descargar_desde_popup(page, accion, destino, intentos=3):
    ultimo = None
    for _ in range(intentos):
        try:
            with page.expect_popup(timeout=120000) as popup_info:
                accion()
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded")
            try:
                popup.wait_for_url(
                    re.compile(r"\.pdf(?:$|\?)", re.I), timeout=10000
                )
                respuesta = popup.context.request.get(popup.url)
                if not respuesta.ok:
                    raise RuntimeError(
                        f"El PDF respondió HTTP {respuesta.status}."
                    )
                destino.write_bytes(respuesta.body())
            except PlaywrightTimeout:
                boton = popup.get_by_role("button", name="Descargar")
                if boton.count() == 0:
                    frame = popup.locator("iframe").first.content_frame
                    if frame is None:
                        raise RuntimeError(
                            "El popup no abrió un PDF directo ni mostró visor descargable."
                        )
                    boton = frame.get_by_role("button", name="Descargar")
                with popup.expect_download(timeout=120000) as descarga_info:
                    boton.click()
                descarga_info.value.save_as(destino)
            popup.close()
            if destino.stat().st_size == 0:
                raise RuntimeError("El archivo descargado quedó vacío.")
            return
        except Exception as exc:
            ultimo = exc
            time.sleep(4)
    raise RuntimeError(f"No apareció la descarga luego de {intentos} intentos: {ultimo}")


def _seleccionar_opcion(page, etiqueta, opcion):
    page.locator(f'[id="{etiqueta}_label"]').click()
    page.get_by_role("option", name=opcion, exact=True).click()


def _seleccionar_estado(page, estado):
    ultimo = ""
    for _ in range(3):
        etiqueta = page.get_by_text("Estado:", exact=True)
        etiqueta.wait_for(timeout=20000)
        contenedor = etiqueta.locator("xpath=..")
        combo = contenedor.locator(".ui-selectonemenu").first
        if combo.count() == 0:
            combo = etiqueta.locator(
                "xpath=following::*[contains(@class,'ui-selectonemenu')][1]"
            )
        ultimo = combo.locator(".ui-selectonemenu-label").inner_text().strip()
        if ultimo == estado:
            return
        combo.locator(".ui-selectonemenu-trigger").click()
        page.get_by_role("option", name=estado, exact=True).click()
        # PrimeFaces vuelve a construir el control luego de la selección.
        page.wait_for_timeout(1400)
        etiqueta = page.get_by_text("Estado:", exact=True)
        combo = etiqueta.locator("xpath=..").locator(".ui-selectonemenu").first
        ultimo = combo.locator(".ui-selectonemenu-label").inner_text().strip()
        if ultimo == estado:
            return
    raise RuntimeError(
        f"El estado solicitado era {estado}, pero el sistema quedó en {ultimo}."
    )


def _campo_buscar(page):
    etiqueta = page.get_by_text("Buscar:", exact=True)
    etiqueta.wait_for(timeout=20000)
    campo = etiqueta.locator("xpath=..").locator("input:visible").first
    if campo.count() == 0:
        campo = etiqueta.locator("xpath=following::input[not(@type='hidden')][1]")
    return campo


def _leer_ficha_admision(page):
    numero = page.get_by_text("Nro. Internación:", exact=True).locator(
        "xpath=following::input[1]"
    ).input_value().strip()
    dni = page.get_by_text("DNI:", exact=True).locator(
        "xpath=following::input[1]"
    ).input_value().strip()
    paciente = page.get_by_text("Paciente:", exact=True).locator(
        "xpath=following::input[1]"
    ).input_value().strip()
    fecha_valor = page.get_by_text("Fecha Internación (*):", exact=True).locator(
        "xpath=following::input[1]"
    ).input_value().strip()
    return {
        "numero": numero,
        "dni": dni,
        "paciente": paciente,
        "fecha": fecha_valor,
    }


def _esperar_ficha_admision_cargada(page, trabajo):
    page.wait_for_url("**/datosAdmisionInternado.faces", timeout=60000)
    page.wait_for_load_state("domcontentloaded")
    try:
        page.locator("#LoadingDialog_modal").wait_for(
            state="hidden", timeout=60000
        )
    except Exception:
        pass
    try:
        page.locator(".ui-blockui:visible").wait_for(
            state="hidden", timeout=60000
        )
    except Exception:
        pass
    page.get_by_role("button", name="Imprimir", exact=True).wait_for(
        state="visible", timeout=30000
    )
    deadline = time.time() + 15
    ultimo = None
    while time.time() < deadline:
        ultimo = _leer_ficha_admision(page)
        if (
            ultimo["numero"] == trabajo["internacion"]
            and ultimo["dni"] == trabajo["dni"]
            and ultimo["paciente"]
            and ultimo["fecha"]
        ):
            return ultimo
        page.wait_for_timeout(700)
    raise RuntimeError(
        "La ficha de admisión abrió, pero quedó sin cargar los datos esperados: "
        f"{ultimo}"
    )


def _buscar_fila_internacion(page, trabajo):
    campo = _campo_buscar(page)
    campo.fill(trabajo["internacion"])
    if campo.input_value() != trabajo["internacion"]:
        raise RuntimeError("El número de internación no quedó cargado en Buscar.")
    page.get_by_role("button", name="Buscar", exact=True).click()
    fila = page.locator("tr").filter(has_text=trabajo["internacion"]).filter(
        has_text=trabajo["dni"]
    )
    fila.first.wait_for(timeout=30000)
    if fila.count() != 1:
        raise RuntimeError(
            f"Se encontraron {fila.count()} filas para la internación y DNI indicados."
        )
    return fila.first


def _login(page, usuario, clave):
    page.goto(URL, wait_until="domcontentloaded")
    page.get_by_role("textbox", name="Usuario").fill(usuario)
    page.get_by_role("textbox", name="Contraseña").fill(clave)
    page.get_by_role("button", name=re.compile("Ingresar")).click()
    page.get_by_role("link", name="ADMISION INTERNADOS").wait_for(timeout=30000)


def _entrar_modulo(page, nombre):
    url_actual = (page.url or "").lower()
    if nombre == "ADMISION INTERNADOS" and "admisioninternados/" in url_actual:
        return
    if nombre == "INTERNACION" and "/internacion/" in url_actual:
        return

    ultimo = None
    for _ in range(2):
        try:
            page.get_by_role("link", name=nombre, exact=True).click()
            combo = page.locator('[id="formInicio:comboCentroAtencion_label"]')
            if combo.count() and combo.first.is_visible():
                combo.click()
                page.get_by_role("option", name="SAAVEDRA", exact=True).click()
                page.get_by_role("button", name="Aceptar", exact=True).click()
            _esperar_capa_carga(page, timeout=90000)
            return
        except Exception as exc:
            ultimo = exc
            _esperar_capa_carga(page, timeout=60000)
            page.wait_for_timeout(1800)
    raise RuntimeError(f"No se pudo abrir el modulo {nombre}: {ultimo}")


def _ir_a_pacientes_internados(page):
    _click_link_con_reintento(page, re.compile(r"^Admisi[oó]n", re.I))
    _click_link_con_reintento(page, re.compile(r"^Pacientes\s+Internados$", re.I))
    buscar = page.locator('[id="formSelectInternacion:j_idt227"]')
    try:
        buscar.wait_for(state="visible", timeout=15000)
        return
    except Exception:
        # Fallback: algunos despliegues dejan abierta otra pantalla del módulo.
        page.goto(URL_ADMISION_PACIENTES, wait_until="domcontentloaded")
        _esperar_capa_carga(page, timeout=60000)
        buscar.wait_for(state="visible", timeout=30000)


def _buscar_admision(page, trabajo):
    _entrar_modulo(page, "ADMISION INTERNADOS")
    _ir_a_pacientes_internados(page)
    _seleccionar_estado(page, "TODAS")
    campo = page.locator('[id="formSelectInternacion:j_idt227"]')
    campo.wait_for()
    campo.fill(trabajo["internacion"])
    page.get_by_role("button", name="Buscar", exact=True).click()
    page.locator("#LoadingDialog_modal").wait_for(state="hidden", timeout=60000)
    try:
        page.locator(".ui-blockui:visible").wait_for(state="hidden", timeout=60000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    celda = page.get_by_role("gridcell", name=trabajo["internacion"], exact=True)
    celda.wait_for(timeout=30000)
    if celda.count() != 1:
        raise RuntimeError(
            f"La búsqueda devolvió {celda.count()} filas para la internación."
        )
    fila = celda.locator("xpath=ancestor::tr[1]")
    texto_dni = fila.get_by_text(trabajo["dni"], exact=True)
    if texto_dni.count() != 1:
        raise RuntimeError("El DNI de la fila no coincide con el solicitado.")
    celda.click()
    ficha = _esperar_ficha_admision_cargada(page, trabajo)
    fecha_match = re.search(
        r"\b(\d{2}/\d{2}/(?:\d{2}|\d{4}))\b", ficha["fecha"]
    )
    if not fecha_match:
        raise RuntimeError("No pude leer la fecha de internación de la ficha.")
    return fecha_match.group(1)


def _informe_hospitalizacion(page, carpeta):
    def esta_activo(caja):
        return "ui-state-active" in (caja.get_attribute("class") or "")

    def fijar_estado(caja, activo_objetivo, intentos=4):
        for _ in range(intentos):
            if esta_activo(caja) == activo_objetivo:
                return
            # Click DOM directo para evitar errores de puntero en overlays.
            caja.evaluate("el => el.click()")
            _esperar_ajax(page)
            page.wait_for_timeout(150)
        raise RuntimeError(
            f"No se pudo {'marcar' if activo_objetivo else 'desmarcar'} una casilla del modal."
        )

    page.get_by_role("button", name="Imprimir", exact=True).click()
    dialogo = page.get_by_label("Imprimir", exact=True)
    dialogo.wait_for(state="visible", timeout=30000)
    _captura_debug(page, carpeta, "DEBUG_01_modal_informe_inicial.png")

    # Si existe casilla de seleccionar todo en cabecera, la apagamos primero.
    cabecera_activa = dialogo.locator(
        "thead .ui-chkbox-box.ui-state-active, .ui-datatable-header .ui-chkbox-box.ui-state-active"
    )
    if cabecera_activa.count():
        cabecera_activa.first.evaluate("el => el.click()")
        _esperar_ajax(page)

    patron_informe = re.compile(r"INFORME\s+HOSPITALIZACI(?:O|Ó)N", re.I)
    fila_objetivo = None
    for _ in range(3):
        filas = dialogo.locator("tbody tr")
        fila_objetivo = None
        for i in range(filas.count()):
            fila = filas.nth(i)
            caja = fila.locator(".ui-chkbox-box").first
            if caja.count() == 0:
                continue
            texto_fila = re.sub(r"\s+", " ", fila.inner_text()).strip()
            es_objetivo = bool(patron_informe.search(texto_fila))
            if es_objetivo:
                fila_objetivo = fila
                fijar_estado(caja, True)
            else:
                fijar_estado(caja, False)

        if fila_objetivo is None:
            raise RuntimeError("No se encontró la fila de INFORME HOSPITALIZACION en el modal.")

        activos = dialogo.locator("tbody .ui-chkbox-box.ui-state-active")
        objetivo_activo = esta_activo(
            fila_objetivo.locator(".ui-chkbox-box").first
        )
        if activos.count() == 1 and objetivo_activo:
            break
        page.wait_for_timeout(400)

    _captura_debug(page, carpeta, "DEBUG_02_modal_informe_filtrado.png")
    activos = dialogo.locator("tbody .ui-chkbox-box.ui-state-active")
    if activos.count() != 1:
        raise RuntimeError(
            f"Debía quedar 1 casilla activa y quedaron {activos.count()}."
        )
    destino = carpeta / "INFORME DE HOSPITALIZACION.pdf"
    _descargar_desde_popup(
        page,
        lambda: dialogo.get_by_role("button", name="Imprimir", exact=True).click(),
        destino,
    )
    page.get_by_role("button", name="Cerrar", exact=True).click()


def _documentacion(page, carpeta):
    page.get_by_role("link", name="Documentación", exact=True).click()
    tabla = page.locator(
        '[id^="formPrincipal:tablaDocAdicionalInt"]'
    ).first
    tabla.wait_for(state="visible", timeout=60000)
    acciones = page.locator(
        '[id^="formPrincipal:tablaDocAdicionalInt:"]'
        '[id$=":j_idt225"]'
    )
    total = acciones.count()
    if total == 0:
        vacio = tabla.get_by_text(
            re.compile("No se encontraron|Sin registros", re.I)
        )
        if vacio.count() == 0:
            raise RuntimeError(
                "La tabla de Documentación no indicó documentos ni vacío."
            )
        page.get_by_role("button", name="Volver", exact=True).click()
        return
    for i in range(total):
        accion = page.locator(
            '[id^="formPrincipal:tablaDocAdicionalInt:"]'
            '[id$=":j_idt225"]'
        ).nth(i)
        with page.expect_popup(timeout=120000) as popup_info:
            accion.click()
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded")
        popup.wait_for_url(
            re.compile(r"\.pdf(?:$|\?)", re.I), timeout=120000
        )
        nombre = (
            unquote(Path(urlparse(popup.url).path).name)
            or f"DOCUMENTO {i + 1}.pdf"
        )
        destino = _nombre_disponible(carpeta, nombre)
        respuesta = popup.context.request.get(popup.url)
        if not respuesta.ok:
            raise RuntimeError(
                f"El documento {i + 1} respondió HTTP {respuesta.status}."
            )
        destino.write_bytes(respuesta.body())
        popup.close()
        if destino.stat().st_size == 0:
            raise RuntimeError(f"El documento {i + 1} quedó vacío.")
    page.get_by_role("button", name="Volver", exact=True).click()


def _buscar_internacion(page, trabajo):
    try:
        page.get_by_role("link").nth(1).click(timeout=10000)
        _esperar_capa_carga(page)
    except Exception:
        pass
    _entrar_modulo(page, "INTERNACION")
    _click_link_con_reintento(page, re.compile(r"^Internaci[oó]n\s", re.I))
    opcion_internacion = page.get_by_role(
        "link", name=re.compile(r"^Internaci[oó]n$", re.I)
    ).last
    opcion_internacion.wait_for(state="visible")
    opcion_internacion.click()
    estado = "ACTIVAS" if trabajo["estado_paciente"] == "ACTIVO" else "TODAS"
    page.locator('[id="formSelectInternacion:j_idt378_label"]').click()
    page.get_by_role("option", name=estado, exact=True).click()
    campo = _campo_buscar(page)
    campo.fill(trabajo["internacion"])
    page.get_by_role("button", name="Buscar", exact=True).click()
    page.locator("#LoadingDialog_modal").wait_for(
        state="hidden", timeout=60000
    )
    page.locator(".ui-blockui:visible").wait_for(
        state="hidden", timeout=60000
    )
    page.wait_for_timeout(1500)
    celda = page.get_by_role(
        "gridcell", name=trabajo["internacion"], exact=True
    )
    celda.wait_for(timeout=30000)
    fila = celda.locator("xpath=ancestor::tr[1]")
    if trabajo["dni"] not in fila.inner_text():
        raise RuntimeError("La fila de la internación no contiene el DNI.")
    caja_fila = fila.bounding_box()
    if not caja_fila:
        raise RuntimeError("No se pudo determinar la posición de la fila.")
    page.mouse.click(
        caja_fila["x"] + caja_fila["width"] * 0.45,
        caja_fila["y"] + caja_fila["height"] / 2,
    )
    page.wait_for_timeout(2000)
    _aceptar_paciente(page)
    _cerrar_advertencias_opcionales(page)
    page.get_by_role(
        "link", name="Evolución y Diagnóstico", exact=True
    ).wait_for(timeout=180000)


def _fecha_evolucion(texto):
    hallazgo = re.search(
        r"\b(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})\b", texto
    )
    if not hallazgo:
        return None
    return datetime.strptime(
        f"{hallazgo.group(1)} {hallazgo.group(2)}",
        "%d/%m/%Y %H:%M",
    )


def _esperar_ajax(page):
    try:
        page.locator("#LoadingDialog_modal").wait_for(
            state="hidden", timeout=30000
        )
    except Exception:
        pass
    page.wait_for_timeout(500)


def _ordenar_evoluciones(page, modal):
    encabezado = modal.get_by_text(
        "Fecha Hora de Carga", exact=False
    ).first
    for _ in range(3):
        filas = modal.locator("tbody tr")
        fechas = [
            _fecha_evolucion(filas.nth(i).inner_text())
            for i in range(min(filas.count(), 10))
        ]
        fechas = [fecha for fecha in fechas if fecha]
        if len(fechas) >= 2 and fechas[0] <= fechas[-1]:
            return
        encabezado.click()
        _esperar_ajax(page)
    raise RuntimeError("No se pudo ordenar las evoluciones cronológicamente.")


def _volver_primera_pagina(page, modal):
    primera = modal.locator(
        ".ui-paginator-first:not(.ui-state-disabled)"
    )
    if primera.count():
        primera.click()
        _esperar_ajax(page)


def _ir_pagina_evoluciones(page, modal, objetivo):
    _volver_primera_pagina(page, modal)
    pagina = 1
    while pagina < objetivo:
        siguiente = modal.locator(
            ".ui-paginator-next:not(.ui-state-disabled)"
        )
        if siguiente.count() == 0:
            raise RuntimeError(f"No se pudo llegar a la página {objetivo}.")
        siguiente.click()
        _esperar_ajax(page)
        pagina += 1


def _evolucion_en_rango(fecha, desde, hasta):
    return desde <= fecha.date() <= hasta


def _escanear_evoluciones(page, modal, desde, hasta):
    _volver_primera_pagina(page, modal)
    pagina = 1
    relevantes = []
    total = 0
    while True:
        filas = modal.locator("tbody tr")
        coincidencias = 0
        for indice in range(filas.count()):
            fecha = _fecha_evolucion(filas.nth(indice).inner_text())
            if fecha and _evolucion_en_rango(fecha, desde, hasta):
                coincidencias += 1
        if coincidencias:
            relevantes.append(pagina)
            total += coincidencias
        siguiente = modal.locator(
            ".ui-paginator-next:not(.ui-state-disabled)"
        )
        if siguiente.count() == 0:
            break
        siguiente.click()
        _esperar_ajax(page)
        pagina += 1
    return relevantes, total


def _seleccionar_bloque_evoluciones(
    page, modal, pagina_desde, pagina_hasta, desde, hasta
):
    _ir_pagina_evoluciones(page, modal, pagina_desde)
    total = 0
    for numero in range(pagina_desde, pagina_hasta + 1):
        filas = modal.locator("tbody tr")
        for indice in range(filas.count()):
            fila = filas.nth(indice)
            fecha = _fecha_evolucion(fila.inner_text())
            if not fecha or not _evolucion_en_rango(fecha, desde, hasta):
                continue
            caja = fila.locator(".ui-chkbox-box").first
            clases = caja.get_attribute("class") or ""
            if "ui-state-active" not in clases:
                caja.click()
            total += 1
        if numero < pagina_hasta:
            modal.locator(
                ".ui-paginator-next:not(.ui-state-disabled)"
            ).click()
            _esperar_ajax(page)
    return total


def _verificar_bloque_evoluciones(
    page, modal, pagina_desde, pagina_hasta, desde, hasta
):
    _ir_pagina_evoluciones(page, modal, pagina_desde)
    total = 0
    for numero in range(pagina_desde, pagina_hasta + 1):
        filas = modal.locator("tbody tr")
        for indice in range(filas.count()):
            fila = filas.nth(indice)
            fecha = _fecha_evolucion(fila.inner_text())
            if not fecha or not _evolucion_en_rango(fecha, desde, hasta):
                continue
            clases = (
                fila.locator(".ui-chkbox-box").first.get_attribute("class")
                or ""
            )
            if "ui-state-active" not in clases:
                raise RuntimeError(
                    f"Se desmarcó la evolución {fecha:%d/%m/%Y %H:%M}."
                )
            total += 1
        if numero < pagina_hasta:
            modal.locator(
                ".ui-paginator-next:not(.ui-state-disabled)"
            ).click()
            _esperar_ajax(page)
    return total


def _descargar_pdf_url(page, modal, destino):
    with page.expect_popup(timeout=180000) as popup_info:
        modal.get_by_role(
            "button", name="Imprimir", exact=True
        ).click()
    popup = popup_info.value
    popup.wait_for_load_state("domcontentloaded")
    popup.wait_for_url(re.compile(r"\.pdf(?:$|\?)", re.I), timeout=180000)
    respuesta = popup.context.request.get(popup.url)
    if not respuesta.ok:
        raise RuntimeError(f"El PDF respondió HTTP {respuesta.status}.")
    destino.write_bytes(respuesta.body())
    popup.close()
    if destino.stat().st_size == 0:
        raise RuntimeError("El PDF generado quedó vacío.")


def _cerrar_modal_evoluciones(page):
    modal = page.get_by_label("Imprimir")
    if modal.count() and modal.is_visible():
        modal.get_by_role("button", name="Volver", exact=True).click()
        modal.wait_for(state="hidden", timeout=30000)


def _imprimir_bloque_evoluciones(
    page,
    pagina_desde,
    pagina_hasta,
    desde,
    hasta,
    destino,
):
    ultimo = None
    for _ in range(3):
        try:
            page.get_by_role(
                "button", name="Imprimir Evoluciones"
            ).click()
            modal = page.get_by_label("Imprimir")
            modal.wait_for(state="visible", timeout=60000)
            _ordenar_evoluciones(page, modal)
            seleccionadas = _seleccionar_bloque_evoluciones(
                page, modal, pagina_desde, pagina_hasta, desde, hasta
            )
            verificadas = _verificar_bloque_evoluciones(
                page, modal, pagina_desde, pagina_hasta, desde, hasta
            )
            if seleccionadas == 0 or verificadas != seleccionadas:
                raise RuntimeError(
                    f"Selección inconsistente: "
                    f"{seleccionadas}/{verificadas}."
                )
            _descargar_pdf_url(page, modal, destino)
            _cerrar_modal_evoluciones(page)
            return
        except Exception as exc:
            ultimo = exc
            for pagina in list(page.context.pages):
                if pagina != page:
                    try:
                        pagina.close()
                    except Exception:
                        pass
            try:
                _cerrar_modal_evoluciones(page)
            except Exception:
                pass
            page.wait_for_timeout(3000)
    raise RuntimeError(
        f"No se pudo imprimir páginas {pagina_desde}-{pagina_hasta}: "
        f"{ultimo}"
    )


def _evoluciones(page, trabajo, fecha_ingreso, carpeta):
    page.get_by_role(
        "link", name="Evolución y Diagnóstico", exact=True
    ).click()
    page.get_by_role("button", name="Imprimir Evoluciones").click()
    modal = page.get_by_label("Imprimir")
    modal.wait_for(state="visible", timeout=60000)
    _ordenar_evoluciones(page, modal)

    if trabajo["rango_tipo"] == "TODA":
        hallazgo = re.search(
            r"\b(\d{2}/\d{2}/(?:\d{2}|\d{4}))\b", fecha_ingreso
        )
        if not hallazgo:
            raise RuntimeError("No pude interpretar la fecha de ingreso.")
        valor_ingreso = hallazgo.group(1)
        formato = "%d/%m/%y" if len(valor_ingreso) == 8 else "%d/%m/%Y"
        desde = datetime.strptime(valor_ingreso, formato).date()
        hasta = datetime.now().date()
    else:
        desde = datetime.strptime(
            trabajo["fecha_desde"], "%Y-%m-%d"
        ).date()
        hasta = datetime.strptime(
            trabajo["fecha_hasta"], "%Y-%m-%d"
        ).date()
    if desde > hasta:
        raise RuntimeError("La fecha desde es posterior a la fecha hasta.")

    paginas, total = _escanear_evoluciones(
        page, modal, desde, hasta
    )
    if not paginas:
        raise RuntimeError("No hay evoluciones en el rango solicitado.")
    _cerrar_modal_evoluciones(page)

    primera, ultima = paginas[0], paginas[-1]
    bloques = [
        (inicio, min(inicio + 9, ultima))
        for inicio in range(primera, ultima + 1, 10)
    ]
    for indice, (pagina_desde, pagina_hasta) in enumerate(
        bloques, start=1
    ):
        nombre = (
            "EVOLUCIONES MEDICAS.pdf"
            if len(bloques) == 1
            else f"EVOLUCIONES MEDICAS {indice:02d}.pdf"
        )
        _imprimir_bloque_evoluciones(
            page,
            pagina_desde,
            pagina_hasta,
            desde,
            hasta,
            carpeta / nombre,
        )


def _epicrisis(page, fecha_ingreso, carpeta):
    page.get_by_role("button", name="Historia Clínica").click()
    page.get_by_role("button", name="Resumen HC").click()
    _esperar_ajax(page)
    hallazgo = re.search(
        r"\b(\d{2}/\d{2}/(?:\d{2}|\d{4}))\b", fecha_ingreso
    )
    if not hallazgo:
        raise RuntimeError("No pude interpretar la fecha de ingreso.")
    fecha = datetime.strptime(
        hallazgo.group(1),
        "%d/%m/%y" if len(hallazgo.group(1)) == 8 else "%d/%m/%Y",
    )
    patron = re.compile(
        rf"{fecha:%d/%m}/(?:{fecha:%y}|{fecha:%Y})"
    )
    filas = page.locator("tr").filter(has_text=patron)
    candidatos = []
    for indice in range(filas.count()):
        fila = filas.nth(indice)
        boton = fila.get_by_role(
            "button", name=re.compile("Epicrisis", re.I)
        )
        if boton.count():
            candidatos.append(boton.first)
    if len(candidatos) != 1:
        raise RuntimeError(
            f"Se encontraron {len(candidatos)} epicrisis para "
            f"{fecha:%d/%m/%Y}."
        )
    destino = carpeta / "EPICRISIS.pdf"
    with page.expect_popup(timeout=120000) as popup_info:
        candidatos[0].click()
    popup = popup_info.value
    popup.wait_for_load_state("domcontentloaded")
    popup.wait_for_url(re.compile(r"\.pdf(?:$|\?)", re.I), timeout=120000)
    respuesta = popup.context.request.get(popup.url)
    if not respuesta.ok:
        raise RuntimeError(f"La Epicrisis respondió HTTP {respuesta.status}.")
    destino.write_bytes(respuesta.body())
    popup.close()
    if destino.stat().st_size == 0:
        raise RuntimeError("EPICRISIS.pdf quedó vacío.")
    volver = page.get_by_role("button", name="Volver", exact=True)
    if volver.count():
        volver.click()


def _recortar_pdf(origen, destino, desde, hasta):
    lector = PdfReader(origen)
    escritor = PdfWriter()
    patrones = [
        (re.compile(r"\b(\d{2}/\d{2}/\d{4})\b"), "%d/%m/%Y"),
        (re.compile(r"\b(\d{2}-\d{2}-\d{4})\b"), "%d-%m-%Y"),
        (re.compile(r"\b(\d{2}/\d{2}/\d{2})\b"), "%d/%m/%y"),
        (re.compile(r"\b(\d{2}-\d{2}-\d{2})\b"), "%d-%m-%y"),
    ]
    ultima_relevante = False
    for pagina in lector.pages:
        texto = pagina.extract_text() or ""
        fechas = []
        for patron, formato in patrones:
            for valor in patron.findall(texto):
                try:
                    fechas.append(datetime.strptime(valor, formato).date())
                except ValueError:
                    pass
        if any(desde <= fecha <= hasta for fecha in fechas):
            escritor.add_page(pagina)
            ultima_relevante = True
        elif not fechas and ultima_relevante:
            escritor.add_page(pagina)
        elif fechas:
            ultima_relevante = False
    if not escritor.pages:
        raise RuntimeError("El PDF de indicaciones no contenía páginas del rango pedido.")
    with open(destino, "wb") as salida:
        escritor.write(salida)


def _superficies_arrastre_timeline(page):
    candidatos = page.locator(
        ".vis-timeline, .timeline, "
        "[class*='Timeline'], [class*='timeline']"
    )
    superficies = []
    for indice in range(candidatos.count()):
        candidato = candidatos.nth(indice)
        try:
            if not candidato.is_visible():
                continue
            caja = candidato.bounding_box()
            if not caja or caja["width"] < 300:
                continue
            clase = (candidato.get_attribute("class") or "").casefold()
            if "event" in clase or "item" in clase:
                continue
            puntaje = caja["width"] / 100
            if any(
                texto in clase
                for texto in (
                    "scroll",
                    "ruler",
                    "rule",
                    "drag",
                    "navigator",
                    "header",
                )
            ):
                puntaje += 100
            if 2 <= caja["height"] <= 45:
                puntaje += 80
            elif caja["height"] <= 120:
                puntaje += 30
            superficies.append((puntaje, candidato, caja, clase))
        except Exception:
            continue
    superficies.sort(key=lambda item: item[0], reverse=True)
    return superficies


def _evento_en_area_visible(page, caja_evento, caja_timeline):
    ancho = page.evaluate("window.innerWidth")
    alto = page.evaluate("window.innerHeight")
    margen = 12
    izquierda = max(margen, caja_timeline["x"] + margen)
    derecha = min(
        ancho - margen,
        caja_timeline["x"] + caja_timeline["width"] - margen,
    )
    arriba = max(margen, caja_timeline["y"])
    abajo = min(
        alto - margen,
        caja_timeline["y"] + caja_timeline["height"],
    )
    centro_x = caja_evento["x"] + caja_evento["width"] / 2
    centro_y = caja_evento["y"] + caja_evento["height"] / 2
    return (
        izquierda <= centro_x <= derecha
        and arriba <= centro_y <= abajo
    )


def _arrastrar_timeline_hacia_evento(page, evento, superficies):
    caja_evento = evento.bounding_box()
    if not caja_evento:
        return False
    ancho_ventana = page.evaluate("window.innerWidth")
    objetivo_x = ancho_ventana / 2
    movimiento_deseado = objetivo_x - (
        caja_evento["x"] + caja_evento["width"] / 2
    )
    if abs(movimiento_deseado) < 15:
        return True

    for _, _, caja, _ in superficies[:8]:
        izquierda = max(caja["x"] + 30, 30)
        derecha = min(
            caja["x"] + caja["width"] - 30,
            ancho_ventana - 30,
        )
        if derecha - izquierda < 120:
            continue
        inicio_x = (izquierda + derecha) / 2
        maximo = (derecha - izquierda) * 0.42
        desplazamiento = max(
            -maximo, min(maximo, movimiento_deseado)
        )
        fin_x = inicio_x + desplazamiento
        y = caja["y"] + caja["height"] / 2
        antes = caja_evento["x"]
        try:
            page.mouse.move(inicio_x, y)
            page.mouse.down()
            page.mouse.move(fin_x, y, steps=24)
            page.mouse.up()
            page.wait_for_timeout(900)
        except Exception:
            try:
                page.mouse.up()
            except Exception:
                pass
            continue
        despues = evento.bounding_box()
        if not despues:
            continue
        cambio = despues["x"] - antes
        if abs(cambio) >= 5 and cambio * movimiento_deseado > 0:
            return True
    return False


def _abrir_evento_internacion(page, fecha_evento):
    patron = re.compile(
        rf"{fecha_evento:%d/%m/%y}\s*-\s*INTERNACION", re.I
    )
    evento = page.get_by_text(patron).first
    evento.wait_for(state="attached", timeout=30000)
    superficies = _superficies_arrastre_timeline(page)
    if not superficies:
        raise RuntimeError(
            "No pude identificar la regla de la línea temporal."
        )
    caja_timeline = max(
        (item[2] for item in superficies),
        key=lambda caja: caja["width"] * caja["height"],
    )

    for _ in range(14):
        caja_evento = evento.bounding_box()
        if (
            caja_evento
            and _evento_en_area_visible(
                page, caja_evento, caja_timeline
            )
        ):
            page.mouse.click(
                caja_evento["x"] + caja_evento["width"] / 2,
                caja_evento["y"] + caja_evento["height"] / 2,
            )
            return
        if not _arrastrar_timeline_hacia_evento(
            page, evento, superficies
        ):
            superficies = _superficies_arrastre_timeline(page)
        page.wait_for_timeout(350)

    caja_final = evento.bounding_box()
    raise RuntimeError(
        "No pude traer al área visible la internación "
        f"del {fecha_evento:%d/%m/%Y}. "
        f"Posición final: {caja_final}."
    )


def _checkbox_posterior(modal, texto):
    etiqueta = modal.get_by_text(texto, exact=True)
    etiqueta.wait_for(state="visible")
    caja = etiqueta.locator(
        "xpath=following::*[contains(@class,'ui-chkbox-box')][1]"
    )
    if caja.count() != 1:
        raise RuntimeError(
            f"No pude identificar el checkbox posterior de {texto}."
        )
    return caja


def _indicaciones(page, trabajo, fecha_ingreso, carpeta):
    hallazgo = re.search(
        r"\b(\d{2}/\d{2}/(?:\d{2}|\d{4}))\b", fecha_ingreso
    )
    if not hallazgo:
        raise RuntimeError("No pude interpretar la fecha de ingreso.")
    valor = hallazgo.group(1)
    fecha_evento = datetime.strptime(
        valor, "%d/%m/%y" if len(valor) == 8 else "%d/%m/%Y"
    )
    boton_historia = page.get_by_role(
        "button", name="Historia Clínica"
    )
    if boton_historia.count() and boton_historia.is_visible():
        boton_historia.click()
    else:
        # Después de descargar Epicrisis se vuelve directamente a la línea
        # temporal de Historia Clínica, donde ese botón ya no existe.
        page.get_by_role(
            "button", name="Resumen HC"
        ).wait_for(state="visible", timeout=60000)
    _abrir_evento_internacion(page, fecha_evento)
    page.get_by_role("button", name="Imprimir Evento Atención").click()
    formulario = page.locator("#frmImpresionEstudios")
    formulario.wait_for(state="visible", timeout=60000)
    modal = formulario.locator(
        "xpath=ancestor::*[contains(@class,'ui-dialog')][1]"
    )
    seleccionar_todos = _checkbox_posterior(
        modal, "Seleccionar Todos"
    )
    if "ui-state-active" in (
        seleccionar_todos.get_attribute("class") or ""
    ):
        seleccionar_todos.click()
        page.wait_for_timeout(500)
    for nombre in ("Indicaciones", "Prescripciones"):
        caja = _checkbox_posterior(modal, nombre)
        if "ui-state-active" not in (
            caja.get_attribute("class") or ""
        ):
            caja.click()
    activas = modal.locator(".ui-chkbox-box.ui-state-active")
    if activas.count() != 2:
        raise RuntimeError(
            f"Debían quedar 2 casillas activas y quedaron "
            f"{activas.count()}."
        )

    temporal = carpeta / "_indicaciones_completo.pdf"
    with page.expect_popup(timeout=180000) as popup_info:
        modal.get_by_role(
            "button", name="Aceptar", exact=True
        ).click()
    popup = popup_info.value
    popup.wait_for_load_state("domcontentloaded")
    popup.wait_for_url(
        re.compile(r"\.pdf(?:$|\?)", re.I), timeout=180000
    )
    respuesta = popup.context.request.get(popup.url)
    if not respuesta.ok:
        raise RuntimeError(
            f"El PDF de indicaciones respondió HTTP {respuesta.status}."
        )
    temporal.write_bytes(respuesta.body())
    popup.close()

    desde = (
        fecha_evento.date()
        if trabajo["rango_tipo"] == "TODA"
        else datetime.strptime(trabajo["fecha_desde"], "%Y-%m-%d").date()
    )
    hasta = (
        datetime.now().date()
        if trabajo["rango_tipo"] == "TODA"
        else datetime.strptime(trabajo["fecha_hasta"], "%Y-%m-%d").date()
    )
    _recortar_pdf(
        temporal, carpeta / "INDICACIONES Y PRESCIPCIONES MEDICAS.pdf", desde, hasta
    )
    temporal.unlink(missing_ok=True)


def ejecutar_trabajo(trabajo, usuario, clave, informar):
    carpeta = _carpeta_trabajo(trabajo)
    carpeta.mkdir(parents=True, exist_ok=True)
    progreso = _leer_progreso(carpeta)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=PLAYWRIGHT_HEADLESS,
            slow_mo=PLAYWRIGHT_SLOW_MO_MS,
        )
        opciones_contexto = dict(
            accept_downloads=True,
            viewport={"width": 1500, "height": 900},
        )
        if PLAYWRIGHT_RECORD_VIDEO:
            opciones_contexto["record_video_dir"] = str(carpeta / "video")
        context = browser.new_context(**opciones_contexto)
        page = context.new_page()
        page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
        try:
            informar(
                "EN PROCESO",
                f"Iniciando sesión... ({AUTOMATION_VERSION})",
                carpeta,
            )
            _login(page, usuario, clave)

            fecha_ingreso = progreso.get("admision", {}).get("fecha_ingreso")
            en_ficha_admision = False
            necesita_admision = (
                not _seccion_completa(progreso, "admision")
                or not fecha_ingreso
            )
            if necesita_admision:
                informar(
                    "EN PROCESO",
                    "Buscando admisión y fecha de ingreso...",
                )
                fecha_ingreso = _buscar_admision(page, trabajo)
                en_ficha_admision = True
                _completar_seccion(
                    carpeta,
                    progreso,
                    "admision",
                    fecha_ingreso=fecha_ingreso,
                )
            else:
                informar(
                    "EN PROCESO",
                    f"Admisión ya validada. Ingreso: {fecha_ingreso}.",
                )

            informe = carpeta / "INFORME DE HOSPITALIZACION.pdf"
            informe_valido = (
                informe.exists() and informe.stat().st_size > 0
            )
            if informe_valido and not _seccion_completa(
                progreso, "informe"
            ):
                _completar_seccion(carpeta, progreso, "informe")
            necesita_informe = not informe_valido
            necesita_documentacion = not _seccion_completa(
                progreso, "documentacion"
            )

            if (
                necesita_informe or necesita_documentacion
            ) and not en_ficha_admision:
                fecha_ingreso = _buscar_admision(page, trabajo)
                en_ficha_admision = True

            if necesita_informe:
                if not en_ficha_admision:
                    fecha_ingreso = _buscar_admision(page, trabajo)
                informar(
                    "EN PROCESO",
                    "Procesando informe de hospitalización...",
                )
                _informe_hospitalizacion(page, carpeta)
                _completar_seccion(carpeta, progreso, "informe")
                informar(
                    "EN PROCESO",
                    "Informe de hospitalización descargado.",
                )
            else:
                informar(
                    "EN PROCESO",
                    "Informe de hospitalización ya descargado; se omite.",
                )

            if necesita_documentacion:
                informar(
                    "EN PROCESO",
                    "Procesando documentación adicional...",
                )
                _documentacion(page, carpeta)
                _completar_seccion(carpeta, progreso, "documentacion")
                informar(
                    "EN PROCESO",
                    "Documentación adicional descargada.",
                )
            else:
                informar(
                    "EN PROCESO",
                    "Documentación adicional ya completada; se omite.",
                )

            epicrisis = carpeta / "EPICRISIS.pdf"
            indicaciones = (
                carpeta / "INDICACIONES Y PRESCIPCIONES MEDICAS.pdf"
            )
            necesita_evoluciones = not _seccion_completa(
                progreso, "evoluciones"
            )
            necesita_epicrisis = (
                not _seccion_completa(progreso, "epicrisis")
                or not epicrisis.exists()
                or epicrisis.stat().st_size == 0
            )
            necesita_indicaciones = (
                not _seccion_completa(progreso, "indicaciones")
                or not indicaciones.exists()
                or indicaciones.stat().st_size == 0
            )

            if (
                necesita_evoluciones
                or necesita_epicrisis
                or necesita_indicaciones
            ):
                informar(
                    "EN PROCESO",
                    "Cargando internación e historia clínica...",
                )
                _buscar_internacion(page, trabajo)

            if necesita_evoluciones:
                informar(
                    "EN PROCESO",
                    "Procesando evoluciones médicas...",
                )
                _evoluciones(page, trabajo, fecha_ingreso, carpeta)
                _completar_seccion(carpeta, progreso, "evoluciones")
                informar(
                    "EN PROCESO",
                    "Evoluciones médicas descargadas.",
                )
            else:
                informar(
                    "EN PROCESO",
                    "Evoluciones ya completadas; se omiten.",
                )

            if necesita_epicrisis:
                informar("EN PROCESO", "Procesando epicrisis...")
                _epicrisis(page, fecha_ingreso, carpeta)
                _completar_seccion(carpeta, progreso, "epicrisis")
                informar("EN PROCESO", "Epicrisis descargada.")
            else:
                informar(
                    "EN PROCESO",
                    "Epicrisis ya descargada; se omite.",
                )

            if necesita_indicaciones:
                informar(
                    "EN PROCESO",
                    "Procesando indicaciones y prescripciones...",
                )
                _indicaciones(page, trabajo, fecha_ingreso, carpeta)
                _completar_seccion(carpeta, progreso, "indicaciones")
                informar(
                    "EN PROCESO",
                    "Indicaciones descargadas.",
                )
            else:
                informar(
                    "EN PROCESO",
                    "Indicaciones ya descargadas; se omiten.",
                )
            return carpeta
        except Exception:
            try:
                if not page.is_closed():
                    page.screenshot(path=str(carpeta / "ERROR.png"), full_page=True)
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()
