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
os.environ.setdefault("VAPID_PUBLICA", "pub-de-mentira")
os.environ.setdefault("VAPID_PRIVADA", "priv-de-mentira")

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

print("── El alta del aparato")
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
guardada_pub = M.VAPID_PUBLICA
M.VAPID_PUBLICA = ""
ok(M.hacer_ronda_push(forzado=True) == 0, "sin las claves VAPID no se intenta siquiera")
M.VAPID_PUBLICA = guardada_pub

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
