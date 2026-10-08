# -*- coding: utf-8 -*-
"""El camino de VERDAD del paso 3: servidor real, PUT real, operador real.

Una guardia estructural comprueba que el codigo dice lo que debe decir, NO
que el camino funcione. Eso ya costo caro (ARREGLO 91: el 89 se probo con
guardias y dejo el boton del aviso llevando al mes equivocado). Asi que esto
levanta el ThreadingHTTPServer de main.py con una Mongo de mentira detras y
hace peticiones HTTP de verdad.

Lo que reproduce, y es el agujero que cerro el paso 3: antes de esto, un
operador con permiso de registrar remesas mandaba un bloque con los
prestamos en cero y el servidor los guardaba. Medido quitando el filtro:
el prestamo quedaba en {"monto": 0, "cliente": "BORRADO"} y el egreso en
99999.

A mano:  python3 pruebas/permisos_e2e.py
"""
import sys, os, types, json, threading, urllib.request, urllib.error, time
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

f = types.ModuleType("pymongo"); f.ASCENDING = 1; f.MongoClient = object
class _RO(object):
    def __init__(self, filtro, doc, upsert=False):
        self.filtro, self.doc = filtro, doc
f.ReplaceOne = _RO
e = types.ModuleType("pymongo.errors"); e.PyMongoError = Exception; f.errors = e
sys.modules["pymongo"] = f; sys.modules["pymongo.errors"] = e
fb = types.ModuleType("bson"); fb.ObjectId = str
eb = types.ModuleType("bson.errors"); eb.InvalidId = Exception; fb.errors = eb
sys.modules["bson"] = fb; sys.modules["bson.errors"] = eb

import main
main.Manejador.log_message = lambda *a, **k: None

class Col(object):
    def __init__(s, docs=None): s.docs = list(docs or [])
    def find_one(s, q=None):
        for d in s.docs:
            if all(d.get(k) == v for k, v in (q or {}).items()): return dict(d)
        return None
    def find(s, q=None, proy=None): return list(dict(d) for d in s.docs)
    def update_one(s, q, upd, upsert=False):
        for d in s.docs:
            if all(d.get(k) == v for k, v in (q or {}).items()):
                d.update(upd.get("$set") or {}); return
        if upsert: s.docs.append(dict(q, **(upd.get("$set") or {})))
    def insert_one(s, d): s.docs.append(dict(d))
    def delete_one(s, q): pass
    def delete_many(s, q): pass
    def create_index(s, *a, **k): pass
    def bulk_write(s, ops, ordered=True):
        for op in ops:
            k = op.filtro.get("_id")
            s.docs = [d for d in s.docs if d.get("_id") != k]
            s.docs.append(dict(op.doc))
    def count_documents(s, q=None): return len(s.docs)

VENCE = datetime.now(timezone.utc) + timedelta(days=7)
class Base(object):
    def __init__(s):
        s.c = {
            "usuarios": Col([
                {"_id": "u-ad", "correo": "ella@x.com", "nombre": "Ella",
                 "rol": "admin", "activo": True, "permisos": {"editar": True}},
                {"_id": "u-op", "correo": "ope@x.com", "nombre": "Operador",
                 "rol": "brl", "activo": True, "permisos": {"editar": True}},
                {"_id": "u-op2", "correo": "ope2@x.com", "nombre": "Con egresos",
                 "rol": "brl", "activo": True,
                 "permisos": {"editar": True, "egresos": True}},
            ]),
            "sesiones": Col([
                {"_id": "T-AD", "usuarioId": "u-ad", "vence": VENCE},
                {"_id": "T-OP", "usuarioId": "u-op", "vence": VENCE},
                {"_id": "T-OP2", "usuarioId": "u-op2", "vence": VENCE},
            ]),
            "estado": Col([
                {"_id": "prestamos", "v": [{"id": 9, "monto": 100, "cliente": "SUYO"}]},
                {"_id": "egresos", "v": [{"id": 8, "motivo": "LUZ", "monto": 50}]},
                {"_id": "config", "v": {"pct_sueldo": 30, "aperturaUsdt": 2544.79}},
                {"_id": "cuentas", "v": [{"id": "c1", "nombre": "BDV", "saldo": 1000}]},
                {"_id": "brl", "v": []},
                {"_id": "__meta", "ts": 1000},
            ]),
            "auditoria": Col(), "respaldos": Col(),
        }
    def __getitem__(s, n): return s.c.setdefault(n, Col())
    def __getattr__(s, n): return s.c.setdefault(n, Col())
    def list_collection_names(s): return list(s.c)

BASE = Base()
main.obtener_base = lambda: BASE
main.ORIGENES_OK = ["*"]

# Puerto 0: lo elige el sistema. Uno fijo choca con lo que haya levantado
# y la prueba fallaria por una razon que no tiene nada que ver.
srv = main.ThreadingHTTPServer(("127.0.0.1", 0), main.Manejador)
PUERTO = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)

def put(testigo, estado, ts=2000):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/estado" % PUERTO,
        data=json.dumps({"estado": estado, "_ts": ts}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + testigo},
        method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        return ex.code, json.loads(ex.read().decode())

def get(testigo):
    req = urllib.request.Request("http://127.0.0.1:%d/estado" % PUERTO,
                                 headers={"Authorization": "Bearer " + testigo})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

fallos = []
def ok(c, m, x=""):
    print(("  ok   " if c else "  FALLA ") + m + ("" if c else "  -> " + str(x)))
    if not c: fallos.append(m)

