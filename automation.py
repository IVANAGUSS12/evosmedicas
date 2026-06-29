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
AUTOMATION_VERSION = "2026-06-29-c"
PLAYWRIGHT_SLOW_MO_MS = max(0, int(os.environ.get("PLAYWRIGHT_SLOW_MO_MS", "220")))
PLAYWRIGHT_TIMEOUT_MS = max(20000, int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "35000")))
EVOLUCIONES_PAGINAS_POR_ARCHIVO = max(
    1, int(os.environ.get("EVOLUCIONES_PAGINAS_POR_ARCHIVO", "5"))
)
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


class _PopupPdfYaAbierto(RuntimeError):
    pass


def _checkbox_activo(caja):
    return "ui-state-active" in (caja.get_attribute("class") or "")


def _fijar_checkbox(page, caja, activo_objetivo, intentos=4):
    for _ in range(intentos):
        if _checkbox_activo(caja) == activo_objetivo:
            return
        caja.evaluate("el => el.click()")
        _esperar_ajax(page)
        page.wait_for_timeout(150)
    raise RuntimeError(
        f"No se pudo {'marcar' if activo_objetivo else 'desmarcar'} "
        "una casilla de evoluciones."
    )


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


def _limpiar_seleccion_evoluciones(page, modal, primera, ultima):
    page.evaluate(
        """() => {
            const tabla = document.getElementById('frmImpresion:tablaEvo_data')
                || document.querySelector('[id$="tablaEvo_data"]');
            const selection = document.querySelector(
                'input[type="hidden"][id$="tablaEvo_selection"], ' +
                'input[type="hidden"][name$="tablaEvo_selection"]'
            );
            const pintar = (fila, seleccionada) => {
                fila.setAttribute('aria-selected', seleccionada ? 'true' : 'false');
                fila.classList.toggle('ui-state-highlight', seleccionada);
                const input = fila.querySelector(
                    ':scope > td.ui-selection-column input[type="checkbox"]'
                );
                const box = fila.querySelector(
                    ':scope > td.ui-selection-column .ui-chkbox-box'
                );
                const icon = fila.querySelector(
                    ':scope > td.ui-selection-column .ui-chkbox-icon'
                );
                if (input) {
                    input.checked = seleccionada;
                    input.setAttribute('aria-checked', seleccionada ? 'true' : 'false');
                }
                if (box) {
                    box.classList.toggle('ui-state-active', seleccionada);
                }
                if (icon) {
                    icon.classList.toggle('ui-icon-check', seleccionada);
                    icon.classList.toggle('ui-icon-blank', !seleccionada);
                }
            };
            const limpiarHeader = () => {
                document
                    .querySelectorAll(
                        '#frmImpresion\\\\:tablaEvo_head .ui-chkbox-box, ' +
                        'thead .ui-chkbox-box'
                    )
                    .forEach((box) => box.classList.remove('ui-state-active'));
                document
                    .querySelectorAll(
                        '#frmImpresion\\\\:tablaEvo_head .ui-chkbox-icon, ' +
                        'thead .ui-chkbox-icon'
                    )
                    .forEach((icon) => {
                        icon.classList.remove('ui-icon-check');
                        icon.classList.add('ui-icon-blank');
                    });
                document.querySelectorAll('thead input[type="checkbox"]').forEach((input) => {
                    input.checked = false;
                    input.setAttribute('aria-checked', 'false');
                });
            };
            if (tabla) {
                tabla
                    .querySelectorAll(':scope > tr[data-rk]')
                    .forEach((fila) => pintar(fila, false));
            }
            if (selection) {
                selection.value = '';
                selection.dispatchEvent(new Event('change', { bubbles: true }));
            }
            limpiarHeader();
        }"""
    )
    _esperar_ajax(page)


def _ids_evoluciones_pagina(page, modal, desde, hasta):
    filas = modal.locator("tbody tr")
    ids = []
    for indice in range(filas.count()):
        fila = filas.nth(indice)
        fecha = _fecha_evolucion(fila.inner_text())
        if not fecha or not _evolucion_en_rango(fecha, desde, hasta):
            continue
        data_rk = fila.get_attribute("data-rk")
        if not data_rk:
            raise RuntimeError("Una fila de evolucion no tiene data-rk.")
        ids.append(data_rk)
    return ids


