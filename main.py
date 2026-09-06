# API del Centro de Gestion — paso 2b: autenticacion con usuarios reales.
#
# Que hace hoy:
#   GET  /            y  /salud         estado de la API y de Mongo
#   POST /auth/entrar                   correo + clave -> testigo de sesion
#   GET  /auth/yo                       quien soy (y renueva la sesion)
#   POST /auth/salir                    cierra la sesion
#   POST /auth/cambiar-clave            cambia la clave del usuario en sesion
#
# Todavia NO toca los datos del negocio. Eso viene despues, empezando por
# la coleccion de clientes.

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

# ── Ajustes ──────────────────────────────────────────────────────────────
# Sitios autorizados a llamar esta API desde el navegador.
# Se pueden cambiar sin tocar el codigo: variable ORIGENES en Railway,
# separados por coma. Si no esta puesta, valen los dos de siempre.
_ORIGENES_POR_DEFECTO = "https://centrogestion.pages.dev,https://centrogestion-test.pages.dev"
ORIGENES_PERMITIDOS = [
    o.strip().rstrip("/")
    for o in os.environ.get("ORIGENES", _ORIGENES_POR_DEFECTO).split(",")
    if o.strip()
]

MONGO_URL = os.environ.get("MONGO_URL", "")
NOMBRE_BASE = os.environ.get("MONGO_DB", "centrogestion")

DIAS_SESION = 30              # vence a los 30 dias SIN USARSE (se renueva sola)
MAX_INTENTOS = 10             # intentos fallidos seguidos sobre un mismo correo
BLOQUEO_SEGUNDOS = 15 * 60    # cuanto dura el bloqueo despues de fallar

CUERPO_MAXIMO = 64 * 1024     # nadie manda mas que esto en estas rutas

_cliente = None
_candado = threading.Lock()
_intentos = {}                # correo -> [cantidad, momento del ultimo fallo]
_candado_intentos = threading.Lock()


# ── Mongo ────────────────────────────────────────────────────────────────
def obtener_base():
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
    if not MONGO_URL:
        return {"conectado": False, "detalle": "falta la variable MONGO_URL"}
    try:
        obtener_base().command("ping")
        return {"conectado": True, "base": NOMBRE_BASE}
    except Exception as e:
        return {"conectado": False, "detalle": type(e).__name__}


# ── Claves: cifrado en un solo sentido ───────────────────────────────────
# scrypt viene con Python, no agrega dependencias. Lo guardado es
# "scrypt$sal$resultado"; la clave original no se puede recuperar de ahi.
def cifrar_clave(clave):
    sal = secrets.token_bytes(16)
    res = hashlib.scrypt(clave.encode("utf-8"), salt=sal, n=16384, r=8, p=1, dklen=32)
    return "scrypt$" + sal.hex() + "$" + res.hex()


def verificar_clave(clave, guardado):
    try:
        etiqueta, sal_hex, res_hex = str(guardado).split("$")
        if etiqueta != "scrypt":
            return False
        res = hashlib.scrypt(
            clave.encode("utf-8"), salt=bytes.fromhex(sal_hex),
            n=16384, r=8, p=1, dklen=32,
        )
        # comparacion de tiempo constante: no delata cuanto acerto
        return hmac.compare_digest(res.hex(), res_hex)
    except Exception:
        return False


# ── Freno a los intentos fallidos ────────────────────────────────────────
def esta_bloqueado(correo):
    with _candado_intentos:
        dato = _intentos.get(correo)
        if not dato:
            return False
        cantidad, ultimo = dato
        if time.time() - ultimo > BLOQUEO_SEGUNDOS:
            _intentos.pop(correo, None)
            return False
        return cantidad >= MAX_INTENTOS


def anotar_fallo(correo):
    with _candado_intentos:
        cantidad, _ = _intentos.get(correo, (0, 0))
        _intentos[correo] = (cantidad + 1, time.time())


def limpiar_intentos(correo):
    with _candado_intentos:
        _intentos.pop(correo, None)


# ── Usuarios y sesiones ──────────────────────────────────────────────────
def ahora():
    return datetime.now(timezone.utc)


