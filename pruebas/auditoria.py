# -*- coding: utf-8 -*-
"""Pruebas de "que ha hecho cada operador" (/auditoria/resumen).

A mano:  python3 pruebas/auditoria.py
"""
import os
import sys
import types
import time
import inspect

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

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

import main  # noqa: E402

fallos = []


def ok(cond, msg, extra=""):
    print(("  ok   " if cond else "  FALLA ") + msg + ("" if cond else "  -> " + str(extra)))
    if not cond:
        fallos.append(msg)


AHORA = int(time.time() * 1000)
DIA = 24 * 60 * 60 * 1000


class Cursor(list):
    def limit(self, n):
        return Cursor(self[:n])

    def sort(self, campo, orden):
        return Cursor(sorted(self, key=lambda d: d.get(campo) or 0, reverse=(orden < 0)))


class Col(object):
    def __init__(s, docs=None):
        s.docs = list(docs or [])

    def find(s, filtro=None, proy=None):
        f = filtro or {}
        out = []
        for d in s.docs:
            if "ts" in f and isinstance(f["ts"], dict):
                if d.get("ts", 0) < f["ts"].get("$gte", 0):
                    continue
            if "usuario" in f and d.get("usuario") != f["usuario"]:
                continue
            if "role" in f and d.get("role") != f["role"]:
                continue
            out.append({k: v for k, v in d.items() if k != "_id"})
        return Cursor(out)


ENTRADAS = [
    {"ts": AHORA - 1 * DIA, "usuario": "Carlos", "role": "brl", "accion": "REMESA", "fecha": "07/10"},
    {"ts": AHORA - 1 * DIA, "usuario": "Carlos", "role": "brl", "accion": "REMESA EE.UU", "fecha": "07/10"},
    {"ts": AHORA - 2 * DIA, "usuario": "Carlos", "role": "brl", "accion": "EGRESO", "fecha": "06/10"},
    {"ts": AHORA - 3 * DIA, "usuario": "Carlos", "role": "brl", "accion": "BORRAR REMESA", "fecha": "05/10"},
    {"ts": AHORA - 1 * DIA, "usuario": "Jhoselin", "role": "admin", "accion": "TRASPASO", "fecha": "07/10"},
    # Vieja: fuera de la ventana de 30 dias.
    {"ts": AHORA - 90 * DIA, "usuario": "Carlos", "role": "brl", "accion": "REMESA", "fecha": "10/07"},
]


class Base(object):
    def __init__(s):
        s.c = {main.COL_AUDITORIA: Col(ENTRADAS)}

    def __getitem__(s, n):
        return s.c.setdefault(n, Col())


main.obtener_base = lambda: Base()

print("== 1. Agrupa por persona ==")
r = main.resumen_auditoria()
porNombre = {p["usuario"]: p for p in r["personas"]}
ok(sorted(porNombre) == ["Carlos", "Jhoselin"], "sale cada persona una vez",
   sorted(porNombre))
ok(porNombre["Carlos"]["total"] == 4,
   "y solo lo de la ventana: la de hace 90 dias no cuenta",
   porNombre["Carlos"]["total"])
ok(porNombre["Jhoselin"]["total"] == 1, "la otra persona tambien")
ok(r["personas"][0]["usuario"] == "Carlos",
   "ordenado de mas activo a menos", [p["usuario"] for p in r["personas"]])

print("\n== 2. Cuenta por tipo de accion ==")
acc = porNombre["Carlos"]["acciones"]
ok(acc.get("REMESA") == 2,
   "'REMESA' y 'REMESA EE.UU' cuentan juntas: para contar son lo mismo", acc)
ok(acc.get("EGRESO") == 1, "los egresos aparte", acc)
ok(acc.get("BORRAR") == 1,
   "y los borrados aparte, que es lo que de verdad hay que poder ver", acc)

print("\n== 3. La ultima vez ==")
ok(porNombre["Carlos"]["ultima"] == "07/10",
   "la fecha de la mas reciente, no la de la primera que se leyo",
   porNombre["Carlos"]["ultima"])
ok(porNombre["Carlos"]["role"] == "brl", "y su rol")

print("\n== 4. La ventana se puede mover, y no se va de madre ==")
ok(main.resumen_auditoria(365)["personas"][0]["total"] == 5,
   "a 365 dias entra tambien la vieja")
ok(main.resumen_auditoria(0)["dias"] == 1, "0 dias se sube a 1")
ok(main.resumen_auditoria(99999)["dias"] == 365, "y no se pasa de un año")
ok(main.resumen_auditoria("no soy un numero")["dias"] == main.DIAS_RESUMEN,
   "un valor raro cae en el por omision")

print("\n== 5. Un resumen cortado lo dice ==")
# Un numero cortado que parece completo es peor que no darlo.
fuente = inspect.getsource(main.resumen_auditoria)
ok('"cortado"' in fuente, "el resumen dice si llego al tope")
ok("MAX_RESUMEN" in fuente, "y hay un tope")
ok(main.resumen_auditoria()["cortado"] is False,
   "con pocos registros no esta cortado")

print("\n== 6. Filtrar la lista por PERSONA, no solo por rol ==")
ok("usuario" in inspect.signature(main.listar_auditoria).parameters,
   "listar_auditoria acepta la persona")
solo = main.listar_auditoria(usuario="Jhoselin")
ok(len(solo) == 1 and solo[0]["usuario"] == "Jhoselin",
   "y filtra de verdad", solo)
ok(len(main.listar_auditoria()) == len(ENTRADAS),
   "sin filtro salen todas")

print("\n== 7. Esta enchufado ==")
_src = ""
for _n in dir(main):
    _o = getattr(main, _n)
    if inspect.isclass(_o) and hasattr(_o, "do_GET"):
        _src = inspect.getsource(_o.do_GET)
        break
ok('ruta == "/auditoria/resumen"' in _src, "la ruta existe")
ok("resumen_auditoria(" in _src, "y llama al resumen")
ok('consulta.get("usuario")' in _src, "y /auditoria acepta filtrar por persona")

print("\n== 8. Sin base no revienta ==")
main.obtener_base = lambda: None
ok(main.resumen_auditoria() is None, "devuelve None en vez de reventar")
ok(main.listar_auditoria() is None, "y la lista tambien")

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
