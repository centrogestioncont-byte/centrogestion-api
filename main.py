# API del Centro de Gestion — paso 2b: autenticacion con usuarios reales.
#
# Que hace hoy:
#   GET  /            y  /salud         estado de la API y de Mongo
#   POST /auth/entrar                   correo + clave -> testigo de sesion
#   GET  /auth/yo                       quien soy (y renueva la sesion)
#   POST /auth/salir                    cierra la sesion
#   POST /auth/cambiar-clave            cambia la clave del usuario en sesion
#   GET/POST/PUT /usuarios              gestion de usuarios (solo admin)
#   GET/POST/PUT /clientes              la primera coleccion del negocio
#   GET/PUT /estado                     TODO el resto del negocio
#   POST /estado/importar               la carga inicial desde Firebase
#   GET/POST /auditoria                 quien hizo que y cuando
#   GET  /mercado                       el P2P de Binance, a su volumen
#
# /estado es el reemplazo de Firebase. El navegador manda su bloque, el
# SERVIDOR fusiona y devuelve el resultado. Antes cada dispositivo fusionaba
# por su cuenta, con su propio reloj, y el ultimo que escribia por PUT pisaba
# el nodo entero: asi se perdio contabilidad en agosto. Ahora el arbitro es
# uno solo.

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, MongoClient, ReplaceOne
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


def version_desplegada():
    """Que codigo esta corriendo, para poder mirarlo desde afuera.

    Railway pone estas variables solo, en cada despliegue. Existe porque ya
    perdimos un rato con esto: un deploy quedo FAILED, siguio corriendo el
    contenedor viejo, y desde afuera no habia forma de darse cuenta. Ahora
    se compara el commit contra el de GitHub y se acaba la discusion.

    Va en /salud, que no pide sesion. Por eso lleva el commit corto y la
    rama y NO el mensaje del commit: el SHA no le dice nada a quien no
    tiene acceso al repositorio, y un mensaje como "arreglar el agujero de
    X" si. Fuera de Railway —corriendo a mano— informa "desconocido", que
    es la verdad y no una mentira tranquilizadora.
    """
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "") or ""
    return {
        "commit": sha[:7] if sha else "desconocido",
        "rama": os.environ.get("RAILWAY_GIT_BRANCH", "") or "desconocida",
    }

DIAS_SESION = 30              # vence a los 30 dias SIN USARSE (se renueva sola)
MAX_INTENTOS = 10             # intentos fallidos seguidos sobre un mismo correo
BLOQUEO_SEGUNDOS = 15 * 60    # cuanto dura el bloqueo despues de fallar

CUERPO_MAXIMO = 64 * 1024              # las rutas de siempre
CUERPO_MAXIMO_ESTADO = 12 * 1024 * 1024  # /estado manda el bloque completo

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


def ahora_ms():
    """Milisegundos desde 1970. Es la unidad que usa el motor de fusion:
    los _mod de todos los registros se comparan entre si como numeros."""
    return int(time.time() * 1000)


def preparar_base():
    """Indices y creacion del primer administrador. Se corre al arrancar."""
    base = obtener_base()
    if base is None:
        print("AVISO: sin MONGO_URL, no se puede preparar la base", flush=True)
        return
    base.usuarios.create_index([("correo", ASCENDING)], unique=True)
    base.sesiones.create_index([("usuarioId", ASCENDING)])
    # El codigo de cliente es la clave que usan las remesas: unico, sin repetidos.
    base.clientes.create_index([("cod", ASCENDING)], unique=True)
    # La auditoria se lee siempre ordenada por fecha y se poda por fecha.
    base[COL_AUDITORIA].create_index([("ts", ASCENDING)])

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


# ── Gestion de usuarios ──────────────────────────────────────────────────
# Los roles son los mismos que ya usa la app; el servidor no inventa
# ninguno nuevo. "lector" es el Supervisor: ve todo y no modifica.
ROLES_VALIDOS = ["admin", "brl", "vzla", "eeuu", "lector"]
LARGO_MINIMO_CLAVE = 8


def _oid(texto):
    try:
        return ObjectId(str(texto))
    except (InvalidId, TypeError):
        return None


def _admins_activos(base, excepto=None):
    filtro = {"rol": "admin", "activo": True}
    if excepto is not None:
        filtro["_id"] = {"$ne": excepto}
    return base.usuarios.count_documents(filtro)


def _cerrar_sesiones_de(base, uid):
    """Revocacion real: al desactivar o cambiar la clave, las sesiones
    abiertas de esa persona dejan de servir en el acto."""
    base.sesiones.delete_many({"usuarioId": uid})


def listar_usuarios():
    base = obtener_base()
    salida = []
    for u in base.usuarios.find({}).sort("nombre", ASCENDING):
        salida.append({
            "id": str(u["_id"]),
            "correo": u.get("correo", ""),
            "nombre": u.get("nombre", ""),
            "rol": u.get("rol", "lector"),
            "permisos": u.get("permisos", {}),
            "activo": u.get("activo", True),
            "creado": u.get("creado"),
        })
    return salida


# ── Clientes ─────────────────────────────────────────────────────────────
# Primera coleccion de datos del negocio que sale de Firebase y pasa a
# vivir aca. El "cod" es la clave que usan las remesas para apuntar a un
# cliente, asi que:
#   · es unico (indice en Mongo),
#   · no se puede cambiar despues de creado,
#   · y los clientes NO SE BORRAN: se desactivan. Borrar uno dejaria
#     operaciones apuntando a un codigo que ya no existe.
LARGO_MAXIMO_TEXTO = 200
PAISES_LIBRES = True   # el pais es texto libre: la app ya maneja varios


def _texto(valor, maximo=LARGO_MAXIMO_TEXTO):
    return str(valor if valor is not None else "").strip()[:maximo]


def _mod_de_cliente(c):
    """Cuando fue tocado por ultima vez, en milisegundos.

    Los clientes cargados antes de que la API guardara esta marca no la
    tienen. Para esos se usa la fecha que el propio _id de Mongo lleva
    adentro: un ObjectId empieza por su hora de creacion. No es la fecha de
    la ultima edicion, pero es un numero real, estable entre lecturas y
    anterior a cualquier edicion posterior —que es todo lo que la fusion
    necesita para no elegir mal—. Inventar la hora actual en cada lectura
    seria peor: cada dispositivo creeria haber tocado el registro recien.
    """
    m = c.get("_mod")
    if isinstance(m, (int, float)) and not isinstance(m, bool):
        return int(m)
    try:
        return int(c["_id"].generation_time.timestamp() * 1000)
    except Exception:
        return 0


def cliente_publico(c):
    return {
        "id": str(c["_id"]),
        "cod": c.get("cod", ""),
        "nombre": c.get("nombre", ""),
        "tel": c.get("tel", ""),
        "pais": c.get("pais", ""),
        "ruta": c.get("ruta", ""),
        "nota": c.get("nota", ""),
        "activo": c.get("activo", True),
        "_mod": _mod_de_cliente(c),
    }


def listar_clientes():
    """Devuelve todos, activos e inactivos. El front decide que muestra:
    un cliente desactivado tiene que seguir siendo legible en las remesas
    viejas que lo referencian."""
    base = obtener_base()
    return [cliente_publico(c) for c in base.clientes.find({}).sort("cod", ASCENDING)]


def _validar_cliente(cuerpo, con_cod):
    """Devuelve (datos, error). El error ya viene con codigo y cuerpo."""
    if not isinstance(cuerpo, dict):
        return None, (400, {"ok": False, "error": "faltan datos"})
    datos = {
        "nombre": _texto(cuerpo.get("nombre")),
        "tel": _texto(cuerpo.get("tel"), 40),
        "pais": _texto(cuerpo.get("pais"), 60),
        "ruta": _texto(cuerpo.get("ruta"), 60),
        "nota": _texto(cuerpo.get("nota"), 500),
    }
    if not datos["nombre"]:
        return None, (400, {"ok": False, "error": "falta el nombre"})
    if con_cod:
        cod = _texto(cuerpo.get("cod"), 20)
        if not cod:
            return None, (400, {"ok": False, "error": "falta el codigo"})
        datos["cod"] = cod
    return datos, None

# ── Estado del negocio: el reemplazo de Firebase ─────────────────────────
# Todo lo que abajo se llama "motor de fusion" es una traduccion literal de
# lo que hoy corre en el navegador (index.html). No se corrigio ni se mejoro
# NADA al portarlo: se probaron 21.812 casos generados contra el JS original
# y las dos implementaciones deciden igual en todos. Las rarezas de
# JavaScript que parecen errores estan copiadas a proposito y explicadas
# donde aparecen; cambiarlas aca haria que el servidor y los telefonos
# resolvieran distinto el mismo conflicto, que es exactamente como se pierde
# plata sin que nadie lo note hasta el cierre de mes.

MERGE_ID_FIELD = {
    "clientes": "cod", "brl": "_uid", "vzla": "_uid", "eeuu": "_uid",
    "cierresMes": "mesKey",
}

MERGE_FIELDS = [
    "cuentas", "compromisos", "egresos", "egresos_personales", "prestamos",
    "cuentasCobrar", "clientes", "inventarioUsdt", "brl", "vzla", "eeuu",
    "traspasos", "reposiciones", "gastos_eeuu", "deudas_paul",
    "movimientosCapital", "pagosSocios", "gananciaExtra", "gastos_socios",
    "inventarioUsdt_cerrado", "cierresMes", "capital", "ajustesSaldo",
]

PODA_CATS = [
    "brl", "vzla", "eeuu", "clientes", "inventarioUsdt", "inventarioUsdt_cerrado",
    "cuentas", "cuentasCobrar", "egresos", "egresos_personales", "prestamos",
    "compromisos", "traspasos", "deudas_paul", "gananciaExtra", "gastos_socios",
    "gastos_eeuu", "reposiciones", "ajustesSaldo", "pagosSocios",
    # "capital" entro el 08/09/2026. Faltaba en las dos puntas: el boton de
    # borrar del navegador anotaba la marca y nadie la aplicaba, asi que la
    # fila volvia por la fusion desde cualquier otro dispositivo. Tiene que
    # estar aca Y en _PODA_CATS del front: si solo una punta poda, el
    # servidor y los telefonos resuelven distinto el mismo conflicto.
    "capital",
]

DATA_KEYS = [
    "brl", "vzla", "eeuu", "egresos", "prestamos", "capital", "inventarioUsdt",
    "gastos_eeuu", "deudas_paul", "clientes", "movimientosCapital", "config",
    "cierresMes", "cuentas", "tasasCambio", "traspasos", "cuentasCobrar",
    "egresos_personales", "_deleted", "inventarioUsdt_cerrado", "reposiciones",
    "pagosSocios", "gananciaExtra", "tasasDia", "_tasasDiaMeta", "gastos_socios",
    "compromisos", "_deletedMerge", "histBalance", "histTasas", "histSaldos",
    "ajustesSaldo",
    # 08/10/2026. Faltaban las tres, y guardar_estado recorre ESTA lista:
    # una clave que no este aqui no se escribe nunca en Mongo. O sea que
    # viajaban del navegador al servidor y el servidor las tiraba, sin un
    # solo error. Medido comparando las dos listas: la app manda 36 claves
    # y el servidor guardaba 33.
    #   histApertura   el historial de la apertura (ARREGLO 94), que existe
    #                  justo para contestar "¿cuanto era antes?" cuando el
    #                  otro aparato la cambia. No se guardaba ninguna linea.
    #   histComp       lo que apunta de la competencia, indexado por fecha.
    #                  CLAUDE.md lo pide en DATA_KEYS a proposito, para que
    #                  se una entre los dos aparatos en vez de pisarse.
    #   mapaBinance    a que cuenta va cada operacion importada de Binance.
    "histApertura", "histComp", "mapaBinance",
    # ARREGLO 61 (15/09/2026): "_modCampos" son las marcas de quien toco que
    # clave de config y cuando. El navegador las manda en cada guardado y
    # hasta ahora el servidor NO las guardaba —no estaban en esta lista— asi
    # que las tiraba. Sin marcas, merge_config_safe no podia decidir por
    # tiempo y usaba "solo relleno lo que esta vacio": un valor de config que
    # ya existia NO habia forma de cambiarlo desde la app. La apertura, las
    # comisiones del banco, el % del sueldo, los socios: todo congelado.
    "_modCampos",
]

