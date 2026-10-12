# -*- coding: utf-8 -*-
"""ARREGLO 114 · la notificacion al telefono.

No se puede probar la ENTREGA desde aqui: hace falta un servicio de push de
verdad y la red del entorno esta cerrada. Lo que si se prueba es todo lo que
decide SI se manda, A QUIEN y CUANTAS veces — que es donde estan los errores
que cuestan: sonar todos los dias, sonar de madrugada, o mandar un numero de
hace tres dias. El envio se sustituye por una funcion que anota lo que
recibe, que es el mismo camino del ARREGLO 84 con html2pdf.
"""
import os, sys, json
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
# A proposito SIN variables de entorno: lo primero que se prueba es que el
# servidor se hace las claves solo. Ella trabaja desde el telefono y un
# arreglo que depende de que consiga una PC es un arreglo que no llega.

import types
if "pymongo" not in sys.modules:
    falso = types.ModuleType("pymongo")
    falso.ASCENDING = 1
    falso.MongoClient = object
    falso.ReplaceOne = object
    errores = types.ModuleType("pymongo.errors")
    errores.PyMongoError = Exception
    falso.errors = errores
    sys.modules["pymongo"] = falso
    sys.modules["pymongo.errors"] = errores
if "bson" not in sys.modules:
    fb = types.ModuleType("bson")
    fb.ObjectId = str
    eb = types.ModuleType("bson.errors")
    eb.InvalidId = Exception
    fb.errors = eb
    sys.modules["bson"] = fb
    sys.modules["bson.errors"] = eb

import main as M

fallos = []
def ok(cond, titulo, detalle=""):
    print(("  ok   " if cond else "  FALLA ") + titulo + (("  -> " + str(detalle)) if detalle and not cond else ""))
    if not cond:
        fallos.append(titulo)

# ── Una base de mentira, con lo justo ──
class Col:
    def __init__(s, docs=None): s.docs = docs or {}
    def find_one(s, q, proj=None): return s.docs.get(q.get("_id"))
    def find(s, q=None): return [dict(d) for d in s.docs.values()]
    def replace_one(s, q, doc, upsert=False): s.docs[q["_id"]] = dict(doc)
    def delete_one(s, q): s.docs.pop(q.get("_id"), None)
    def update_one(s, q, upd):
        d = s.docs.get(q["_id"])
        if d: d.update(upd.get("$set", {}))
class Base:
    def __init__(s): s.c = {}
    def __getitem__(s, n): return s.c.setdefault(n, Col())

BASE = Base()
M.obtener_base = lambda: BASE

MANDADOS = []
def _falso_enviar(sus, carga):
    MANDADOS.append({"a": sus["_id"], "carga": dict(carga)})
    return True, False
M._enviar_uno = _falso_enviar

def poner_aviso(clave, titulo="Te llenaste de bolivares", horas=0):
    ts = (M.ahora().timestamp() - horas * 3600) * 1000
    BASE[M.COL_ESTADO].docs["avisoPush"] = {
        "_id": "avisoPush",
        "v": {"clave": clave, "titulo": titulo, "texto": "El 32,78% esta en bolivares.",
              "nivel": "ambar", "ts": ts}}

def suscribir(punto):
    M.guardar_suscripcion({"correo": "ella@ejemplo.com"},
        {"endpoint": punto, "keys": {"p256dh": "pp", "auth": "aa"}})

print("── Las claves se las hace el servidor solo")
# cryptography hace falta para generarlas. En el CI se instala, asi que esto
# se ejecuta de verdad; en un entorno sin ella se dice y se sigue, en vez de
# fingir que paso. Un "ok" que no ejecuto nada es peor que un salto dicho.
try:
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    _ec.generate_private_key(_ec.SECP256R1())
    HAY_CRYPTO = True
except BaseException as _e:
    # BaseException y no Exception: cuando las bindings de Rust de
    # cryptography no cargan, lo que sale es un pyo3_runtime.PanicException,
    # que NO es una Exception. Con el except estrecho, esto reventaba.
    HAY_CRYPTO = False
    print("  SALTADO  la generacion de claves: cryptography no se puede usar aqui (%s)" % type(_e).__name__)
    # En el CI esto NO se puede saltar. El workflow pone PUSH_EXIGE_CRYPTO=1
    # justo despues de instalar cryptography: sin esta linea, un salto salia
    # en verde igual que una prueba pasada, asi que el "success" del CI no
    # probaba que la generacion se hubiera ejecutado nunca. Es el mismo
    # agujero que una guardia que pasa con el fallo puesto, un piso mas
    # arriba.
    if os.environ.get("PUSH_EXIGE_CRYPTO") == "1":
        print("\nFALLA: aqui cryptography TIENE que funcionar y no funciona.")
        sys.exit(1)