def _aplicar_ids_evoluciones(page, ids):
    page.evaluate(
        """(ids) => {
            const seleccion = [...new Set(ids.filter(Boolean))];
            const tabla = document.getElementById('frmImpresion:tablaEvo_data')
                || document.querySelector('[id$="tablaEvo_data"]');
            const selection = document.querySelector(
                'input[type="hidden"][id$="tablaEvo_selection"], ' +
                'input[type="hidden"][name$="tablaEvo_selection"]'
            );
            const setIds = new Set(seleccion);
            const pintar = (fila, seleccionada) => {
                fila.setAttribute('aria-selected', seleccionada ? 'true' : 'false');
                fila.classList.toggle('ui-state-highlight', seleccionada);
                const input = fila.querySelector(
                    ':scope > td.ui-selection-column input[type="checkbox"]'
                );
                const box = fila.querySelector(
                    ':scope > td.ui-selection-column .ui-chkbox-box'
                );
                const icon = fila.querySelector(
                    ':scope > td.ui-selection-column .ui-chkbox-icon'
                );
                if (input) {
                    input.checked = seleccionada;
                    input.setAttribute('aria-checked', seleccionada ? 'true' : 'false');
                }
                if (box) {
                    box.classList.toggle('ui-state-active', seleccionada);
                }
                if (icon) {
                    icon.classList.toggle('ui-icon-check', seleccionada);
                    icon.classList.toggle('ui-icon-blank', !seleccionada);
                }
            };
            if (tabla) {
                tabla.querySelectorAll(':scope > tr[data-rk]').forEach((fila) => {
                    pintar(fila, setIds.has(fila.getAttribute('data-rk')));
                });
            }
            if (selection) {
                selection.value = seleccion.join(',');
                selection.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""",
        ids,
    )
    _esperar_ajax(page)


def _seleccionar_bloque_evoluciones(
    page, modal, pagina_desde, pagina_hasta, desde, hasta
):
    _ir_pagina_evoluciones(page, modal, pagina_desde)
    ids = []
    for numero in range(pagina_desde, pagina_hasta + 1):
        ids.extend(_ids_evoluciones_pagina(page, modal, desde, hasta))
        _aplicar_ids_evoluciones(page, ids)
        if numero < pagina_hasta:
            modal.locator(
                ".ui-paginator-next:not(.ui-state-disabled)"
            ).click()
            _esperar_ajax(page)
    return len(ids)


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
            data_rk = fila.get_attribute("data-rk")
            seleccionado = page.evaluate(
                """(id) => {
                    const selection = document.querySelector(
                        'input[type="hidden"][id$="tablaEvo_selection"], ' +
                        'input[type="hidden"][name$="tablaEvo_selection"]'
                    );
                    const valores = selection && selection.value
                        ? selection.value.split(',').filter(Boolean)
                        : [];
                    return valores.includes(id);
                }""",
                data_rk,
            )
            clases = fila.locator(".ui-chkbox-box").first.get_attribute("class") or ""
            if not seleccionado or "ui-state-active" not in clases:
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


def _pdf_valido(ruta):
    ruta = Path(ruta)
    if not ruta.is_file() or ruta.stat().st_size < 100:
        return False
    try:
        with ruta.open("rb") as archivo:
            if archivo.read(5) != b"%PDF-":
                return False
        lector = PdfReader(str(ruta))
        return len(lector.pages) > 0
    except Exception:
        return False


def _pdfs_en_carpeta(carpeta):
    if not carpeta.is_dir():
        return set()
    return {
        archivo.resolve()
        for archivo in carpeta.glob("*.pdf")
        if archivo.is_file()
    } | {
        archivo.resolve()
        for archivo in carpeta.glob("*.PDF")
        if archivo.is_file()
    }


def _adoptar_pdf_descargado(carpeta, destino, conocidos, desde_ts, timeout=15000):
    fin = time.monotonic() + timeout / 1000
    destino = Path(destino)
    carpeta = Path(carpeta)
    while time.monotonic() < fin:
        candidatos = []
        for archivo in _pdfs_en_carpeta(carpeta):
            if archivo == destino.resolve() or archivo in conocidos:
                continue
            try:
                if archivo.stat().st_mtime < desde_ts:
                    continue
            except OSError:
                continue
            candidatos.append(archivo)

        candidatos.sort(
            key=lambda archivo: archivo.stat().st_mtime,
            reverse=True,
        )
        for archivo in candidatos:
            if not _pdf_valido(archivo):
                continue
            archivo.replace(destino)
            return True
        time.sleep(0.5)
    return _pdf_valido(destino)


