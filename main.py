# API del Centro de Gestion — paso 2a: conexion a MongoDB.
# Sigue sin autenticacion y sin datos: lo unico que agrega es conectarse
# a Mongo y decir en /salud si la conexion esta viva.

import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Unicos sitios autorizados a llamar esta API desde el navegador.
ORIGENES_PERMITIDOS = [
    "https://centrogestion.pages.dev",
    "https://centrogestion-test.pages.dev",
]

MONGO_URL = os.environ.get("MONGO_URL", "")
NOMBRE_BASE = os.environ.get("MONGO_DB", "centrogestion")

_cliente = None
_candado = threading.Lock()


def obtener_base():
    """Crea el cliente de Mongo una sola vez y lo reutiliza."""
    global _cliente
    if not MONGO_URL:
        return None
    if _cliente is None:
        with _candado:
            if _cliente is None:
                _cliente = MongoClient(
                    MONGO_URL,
                    serverSelectionTimeoutMS=3000,
                    connectTimeoutMS=3000,
                )
    return _cliente[NOMBRE_BASE]


def estado_mongo():
    """Revisa la conexion sin tumbar la API si Mongo no responde."""
    if not MONGO_URL:
        return {"conectado": False, "detalle": "falta la variable MONGO_URL"}
    try:
        base = obtener_base()
        base.command("ping")
        return {"conectado": True, "base": NOMBRE_BASE}
    except PyMongoError as e:
        return {"conectado": False, "detalle": type(e).__name__}
    except Exception as e:
        return {"conectado": False, "detalle": type(e).__name__}


class Manejador(BaseHTTPRequestHandler):
    server_version = "centrogestion-api"

    def _cors(self):
        origen = self.headers.get("Origin", "")
        if origen in ORIGENES_PERMITIDOS:
            self.send_header("Access-Control-Allow-Origin", origen)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "86400")

    def _responder(self, codigo, cuerpo):
        datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(datos)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        ruta = self.path.split("?")[0].rstrip("/") or "/"
        if ruta in ("/", "/salud"):
            mongo = estado_mongo()
            self._responder(200, {
                "ok": True,
                "mensaje": "estoy viva",
                "hora": datetime.now(timezone.utc).isoformat(),
                "mongo": mongo,
            })
        else:
            self._responder(404, {"ok": False, "error": "ruta no encontrada"})

    def log_message(self, formato, *args):
        print("%s - %s" % (self.address_string(), formato % args), flush=True)


if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", "8080"))
    servidor = ThreadingHTTPServer(("0.0.0.0", puerto), Manejador)
    print("API escuchando en el puerto %d" % puerto, flush=True)
    servidor.serve_forever()
