# -*- coding: utf-8 -*-
"""Fusion contra el export real de la dueña: que nada se pierda por el camino.

A mano:  python3 pruebas/real.py /ruta/al/export.json
"""
import copy, json, os, sys, types
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
if "pymongo" not in sys.modules:
    f = types.ModuleType("pymongo"); f.ASCENDING=1; f.MongoClient=object; f.ReplaceOne=object
    e = types.ModuleType("pymongo.errors"); e.PyMongoError=Exception; f.errors=e
    sys.modules["pymongo"]=f; sys.modules["pymongo.errors"]=e
# bson viene DENTRO de pymongo, asi que en una maquina donde pymongo esta
# instalado esto pasa sin que nadie lo note. Donde no lo esta, la prueba ni
# arranca. Se dobla igual: ObjectId solo se usa para usuarios, no para fusionar.
if "bson" not in sys.modules:
    b_ = types.ModuleType("bson"); b_.ObjectId=str
    be = types.ModuleType("bson.errors"); be.InvalidId=Exception; b_.errors=be
    sys.modules["bson"]=b_; sys.modules["bson.errors"]=be
import main

ruta = sys.argv[1] if len(sys.argv) > 1 else None
if not ruta or not os.path.exists(ruta):
    print("uso: python3 pruebas/real.py /ruta/export.json"); sys.exit(2)
est = json.load(open(ruta, encoding="utf-8"))
est = {k: v for k, v in est.items() if k in main.DATA_KEYS}

fallos = []
def ok(c, m, d=None):
    print(("  ok   " if c else "  FALLA ") + m + ("" if c or d is None else "  -> " + repr(d)[:160]))
    if not c: fallos.append(m)

print("\nEl export de la dueña, fusionado consigo mismo (no puede cambiar nada)")
r = main.fusionar_estado(copy.deepcopy(est), copy.deepcopy(est), 2, 1)
for k in sorted(main.MERGE_FIELDS):
    a, b = est.get(k), r.get(k)
    if isinstance(a, list):
        ok(len(b or []) == len(a), "%-22s %d registros, ninguno se pierde" % (k, len(a)), len(b or []))
cfg_a, cfg_b = est.get("config") or {}, r.get("config") or {}
perdidas = [k for k in cfg_a if k not in cfg_b and k not in main.CONFIG_PROHIBIDO]
ok(not perdidas, "config: no se pierde ninguna clave", perdidas)
for k in ("aperturaUsdt", "aperturaFecha"):
    ok(cfg_b.get(k) == cfg_a.get(k), "config.%s intacto" % k, (cfg_a.get(k), cfg_b.get(k)))

print("\nY con el otro aparato un dia atrasado, lo suyo no lo pisa")
viejo = copy.deepcopy(est)
(viejo.setdefault("config", {}))["aperturaUsdt"] = 1.0
viejo["config"]["aperturaFecha"] = "2026-01-01"
viejo.setdefault("_modCampos", {}).setdefault("config", {})
viejo["_modCampos"]["config"]["aperturaUsdt"] = 1
viejo["_modCampos"]["config"]["aperturaFecha"] = 1
nuevo = copy.deepcopy(est)
nuevo.setdefault("_modCampos", {}).setdefault("config", {})
for k in ("aperturaUsdt", "aperturaFecha"):
    nuevo["_modCampos"]["config"][k] = 9999999999999
r = main.fusionar_estado(viejo, nuevo, 2, 1)      # llega el atrasado contra lo nuevo guardado
ok((r["config"].get("aperturaUsdt") == (est.get("config") or {}).get("aperturaUsdt")),
   "la apertura buena se queda", r["config"].get("aperturaUsdt"))

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
