import os
import logging
from logging.handlers import RotatingFileHandler

from waitress import serve

from app import (
    APP_HOST,
    APP_PORT,
    DATA_DIR,
    MAX_AUTOMATIZACIONES,
    _recuperar_trabajos_interrumpidos,
    app,
    iniciar_consumidores,
)


if __name__ == "__main__":
    if os.environ.get("SECRETARIO_SECRET", "").startswith("REEMPLAZAR_"):
        raise RuntimeError(
            "SECRETARIO_SECRET todavía tiene el valor de ejemplo."
        )
    logs = DATA_DIR / "logs"
    logs.mkdir(exist_ok=True)
    manejador = RotatingFileHandler(
        logs / "servidor.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    manejador.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    )
    app.logger.setLevel(logging.INFO)
    app.logger.addHandler(manejador)

    _recuperar_trabajos_interrumpidos()
    iniciar_consumidores()
    hilos_http = max(
        8,
        int(os.environ.get("HTTP_THREADS", str(MAX_AUTOMATIZACIONES * 4 + 8))),
    )
    print(
        "Secretario de Sala iniciado en "
        f"http://{APP_HOST}:{APP_PORT} | "
        f"Chromium simultáneos: {MAX_AUTOMATIZACIONES} | "
        f"hilos HTTP: {hilos_http}",
        flush=True,
    )
    serve(
        app,
        host=APP_HOST,
        port=APP_PORT,
        threads=hilos_http,
        channel_timeout=300,
        cleanup_interval=30,
    )