# Cada registro cambiado lleva su marca _mod, que es lo que manda la app de
# verdad (_marcarTodoLoQueSeFusiona) y lo que decide la fusion. Sin marca, el
# reloj del bloque es lo unico que queda y el resultado depende de en que
# orden corrieron las pruebas — que es como se colo un falso verde aqui.
AHORA = 9999999999999
VENENO = {
    "brl": [{"_uid": "r1", "cliente": "JUAN", "pr": 1.5, "_mod": AHORA}],
    "cuentas": [{"id": "c1", "nombre": "BDV", "saldo": 900, "_mod": AHORA}],
    "prestamos": [{"id": 9, "monto": 0, "cliente": "BORRADO", "_mod": AHORA}],
    "egresos": [{"id": 8, "motivo": "ROBADO", "monto": 99999, "_mod": AHORA}],
    "config": {"pct_sueldo": 99, "aperturaUsdt": 0.01},
    "_modCampos": {"config": {"pct_sueldo": AHORA, "aperturaUsdt": AHORA}},
}

# EL CASO DE VERDAD, el que se vio con su primer operador el 08/10: la app
# manda el bloque ENTERO en cada guardado, con las 36 claves, haya cambiado
# algo o no. Rechazando por "esta la clave en el bloque" salian las 14
# guardadas de golpe y el operador veia un aviso de 14 lineas sin haber
# intentado tocar nada.
print("== El operador manda el bloque entero sin tocar nada ==")
EL_ESTADO_TAL_CUAL = get("T-OP")["estado"]
cod, d = put("T-OP", EL_ESTADO_TAL_CUAL, ts=1500)
ok(cod == 200, "guarda bien", cod)
ok((d.get("clavesRechazadas") or []) == [],
   "y NO se le avisa de nada: no intento cambiar nada",
   d.get("clavesRechazadas"))

print("\n== El operador guarda su remesa y, de paso, intenta tocar todo ==")
cod, d = put("T-OP", VENENO)
ok(cod == 200, "el guardado sale bien (su remesa SI tiene que entrar)", cod)
ok(sorted(d.get("clavesRechazadas") or []) == ["config", "egresos", "prestamos"],
   "el servidor rechaza prestamos, egresos y config", d.get("clavesRechazadas"))

est = get("T-AD")["estado"]
ok(est["prestamos"][0]["cliente"] == "SUYO" and est["prestamos"][0]["monto"] == 100,
   "el prestamo sigue intacto", est["prestamos"])
ok(est["egresos"][0]["motivo"] == "LUZ" and est["egresos"][0]["monto"] == 50,
   "el egreso sigue intacto", est["egresos"])
ok(est["config"]["pct_sueldo"] == 30 and est["config"]["aperturaUsdt"] == 2544.79,
   "la configuracion y la apertura siguen intactas", est["config"])
ok(len(est["brl"]) == 1 and est["brl"][0]["cliente"] == "JUAN",
   "pero SU REMESA si entro", est["brl"])
ok(est["cuentas"][0]["saldo"] == 900,
   "y el saldo de la cuenta bajo, que es lo que hace una remesa", est["cuentas"])

aud = [a for a in BASE.c["auditoria"].docs if a.get("accion") == "escritura rechazada"]
ok(len(aud) == 1, "quedo anotado en la auditoria del servidor", len(aud))
ok(aud and "prestamos" in aud[0].get("detalle", ""),
   "y el detalle dice que claves fueron", aud[0].get("detalle") if aud else "")
ok(aud and aud[0].get("usuario") == "Operador", "con el nombre de quien fue",
   aud[0].get("usuario") if aud else "")

print("\n== Al que SI tiene el permiso de egresos, los egresos le entran ==")
cod, d = put("T-OP2", {"egresos": [{"id": 8, "motivo": "LUZ DE VERDAD", "monto": 55,
                                    "_mod": 9999999999999}]}, ts=3000)
ok(cod == 200, "guarda bien", cod)
ok((d.get("clavesRechazadas") or []) == [], "sin rechazos", d.get("clavesRechazadas"))
est = get("T-AD")["estado"]
ok(est["egresos"][0]["motivo"] == "LUZ DE VERDAD", "y el egreso cambio",
   est["egresos"])

print("\n== A ella no se le toca nada ==")
cod, d = put("T-AD", {"prestamos": [{"id": 9, "monto": 777, "cliente": "SUYO",
                                     "_mod": 9999999999999}]}, ts=4000)
ok(cod == 200 and (d.get("clavesRechazadas") or []) == [],
   "la administradora no tiene rechazos", d.get("clavesRechazadas"))
est = get("T-AD")["estado"]
ok(est["prestamos"][0]["monto"] == 777, "y su cambio SI entra", est["prestamos"])

print("\n== Las tres claves que se tiraban ahora se guardan ==")
cod, d = put("T-AD", {"histApertura": {"2026-10-08T10:00:00": {"monto": 1}},
                      "histComp": {"2026-10-08": {"retorna": 170}},
                      "mapaBinance": {"abc": {"cuentaId": "c1"}}}, ts=5000)
est = get("T-AD")["estado"]
for k in ("histApertura", "histComp", "mapaBinance"):
    ok(k in est and est[k], "'%s' quedo guardada" % k, est.get(k))

srv.shutdown()
print("\n" + ("FALLARON %d" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
