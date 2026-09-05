# API del Centro de Gestion — paso 1: solo prueba de vida.
# Sin dependencias: usa el servidor HTTP que ya trae Python.
# Todavia no habla con Mongo ni con nadie. Eso viene en el paso siguiente.

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Unicos sitios autorizados a llamar esta API desde el navegador.
ORIGENES_PERMITIDOS = [
    "https://centrogestion.pages.dev",
    "https://centrogestion-test.pages.dev",
]


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
            self._responder(200, {
                "ok": True,
                "mensaje": "estoy viva",
                "hora": datetime.now(timezone.utc).isoformat(),
            })
        else:
            self._responder(404, {"ok": False, "error": "ruta no encontrada"})

    def log_message(self, formato, *args):
        # Log en una linea, para que se lea bien en Railway.
        print("%s - %s" % (self.address_string(), formato % args), flush=True)


if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", "8080"))
    servidor = ThreadingHTTPServer(("0.0.0.0", puerto), Manejador)
    print("API escuchando en el puerto %d" % puerto, flush=True)
    servidor.serve_forever()