def _limpiar_pdfs_descargados(carpeta, destino, conocidos, desde_ts, timeout=3000):
    fin = time.monotonic() + timeout / 1000
    destino = Path(destino).resolve()
    carpeta = Path(carpeta)
    while time.monotonic() < fin:
        removidos = False
        for archivo in _pdfs_en_carpeta(carpeta):
            if archivo == destino or archivo in conocidos:
                continue
            try:
                if archivo.stat().st_mtime < desde_ts:
                    continue
                if _pdf_valido(archivo):
                    archivo.unlink()
                    removidos = True
            except OSError:
                continue
        if removidos:
            return
        time.sleep(0.5)


def _cerrar_paginas_secundarias(page):
    for pagina in list(page.context.pages):
        if pagina != page:
            try:
                pagina.close()
            except Exception:
                pass


def _buscar_pagina_secundaria(page, conocidas=None, timeout=10000):
    conocidas = conocidas or set()
    fin = time.monotonic() + timeout / 1000
    while time.monotonic() < fin:
        candidatas = []
        for pagina in list(page.context.pages):
            try:
                if pagina == page or pagina.is_closed():
                    continue
            except Exception:
                continue
            candidatas.append(pagina)
            if id(pagina) not in conocidas:
                return pagina

        for pagina in candidatas:
            try:
                if re.search(r"\.pdf(?:$|\?)", pagina.url, re.I):
                    return pagina
            except Exception:
                pass
        page.wait_for_timeout(250)
    return None


def _abrir_popup_pdf_desde_modal(page, modal, intentos=3):
    ultimo = None
    for intento in range(intentos):
        try:
            _cerrar_paginas_secundarias(page)
            _esperar_capa_carga(page, timeout=15000)
            boton = modal.get_by_role(
                "button", name="Imprimir", exact=True
            )
            boton.wait_for(state="visible", timeout=30000)
            paginas_antes = {id(pagina) for pagina in page.context.pages}
            with page.expect_popup(timeout=60000) as popup_info:
                boton.click()
            return popup_info.value
        except PlaywrightTimeout as exc:
            ultimo = exc
            popup = _buscar_pagina_secundaria(
                page, paginas_antes, timeout=15000
            )
            if popup is not None:
                return popup
            _cerrar_paginas_secundarias(page)
            if intento < intentos - 1:
                page.wait_for_timeout(2500)
    raise RuntimeError(
        "No se abrio el popup del PDF luego de volver a imprimir "
        f"{intentos} veces: {ultimo}"
    )


def _esperar_url_pdf(pagina, timeout=180000):
    patron = re.compile(r"\.pdf(?:$|\?)", re.I)
    fin = time.monotonic() + timeout / 1000
    ultimo = None
    while time.monotonic() < fin:
        try:
            url = pagina.url
            if patron.search(url):
                return url
            pagina.wait_for_url(patron, timeout=3000)
            return pagina.url
        except PlaywrightTimeout as exc:
            ultimo = exc
        except Exception as exc:
            ultimo = exc
            break
    raise RuntimeError(f"No se detecto la URL del PDF abierto: {ultimo}")


def _descargar_pdf_url(page, modal, destino):
    temporal = destino.with_suffix(destino.suffix + ".part")
    temporal.unlink(missing_ok=True)
    carpeta = destino.parent
    conocidos = _pdfs_en_carpeta(carpeta)
    desde_ts = time.time() - 2
    popup = _abrir_popup_pdf_desde_modal(page, modal)
    try:
        pdf_url = _esperar_url_pdf(popup)
        respuesta = popup.context.request.get(pdf_url)
        if not respuesta.ok:
            raise RuntimeError(
                f"El PDF respondió HTTP {respuesta.status}."
            )
        temporal.write_bytes(respuesta.body())
        if not _pdf_valido(temporal):
            raise RuntimeError(
                "El archivo generado no es un PDF válido."
            )
        temporal.replace(destino)
        _limpiar_pdfs_descargados(carpeta, destino, conocidos, desde_ts)
    except Exception as exc:
        if _adoptar_pdf_descargado(
            carpeta, destino, conocidos, desde_ts, timeout=15000
        ):
            return
        if _pdf_valido(destino):
            _limpiar_pdfs_descargados(carpeta, destino, conocidos, desde_ts)
            return
        raise _PopupPdfYaAbierto(
            "El popup del PDF ya se abrio; no se vuelve a presionar Imprimir "
            f"para este bloque. Error al guardar: {exc}"
        ) from exc
    finally:
        temporal.unlink(missing_ok=True)
        try:
            if not popup.is_closed():
                popup.close()
        except Exception:
            pass