# ── Claves de config que viajan JUNTAS ───────────────────────────────────
# La apertura no es un dato, son cinco. Si se deciden por separado puede
# quedarse la FECHA de un aparato y el MONTO de otro — una apertura que no
# existio en ninguno, y contra la que mide toda la conciliacion. Es la misma
# lista que _MERGE_BLOQUES en el navegador: las dos puntas tienen que
# resolver igual el mismo conflicto.
MERGE_BLOQUES = {
    "config": [["aperturaUsdt", "aperturaFecha", "aperturaSaldos",
                "aperturaTs", "aperturaBase"]],
}

RENUMERAR = ("brl", "vzla", "eeuu")
DIAS_MARCA_MS = 2592000000   # 30 dias, igual que _marcarBorradoMerge


# ══════════════════════════════════════════════════════════════════════════
# QUE PUEDE ESCRIBIR CADA PERSONA (PASO 3 DE LA AUDITORIA DEL 08/10/2026)
# ══════════════════════════════════════════════════════════════════════════
#
# Sus palabras: "los permisos que yo le doy, por lo menos que un dia yo quiero
# que registren egresos. O quiero que registren prestamo, o quiero que
# registren un gasto. Yo solo activo a los que yo crea conveniente."
#
# Las casillas de la FASE B ya hacen eso... en PANTALLA. Lo que no hacian es
# nada aqui: PUT /estado solo comprobaba "editar", asi que cualquier persona
# que pudiera registrar una remesa podia reescribir los prestamos, los
# egresos y los cierres de mes aunque esas pestanas no le aparecieran. Le
# bastaba con abrir las herramientas del navegador. O sea que era de los
# candados que parecen candado y no lo son, que es peor que ninguno.
#
# ── Es una lista de claves GUARDADAS, no de claves permitidas ────────────
#
# Y es a proposito, por una asimetria que en esta app no se negocia: una
# clave que no este aqui pasa igual que siempre. Al reves —permitir solo lo
# apuntado— un flujo que toque una clave que nadie previo se perderia EN
# SILENCIO, y eso aqui es contabilidad descuadrada. Un guardado de mas es el
# estado de hoy; uno de menos es dinero perdido.
#
# ── Lo que NO se guarda, y por que ──────────────────────────────────────
#
# Medido recorriendo el archivo entero y anotando, clave por clave, QUE
# funciones la escriben (no deducido: registrar una remesa toca mas de lo
# que parece). Quedan fuera:
#
#   cuentas, capital          registrar una remesa mueve los saldos
#   inventarioUsdt            y consume lotes del FIFO
#   inventarioUsdt_cerrado
#   cuentasCobrar             una remesa PENDIENTE crea la fila por cobrar
#                             (saveTx y saveTxEE escriben aqui) — guardarla
#                             por el permiso "cobrar" romperia justo lo que
#                             un operador tiene que poder hacer
#   movimientosCapital        lo escribe registrarMovimientoCapital, que la
#                             llama la propia remesa
#   brl, vzla                 son lo que el operador registra; es su trabajo
#   clientes                  tienen su propia ruta (/clientes) y
#                             _aplicarEstadoDeApi ignora la copia que venga
#                             en el bloque de estado: guardarla aqui no
#                             haria nada
#   los historiales           van indexados por fecha y se unen
#   _deleted, _deletedMerge   son la infraestructura de la fusion; sin ellas
#   _modCampos                los dos aparatos se pisan
#
CLAVES_GUARDADAS = {
    # cada una la escriben SOLO las funciones de su propia pantalla
    "prestamos":          "prestamos",            # rPrestamos, savePr
    "egresos":            "egresos",              # saveEg, pagarCompromiso, generarFijasMes
    "egresos_personales": "egresos",              # saveEgPersonal, rMiGestion
    "cierresMes":         "cierre",               # ejecutarCierreMes, reabrirMes
    "pagosSocios":        "cierre",               # savePagoSocio
    "gastos_socios":      "cierre",               # saveGastoSocio, saveGastoSocioForm
    "deudas_paul":        "dash_eeuu",            # saveDeudaPaul, pagarPaul
    "gastos_eeuu":        "dash_eeuu",            # saveGE, pagarPaul
    "eeuu":               "nueva_eeuu",           # saveTxEE
    "traspasos":          "traspasos",            # saveTraspaso
    "config":             "config_admin",         # las comisiones, el % de sueldo, la apertura
    "ajustesSaldo":       "capital_total",        # updateCuentaSaldo, editarSaldoCuentaEnConfig
    "tasasDia":           "calculadora",          # setTasaDia
    "_tasasDiaMeta":      "calculadora",          # setTasaDia, soltarTasaDia
    "mapaBinance":        "inventario_usdt",      # _binFijarCuenta
}

# Lo que trae puesto cada rol. ESTO NO ES EL PERMISO: es lo que vale cuando
# la persona todavia no tiene esa casilla decidida.
#
# Tiene que ser la misma tabla que PERMISOS_POR_ROL en index.html, y que sea
# la misma no lo puede comprobar ninguna prueba: los dos repositorios no se
# ven entre si. Por eso pruebas/permisos.py la fija valor por valor — para
# que cambiarla cueste tocar la prueba a proposito y no se mueva de lado.
#
# Hace falta porque la app manda la lista COMPLETA al guardar permisos
# (FASE B) pero los usuarios creados por POST /usuarios salen con solo
# {"editar": ...}. Sin los valores por omision, a esos se les cerraria todo
# de golpe — que es exactamente lo que la FASE B evito en el navegador.
PERMISOS_POR_ROL = {
    "admin":  "*",
    "lector": {"todo": True, "salvo": ["editar"]},
    "brl":    {"si": ["editar", "mi_ganancia", "op_diario", "nueva", "clientes"]},
    "vzla":   {"si": ["editar", "mi_ganancia", "op_diario", "nueva", "clientes"]},
    "eeuu":   {"si": ["editar", "mi_ganancia", "op_diario", "nueva_eeuu",
                      "clientes", "dash_eeuu"]},
}


def tiene_permiso(usuario, clave):
    """Equivalente de tienePermiso() del navegador. El rol es un punto de
    partida, no el permiso: manda lo que Mongo guarda para esa persona y el
    rol solo decide las casillas que no estan decididas."""
    usuario = usuario or {}
    if usuario.get("rol") == "admin":
        return True
    permisos = usuario.get("permisos") or {}
    if clave in permisos:
        return bool(permisos[clave])
    omision = PERMISOS_POR_ROL.get(usuario.get("rol"))
    if omision == "*":
        return True
    if not isinstance(omision, dict):
        return False
    if omision.get("todo"):
        return clave not in (omision.get("salvo") or [])
    return clave in (omision.get("si") or [])


def filtrar_por_permisos(entrante, usuario):
    """Saca del bloque entrante las claves que esta persona no puede tocar.

    Devuelve (bloque_filtrado, claves_rechazadas). Quitar una clave es
    seguro: fusionar_estado hace `if k not in entrante: continue`, o sea que
    una clave que no llega se conserva TAL CUAL como estaba guardada. No se
    borra nada; simplemente lo que mando esa persona no cuenta.

    Ojo: esto no es un aviso cosmetico. Si alguien escribio algo que no le
    tocaba, el aparato lo nota solo —adopta lo que contesta el servidor y el
    aviso de choque (ARREGLO 67) compara lo que mando contra lo que quedo—
    pero ademas se anota en la auditoria del servidor, que es la unica que no
    depende de que el navegador quiera anotarla.
    """
    if not isinstance(entrante, dict):
        return entrante, []
    rechazadas = [k for k, permiso in CLAVES_GUARDADAS.items()
                  if k in entrante and not tiene_permiso(usuario, permiso)]
    if not rechazadas:
        return entrante, []
    limpio = {k: v for k, v in entrante.items() if k not in rechazadas}
    return limpio, sorted(rechazadas)


# ── Equivalencias exactas con JavaScript ─────────────────────────────────
def _es_numero(v):
    """typeof v === "number". En JS los booleanos NO son numeros."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _clave_js(v):
    """String(v) tal como lo hace JS al usar v como clave de objeto."""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, float):
        if v != v:
            return "NaN"
        if v == int(v) and abs(v) < 1e21:
            return str(int(v))
        return repr(v)
    return str(v)


def _falsy_js(v):
    """!v en JS. Ojo: {} y [] son VERDADEROS en JS, al reves que en Python."""
    if v is None or v is False:
        return True
    if isinstance(v, str):
        return v == ""
    if _es_numero(v):
        return v == 0 or v != v
    return False


def _identicos(a, b):
    """JSON.stringify(a)===JSON.stringify(b): respeta el orden de las claves."""
    try:
        return json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)
    except (TypeError, ValueError):
        return False


def _igual_estricto(a, b):
    """a===b para los valores que salen de un JSON."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, str) != isinstance(b, str):
        return False
    return a == b


# ── Fusion de una lista, registro por registro ───────────────────────────
def merge_array_by_id(remota, local, campo_id, ts_remoto, ts_local):
    campo_id = campo_id or "id"
    if not isinstance(remota, list):
        return local
    if not isinstance(local, list):
        return list(remota)

    indices = {}
    for i, item in enumerate(local):
        if isinstance(item, dict) and item.get(campo_id) is not None:
            indices[_clave_js(item[campo_id])] = i

    fusionada = list(local)
    remoto_mas_nuevo = (
        _es_numero(ts_remoto) and _es_numero(ts_local) and ts_remoto > ts_local
    )

    for remoto in remota:
        if not isinstance(remoto, dict) or remoto.get(campo_id) is None:
            continue
        idx = indices.get(_clave_js(remoto[campo_id]))
        if idx is None:
            fusionada.append(remoto)
            continue

        actual = fusionada[idx]
        if _identicos(actual, remoto):
            continue

        remoto_tiene = _es_numero(remoto.get("_mod"))
        local_tiene = _es_numero(actual.get("_mod")) if isinstance(actual, dict) else False

        if remoto_tiene and local_tiene:
            gana_remoto = remoto["_mod"] > actual["_mod"]
        elif local_tiene and not remoto_tiene:
            # El local fue tocado a proposito y tiene marca; lo remoto es una
            # copia sin marca. Gana el local SIEMPRE, sin mirar el reloj
            # general del dispositivo.
            gana_remoto = False
        elif remoto_tiene and not local_tiene:
            gana_remoto = True
        else:
            gana_remoto = remoto_mas_nuevo

        if gana_remoto:
            fusionada[idx] = remoto

    return fusionada


# ── Fusion de la configuracion, campo a campo ────────────────────────────
def _claves_js(v):
    """Object.keys(v). Un arreglo o un texto dan las claves "0","1","2"..."""
    if isinstance(v, dict):
        return list(v.keys())
    if isinstance(v, (list, str)):
        return [str(i) for i in range(len(v))]
    return []


def _leer_js(v, k):
    """v[k] con k siempre texto, como en JS."""
    if isinstance(v, dict):
        return v.get(k)
    if isinstance(v, (list, str)):
        try:
            i = int(k)
        except (TypeError, ValueError):
            return None
        return v[i] if 0 <= i < len(v) else None
    return None


def _asignar_js(v):
    """Object.assign({}, v). Un arreglo se DESARMA en {"0":..,"1":..}: es una
    rareza de JavaScript, pero pasa de verdad —config.socios y
    config.rutasExtra son arreglos— y el navegador ya se comporta asi. El
    servidor tiene que decidir igual, no mejor."""
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, (list, str)):
        return {str(i): x for i, x in enumerate(v)}
    return {}


