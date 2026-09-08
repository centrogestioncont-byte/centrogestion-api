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
from urllib.parse import parse_qs, urlparse

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
]

RENUMERAR = ("brl", "vzla", "eeuu")
DIAS_MARCA_MS = 2592000000   # 30 dias, igual que _marcarBorradoMerge


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


def merge_config_safe(remota, local):
    if _falsy_js(remota):
        return local
    if _falsy_js(local):
        return remota
    salida = _asignar_js(local)
    for k in _claves_js(remota):
        rv = _leer_js(remota, k)
        lv = _leer_js(local, k)
        if isinstance(rv, dict):
            # typeof lv === "object" en JS es cierto tambien para arreglos y
            # para null; null ya cae en el `else` por ser falsy en la rama de
            # arriba de la recursion.
            salida[k] = merge_config_safe(rv, lv if isinstance(lv, (dict, list)) else {})
        elif lv is None or (isinstance(lv, str) and lv == "") or (_es_numero(lv) and lv == 0):
            # El JS pregunta con === por undefined, null, "" y 0, y por nada
            # mas. Un local en false NO entra aca: false es un valor puesto a
            # proposito (una comision exonerada, un modulo apagado) y no debe
            # dejarse pisar por lo remoto.
            salida[k] = rv
    return salida


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

    for k in DATA_KEYS:
        if k == "_deletedMerge":
            continue
        if k not in entrante:
            continue
        if k == "config":
            salida["config"] = merge_config_safe(entrante.get("config"), salida.get("config") or {})
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
                resultado, salteadas = restaurar_respaldo(archivo)
            except PyMongoError:
                return self._responder(503, {"ok": False, "error": "base no disponible"})
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
            return self._responder(200, {
                "ok": True,
                "estado": fusionado,
                "_ts": ts_nuevo,
                "clavesEscritas": escritas,
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
                    {"$set": {"activo": bool(cuerpo["activo"]), "modificado": ahora()}},
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
    puerto = int(os.environ.get("PORT", "8080"))
    servidor = ThreadingHTTPServer(("0.0.0.0", puerto), Manejador)
    print("API escuchando en el puerto %d" % puerto, flush=True)
    servidor.serve_forever()