def _cerrar_modal_evoluciones(page):
    if page.is_closed():
        return False
    for pagina in list(page.context.pages):
        if pagina != page:
            try:
                pagina.close()
            except Exception:
                pass
    modal = page.get_by_label("Imprimir")
    if modal.count() and modal.is_visible():
        volver = modal.get_by_role(
            "button", name="Volver", exact=True
        )
        try:
            _esperar_capa_carga(page, timeout=12000)
            volver.click(timeout=10000)
        except Exception:
            try:
                volver.click(force=True, timeout=5000)
            except Exception:
                try:
                    volver.evaluate("(boton) => boton.click()")
                except Exception:
                    return False
        try:
            modal.wait_for(state="hidden", timeout=30000)
        except Exception:
            return False
    return True


def _cerrar_impresion_abierta(page):
    if page.is_closed():
        return
    for pagina in list(page.context.pages):
        if pagina != page:
            try:
                pagina.close()
            except Exception:
                pass
    for _ in range(3):
        modal = page.get_by_label("Imprimir")
        try:
            if not (modal.count() and modal.is_visible()):
                break
        except Exception:
            break
        botones = [
            modal.get_by_role("button", name="Volver", exact=True),
            modal.get_by_role("button", name="Cerrar", exact=True),
        ]
        cerrado = False
        for boton in botones:
            if not boton.count():
                continue
            try:
                boton.first.click(force=True, timeout=5000)
                modal.wait_for(state="hidden", timeout=10000)
                cerrado = True
                break
            except Exception:
                try:
                    boton.first.evaluate("(el) => el.click()")
                    modal.wait_for(state="hidden", timeout=10000)
                    cerrado = True
                    break
                except Exception:
                    pass
        if cerrado:
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    try:
        page.locator("#LoadingDialog_modal").evaluate_all(
            "(els) => els.forEach((el) => el.style.display = 'none')"
        )
    except Exception:
        pass
    try:
        page.locator("#popupImpresion_modal").evaluate_all(
            "(els) => els.forEach((el) => el.style.display = 'none')"
        )
    except Exception:
        pass
    _esperar_capa_carga(page, timeout=3000)


def _modal_evoluciones_abierto(page):
    modal = page.get_by_label("Imprimir")
    try:
        if modal.count() and modal.is_visible():
            return modal
    except Exception:
        pass
    return None


def _abrir_modal_evoluciones(page):
    modal = _modal_evoluciones_abierto(page)
    if modal is not None:
        return modal
    _esperar_capa_carga(page, timeout=15000)
    page.get_by_role("button", name="Imprimir Evoluciones").click()
    modal = page.get_by_label("Imprimir")
    modal.wait_for(state="visible", timeout=60000)
    return modal


def _imprimir_bloque_evoluciones(
    page,
    primera,
    ultima,
    pagina_desde,
    pagina_hasta,
    desde,
    hasta,
    destino,
    al_guardar=None,
):
    if _pdf_valido(destino):
        if al_guardar is not None:
            al_guardar()
        _cerrar_paginas_secundarias(page)
        return
    destino.unlink(missing_ok=True)

    ultimo = None
    for _ in range(3):
        try:
            modal = _abrir_modal_evoluciones(page)
            _ordenar_evoluciones(page, modal)
            _limpiar_seleccion_evoluciones(page, modal, primera, ultima)
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
            if _pdf_valido(destino) and al_guardar is not None:
                al_guardar()
            # Desde este punto el bloque ya está validado y guardado.
            # Un fallo al cerrar la interfaz no debe volver a imprimirlo.
            _cerrar_paginas_secundarias(page)
            return
        except Exception as exc:
            ultimo = exc
            if isinstance(exc, _PopupPdfYaAbierto):
                if _pdf_valido(destino):
                    if al_guardar is not None:
                        al_guardar()
                    return
                raise
            if _pdf_valido(destino):
                if al_guardar is not None:
                    al_guardar()
                _cerrar_paginas_secundarias(page)
                return
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
            if page.is_closed():
                break
            try:
                page.wait_for_timeout(3000)
            except Exception:
                break
    raise RuntimeError(
        f"No se pudo imprimir páginas {pagina_desde}-{pagina_hasta}: "
        f"{ultimo}"
    )