def _gana_remoto(k, tr, tl, rv, lv):
    """Quien gana una clave de config. En este orden:

    1. Los dos dicen cuando la tocaron -> la marca mas nueva.
    2. Solo uno lo dice -> ese. Quien afirma "yo cambie esto" gana sobre quien
       no afirma nada.
    3. Ninguno lo dice -> se queda lo guardado, salvo que lo guardado este
       vacio. Es la regla vieja, que se mantiene para todo lo que el navegador
       nunca marco: asi este arreglo no cambia de golpe el comportamiento de
       claves que llevan anios quietas.
    """
    if _es_numero(tr) and _es_numero(tl):
        return tr > tl
    if _es_numero(tr):
        return True
    if _es_numero(tl):
        return False
    # Sin marcas: la regla de siempre. undefined/null/"" y 0 se dejan rellenar;
    # un false puesto a proposito (una comision exonerada, un modulo apagado)
    # NO se deja pisar.
    return lv is None or (isinstance(lv, str) and lv == "") or (_es_numero(lv) and lv == 0)


def _marca_mas_nueva(marcas, claves):
    t = None
    for k in claves:
        v = (marcas or {}).get(k)
        if _es_numero(v) and (t is None or v > t):
            t = v
    return t


def merge_config_safe(remota, local, marcas_rem=None, marcas_loc=None):
    """Fusiona config. `remota` es lo que manda el aparato y `local` lo
    guardado; el servidor es el arbitro.

    ARREGLO 61: antes esta funcion solo copiaba lo remoto cuando lo guardado
    estaba vacio, asi que un valor de config que YA existia no se podia
    cambiar desde ningun aparato. La app lo cambiaba en su memoria, lo
    enseñaba, y en el guardado siguiente el servidor le devolvia el viejo.
    Desde el 14/09 esta medido: el telefono y la PC daban dos aperturas
    distintas y ninguna se imponia.
    """
    if _falsy_js(remota):
        return local
    if _falsy_js(local):
        return remota
    marcas_rem = marcas_rem if isinstance(marcas_rem, dict) else {}
    marcas_loc = marcas_loc if isinstance(marcas_loc, dict) else {}
    salida = _asignar_js(local)

    # Los bloques primero: sus claves quedan decididas y no se vuelven a mirar.
    ya = set()
    for bloque in MERGE_BLOQUES.get("config", []):
        hay_rem = any(k in remota for k in bloque)
        if not hay_rem:
            continue
        ya.update(bloque)
        hay_loc = any(k in salida for k in bloque)
        tr = _marca_mas_nueva(marcas_rem, bloque)
        tl = _marca_mas_nueva(marcas_loc, bloque)
        entra = (not hay_loc) or (tr is not None and (tl is None or tr > tl))
        if not entra:
            continue
        # Entra el bloque ENTERO: las claves que el aparato no trae se borran,
        # porque son de la apertura vieja y mezclarlas seria volver al mismo
        # problema.
        for k in bloque:
            if k in remota:
                salida[k] = _leer_js(remota, k)
            elif k in salida:
                del salida[k]

    for k in _claves_js(remota):
        if k in ya:
            continue
        rv = _leer_js(remota, k)
        lv = _leer_js(local, k)
        tr, tl = marcas_rem.get(k), marcas_loc.get(k)
        if isinstance(rv, dict) and not _es_numero(tr) and not _es_numero(tl):
            # Sin marcas de ningun lado se sigue entrando al objeto, como
            # siempre: dentro puede haber claves que el servidor no tiene.
            salida[k] = merge_config_safe(rv, lv if isinstance(lv, (dict, list)) else {})
        elif _gana_remoto(k, tr, tl, rv, lv):
            salida[k] = rv
    return salida


def unir_marcas_campos(a, b):
    """Une dos _modCampos quedandose con la marca MAS NUEVA de cada clave.

    ARREGLO 61: reemplazarlas perderia el rastro de lo que toco el otro
    aparato, y con el rastro perdido la fusion vuelve a no poder decidir.
    """
    salida = {}
    for origen in (a, b):
        if not isinstance(origen, dict):
            continue
        for campo, claves in origen.items():
            if not isinstance(claves, dict):
                continue
            destino = salida.setdefault(campo, {})
            for k, t in claves.items():
                if not _es_numero(t):
                    continue
                if not _es_numero(destino.get(k)) or t > destino[k]:
                    destino[k] = t
    return salida


# ── Restos del acceso por PIN dentro de "config" ─────────────────────────
# El acceso por PIN se retiro. Los perfiles locales guardaban el nombre y el
# PIN de cada persona SIN CIFRAR dentro de config, y config viaja al servidor
# en cada guardado y sale en cada respaldo. Nadie lee ya esas claves, pero
# mientras sigan ahi los PIN andan en claro por todos los dispositivos.
#
# Borrarlas en el navegador no alcanza. merge_config_safe arranca de lo
# guardado y le AGREGA las claves que el dispositivo no trae: una clave que
# el servidor todavia tenga vuelve sola en el guardado siguiente. Este es el
# unico lugar donde el borrado es definitivo.
#
# "modulos" se suma con la FASE B. Eran los permisos por ROL, y ya no deciden
# nada: ahora los permisos son de cada persona y viven en la coleccion de
# usuarios. Si se quedaran aqui, el bloque de estado cargaria para siempre con
# un permiso muerto que cualquier aparato viejo podria seguir mandando.
CONFIG_PROHIBIDO = ["usuarios", "pins", "modulos"]


def limpiar_config(cfg):
    """Saca de un objeto config las claves prohibidas. Devuelve cuantas saco."""
    if not isinstance(cfg, dict):
        return 0
    quitadas = 0
    for k in CONFIG_PROHIBIDO:
        if k in cfg:
            del cfg[k]
            quitadas += 1
    return quitadas


def limpiar_config_de_estado(estado):
    """Igual, pero recibe el bloque de estado entero."""
    if not isinstance(estado, dict):
        return 0
    return limpiar_config(estado.get("config"))


# ── Marcas de borrado ────────────────────────────────────────────────────
def esta_borrado(marcas, campo, ident):
    lista = (marcas or {}).get(campo)
    if not lista:
        return False
    return any(_igual_estricto(x.get("id"), ident) for x in lista if isinstance(x, dict))


def podar_borrados(estado, marcas):
    """Saca lo marcado como borrado y colapsa copias del mismo id en el
    inventario, igual que _podarBorrados() en el navegador."""
    quitados = 0
    for k in PODA_CATS:
        if not isinstance(estado.get(k), list):
            continue
        campo_id = MERGE_ID_FIELD.get(k, "id")
        antes = len(estado[k])
        # OJO: el JS pregunta r[idF] !== undefined, NO !== null. Un registro
        # con el id en null SI pasa por la lista de borrados, y si hay una
        # marca con id null, se va. Tratarlo como "sin id" —el error que
        # tenia este port— dejaba vivos registros que el navegador borra.
        # En merge_array_by_id es al reves: alli el JS descarta null ademas
        # de undefined, y por eso alla si se usa `is not None`.
        estado[k] = [
            r for r in estado[k]
            if not (isinstance(r, dict) and campo_id in r
                    and esta_borrado(marcas, k, r[campo_id]))
        ]
        quitados += antes - len(estado[k])

        if k in ("inventarioUsdt", "inventarioUsdt_cerrado"):
            vistos = set()
            antes2 = len(estado[k])
            limpia = []
            for r in estado[k]:
                # String(r && r.id). Solo el "undefined" —la clave que no
                # esta— queda fuera del colapso: String(null) es "null", que
                # SI participa, asi que dos registros con el id en null se
                # colapsan en uno. Excluir tambien a "null", como hacia este
                # port, dejaba copias que el navegador ya habia unificado.
                if not isinstance(r, dict) or "id" not in r:
                    ident = "undefined"
                else:
                    ident = _clave_js(r["id"])
                if ident == "undefined":
                    limpia.append(r)
                    continue
                if ident in vistos:
                    continue
                vistos.add(ident)
                limpia.append(r)
            estado[k] = limpia
            quitados += antes2 - len(estado[k])
    return quitados


# ── Renumeracion de remesas por fecha ────────────────────────────────────
def _clave_fecha(r):
    d = r.get("d") if isinstance(r, dict) else None
    partes = d.split("/") if isinstance(d, str) else []
    if len(partes) >= 2:
        try:
            return int(partes[1]) * 100 + int(partes[0])
        except (ValueError, TypeError):
            return 0
    return 0


def renumerar_por_fecha(lista):
    if not isinstance(lista, list):
        return
    # localeCompare sobre el _uid como desempate, igual que el front.
    lista.sort(key=lambda r: (_clave_fecha(r), (r.get("_uid") or "") if isinstance(r, dict) else ""))
    for i, r in enumerate(lista):
        if isinstance(r, dict):
            r["n"] = i + 1


# ── Fusion del bloque completo ───────────────────────────────────────────
def unir_marcas(a, b, ahora_ms=None):
    """Une dos _deletedMerge sin perder marcas de ninguno de los dos lados y
    sin duplicar. Las de mas de 30 dias se descartan, igual que en el front.

    La diferencia con el navegador es de quien es el reloj: alla cada
    dispositivo podaba con SU hora, asi que un telefono adelantado podia
    vencer marcas que para los demas seguian vivas —y lo borrado revivia.
    Aca el reloj es uno solo, el del servidor."""
    if ahora_ms is None:
        ahora_ms = int(time.time() * 1000)
    corte = ahora_ms - DIAS_MARCA_MS
    salida = {}
    for fuente in (a, b):
        for campo, marcas in (fuente or {}).items():
            if not isinstance(marcas, list):
                continue
            actuales = salida.setdefault(campo, [])
            for m in marcas:
                if not isinstance(m, dict) or not _es_numero(m.get("ts")) or m["ts"] <= corte:
                    continue
                if not any(_igual_estricto(x.get("id"), m.get("id")) for x in actuales):
                    actuales.append({"id": m.get("id"), "ts": m["ts"]})
    return {k: v for k, v in salida.items() if v}


def fusionar_estado(entrante, guardado, ts_entrante, ts_guardado):
    """Devuelve el estado fusionado. `guardado` es lo que hay en Mongo y
    `entrante` lo que manda el dispositivo.

    Ojo con el orden: en el navegador la fusion se hace CONTRA el estado vivo
    del dispositivo, o sea que el dispositivo hace de "local". Aca el arbitro
    es el servidor, asi que lo guardado hace de local y lo que llega del
    dispositivo hace de remoto. Es el mismo algoritmo con los papeles claros,
    y por eso deja de importar cual reloj va adelantado."""
    salida = dict(guardado or {})
    entrante = entrante or {}

    marcas = unir_marcas(guardado.get("_deletedMerge") if guardado else None,
                         entrante.get("_deletedMerge"))
    # ARREGLO 61: las marcas de config se unen ANTES de fusionar config, y con
    # las de cada lado tal como llegaron —no con las ya unidas—, que es lo que
    # permite comparar "cuando lo tocaste tu" contra "cuando lo toque yo".
    marcas_campos = unir_marcas_campos(guardado.get("_modCampos") if guardado else None,
                                       entrante.get("_modCampos"))
    _mc_ent = (entrante.get("_modCampos") or {}).get("config") if isinstance(entrante.get("_modCampos"), dict) else {}
    _mc_gua = ((guardado or {}).get("_modCampos") or {}).get("config") if isinstance((guardado or {}).get("_modCampos"), dict) else {}

    for k in DATA_KEYS:
        if k in ("_deletedMerge", "_modCampos"):
            continue
        if k not in entrante:
            continue
        if k == "config":
            salida["config"] = merge_config_safe(entrante.get("config"), salida.get("config") or {},
                                                 _mc_ent, _mc_gua)
            continue
        if k in MERGE_FIELDS:
            campo_id = MERGE_ID_FIELD.get(k, "id")
            llega = entrante.get(k)
            if isinstance(llega, list):
                llega = [
                    it for it in llega
                    if not (isinstance(it, dict) and campo_id in it
                            and esta_borrado(marcas, k, it[campo_id]))
                ]
            salida[k] = merge_array_by_id(llega, salida.get(k), campo_id,
                                          ts_entrante, ts_guardado)
        else:
            salida[k] = entrante[k]

    salida["_deletedMerge"] = marcas
    salida["_modCampos"] = marcas_campos
    limpiar_config_de_estado(salida)
    podar_borrados(salida, marcas)
    for k in RENUMERAR:
        if isinstance(salida.get(k), list):
            renumerar_por_fecha(salida[k])
    return salida