def preparar_base():
    """Indices y creacion del primer administrador. Se corre al arrancar."""
    base = obtener_base()
    if base is None:
        print("AVISO: sin MONGO_URL, no se puede preparar la base", flush=True)
        return
    base.usuarios.create_index([("correo", ASCENDING)], unique=True)
    base.sesiones.create_index([("usuarioId", ASCENDING)])

    correo = (os.environ.get("ADMIN_CORREO", "") or "").strip().lower()
    nombre = os.environ.get("ADMIN_NOMBRE", "") or ""
    clave = os.environ.get("ADMIN_CLAVE", "") or ""
    if not (correo and clave):
        return
    if base.usuarios.find_one({"correo": correo}):
        return
    base.usuarios.insert_one({
        "correo": correo,
        "nombre": nombre or correo,
        "rol": "admin",
        "permisos": {"editar": True},
        "activo": True,
        "clave": cifrar_clave(clave),
        "creado": ahora(),
    })
    print("Usuario administrador inicial creado: %s" % correo, flush=True)


def crear_sesion(usuario):
    base = obtener_base()
    testigo = secrets.token_urlsafe(32)
    base.sesiones.insert_one({
        "_id": testigo,
        "usuarioId": usuario["_id"],
        "creada": ahora(),
        "ultimoUso": ahora(),
        "vence": ahora() + timedelta(days=DIAS_SESION),
    })
    return testigo


def usuario_de_sesion(testigo):
    """Devuelve el usuario si el testigo sirve, y corre el vencimiento."""
    if not testigo:
        return None
    base = obtener_base()
    if base is None:
        return None
    ses = base.sesiones.find_one({"_id": testigo})
    if not ses:
        return None
    vence = ses.get("vence")
    if vence is not None:
        if vence.tzinfo is None:
            vence = vence.replace(tzinfo=timezone.utc)
        if vence < ahora():
            base.sesiones.delete_one({"_id": testigo})
            return None
    usuario = base.usuarios.find_one({"_id": ses["usuarioId"]})
    if not usuario or not usuario.get("activo", True):
        base.sesiones.delete_one({"_id": testigo})
        return None
    # renovacion por uso: la sesion solo vence si dejas de usar la app
    base.sesiones.update_one(
        {"_id": testigo},
        {"$set": {"ultimoUso": ahora(), "vence": ahora() + timedelta(days=DIAS_SESION)}},
    )
    return usuario


def usuario_publico(usuario):
    """Lo que se le puede contar al navegador: nunca la clave."""
    return {
        "id": str(usuario["_id"]),
        "correo": usuario.get("correo", ""),
        "nombre": usuario.get("nombre", ""),
        "rol": usuario.get("rol", "lector"),
        "permisos": usuario.get("permisos", {}),
    }


# ── Respaldo de la base ──────────────────────────────────────────────────
# Colecciones que NUNCA salen en un respaldo:
#   sesiones -> son testigos vivos; el archivo terminaria siendo una llave
#               de entrada para cualquiera que lo abra.
# De "usuarios" se saca el campo de la clave cifrada: el archivo se baja al
# telefono y se sube a la nube, y ahi no tiene por que viajar. Al restaurar,
# las claves se vuelven a poner; los datos del negocio no dependen de eso.
COLECCIONES_FUERA = ["sesiones"]
CAMPOS_FUERA = {"usuarios": ["clave"]}

# Colecciones que SI se respaldan pero NUNCA se restauran.
# "usuarios" entra aca por una razon concreta: el respaldo sale sin el campo
# de la clave, y los _id salen convertidos a texto. Restaurarla dejaria
# usuarios que no pueden entrar y sesiones apuntando a un id que ya no
# coincide: te quedas afuera del ambiente. Los usuarios se crean al arrancar
# o desde la pantalla de gestion, no desde un archivo.
COLECCIONES_NO_RESTAURAR = ["usuarios"]


def armar_respaldo(solo_resumen=False):
    base = obtener_base()
    if base is None:
        return None
    nombres = [c for c in base.list_collection_names() if c not in COLECCIONES_FUERA]
    nombres.sort()
    datos = {}
    conteo = {}
    for nombre in nombres:
        docs = list(base[nombre].find({}))
        conteo[nombre] = len(docs)
        if solo_resumen:
            continue
        quitar = CAMPOS_FUERA.get(nombre, [])
        if quitar:
            for d in docs:
                for campo in quitar:
                    d.pop(campo, None)
        datos[nombre] = docs
    respaldo = {
        "_respaldo": {
            "fecha": ahora().isoformat(),
            "ambiente": os.environ.get("AMBIENTE", "produccion"),
            "base": NOMBRE_BASE,
            "colecciones": conteo,
            "total": sum(conteo.values()),
            "sinClaves": True,
        }
    }
    if not solo_resumen:
        respaldo["datos"] = datos
    return respaldo