ok(not M.VAPID_PUBLICA and not M.VAPID_PRIVADA, "al arrancar no hay ninguna")
if HAY_CRYPTO:
    ok(M.asegurar_vapid(), "y se generan a la primera")
if HAY_CRYPTO:
  pub1, priv1 = M.VAPID_PUBLICA, M.VAPID_PRIVADA
  # La publica va cruda de 65 bytes, que es lo que el navegador espera en
  # applicationServerKey: en base64url son 87 caracteres y empieza por "B".
  import base64 as _b64
  crudo = _b64.urlsafe_b64decode(pub1 + "=" * ((4 - len(pub1) % 4) % 4))
  ok(len(crudo) == 65 and crudo[0] == 4,
     "la publica son los 65 bytes crudos que pide el navegador", len(crudo))
  ok("BEGIN PRIVATE KEY" in priv1, "y la privada va en PEM, que es lo que lee pywebpush")
  # Dos llamadas no pueden dar dos pares: el aparato quedaria suscrito a una
  # clave con la que el servidor ya no firma, y no le llegaria nada sin un solo
  # error. Se simula el reinicio: se olvidan las de memoria y se vuelven a pedir.
  M.VAPID_PUBLICA = ""; M.VAPID_PRIVADA = ""
  M.asegurar_vapid()
  ok(M.VAPID_PUBLICA == pub1 and M.VAPID_PRIVADA == priv1,
     "tras un reinicio se leen de la base, NO se hacen otras")
ok("claves" in M.COLECCIONES_FUERA, "y la clave privada no viaja en el respaldo")
if not HAY_CRYPTO:
    # El resto de las pruebas no generan nada: solo necesitan que
    # push_configurado() diga que si.
    M.VAPID_PUBLICA, M.VAPID_PRIVADA = "pub-de-mentira", "priv-de-mentira"

print("\n── El alta del aparato")
ok(M.guardar_suscripcion({"correo":"x"}, {"endpoint":"https://fcm.test/1","keys":{"p256dh":"p","auth":"a"}}),
   "una suscripcion completa se guarda")
ok(not M.guardar_suscripcion({"correo":"x"}, {"endpoint":"https://fcm.test/2"}),
   "sin las claves del aparato no se guarda: no se podria cifrar")
ok(not M.guardar_suscripcion({"correo":"x"}, {"endpoint":"http://fcm.test/3","keys":{"p256dh":"p","auth":"a"}}),
   "ni con un endpoint que no sea https")
# El endpoint ES el identificador: volver a suscribir el mismo telefono
# actualiza en vez de duplicar, o sonaria dos veces por aviso.
M.guardar_suscripcion({"correo":"x"}, {"endpoint":"https://fcm.test/1","keys":{"p256dh":"p2","auth":"a2"}})
ok(len(BASE[M.COL_PUSH].docs) == 1, "re-suscribir el mismo aparato no lo duplica",
   len(BASE[M.COL_PUSH].docs))
BASE.c[M.COL_PUSH] = Col()

print("\n── Cuando NO se manda")
suscribir("https://fcm.test/tel")
MANDADOS.clear()
ok(M.hacer_ronda_push(forzado=True) == 0, "sin aviso guardado no se manda nada")
poner_aviso("conc_ves_35", horas=72)
ok(M.hacer_ronda_push(forzado=True) == 0,
   "un aviso de hace 72 h esta vencido: la tasa de hoy ya es otra")
poner_aviso("conc_ves_35", horas=0)
# De madrugada no. Una alarma que la despierta se apaga para siempre.
noche = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)   # 03:00 en Caracas
ok(not M.es_hora_de_molestar(noche), "a las 3 de la madrugada no se molesta")
dia = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)    # 14:00 en Caracas
ok(M.es_hora_de_molestar(dia), "a las 2 de la tarde si")
guardadas = (M.VAPID_PUBLICA, M.VAPID_PRIVADA, M.push_configurado)
M.push_configurado = lambda: False
ok(M.hacer_ronda_push(forzado=True) == 0, "sin claves VAPID no se intenta siquiera")
M.VAPID_PUBLICA, M.VAPID_PRIVADA, M.push_configurado = guardadas