# ── Guardado: un documento de Mongo por clave ────────────────────────────
# No un documento gigante: asi no hay techo de tamaño, se escribe solo lo que
# cambio, y el dia que una coleccion se abra por separado ya esta sola en su
# lugar.
COL_ESTADO = "estado"
DOC_META = "_meta"
_candado_estado = threading.Lock()


def leer_estado():
    base = obtener_base()
    if base is None:
        return None, 0
    estado, ts = {}, 0
    for doc in base[COL_ESTADO].find({}):
        if doc["_id"] == DOC_META:
            ts = doc.get("ts", 0)
        else:
            estado[doc["_id"]] = doc.get("v")
    return estado, ts


def guardar_estado(nuevo, previo, ts):
    """Escribe solo las claves que cambiaron de verdad."""
    base = obtener_base()
    ops = []
    for k in DATA_KEYS:
        if k not in nuevo:
            continue
        antes = json.dumps(previo.get(k), ensure_ascii=False, sort_keys=True, default=str)
        ahora = json.dumps(nuevo.get(k), ensure_ascii=False, sort_keys=True, default=str)
        if antes != ahora or k not in previo:
            ops.append(ReplaceOne({"_id": k}, {"_id": k, "v": nuevo[k]}, upsert=True))
    ops.append(ReplaceOne({"_id": DOC_META}, {"_id": DOC_META, "ts": ts}, upsert=True))
    base[COL_ESTADO].bulk_write(ops, ordered=False)
    return len(ops) - 1


def estado_del_cuerpo(cuerpo):
    """Acepta las dos formas: {"estado": {...}} y el archivo que produce el
    boton Exportar de la app, que es el bloque pelado con un par de campos
    extra. Asi la carga inicial es subir el archivo tal como se descargo, sin
    tener que envolverlo a mano —que es donde uno se equivoca."""
    if not isinstance(cuerpo, dict):
        return None
    if isinstance(cuerpo.get("estado"), dict):
        return cuerpo["estado"]
    if any(k in cuerpo for k in DATA_KEYS):
        return cuerpo
    return None


def contar_estado(estado):
    return {k: (len(v) if isinstance(v, list) else 1)
            for k, v in (estado or {}).items() if v is not None}


# ── Auditoria ────────────────────────────────────────────────────────────
# Quien hizo que y cuando. Vivia en un nodo aparte de Firebase; al sacar
# Firebase se queda sin casa, y es justamente lo que no se puede perder.
#
# Se guarda en su propia coleccion y NO dentro de /estado: el estado viaja
# entero en cada guardado, y meterle un historial que solo crece haria que
# cada telefono suba megabytes por cada cambio.
COL_AUDITORIA = "auditoria"
DIAS_AUDITORIA = 90            # lo que se conserva; el resto se poda solo
MAX_AUDITORIA = 300            # cuantas entradas devuelve una consulta


def anotar_auditoria(usuario, accion, detalle):
    base = obtener_base()
    if base is None:
        return False
    ahora_ms = int(time.time() * 1000)
    base[COL_AUDITORIA].insert_one({
        "ts": ahora_ms,
        "fecha": cuerpo_fecha_local(ahora_ms),
        "usuario": usuario.get("nombre") or usuario.get("correo") or "?",
        "role": usuario.get("rol") or "?",
        "accion": _texto(accion, 80),
        "detalle": _texto(detalle, 500),
    })
    # Podar lo vencido aprovechando que ya estamos escribiendo. Con el indice
    # por ts es un borrado por rango, no un recorrido.
    limite = ahora_ms - DIAS_AUDITORIA * 24 * 60 * 60 * 1000
    base[COL_AUDITORIA].delete_many({"ts": {"$lt": limite}})
    return True


def cuerpo_fecha_local(ms):
    """La app muestra la hora de Caracas. Se guarda ya formateada porque es
    lo unico que se hace con este campo: mostrarlo."""
    return datetime.fromtimestamp(ms / 1000, timezone(timedelta(hours=-4))).strftime(
        "%d/%m/%Y, %H:%M:%S")


def listar_auditoria(role=None, tipo=None, limite=MAX_AUDITORIA):
    base = obtener_base()
    if base is None:
        return None
    filtro = {}
    if role:
        filtro["role"] = role
    if tipo:
        # El front filtra por prefijo: "REMESA" tiene que traer
        # "REMESA_CREADA" y "REMESA_ELIMINADA".
        filtro["accion"] = {"$regex": "^" + _escapar_regex(str(tipo))}
    cursor = base[COL_AUDITORIA].find(filtro, {"_id": 0}).sort("ts", -1).limit(int(limite))
    return list(cursor)


def _escapar_regex(t):
    return "".join(("\\" + c) if c in ".^$*+?()[]{}|\\" else c for c in t)


# ── Respaldo de la base ──────────────────────────────────────────────────
# Colecciones que NUNCA salen en un respaldo:
#   sesiones -> son testigos vivos; el archivo terminaria siendo una llave
#               de entrada para cualquiera que lo abra.
# De "usuarios" se saca el campo de la clave cifrada: el archivo se baja al
# telefono y se sube a la nube, y ahi no tiene por que viajar. Al restaurar,
# las claves se vuelven a poner; los datos del negocio no dependen de eso.
# ARREGLO 103: "respaldos" va FUERA, y no es un detalle. armar_respaldo()
# recorre todas las colecciones de la base: sin esta linea, la copia de hoy se
# llevaria dentro las trece anteriores, la de mañana esas catorce otra vez, y
# en una semana la base no cabe. (COL_RESPALDOS se define mas abajo, con el
# resto del motor; aqui va el literal para no mover el orden del archivo.)
COLECCIONES_FUERA = ["sesiones", "respaldos"]
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
        # El respaldo se baja al telefono y se sube a la nube: los restos del
        # acceso por PIN no tienen por que viajar ahi. En "estado" la config
        # es un documento suelto, {"_id": "config", "v": {...}}.
        if nombre == COL_ESTADO:
            for d in docs:
                if d.get("_id") == "config":
                    limpiar_config(d.get("v"))
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


# ── ARREGLO 103 · el respaldo se hace SOLO ────────────────────────────────
#
# armar_respaldo() existe desde hace tiempo, pero solo cuando alguien se lo
# pide a mano. Auditado el 07/10: no habia nada programado, asi que lo unico
# que separaba sus datos de la nada era que se acordara de exportar.
#
# LO QUE ESTO CUBRE Y LO QUE NO, que es la mitad importante:
#
#   un borrado por error, una fusion que se come algo   SI lo cubre
#   perder la base entera (la cuenta, el proveedor)     NO lo cubre
#
# Una copia DENTRO de la misma base no sobrevive a que se pierda la base. Por
# eso esto no sustituye a que ella se baje un archivo de vez en cuando: lo que
# hace es que esa descarga sea de ayer y no de hace tres meses, y que la app
# pueda decirle cuanto hace que no se baja una.
COL_RESPALDOS = "respaldos"
RESPALDOS_QUE_SE_GUARDAN = 14      # dos semanas de copias diarias
RESPALDO_CADA_HORAS = 23           # se intenta una vez al dia
_candado_respaldo = threading.Lock()


def _clave_dia(momento=None):
    return (momento or ahora()).strftime("%Y-%m-%d")


def hacer_respaldo(forzado=False):
    """Guarda una copia del dia. Devuelve la clave si la hizo, None si no tocaba.

    Va bajo el MISMO candado que las escrituras del estado: el guardado toca
    varios documentos, uno por clave, y copiar en medio se llevaria un estado
    a medio armar —con las remesas nuevas y los saldos viejos—. Es la misma
    razon por la que GET /estado tambien lo pide.
    """
    base = obtener_base()
    if base is None:
        return None
    with _candado_respaldo:
        clave = _clave_dia()
        if not forzado and base[COL_RESPALDOS].find_one({"_id": clave}, {"_id": 1}):
            return None
        with _candado_estado:
            copia = armar_respaldo()
        if not copia:
            return None
        base[COL_RESPALDOS].replace_one(
            {"_id": clave},
            {
                "_id": clave,
                "ts": ahora_ms(),
                "fecha": ahora().isoformat(),
                "resumen": copia.get("_respaldo", {}),
                "datos": copia.get("datos", {}),
            },
            upsert=True,
        )
        # Podar lo viejo aprovechando que ya estamos escribiendo. Se ordena por
        # la clave, que es la fecha: no hace falta mirar dentro de cada copia.
        claves = sorted(
            [d["_id"] for d in base[COL_RESPALDOS].find({}, {"_id": 1})],
            reverse=True,
        )
        sobran = claves[RESPALDOS_QUE_SE_GUARDAN:]
        if sobran:
            base[COL_RESPALDOS].delete_many({"_id": {"$in": sobran}})
        return clave


def estado_respaldos():
    """Cuantas copias hay y de cuando es la ultima. Lo lee la app."""
    base = obtener_base()
    if base is None:
        return {"ok": False, "error": "base no disponible"}
    docs = list(base[COL_RESPALDOS].find({}, {"datos": 0}).sort("_id", -1))
    if not docs:
        return {"ok": True, "copias": 0, "ultima": None, "guarda": RESPALDOS_QUE_SE_GUARDAN}
    u = docs[0]
    return {
        "ok": True,
        "copias": len(docs),
        "guarda": RESPALDOS_QUE_SE_GUARDAN,
        "ultima": {
            "dia": u["_id"],
            "ts": u.get("ts"),
            "fecha": u.get("fecha"),
            "registros": (u.get("resumen") or {}).get("total"),
        },
        "dias": [d["_id"] for d in docs],
    }


def _ronda_de_respaldo():
    """Hilo de fondo: lo intenta cada hora y solo hace uno al dia.

    Se mira la hora en vez de dormir 24 h de golpe porque este servidor se
    reinicia con cada despliegue: durmiendo un dia entero, una semana de
    despliegues seguidos no dejaria ni una copia.
    """
    while True:
        try:
            clave = hacer_respaldo()
            if clave:
                print("Respaldo automatico guardado: %s" % clave, flush=True)
        except Exception as e:
            # Un fallo aqui NO puede tumbar la API: la app tiene que seguir
            # contestando aunque la copia de hoy no se pueda hacer.
            print("AVISO: no se pudo hacer el respaldo: %r" % e, flush=True)
        time.sleep(60 * 60)


def arrancar_respaldo_automatico():
    h = threading.Thread(target=_ronda_de_respaldo, name="respaldo", daemon=True)
    h.start()
    return h


# Las colecciones que la aplicacion usa de verdad. Un nombre fuera de esta
# lista no es una coleccion que este vacia: es un nombre equivocado.
COLECCIONES_CONOCIDAS = [COL_ESTADO, "clientes", COL_AUDITORIA] + \
    COLECCIONES_FUERA + COLECCIONES_NO_RESTAURAR