def restaurar_respaldo(archivo):
    """Solo en pruebas. Reemplaza las colecciones que vengan en el archivo."""
    base = obtener_base()
    datos = (archivo or {}).get("datos") or {}
    resultado = {}
    salteadas = []
    for nombre, docs in datos.items():
        if not isinstance(docs, list):
            continue
        if nombre in COLECCIONES_FUERA or nombre in COLECCIONES_NO_RESTAURAR:
            salteadas.append(nombre)
            continue
        base[nombre].delete_many({})
        if docs:
            base[nombre].insert_many(docs)
        resultado[nombre] = len(docs)
    return resultado, salteadas


# ── Servidor ─────────────────────────────────────────────────────────────
class Manejador(BaseHTTPRequestHandler):
    server_version = "centrogestion-api"

    # -- utilidades --
    def _cors(self):
        origen = self.headers.get("Origin", "")
        if origen in ORIGENES_PERMITIDOS:
            self.send_header("Access-Control-Allow-Origin", origen)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "86400")

    def _responder(self, codigo, cuerpo):
        datos = json.dumps(cuerpo, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(datos)

    def _leer_json(self):
        try:
            largo = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if largo <= 0 or largo > CUERPO_MAXIMO:
            return None
        try:
            return json.loads(self.rfile.read(largo).decode("utf-8"))
        except Exception:
            return None

    def _admin(self):
        """Devuelve el usuario si hay sesion valida Y es administrador."""
        usuario = usuario_de_sesion(self._testigo())
        if not usuario:
            return None, (401, {"ok": False, "error": "sesion vencida"})
        if usuario.get("rol") != "admin":
            return None, (403, {"ok": False, "error": "solo administradores"})
        return usuario, None

    def _testigo(self):
        cab = self.headers.get("Authorization", "")
        if cab.startswith("Bearer "):
            return cab[7:].strip()
        return ""

    # -- rutas --
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        ruta = self.path.split("?")[0].rstrip("/") or "/"
        if ruta in ("/", "/salud"):
            return self._responder(200, {
                "ok": True,
                "mensaje": "estoy viva",
                "hora": ahora().isoformat(),
                "ambiente": os.environ.get("AMBIENTE", "produccion"),
                "mongo": estado_mongo(),
            })
        if ruta == "/auth/yo":
            try:
                usuario = usuario_de_sesion(self._testigo())
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            if not usuario:
                return self._responder(401, {"ok": False, "error": "sesion vencida"})
            return self._responder(200, {"ok": True, "usuario": usuario_publico(usuario)})
        if ruta in ("/respaldo", "/respaldo/resumen"):
            try:
                usuario, error = self._admin()
                if error:
                    return self._responder(*error)
                resumen = (ruta == "/respaldo/resumen")
                respaldo = armar_respaldo(solo_resumen=resumen)
                if respaldo is None:
                    return self._responder(503, {"ok": False, "error": "base no disponible"})
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            if resumen:
                return self._responder(200, {"ok": True, "resumen": respaldo["_respaldo"]})
            datos = json.dumps(respaldo, ensure_ascii=False, default=str).encode("utf-8")
            nombre = "respaldo-%s-%s.json" % (
                NOMBRE_BASE, ahora().strftime("%Y%m%d-%H%M%S"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(datos)))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % nombre)
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            return self.wfile.write(datos)

        return self._responder(404, {"ok": False, "error": "ruta no encontrada"})

    def do_POST(self):
        ruta = self.path.split("?")[0].rstrip("/") or "/"
        cuerpo = self._leer_json()

        if ruta == "/auth/entrar":
            if not isinstance(cuerpo, dict):
                return self._responder(400, {"ok": False, "error": "faltan datos"})
            correo = str(cuerpo.get("correo", "")).strip().lower()
            clave = str(cuerpo.get("clave", ""))
            if not correo or not clave:
                return self._responder(400, {"ok": False, "error": "faltan datos"})
            if esta_bloqueado(correo):
                return self._responder(429, {
                    "ok": False,
                    "error": "demasiados intentos, espera 15 minutos",
                })
            try:
                base = obtener_base()
                if base is None:
                    return self._responder(503, {"ok": False, "error": "base no disponible"})
                usuario = base.usuarios.find_one({"correo": correo})
                # mismo mensaje si el correo no existe o si la clave es
                # incorrecta: no se le informa a nadie que correos hay
                if not usuario or not usuario.get("activo", True) \
                        or not verificar_clave(clave, usuario.get("clave", "")):
                    anotar_fallo(correo)
                    return self._responder(401, {"ok": False, "error": "correo o clave incorrectos"})
                limpiar_intentos(correo)
                testigo = crear_sesion(usuario)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {
                "ok": True,
                "testigo": testigo,
                "usuario": usuario_publico(usuario),
                "diasSesion": DIAS_SESION,
            })

        if ruta == "/auth/salir":
            testigo = self._testigo()
            try:
                base = obtener_base()
                if base is not None and testigo:
                    base.sesiones.delete_one({"_id": testigo})
            except PyMongoError:
                pass
            return self._responder(200, {"ok": True})

        if ruta == "/auth/cambiar-clave":
            if not isinstance(cuerpo, dict):
                return self._responder(400, {"ok": False, "error": "faltan datos"})
            actual = str(cuerpo.get("actual", ""))
            nueva = str(cuerpo.get("nueva", ""))
            if len(nueva) < 8:
                return self._responder(400, {
                    "ok": False, "error": "la clave nueva debe tener al menos 8 caracteres",
                })
            try:
                usuario = usuario_de_sesion(self._testigo())
                if not usuario:
                    return self._responder(401, {"ok": False, "error": "sesion vencida"})
                if not verificar_clave(actual, usuario.get("clave", "")):
                    return self._responder(401, {"ok": False, "error": "la clave actual no coincide"})
                base = obtener_base()
                base.usuarios.update_one(
                    {"_id": usuario["_id"]},
                    {"$set": {"clave": cifrar_clave(nueva), "claveCambiada": ahora()}},
                )
                # cerrar las demas sesiones: si alguien la tenia, queda afuera
                base.sesiones.delete_many({
                    "usuarioId": usuario["_id"],
                    "_id": {"$ne": self._testigo()},
                })
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {"ok": True})

        if ruta == "/restaurar":
            # Candado: esta ruta NO EXISTE fuera del ambiente de pruebas.
            # Restaurar sobre datos reales es una operacion para hacer a mano,
            # mirando lo que se hace, no algo que una ruta pueda disparar sola.
            if os.environ.get("AMBIENTE", "produccion") != "pruebas":
                return self._responder(404, {"ok": False, "error": "ruta no encontrada"})
            try:
                usuario, error = self._admin()
                if error:
                    return self._responder(*error)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            if not isinstance(cuerpo, dict) or cuerpo.get("confirmo") != "SI":
                return self._responder(400, {
                    "ok": False,
                    "error": 'falta la confirmacion: mandar "confirmo": "SI"',
                })
            archivo = cuerpo.get("archivo")
            if not isinstance(archivo, dict) or not archivo.get("datos"):
                return self._responder(400, {"ok": False, "error": "archivo de respaldo invalido"})
            try:
                resultado, salteadas = restaurar_respaldo(archivo)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {
                "ok": True,
                "restaurado": resultado,
                "salteadas": salteadas,
            })

        return self._responder(404, {"ok": False, "error": "ruta no encontrada"})

    def log_message(self, formato, *args):
        print("%s - %s" % (self.address_string(), formato % args), flush=True)


if __name__ == "__main__":
    try:
        preparar_base()
    except Exception as e:
        # que la API arranque igual: asi /salud sigue sirviendo para
        # diagnosticar en vez de quedar todo caido sin explicacion
        print("AVISO: no se pudo preparar la base: %s" % type(e).__name__, flush=True)
    puerto = int(os.environ.get("PORT", "8080"))
    servidor = ThreadingHTTPServer(("0.0.0.0", puerto), Manejador)
    print("API escuchando en el puerto %d" % puerto, flush=True)
    servidor.serve_forever()