print("\n── Cuando SI, y UNA sola vez")
MANDADOS.clear()
ok(M.hacer_ronda_push(forzado=True) == 1, "con un aviso fresco, sale")
ok(MANDADOS and MANDADOS[0]["carga"]["titulo"] == "Te llenaste de bolivares" and
   "32,78" in MANDADOS[0]["carga"]["texto"],
   "y lleva el titulo y el texto dentro: va cifrado de extremo a extremo")
MANDADOS.clear()
ok(M.hacer_ronda_push(forzado=True) == 0,
   "la hora siguiente, con el MISMO aviso, no vuelve a sonar")
# La clave lleva los numeros redondeados: solo un cambio de verdad suena.
poner_aviso("conc_ves_45")
ok(M.hacer_ronda_push(forzado=True) == 1, "pero si la situacion cambia, suena otra vez")

print("\n── Cada aparato va por su cuenta")
suscribir("https://fcm.test/pc")
MANDADOS.clear()
n = M.hacer_ronda_push(forzado=True)
ok(n == 1 and MANDADOS[0]["a"] == "https://fcm.test/pc",
   "un aparato recien suscrito recibe el aviso que el otro ya tenia", n)

print("\n── Un aparato que ya no existe se borra")
def _muerto(sus, carga): return False, True
M._enviar_uno = _muerto
poner_aviso("otra_cosa_1")
M.hacer_ronda_push(forzado=True)
ok(len(BASE[M.COL_PUSH].docs) == 0,
   "si el navegador contesta 410, la suscripcion se olvida en vez de reintentar cada hora",
   len(BASE[M.COL_PUSH].docs))
M._enviar_uno = _falso_enviar

print("\n── ARREGLO 115 · el aviso que sale AL MOMENTO")
# Sustituye a ntfy. Va por su propia puerta y no por la ronda de la hora,
# porque su valor es que llegue YA: sirve para que el OTRO aparato se entere
# de lo que acaba de registrar este.
BASE.c[M.COL_PUSH] = Col()
suscribir("https://fcm.test/tel")
suscribir("https://fcm.test/pc")
MANDADOS.clear()
n = M.mandar_aviso_ahora({"titulo": "Remesa registrada", "texto": "BRL\u2194VES \u00b7 $0,59 USDT"})
ok(n == 2 and len(MANDADOS) == 2, "sale a los dos aparatos", n)
# Excluir al que lo manda no es un detalle: recibir en la misma pantalla la
# notificacion de lo que acabas de escribir es ruido.
MANDADOS.clear()
n = M.mandar_aviso_ahora({"titulo": "Remesa registrada", "texto": "x"}, "https://fcm.test/tel")
ok(n == 1 and MANDADOS[0]["a"] == "https://fcm.test/pc",
   "y NO le vuelve al aparato que la registro", [m["a"] for m in MANDADOS])
# Sin hora de silencio, a diferencia de la ronda: esto es el eco de algo que
# alguien acaba de hacer, no una alarma que suena sola de madrugada.
import inspect as _i
ok("es_hora_de_molestar" not in _i.getsource(M.mandar_aviso_ahora),
   "no mira la hora: es el eco de algo que acaban de hacer, no una alarma")
# Y la clave NO se apunta: dos remesas seguidas son dos avisos, no uno.
ok("ultimaClave" not in _i.getsource(M.mandar_aviso_ahora),
   "ni se apunta la clave: dos remesas seguidas son dos avisos");
MANDADOS.clear()
guardadas2 = M.push_configurado
M.push_configurado = lambda: False
ok(M.mandar_aviso_ahora({"titulo": "x"}) == 0, "y sin claves VAPID no manda nada")
M.push_configurado = guardadas2

print("\n── Lo que no puede viajar en el respaldo")
ok("push_suscripciones" in M.COLECCIONES_FUERA,
   "las suscripciones quedan FUERA del respaldo: son credenciales de un aparato")

print("\n── El servidor no calcula el aviso, lo reparte")
import inspect
fuente = inspect.getsource(M.hacer_ronda_push) + inspect.getsource(M._aviso_guardado)
ok("equilibrio" not in fuente and "suelo" not in fuente.replace("# ", ""),
   "hacer_ronda_push no recalcula el suelo: lee lo que la app dejo escrito")

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