def restaurar_respaldo(archivo):
    """Solo en pruebas. Reemplaza las colecciones que vengan en el archivo.

    Devuelve (resultado, salteadas, desconocidas). Si hay desconocidas no
    escribe NADA: se avisa y se corta.

    Antes cualquier nombre valia. Quien mandaba las claves de la aplicacion
    —brl, vzla, eeuu— en vez de las colecciones de la base creaba colecciones
    con esos nombres, que nadie lee, y recibia 200 con "restaurado": {"brl": 1}.
    O sea: exito informado sin haber restaurado nada. El estado vive en la
    coleccion "estado", un documento por clave, y quedaba intacto. Costo un
    rato averiguar por que el conteo no bajaba.
    """
    base = obtener_base()
    datos = (archivo or {}).get("datos") or {}

    desconocidas = [n for n, docs in datos.items()
                    if isinstance(docs, list) and n not in COLECCIONES_CONOCIDAS]
    if desconocidas:
        return {}, [], sorted(desconocidas)

    resultado = {}
    salteadas = []
    for nombre, docs in datos.items():
        if not isinstance(docs, list):
            continue
        if nombre in COLECCIONES_FUERA or nombre in COLECCIONES_NO_RESTAURAR:
            salteadas.append(nombre)
            continue
        # Un respaldo hecho antes de este cambio todavia trae los restos del
        # acceso por PIN. Restaurarlo los repondria.
        if nombre == COL_ESTADO:
            for d in docs:
                if isinstance(d, dict) and d.get("_id") == "config":
                    limpiar_config(d.get("v"))
        base[nombre].delete_many({})
        if docs:
            base[nombre].insert_many(docs)
        resultado[nombre] = len(docs)
    return resultado, salteadas, desconocidas


# ── Servidor ─────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════
# EL MERCADO P2P — a como esta comprando y vendiendo USDT la gente, AHORA
# ══════════════════════════════════════════════════════════════════════════
# Sus palabras: "no puedo estar todo el tiempo dependiendo de mi competencia
# para saber si bajo o subo la tasa".
#
# Esto vive AQUI y no en la app por una razon que no tiene vuelta: la app
# corre en una pagina, y Binance no autoriza que otro dominio le pregunte —el
# navegador bloquea la respuesta antes de que llegue—. El servidor no tiene esa
# limitacion. No hace falta volver a intentarlo del otro lado.
#
# Lo que se lee es el tablon P2P, el mismo que carga su pagina. No es un
# contrato: puede cambiar sin avisar. Por eso TODO lo de aqui falla suave —si
# algo no viene como se espera, se contesta "sin lectura" y la app sigue
# entera—. La misma regla que la huella: una pieza que no responde no puede
# costarle el dia.
#
# Para VENEZUELA no hay alternativa: Binance no tiene par al contado USDT/VES.
# El unico sitio donde existe ese precio es este tablon.
BINANCE_P2P = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
# El mercado NORMAL de Binance, que no es el P2P. Aqui USDT/BRL es un par como
# cualquier otro y su precio es publico, sin filtro de pais.
#
# Existe porque el tablon P2P de reales viene VACIO para este servidor en las
# DOS direcciones —comprobado el 30/09 con el sondeo, y con la pregunta simple
# tambien—, mientras el de bolivares, desde la misma maquina, trae anuncios.
# Esa puerta esta cerrada y no se va a abrir. Esta es otra.
#
# Para bolivares no hay par de mercado: Binance no lista VES. Ahi el P2P es el
# unico sitio donde existe ese precio, y ese si funciona.
BINANCE_SPOT = "https://api.binance.com/api/v3/ticker/price"
SPOT_POR_MONEDA = {"BRL": "USDTBRL"}

# Y si Binance no contesta, OTROS SITIOS. Su servidor esta en Railway EE.UU. y
# Binance le devuelve 451 —"bloqueado por tu pais"— en el mercado normal, igual
# que le vacia el tablon P2P de reales. Cambiar de region es un ajuste de pago
# que su plan no tiene, asi que el precio hay que buscarlo donde si conteste.
#
# USDT/BRL es de los mercados mas liquidos que hay: entre un sitio y otro la
# diferencia es de decimas de por ciento. Para un SUELO —hasta donde puede
# ofrecer sin perder— eso vale de sobra. Lo que no vale es callar de donde
# salio, y por eso cada lectura trae su "fuente" y la pantalla la enseña.
#
# El orden importa: Binance primero, porque es donde ella opera de verdad.
FUENTES_BRL = [
    ("Binance", BINANCE_SPOT + "?symbol=USDTBRL"),
    ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price"
                  "?ids=tether&vs_currencies=brl"),
    ("Mercado Bitcoin", "https://api.mercadobitcoin.net/api/v4/tickers"
                        "?symbols=USDT-BRL"),
]
# Mas corto que el del P2P: son tres seguidas, y si las tres se cuelgan la
# pantalla se queda esperando. Con 4s el peor caso son 12, no 18.
MERCADO_ESPERA_FUENTE = 4
MERCADO_ESPERA = 6          # segundos; si tarda mas, se responde sin lectura
MERCADO_CACHE_SEG = 300     # 5 min: ella abre la pantalla muchas veces al dia
# Un FALLO no se guarda cinco minutos. El boton de la tarjeta dice "reintentar"
# y con el cache largo no reintentaba nada: devolvia el mismo fallo guardado
# durante cinco minutos, asi que pulsarlo parecia no hacer nada. Veinte
# segundos es bastante para que cien repintados no se conviertan en cien
# preguntas a Binance, y poco para que pulsar el boton signifique algo.
MERCADO_CACHE_FALLO_SEG = 20
MERCADO_MAX_ANUNCIOS = 10   # de los que pasan el filtro, los 10 mejores
# Cuantos anuncios tienen que aceptar un monto para que valga como "el mercado".
# Leer UNO es lo mismo que leer "el mejor precio del tablon", que es justo lo
# que no sirve: publicar contra un precio que solo da una persona.
MERCADO_MIN_ANUNCIOS = 3
# Las dos direcciones del tablon, para el sondeo de mas abajo.
MERCADO_OTRO_LADO = {"BUY": "SELL", "SELL": "BUY"}

# Su volumen tipico, medido en sus propios lotes (70 compras y 131 ventas):
# compra USDT con reales por una mediana de 196 USDT (~R$ 1.000) y vende por
# bolivares por 118 USDT (~112.000 Bs). Importa: en el tablon los buenos
# precios estan en los anuncios GRANDES, asi que leer "el mejor precio" le
# daria uno que no puede tomar, y publicaria una tasa que no puede sostener.
MERCADO_MONTO_POR_DEFECTO = {"BRL": 1000.0, "VES": 112000.0}

# Como se presenta ante Binance. Esto NO es cosmetico: el tablon es la misma
# direccion que carga su pagina y delante tiene un filtro que corta a lo que no
# parece un navegador. El servidor se presentaba como "centrogestion-api" -que
# es justo lo que ese filtro busca- y desde el navegador la tarjeta solo decia
# "sin lectura", sin numero ni nada.
#
# No hay forma de probar esto sin salir a internet, ni aqui ni en el CI: la
# unica prueba de verdad es su servidor desplegado. Por eso el commit anterior
# va primero: si esto no era, la tarjeta ahora dice que fue.
#
# Origin y Referer van porque un navegador de verdad los manda al llamar a esta
# direccion, y un filtro que mira el User-Agent suele mirarlos tambien.
MERCADO_CABECERAS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Language": "es,en;q=0.9",
    "Origin": "https://p2p.binance.com",
    "Referer": "https://p2p.binance.com/es/trade/all-payments/USDT",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
}

_mercado_cache = {}
_mercado_candado = threading.Lock()


