# -*- coding: utf-8 -*-
"""Pruebas del respaldo automatico (ARREGLO 103).

Como fusion.py: importa main.py y le pone una base de mentira delante. No
levanta el servidor ni toca Mongo, pero SI ejecuta hacer_respaldo() entero,
que es lo unico que prueba que la copia se hace, que poda y —sobre todo— que
NO se copia a si misma.

A mano:  python3 pruebas/respaldo.py
"""
import os
import sys
import types

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


# ── una Mongo de mentira, lo justo para lo que usa el respaldo ─────────────
class Cursor(list):
    """Lo que devuelve find() en pymongo: se puede recorrer Y ordenar."""
    def sort(self, campo, orden):
        return Cursor(sorted(self, key=lambda d: d.get(campo), reverse=(orden < 0)))


class Coleccion(object):
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, filtro=None, proy=None):
        salida = []
        for d in self.docs:
            c = dict(d)
            if proy:
                # solo se usan dos formas: {"_id":1} y {"datos":0}
                if proy.get("datos") == 0:
                    c.pop("datos", None)
                elif proy.get("_id") == 1:
                    c = {"_id": d.get("_id")}
            salida.append(c)
        return Cursor(salida)

    def find_one(self, filtro, proy=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in filtro.items()):
                return dict(d)
        return None

    def replace_one(self, filtro, doc, upsert=False):
        for i, d in enumerate(self.docs):
            if d.get("_id") == filtro.get("_id"):
                self.docs[i] = doc
                return
        if upsert:
            self.docs.append(doc)

    def delete_many(self, filtro):
        dentro = (filtro.get("_id") or {}).get("$in") or []
        self.docs = [d for d in self.docs if d.get("_id") not in dentro]

    def sort(self, campo, orden):
        return self


class Base(object):
    def __init__(self, cols):
        self.cols = {k: Coleccion(v) for k, v in cols.items()}

    def list_collection_names(self):
        return list(self.cols.keys())

    def __getitem__(self, n):
        self.cols.setdefault(n, Coleccion())
        return self.cols[n]

    def __getattr__(self, n):
        return self[n]


def montar(extra=None):
    cols = {
        "estado": [{"_id": "brl", "v": [{"cl": "ANA"}, {"cl": "LUIS"}]},
                   {"_id": "config", "v": {"pins": {"ana": "1234"}, "modulos": {"x": 1}, "aperturaUsdt": 2544.79}}],
        "clientes": [{"cod": "C1", "n": "ANA"}],
        "usuarios": [{"correo": "a@b.c", "clave": "scrypt$x$y", "rol": "admin"}],
        "sesiones": [{"_id": "tok", "usuarioId": 1}],
        "auditoria": [{"ts": 1, "accion": "ENTRO"}],
    }
    cols.update(extra or {})
    b = Base(cols)
    main.obtener_base = lambda: b
    return b


print("\n── ARREGLO 103: el respaldo automatico ──")

# 1. Hace la copia del dia, y solo una.
b = montar()
c1 = main.hacer_respaldo()
ok(c1 is not None, "la primera vez hace la copia", c1)
ok(len(b["respaldos"].docs) == 1, "y queda guardada", len(b["respaldos"].docs))
c2 = main.hacer_respaldo()
ok(c2 is None, "la segunda del mismo dia NO repite")
ok(len(b["respaldos"].docs) == 1, "y no deja una copia de mas")

# 2. LO QUE MAS IMPORTA: la copia no se copia a si misma. Sin esto, la de
#    mañana llevaria dentro la de hoy, y en una semana la base no cabe.
copia = b["respaldos"].docs[0]
ok("respaldos" not in (copia.get("datos") or {}),
   "la copia NO contiene las copias anteriores",
   list((copia.get("datos") or {}).keys()))
ok("sesiones" not in (copia.get("datos") or {}),
   "ni las sesiones abiertas")

# 3. Ni las claves. Un respaldo se baja al telefono y se sube a la nube.
us = (copia.get("datos") or {}).get("usuarios") or [{}]
ok("clave" not in us[0], "ni la clave de nadie", us[0])
cfg = [d for d in ((copia.get("datos") or {}).get("estado") or []) if d.get("_id") == "config"]
ok(bool(cfg) and "pins" not in (cfg[0].get("v") or {}) and "modulos" not in (cfg[0].get("v") or {}),
   "ni los PIN ni los permisos por rol que quedaran en config", cfg)

# 4. Y SI se lleva los datos del negocio, que es para lo que esta.
est = (copia.get("datos") or {}).get("estado") or []
brl = [d for d in est if d.get("_id") == "brl"]
ok(bool(brl) and len(brl[0].get("v") or []) == 2, "las remesas sí van dentro", brl)
ok(bool(cfg) and (cfg[0].get("v") or {}).get("aperturaUsdt") == 2544.79,
   "y la apertura, que es un dato del negocio, tambien", cfg)

# 5. Poda: se guardan las ultimas, no todas.
viejas = [{"_id": "2026-01-%02d" % d, "ts": d, "datos": {}} for d in range(1, 21)]
b = montar({"respaldos": viejas})
main.hacer_respaldo()
ok(len(b["respaldos"].docs) == main.RESPALDOS_QUE_SE_GUARDAN,
   "se podan las viejas y quedan %d" % main.RESPALDOS_QUE_SE_GUARDAN,
   len(b["respaldos"].docs))
ok(main._clave_dia() in [d["_id"] for d in b["respaldos"].docs],
   "y la que queda incluye la de hoy")

# 6. Lo que la app lee para enseñarlo.
e = main.estado_respaldos()
ok(e.get("ok") and e.get("copias") == main.RESPALDOS_QUE_SE_GUARDAN,
   "estado_respaldos dice cuantas hay", e)
ok((e.get("ultima") or {}).get("dia") == main._clave_dia(),
   "y de cuando es la ultima", e.get("ultima"))
ok("datos" not in str(e)[:2000],
   "sin arrastrar los datos: es una linea de estado, no el respaldo entero")

# 7. Sin base no revienta: la API tiene que seguir contestando.
main.obtener_base = lambda: None
ok(main.hacer_respaldo() is None, "sin base devuelve None en vez de reventar")
ok(main.estado_respaldos().get("ok") is False, "y el estado lo dice")

# 8. El candado. Esto no se puede ver desde fuera llamando a la funcion —sale
#    igual con candado y sin el— asi que se mira el codigo: armar_respaldo()
#    tiene que ir DENTRO de _candado_estado. Sin eso, una copia tomada en
#    mitad de un guardado se lleva un estado a medias (las remesas nuevas con
#    los saldos viejos), y es justo esa copia la que se restauraria.
import inspect  # noqa: E402
_src = inspect.getsource(main.hacer_respaldo)
_i = _src.find("with _candado_estado")
_j = _src.find("armar_respaldo()")
ok(_i > -1 and _j > _i and (_j - _i) < 120,
   "la copia se toma bajo el candado del estado, no a medio guardar")

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