def _bloques_paginas_evoluciones(primera, ultima):
    return [
        (
            inicio,
            min(
                inicio + EVOLUCIONES_PAGINAS_POR_ARCHIVO - 1,
                ultima,
            ),
        )
        for inicio in range(
            primera,
            ultima + 1,
            EVOLUCIONES_PAGINAS_POR_ARCHIVO,
        )
    ]


def _bloque_evoluciones_completo(progreso, clave, destino):
    bloques = progreso.get("evoluciones_bloques", {})
    return bool(bloques.get(clave, {}).get("completada")) and _pdf_valido(destino)


def _completar_bloque_evoluciones(
    carpeta,
    progreso,
    clave,
    destino,
    pagina_desde,
    pagina_hasta,
):
    bloques = progreso.setdefault("evoluciones_bloques", {})
    bloques[clave] = {
        "completada": True,
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "archivo": destino.name,
        "pagina_desde": pagina_desde,
        "pagina_hasta": pagina_hasta,
    }
    _guardar_progreso(carpeta, progreso)


def _evoluciones(page, trabajo, fecha_ingreso, carpeta, progreso):
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
    bloques = _bloques_paginas_evoluciones(primera, ultima)
    for indice, (pagina_desde, pagina_hasta) in enumerate(
        bloques, start=1
    ):
        clave = f"{pagina_desde}-{pagina_hasta}"
        nombre = (
            "EVOLUCIONES MEDICAS.pdf"
            if len(bloques) == 1
            else f"EVOLUCIONES MEDICAS {indice:02d}.pdf"
        )
        destino = carpeta / nombre
        if _bloque_evoluciones_completo(progreso, clave, destino):
            continue
        if _pdf_valido(destino):
            _completar_bloque_evoluciones(
                carpeta,
                progreso,
                clave,
                destino,
                pagina_desde,
                pagina_hasta,
            )
            continue
        def marcar_guardado():
            _completar_bloque_evoluciones(
                carpeta,
                progreso,
                clave,
                destino,
                pagina_desde,
                pagina_hasta,
            )

        _imprimir_bloque_evoluciones(
            page,
            primera,
            ultima,
            pagina_desde,
            pagina_hasta,
            desde,
            hasta,
            destino,
            al_guardar=marcar_guardado,
        )
        if not _bloque_evoluciones_completo(progreso, clave, destino):
            _completar_bloque_evoluciones(
                carpeta,
                progreso,
                clave,
                destino,
                pagina_desde,
                pagina_hasta,
            )
    _cerrar_impresion_abierta(page)


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


def _localizar_evento_atencion(page, fecha_evento):
    patrones = [
        re.compile(rf"{fecha_evento:%d/%m/%y}\s*-\s*\S+", re.I),
        re.compile(rf"{fecha_evento:%d/%m/%Y}\s*-\s*\S+", re.I),
    ]
    selectores = [
        ".vis-item",
        ".vis-item-content",
        "[class*='timeline'] [class*='item']",
        "[class*='Timeline'] [class*='item']",
    ]
    for patron in patrones:
        for selector in selectores:
            candidato = page.locator(selector).filter(has_text=patron).first
            try:
                if candidato.count():
                    return candidato
            except Exception:
                pass
        candidato = page.get_by_text(patron).first
        try:
            if candidato.count():
                return candidato
        except Exception:
            pass
    raise RuntimeError(
        "No pude encontrar en la linea temporal un evento para "
        f"{fecha_evento:%d/%m/%Y}."
    )


def _abrir_evento_internacion(page, fecha_evento):
    evento = _localizar_evento_atencion(page, fecha_evento)
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
    imprimir = page.get_by_role(
        "button", name="Imprimir Evento Atención"
    )
    if not imprimir.count():
        imprimir = page.get_by_role("button", name="Imprimir", exact=True)
    imprimir.click()
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
    _cerrar_paginas_secundarias(page)
    paginas_antes = {id(pagina) for pagina in page.context.pages}
    try:
        with page.expect_popup(timeout=180000) as popup_info:
            modal.get_by_role(
                "button", name="Aceptar", exact=True
            ).click()
        popup = popup_info.value
    except PlaywrightTimeout as exc:
        popup = _buscar_pagina_secundaria(
            page, paginas_antes, timeout=15000
        )
        if popup is None:
            raise RuntimeError(
                "No se abrio el popup del PDF de indicaciones."
            ) from exc
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
            downloads_path=str(carpeta),
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
                _evoluciones(page, trabajo, fecha_ingreso, carpeta, progreso)
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

            _cerrar_impresion_abierta(page)

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