def _num_o_none(v):
    """Un numero de verdad, o nada. El tablon devuelve los precios como texto."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")) or n <= 0:
        return None
    return n


def _mediana(valores):
    if not valores:
        return None
    v = sorted(valores)
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def _motivo_corto(v, tope=70):
    """Lo que venga, en una linea corta.

    Esto acaba en una tarjeta de SU pantalla, no en un log: un volcado de tres
    parrafos ahi no se lee y encima puede traer cosas de Binance que no
    controlamos. Se aplasta a una linea y se corta.
    """
    t = " ".join(str(v if v is not None else "").split())
    return t[:tope] if t else "sin detalle"


def _monto_legible(monto):
    """1000 -> "1.000". Va dentro de una frase que ella lee."""
    try:
        return "{:,.0f}".format(float(monto)).replace(",", ".")
    except (TypeError, ValueError):
        return str(monto)


MERCADO_CABECERAS_SPOT = {
    "Accept": "application/json",
    "Accept-Language": "es,en;q=0.9",
    "User-Agent": MERCADO_CABECERAS["User-Agent"],
}


def _precio_de(datos):
    """El precio dentro de la respuesta, venga en la forma que venga.

    Cada sitio lo envuelve distinto y no hay contrato: se prueban las tres
    formas conocidas y si ninguna encaja se devuelve nada, en vez de adivinar.

      Binance          {"price": "5.27"}
      CoinGecko        {"tether": {"brl": 5.27}}
      Mercado Bitcoin  [{"pair": "USDT-BRL", "last": "5.27"}]
    """
    if isinstance(datos, list):
        datos = datos[0] if datos and isinstance(datos[0], dict) else None
    if not isinstance(datos, dict):
        return None
    directo = _num_o_none(datos.get("price")) or _num_o_none(datos.get("last"))
    if directo:
        return directo
    dentro = datos.get("tether")
    if isinstance(dentro, dict):
        return _num_o_none(dentro.get("brl"))
    return None


def _pedir_precio(url):
    """Una fuente. Devuelve (precio, motivo del fallo)."""
    pedido = Request(url, headers=dict(MERCADO_CABECERAS_SPOT))
    try:
        with urlopen(pedido, timeout=MERCADO_ESPERA_FUENTE) as r:
            datos = json.loads(r.read().decode("utf-8"))
    except HTTPError as err:
        # 451 es "bloqueado por tu pais" y es EL caso: conviene que se lea tal
        # cual en la pantalla, porque no se arregla con codigo.
        return None, "respondio %s" % err.code
    except TimeoutError:
        return None, "tardo mas de %ss" % MERCADO_ESPERA_FUENTE
    except URLError as err:
        return None, "no se llego (%s)" % _motivo_corto(err.reason, 40)
    except Exception:
        return None, "contesto algo que no se entiende"
    precio = _precio_de(datos)
    if precio is None:
        return None, "no dio precio"
    return precio, ""


def _precio_spot(fiat):
    """El precio de 1 USDT en esa moneda, del primer sitio que conteste.

    Devuelve ({tasa, fuente}, "") o (None, motivo con lo que dijo CADA uno).
    No lleva anuncios ni monto: aqui no hay anuncio que aceptar, el precio es
    uno solo.
    """
    if fiat != "BRL":
        return None, "no hay mercado de %s fuera del P2P" % fiat
    fallos = []
    for nombre, url in FUENTES_BRL:
        precio, fallo = _pedir_precio(url)
        if precio:
            return {"tasa": round(precio, 4), "fuente": nombre}, ""
        fallos.append("%s %s" % (nombre, fallo))
    # Se cuentan TODAS, no solo la primera: cual conteste y cual no es
    # justamente lo que hay que saber para decidir que hacer despues.
    return None, " · ".join(fallos)


def _porque_vacio(datos, fiat):
    """El tablon contesto SIN un solo anuncio. Eso hay que contarlo tal cual.

    Un tablon vacio en un mercado grande —Brasil tiene cientos de anuncios a
    cualquier hora— no es creible: lo normal es que nos esten filtrando en
    silencio, contestando 200 con la lista vacia en vez de un 403 que se vea.
    Asi que se repite lo que dijo EL (success, code, message, total) en vez de
    suponerlo nosotros. Paso el 29/09 con los reales y no habia por donde
    agarrarlo: la pantalla decia "no tiene anuncios" y eso no se lo cree nadie.
    """
    trozos = []
    if datos.get("success") is False:
        trozos.append("dice que no tuvo exito")
    codigo = datos.get("code")
    if codigo not in (None, "", "000000"):
        trozos.append("codigo " + _motivo_corto(codigo, 20))
    mensaje = datos.get("message")
    if mensaje:
        trozos.append(_motivo_corto(mensaje, 40))
    total = datos.get("total")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        trozos.append("total %d" % int(total))
    base = "Binance devolvio 0 anuncios de %s" % fiat
    return base + (" (" + " · ".join(trozos) + ")" if trozos else "")


def _pedir_tablon(fiat, tipo, filas=20, sencillo=False):
    """Una pagina del tablon. Devuelve (lista, motivo del fallo).

    tipo es desde el punto de vista de quien pregunta: "BUY" = quiero comprar
    USDT, y contesta con los anuncios de quien vende.

    Devuelve DOS cosas a proposito. Antes devolvia la lista o None a secas, y
    ese None se confundia mas abajo con "el tablon contesto pero ningun anuncio
    acepta su monto": la pantalla acababa diciendo "sin lectura" para las dos.
    Son problemas distintos y se arreglan en sitios distintos -uno aqui, el
    otro bajando el monto en Configuracion-, asi que hay que poder decir cual.
    """
    pregunta = {"fiat": fiat, "asset": "USDT", "tradeType": tipo,
                "page": 1, "rows": filas}
    if not sencillo:
        # Lo que manda su propia pagina. Cuesta nada y es una cosa menos por la
        # que el filtro pueda decir que esto no es un navegador.
        #
        # Con "sencillo" se quitan los tres: es la pregunta minima que el tablon
        # entiende. Sirve para saber si lo que vacia una moneda es alguno de
        # estos campos y no que de verdad no haya anuncios.
        pregunta.update({"payTypes": [], "publisherType": None, "clientType": "web"})
    cuerpo = json.dumps(pregunta).encode("utf-8")
    pedido = Request(BINANCE_P2P, data=cuerpo, method="POST",
                     headers=dict(MERCADO_CABECERAS))
    try:
        with urlopen(pedido, timeout=MERCADO_ESPERA) as r:
            datos = json.loads(r.read().decode("utf-8"))
    except HTTPError as err:
        # El caso mas esperable: Binance corta a quien no le gusta. El numero
        # importa -403 es "no me fio de ti" y 429 es "vas muy rapido"- y se
        # arreglan distinto, asi que se dice cual fue.
        return None, "Binance respondio %s" % err.code
    except TimeoutError:
        return None, "Binance tardo mas de %ss en contestar" % MERCADO_ESPERA
    except URLError as err:
        # HTTPError hereda de URLError, por eso va despues. Aqui caen las que
        # no llegaron a tener respuesta: DNS, conexion rechazada, salida
        # bloqueada.
        return None, "no se llego a Binance (%s)" % _motivo_corto(err.reason)
    except Exception:
        return None, "Binance contesto algo que no se entiende"
    if not isinstance(datos, dict):
        return None, "Binance contesto algo que no se entiende"
    lista = datos.get("data")
    if not isinstance(lista, list):
        # Cuando corta por filtro suele contestar 200 con su propio mensaje
        # dentro en vez de un error HTTP. Decirlo vale mas que "no se entiende".
        suyo = datos.get("message") or datos.get("code")
        if suyo:
            return None, "Binance dijo: %s" % _motivo_corto(suyo)
        return None, "Binance contesto sin lista de anuncios"
    # La lista vacia trae motivo aunque no sea un fallo de red: lo que hay que
    # contar solo se ve desde aqui, con el cuerpo de la respuesta delante.
    if not lista:
        return lista, _porque_vacio(datos, fiat)
    return lista, ""


def _anuncios_legibles(crudo):
    """(precio, minimo, maximo) de cada anuncio que se puede leer entero.

    Sin limites declarados no se puede saber si acepta un monto: se descarta en
    vez de suponer que si.
    """
    fuera = []
    for fila in crudo:
        if not isinstance(fila, dict):
            continue
        adv = fila.get("adv")
        if not isinstance(adv, dict):
            continue
        precio = _num_o_none(adv.get("price"))
        minimo = _num_o_none(adv.get("minSingleTransAmount"))
        maximo = _num_o_none(adv.get("maxSingleTransAmount"))
        if precio is None or minimo is None or maximo is None:
            continue
        if maximo < minimo:
            continue
        fuera.append((precio, minimo, maximo))
    return fuera


def _precios_a(anuncios, monto):
    """Los precios de los anuncios que aceptan ese monto, en el orden del tablon."""
    return [precio for (precio, minimo, maximo) in anuncios
            if minimo <= monto <= maximo]


def _monto_alcanzable(anuncios, pedido):
    """El monto mas CERCANO al suyo que todavia acepten varios anuncios.

    Cercano, no el mas grande. La primera version buscaba solo entre los
    MAXIMOS y hacia abajo desde el techo del tablon, dando por hecho que si su
    monto no entraba era por pasarse. El 30/09 paso lo contrario: los anuncios
    de VES pedian MINIMOS por encima de sus 112.000 Bs —su operacion era
    demasiado PEQUEÑA— y la busqueda acabo en 36.450.000 Bs, unos 38.000 USDT.
    Le midio el precio de un mercado en el que no opera, y de ahi salia un
    suelo optimista, que es peor que ninguno.

    Reproducido con un tablon como el suyo: a 112.000 lo aceptaban 0 anuncios,
    esto elegia 50.000.000 (4 anuncios) y 200.000 lo aceptaban 8.

    Ahora se miran los minimos Y los maximos —son los unicos montos donde
    cambia quien acepta— y gana el que menos se aleje del suyo, hacia arriba o
    hacia abajo. Sigue sin valer leer UN anuncio: eso es leer "el mejor precio
    del tablon". Si ninguno llega a MERCADO_MIN_ANUNCIOS, manda el que tenga
    mas, y entre iguales el mas cercano.
    """
    candidatos = set()
    for (_, minimo, maximo) in anuncios:
        candidatos.add(minimo)
        candidatos.add(maximo)
    mejor = None                      # (cuantos, -distancia, monto)
    for monto in sorted(candidatos):
        cuantos = len(_precios_a(anuncios, monto))
        if not cuantos:
            continue
        # Con la cuenta hecha, lo unico que importa es la cercania; por debajo
        # de la cuenta, primero mas anuncios y despues la cercania.
        marca = (min(cuantos, MERCADO_MIN_ANUNCIOS), -abs(monto - pedido))
        if mejor is None or marca > mejor[0]:
            mejor = (marca, monto)
    return mejor[1] if mejor else None


def _sondear_otro_lado(fiat, tipo):
    """El mismo tablon en la direccion contraria. Una vez, solo para contarlo.

    El 30/09 los reales volvieron con CERO anuncios y "total 0" —o sea Binance
    diciendo "todo bien, no hay nada"— mientras el tablon de bolivares, desde
    el MISMO servidor, traia 20. Asi que no es un bloqueo general: le pasa algo
    a esa consulta en concreto, y desde fuera las dos posibilidades se ven
    exactamente igual.

    Esto las separa, y la respuesta decide que hacer despues:

      el otro lado trae anuncios -> el tablon de esa moneda existe y solo se
                                    vacia ese sentido
      el otro lado tambien cero  -> Binance no le sirve tablon de esa moneda a
                                    este servidor, y esa tasa no se va a poder
                                    leer sola

    Va con el cuerpo simple: si el normal ya vino vacio, repetirlo aqui seria
    medir otra vez lo mismo. Y nunca levanta —es un diagnostico, no un dato—.
    """
    otro = MERCADO_OTRO_LADO.get(tipo)
    if not otro:
        return "sin otro lado que mirar"
    try:
        crudo, _ = _pedir_tablon(fiat, otro, sencillo=True)
    except Exception:
        return "el otro lado no se pudo mirar"
    if crudo is None:
        return "el otro lado del tablon no contesto"
    if not crudo:
        return "el otro lado del tablon de %s tambien viene vacio" % fiat
    return ("el otro lado del tablon de %s SI trae %d anuncios"
            % (fiat, len(crudo)))


def _leer_mercado(fiat, tipo, monto):
    """El precio alcanzable A SU VOLUMEN, no el mejor del tablon.

    Devuelve ({tasa, anuncios, monto, montoPedido?}, "") o (None, motivo). Se
    queda con los anuncios cuyo rango acepte su monto, y de esos toma la
    MEDIANA: el primero de la lista suele ser diminuto o con condiciones, y
    publicar contra ese numero es publicar contra un precio que no existe
    para ella.

    Si NINGUNO acepta su monto, baja al mayor monto que si esten dando y lo
    dice en "montoPedido". Antes eso era todo o nada y se quedaba sin lectura.
    """
    crudo, fallo = _pedir_tablon(fiat, tipo)
    if crudo is None:
        return None, fallo
    # Un tablon vacio en un mercado grande no es creible, asi que se pregunta
    # UNA vez mas con el cuerpo mas simple posible. Separa "Binance no tiene
    # anuncios" de "no le gusta como se lo pedimos" —y si era lo segundo, lo
    # arregla en el acto, sin esperar a otro despliegue—.
    #
    # Solo cuando ya vino vacio: una lectura buena no cuesta ni una llamada mas
    # de las que costaba. Y solo una vez: dos no aportan nada y Binance corta
    # a quien pregunta mucho.
    if not crudo:
        crudo_otra, fallo_otra = _pedir_tablon(fiat, tipo, sencillo=True)
        if crudo_otra:
            crudo, fallo = crudo_otra, ""
        else:
            # El motivo se construye entero aqui, no pegando un sufijo a lo que
            # hubiera: si el primero venia vacio quedaba una frase empezada por
            # " · con la pregunta simple tampoco", que no dice ni de que moneda.
            base = (fallo_otra or fallo or
                    ("Binance no tiene anuncios de %s ahora mismo" % fiat))
            fallo = base + " · con la pregunta simple tampoco · " + _sondear_otro_lado(fiat, tipo)
    if not crudo:
        return None, fallo or ("Binance no tiene anuncios de %s ahora mismo" % fiat)
    anuncios = _anuncios_legibles(crudo)
    if not anuncios:
        return None, ("de los %d anuncios de %s, ninguno dice sus límites"
                      % (len(crudo), fiat))
    precios = _precios_a(anuncios, monto)
    usado = monto
    if not precios:
        usado = _monto_alcanzable(anuncios, monto)
        precios = _precios_a(anuncios, usado) if usado else []
    if not precios:
        return None, ("de los %d anuncios de %s, ninguno acepta %s"
                      % (len(crudo), fiat, _monto_legible(monto)))
    precios = precios[:MERCADO_MAX_ANUNCIOS]
    dato = {"tasa": round(_mediana(precios), 4),
            "anuncios": len(precios), "monto": usado}
    # Solo cuando NO es el suyo: asi la app puede decirlo sin tener que
    # comparar por su cuenta, y una lectura normal no arrastra un campo de mas.
    if usado != monto:
        dato["montoPedido"] = monto
    return dato, ""


def mercado_p2p(montos=None):
    """Las dos lecturas que necesita, con cache.

    NUNCA levanta: si Binance no contesta, o contesta algo raro, devuelve
    disponible=False y la app dibuja lo que paso. Que se caiga el tablon no
    puede dejarla sin poder trabajar.
    """
    montos = montos or {}
    brl = _num_o_none(montos.get("BRL")) or MERCADO_MONTO_POR_DEFECTO["BRL"]
    ves = _num_o_none(montos.get("VES")) or MERCADO_MONTO_POR_DEFECTO["VES"]
    clave = (brl, ves)
    ahora_seg = time.time()
    with _mercado_candado:
        guardado = _mercado_cache.get(clave)
        if guardado:
            vida = (MERCADO_CACHE_SEG if guardado[1].get("disponible")
                    else MERCADO_CACHE_FALLO_SEG)
            if (ahora_seg - guardado[0]) < vida:
                return guardado[1]
    # Los REALES salen del mercado normal (USDT/BRL es un par de verdad). El
    # P2P de reales esta cerrado para este servidor y no va a volver, asi que
    # ni se le pregunta mientras el mercado conteste.
    compra, fallo_brl = _precio_spot("BRL")
    if compra is None:
        # Solo si el mercado falla se prueba el P2P: por si algun dia se abre,
        # y porque su motivo dice mas que quedarse a medias.
        compra_p2p, fallo_p2p = _leer_mercado("BRL", "BUY", brl)
        if compra_p2p:
            compra, fallo_brl = compra_p2p, ""
        else:
            fallo_brl = "%s · y el P2P: %s" % (fallo_brl, fallo_p2p)
    # Ella VENDE USDT por bolivares -> mira a quien los compra (SELL). Aqui el
    # P2P es el unico sitio: Binance no lista VES.
    venta, fallo_ves = _leer_mercado("VES", "SELL", ves)
    fuera = {
        "ok": True,
        "leido": ahora().isoformat(),
        "compraBRL": compra,
        "ventaVES": venta,
        "disponible": bool(compra or venta),
    }
    # El porque va SIEMPRE que falte un lado, aunque el otro si tenga lectura.
    # Con una sola de las dos tasas no hay suelo, asi que media lectura es un
    # fallo que ella tiene que poder perseguir igual.
    if not compra:
        fuera["motivoBRL"] = fallo_brl
    if not venta:
        fuera["motivoVES"] = fallo_ves
    if not fuera["disponible"]:
        # Un resumen para quien solo mire este campo. Cuando las dos fallan por
        # lo mismo -lo normal si el que corta es Binance- no se dice dos veces.
        fuera["motivo"] = (fallo_brl if fallo_brl == fallo_ves
                           else "reales: %s · bolivares: %s" % (fallo_brl, fallo_ves))
    with _mercado_candado:
        _mercado_cache[clave] = (ahora_seg, fuera)
    return fuera


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

    def _leer_json(self, maximo=CUERPO_MAXIMO):
        try:
            largo = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if largo <= 0 or largo > maximo:
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

    def _editor(self):
        """Sesion valida CON permiso de edicion. El Supervisor (rol
        "lector") entra y ve todo, pero no modifica: por eso se comprueba
        el permiso y no solo que haya sesion."""
        usuario = usuario_de_sesion(self._testigo())
        if not usuario:
            return None, (401, {"ok": False, "error": "sesion vencida"})
        if usuario.get("rol") == "admin":
            return usuario, None
        if not (usuario.get("permisos") or {}).get("editar"):
            return None, (403, {"ok": False, "error": "tu usuario no puede modificar datos"})
        return usuario, None

    def _sesion_sin_base(self):
        """Como _sesion, pero si Mongo esta caido lo dice en vez de reventar.

        El mercado no guarda nada ni lee del negocio: no tiene por que caerse
        con la base. Se separa para no meter un try dentro de cada ruta.
        """
        try:
            usuario = usuario_de_sesion(self._testigo())
        except PyMongoError:
            return None, (503, {"ok": False, "error": "base no disponible"})
        if not usuario:
            return None, (401, {"ok": False, "error": "sesion vencida"})
        return usuario, None

    def _sesion(self):
        """Solo pide sesion valida: alcanza para leer."""
        usuario = usuario_de_sesion(self._testigo())
        if not usuario:
            return None, (401, {"ok": False, "error": "sesion vencida"})
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
                "version": version_desplegada(),
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
        if ruta == "/usuarios":
            try:
                usuario, error = self._admin()
                if error:
                    return self._responder(*error)
                return self._responder(200, {"ok": True, "usuarios": listar_usuarios()})
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})

        if ruta == "/clientes":
            try:
                usuario, error = self._sesion()
                if error:
                    return self._responder(*error)
                return self._responder(200, {"ok": True, "clientes": listar_clientes()})
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})

        if ruta == "/mercado":
            # Pide sesion como todo lo demas, pero NO toca Mongo: si la base
            # esta caida esto sigue contestando, y al reves tambien.
            usuario, error = self._sesion_sin_base()
            if error:
                return self._responder(*error)
            pedido = parse_qs(urlparse(self.path).query)
            montos = {
                "BRL": (pedido.get("brl") or [None])[0],
                "VES": (pedido.get("ves") or [None])[0],
            }
            return self._responder(200, mercado_p2p(montos))

        if ruta == "/estado":
            # Cualquier sesion valida puede leer: el Supervisor tambien tiene
            # que ver los numeros. Lo que no puede es escribir.
            try:
                usuario, error = self._sesion()
                if error:
                    return self._responder(*error)
                # Bajo el mismo candado que las escrituras: el guardado toca
                # varios documentos, uno por clave, y leer en el medio
                # devolveria un estado a medio armar —con las remesas nuevas
                # pero los saldos viejos, por ejemplo.
                with _candado_estado:
                    estado, ts = leer_estado()
                if estado is None:
                    return self._responder(503, {"ok": False, "error": "base no disponible"})
                limpiar_config_de_estado(estado)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {"ok": True, "estado": estado, "_ts": ts})

        if ruta == "/estado/resumen":
            # Cuantos registros hay de cada cosa, sin bajarse el bloque entero.
            # Es con lo que se comprueba una carga inicial o una migracion.
            try:
                usuario, error = self._sesion()
                if error:
                    return self._responder(*error)
                with _candado_estado:
                    estado, ts = leer_estado()
                if estado is None:
                    return self._responder(503, {"ok": False, "error": "base no disponible"})
                limpiar_config_de_estado(estado)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {"ok": True, "conteo": contar_estado(estado), "_ts": ts})

        if ruta == "/auditoria":
            # Cualquier sesion valida la lee: el Supervisor tiene que poder
            # revisar quien toco que, y para eso entra.
            try:
                usuario, error = self._sesion()
                if error:
                    return self._responder(*error)
                consulta = parse_qs(urlparse(self.path).query)
                entradas = listar_auditoria(
                    role=(consulta.get("role") or [""])[0].strip() or None,
                    tipo=(consulta.get("tipo") or [""])[0].strip() or None,
                )
                if entradas is None:
                    return self._responder(503, {"ok": False, "error": "base no disponible"})
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {"ok": True, "entradas": entradas})

        if ruta == "/respaldo/estado":
            # Cuantas copias automaticas hay y de cuando es la ultima. La app
            # lo enseña en Configuracion: si nadie lo ve, nadie se entera de
            # que llevan tres semanas sin hacerse.
            try:
                usuario, error = self._admin()
                if error:
                    return self._responder(*error)
                return self._responder(200, estado_respaldos())
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})

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
        cuerpo = self._leer_json(
            CUERPO_MAXIMO_ESTADO if ruta == "/estado/importar" else CUERPO_MAXIMO)

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
            if len(nueva) < LARGO_MINIMO_CLAVE:
                return self._responder(400, {
                    "ok": False,
                    "error": "la clave nueva debe tener al menos %d caracteres" % LARGO_MINIMO_CLAVE,
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

        if ruta == "/usuarios":
            try:
                yo, error = self._admin()
                if error:
                    return self._responder(*error)
                if not isinstance(cuerpo, dict):
                    return self._responder(400, {"ok": False, "error": "faltan datos"})
                correo = str(cuerpo.get("correo", "")).strip().lower()
                nombre = str(cuerpo.get("nombre", "")).strip()
                rol = str(cuerpo.get("rol", "")).strip()
                clave = str(cuerpo.get("clave", ""))
                if not correo or "@" not in correo:
                    return self._responder(400, {"ok": False, "error": "correo invalido"})
                if not nombre:
                    return self._responder(400, {"ok": False, "error": "falta el nombre"})
                if rol not in ROLES_VALIDOS:
                    return self._responder(400, {"ok": False, "error": "rol invalido"})
                if len(clave) < LARGO_MINIMO_CLAVE:
                    return self._responder(400, {
                        "ok": False,
                        "error": "la clave debe tener al menos %d caracteres" % LARGO_MINIMO_CLAVE,
                    })
                base = obtener_base()
                if base.usuarios.find_one({"correo": correo}):
                    return self._responder(409, {"ok": False, "error": "ya existe un usuario con ese correo"})
                permisos = cuerpo.get("permisos")
                if not isinstance(permisos, dict):
                    permisos = {"editar": rol != "lector"}
                nuevo = {
                    "correo": correo,
                    "nombre": nombre,
                    "rol": rol,
                    "permisos": permisos,
                    "activo": True,
                    "clave": cifrar_clave(clave),
                    "creado": ahora(),
                    "creadoPor": yo.get("correo", ""),
                }
                res = base.usuarios.insert_one(nuevo)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(201, {"ok": True, "id": str(res.inserted_id)})

        if ruta.startswith("/usuarios/") and ruta.endswith("/clave"):
            try:
                yo, error = self._admin()
                if error:
                    return self._responder(*error)
                uid = _oid(ruta.split("/")[2])
                if uid is None:
                    return self._responder(400, {"ok": False, "error": "id invalido"})
                nueva = str((cuerpo or {}).get("nueva", ""))
                if len(nueva) < LARGO_MINIMO_CLAVE:
                    return self._responder(400, {
                        "ok": False,
                        "error": "la clave debe tener al menos %d caracteres" % LARGO_MINIMO_CLAVE,
                    })
                base = obtener_base()
                if not base.usuarios.find_one({"_id": uid}):
                    return self._responder(404, {"ok": False, "error": "usuario no encontrado"})
                base.usuarios.update_one(
                    {"_id": uid},
                    {"$set": {"clave": cifrar_clave(nueva), "claveCambiada": ahora()}},
                )
                # Cambiar la clave cierra las sesiones abiertas de esa persona,
                # salvo la propia si es uno mismo (para no auto-expulsarse).
                if uid == yo["_id"]:
                    base.sesiones.delete_many({"usuarioId": uid, "_id": {"$ne": self._testigo()}})
                else:
                    _cerrar_sesiones_de(base, uid)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {"ok": True})

        if ruta == "/clientes":
            try:
                yo, error = self._editor()
                if error:
                    return self._responder(*error)
                datos, error = _validar_cliente(cuerpo, con_cod=True)
                if error:
                    return self._responder(*error)
                base = obtener_base()
                if base.clientes.find_one({"cod": datos["cod"]}):
                    return self._responder(409, {
                        "ok": False,
                        "error": "ya existe un cliente con el codigo " + datos["cod"],
                    })
                datos["activo"] = True
                datos["creado"] = ahora()
                datos["creadoPor"] = yo.get("correo", "")
                datos["_mod"] = ahora_ms()
                res = base.clientes.insert_one(datos)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(201, {"ok": True, "id": str(res.inserted_id)})

        if ruta == "/clientes/importar":
            # Carga inicial: se corre UNA vez, para subir los clientes que
            # hoy viven dentro del index.html. Si ya hay clientes cargados
            # no hace nada: no es una via para pisar la lista viva.
            try:
                yo, error = self._admin()
                if error:
                    return self._responder(*error)
                lista = (cuerpo or {}).get("clientes")
                if not isinstance(lista, list) or not lista:
                    return self._responder(400, {"ok": False, "error": "falta la lista de clientes"})
                base = obtener_base()
                if base.clientes.count_documents({}) > 0:
                    return self._responder(409, {
                        "ok": False,
                        "error": "ya hay clientes cargados; la importacion es solo para la carga inicial",
                    })
                nuevos = []
                vistos = set()
                for cru in lista:
                    datos, error = _validar_cliente(cru, con_cod=True)
                    if error:
                        return self._responder(400, {
                            "ok": False,
                            "error": "cliente invalido en la lista: " + str(error[1].get("error", "")),
                        })
                    if datos["cod"] in vistos:
                        return self._responder(400, {
                            "ok": False,
                            "error": "codigo repetido en la lista: " + datos["cod"],
                        })
                    vistos.add(datos["cod"])
                    datos["activo"] = True
                    datos["creado"] = ahora()
                    datos["creadoPor"] = yo.get("correo", "")
                    nuevos.append(datos)
                base.clientes.insert_many(nuevos)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(201, {"ok": True, "importados": len(nuevos)})

        if ruta == "/estado/importar":
            # Carga inicial desde el volcado de Firebase. Se corre UNA vez.
            # Si ya hay estado cargado se niega: no es una via para pisar los
            # datos vivos por accidente. Para eso esta PUT /estado, que
            # fusiona.
            try:
                yo, error = self._admin()
                if error:
                    return self._responder(*error)
                entrante = estado_del_cuerpo(cuerpo)
                if entrante is None:
                    return self._responder(400, {
                        "ok": False,
                        "error": "falta el estado: se espera el archivo que exporta la app, "
                                 "o un objeto {\"estado\": {...}}",
                    })
                base = obtener_base()
                if base is None:
                    return self._responder(503, {"ok": False, "error": "base no disponible"})
                if base[COL_ESTADO].count_documents({}) > 0:
                    return self._responder(409, {
                        "ok": False,
                        "error": "ya hay estado cargado; la importacion es solo para la carga inicial",
                    })
                # Se guarda tal cual viene: es la foto de Firebase, no hay
                # nada contra que fusionar todavia. Lo unico que se hace es
                # aplicar las marcas de borrado que ya traiga, para no subir
                # registros que en Firebase estaban podados.
                marcas = entrante.get("_deletedMerge") or {}
                inicial = {k: entrante[k] for k in DATA_KEYS if k in entrante}
                inicial["_deletedMerge"] = unir_marcas(marcas, {})
                podar_borrados(inicial, inicial["_deletedMerge"])
                ts_nuevo = int(time.time() * 1000)
                with _candado_estado:
                    guardar_estado(inicial, {}, ts_nuevo)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(201, {
                "ok": True,
                "conteo": contar_estado(inicial),
                "total": sum(len(v) for v in inicial.values() if isinstance(v, list)),
                "_ts": ts_nuevo,
            })

        if ruta == "/auditoria":
            # Anota una accion. Alcanza con tener sesion: el que solo mira
            # tambien deja rastro cuando entra y cuando sale.
            try:
                usuario, error = self._sesion()
                if error:
                    return self._responder(*error)
                if not isinstance(cuerpo, dict) or not str(cuerpo.get("accion", "")).strip():
                    return self._responder(400, {"ok": False, "error": "falta la accion"})
                # El usuario y el rol NO se leen del cuerpo: los pone el
                # servidor a partir de la sesion. Si los mandara el navegador,
                # cualquiera podria firmar sus actos con el nombre de otro.
                if not anotar_auditoria(usuario, cuerpo.get("accion"), cuerpo.get("detalle")):
                    return self._responder(503, {"ok": False, "error": "base no disponible"})
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(201, {"ok": True})

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
                resultado, salteadas, desconocidas = restaurar_respaldo(archivo)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            if desconocidas:
                return self._responder(400, {
                    "ok": False,
                    "error": "el archivo nombra colecciones que no existen: " +
                             ", ".join(desconocidas) +
                             ". No se restauro nada. El estado del negocio va en "
                             "la coleccion \"" + COL_ESTADO + "\", un documento "
                             "por clave: [{\"_id\": \"brl\", \"v\": [...]}, ...]",
                    "conocidas": sorted(COLECCIONES_CONOCIDAS),
                })
            return self._responder(200, {
                "ok": True,
                "restaurado": resultado,
                "salteadas": salteadas,
            })

        return self._responder(404, {"ok": False, "error": "ruta no encontrada"})

    def do_PUT(self):
        ruta = self.path.split("?")[0].rstrip("/") or "/"
        cuerpo = self._leer_json(CUERPO_MAXIMO_ESTADO if ruta == "/estado" else CUERPO_MAXIMO)
        partes = [p for p in ruta.split("/") if p]

        if ruta == "/estado":
            # El dispositivo manda SU bloque; el servidor fusiona contra lo
            # guardado y devuelve el resultado. El dispositivo adopta lo que
            # vuelve: por eso deja de importar cual reloj va adelantado y
            # desaparece el "gana el ultimo que escribe" del PUT de Firebase.
            try:
                yo, error = self._editor()
                if error:
                    return self._responder(*error)
                if not isinstance(cuerpo, dict) or not isinstance(cuerpo.get("estado"), dict):
                    return self._responder(400, {"ok": False, "error": "falta el estado"})
                entrante = cuerpo["estado"]
                # PASO 3: el permiso decide QUE puede escribir, no solo si
                # puede escribir. Lo que esta persona no puede tocar se saca
                # del bloque antes de fusionar; fusionar_estado conserva tal
                # cual cualquier clave que no llegue, asi que no se borra
                # nada: lo que mando simplemente no cuenta.
                entrante, rechazadas = filtrar_por_permisos(entrante, yo)
                ts_entrante = cuerpo.get("_ts")
                if not _es_numero(ts_entrante):
                    ts_entrante = 0
                # Candado: dos telefonos guardando a la vez tienen que
                # fusionarse uno despues del otro, no pisarse.
                with _candado_estado:
                    guardado, ts_guardado = leer_estado()
                    if guardado is None:
                        return self._responder(503, {"ok": False, "error": "base no disponible"})
                    if not guardado:
                        # Candado: sin carga inicial, este PUT sembraria la base
                        # con el bloque del PRIMER dispositivo que guarde —que
                        # puede ser uno atrasado, o uno recien instalado con la
                        # mitad de los datos. La carga inicial se hace una vez,
                        # a proposito y verificando los conteos, por
                        # /estado/importar.
                        return self._responder(409, {
                            "ok": False,
                            "error": "todavia no se corrio la carga inicial; usa /estado/importar",
                        })
                    fusionado = fusionar_estado(entrante, guardado, ts_entrante, ts_guardado)
                    ts_nuevo = int(time.time() * 1000)
                    escritas = guardar_estado(fusionado, guardado, ts_nuevo)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            if rechazadas:
                # La auditoria del navegador es voluntaria —la escribe la app
                # si quiere—. Esta no: la escribe el servidor, que es el que
                # dijo no.
                try:
                    anotar_auditoria(yo, "escritura rechazada",
                                     "sin permiso para: " + ", ".join(rechazadas))
                except PyMongoError:
                    pass
            return self._responder(200, {
                "ok": True,
                "estado": fusionado,
                "_ts": ts_nuevo,
                "clavesEscritas": escritas,
                "clavesRechazadas": rechazadas,
            })

        if len(partes) == 2 and partes[0] == "usuarios":
            try:
                yo, error = self._admin()
                if error:
                    return self._responder(*error)
                uid = _oid(partes[1])
                if uid is None:
                    return self._responder(400, {"ok": False, "error": "id invalido"})
                if not isinstance(cuerpo, dict):
                    return self._responder(400, {"ok": False, "error": "faltan datos"})
                base = obtener_base()
                destino = base.usuarios.find_one({"_id": uid})
                if not destino:
                    return self._responder(404, {"ok": False, "error": "usuario no encontrado"})

                cambios = {}
                if "nombre" in cuerpo:
                    nombre = str(cuerpo["nombre"]).strip()
                    if not nombre:
                        return self._responder(400, {"ok": False, "error": "falta el nombre"})
                    cambios["nombre"] = nombre
                if "rol" in cuerpo:
                    rol = str(cuerpo["rol"]).strip()
                    if rol not in ROLES_VALIDOS:
                        return self._responder(400, {"ok": False, "error": "rol invalido"})
                    # Candado: nadie se quita a si mismo el rol de administrador.
                    if uid == yo["_id"] and rol != "admin":
                        return self._responder(400, {
                            "ok": False,
                            "error": "no puedes quitarte a ti mismo el rol de administrador",
                        })
                    cambios["rol"] = rol
                if "permisos" in cuerpo and isinstance(cuerpo["permisos"], dict):
                    cambios["permisos"] = cuerpo["permisos"]
                if "activo" in cuerpo:
                    activo = bool(cuerpo["activo"])
                    # Candado: nadie se desactiva a si mismo.
                    if uid == yo["_id"] and not activo:
                        return self._responder(400, {
                            "ok": False,
                            "error": "no puedes desactivarte a ti mismo",
                        })
                    cambios["activo"] = activo
                if not cambios:
                    return self._responder(400, {"ok": False, "error": "nada que cambiar"})

                # Candado: siempre tiene que quedar al menos un admin activo.
                deja_de_ser_admin = (
                    (cambios.get("rol", destino.get("rol")) != "admin")
                    or (cambios.get("activo", destino.get("activo", True)) is False)
                )
                if destino.get("rol") == "admin" and destino.get("activo", True) and deja_de_ser_admin:
                    if _admins_activos(base, excepto=uid) == 0:
                        return self._responder(400, {
                            "ok": False,
                            "error": "debe quedar al menos un administrador activo",
                        })

                cambios["modificado"] = ahora()
                base.usuarios.update_one({"_id": uid}, {"$set": cambios})
                # Desactivar o cambiar de rol cierra las sesiones abiertas.
                # Si el rol se "cambia" al que ya tenia, no se cierra nada:
                # guardar sin cambios no deberia echar a nadie.
                rol_cambio = "rol" in cambios and cambios["rol"] != destino.get("rol")
                if cambios.get("activo") is False or rol_cambio:
                    _cerrar_sesiones_de(base, uid)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {"ok": True, "cambios": list(cambios.keys())})

        if len(partes) == 2 and partes[0] == "clientes":
            try:
                yo, error = self._editor()
                if error:
                    return self._responder(*error)
                cid = _oid(partes[1])
                if cid is None:
                    return self._responder(400, {"ok": False, "error": "id invalido"})
                datos, error = _validar_cliente(cuerpo, con_cod=False)
                if error:
                    return self._responder(*error)
                base = obtener_base()
                if not base.clientes.find_one({"_id": cid}):
                    return self._responder(404, {"ok": False, "error": "cliente no encontrado"})
                # El "cod" no se toca aunque venga en el cuerpo: las remesas
                # ya guardadas apuntan a el.
                datos["modificado"] = ahora()
                datos["_mod"] = ahora_ms()
                base.clientes.update_one({"_id": cid}, {"$set": datos})
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {"ok": True})

        if len(partes) == 3 and partes[0] == "clientes" and partes[2] == "activo":
            try:
                yo, error = self._admin()
                if error:
                    return self._responder(*error)
                cid = _oid(partes[1])
                if cid is None:
                    return self._responder(400, {"ok": False, "error": "id invalido"})
                if not isinstance(cuerpo, dict) or "activo" not in cuerpo:
                    return self._responder(400, {"ok": False, "error": "falta el campo activo"})
                base = obtener_base()
                if not base.clientes.find_one({"_id": cid}):
                    return self._responder(404, {"ok": False, "error": "cliente no encontrado"})
                base.clientes.update_one(
                    {"_id": cid},
                    {"$set": {"activo": bool(cuerpo["activo"]),
                              "modificado": ahora(), "_mod": ahora_ms()}},
                )
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
            return self._responder(200, {"ok": True})

        return self._responder(404, {"ok": False, "error": "ruta no encontrada"})

    def handle_one_request(self):
        """Si un manejador revienta por algo que no es PyMongoError, el
        servidor base cierra la conexion sin decir nada y el navegador se
        queda esperando: no sabe si su guardado entro o no. Preferimos un 500
        con cuerpo JSON, que la app si sabe leer."""
        try:
            return BaseHTTPRequestHandler.handle_one_request(self)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            print("ERROR sin atrapar en %s: %r" % (self.path, e), flush=True)
            try:
                self._responder(500, {"ok": False, "error": "error interno del servidor"})
            except Exception:
                pass
            self.close_connection = True

    def log_message(self, formato, *args):
        print("%s - %s" % (self.address_string(), formato % args), flush=True)


if __name__ == "__main__":
    try:
        preparar_base()
    except Exception as e:
        # que la API arranque igual: asi /salud sigue sirviendo para
        # diagnosticar en vez de quedar todo caido sin explicacion
        print("AVISO: no se pudo preparar la base: %s" % type(e).__name__, flush=True)
    # ARREGLO 103: la copia diaria. Va despues de preparar_base() y antes de
    # escuchar: si la base no esta, el hilo lo dice y lo reintenta a la hora,
    # pero la API arranca igual.
    try:
        arrancar_respaldo_automatico()
        print("Respaldo automatico en marcha (una copia al dia)", flush=True)
    except Exception as e:
        print("AVISO: no arranco el respaldo automatico: %r" % e, flush=True)
    puerto = int(os.environ.get("PORT", "8080"))
    servidor = ThreadingHTTPServer(("0.0.0.0", puerto), Manejador)
    print("API escuchando en el puerto %d" % puerto, flush=True)
    servidor.serve_forever()
