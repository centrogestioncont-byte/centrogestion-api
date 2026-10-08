# -*- coding: utf-8 -*-
"""Pruebas del sello de quien registro cada cosa.

Lo que mas vigila es lo contrario de lo que parece: no que se selle, sino
que NO se selle de mas. Un aparato manda su copia entera del estado, con los
registros de todo el mundo; sellar por "viene en el bloque" le pondria el
nombre de quien guarda a las 192 remesas de ella. Es el mismo error que ya
costo el aviso de 14 lineas en los permisos.

A mano:  python3 pruebas/sello.py
"""
import os
import sys
import types
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


ELLA = {"nombre": "Jhoselin", "correo": "ella@x.com", "rol": "admin"}
OPE = {"nombre": "Carlos", "correo": "ope@x.com", "rol": "brl"}

print("== 1. Lo que este guardado creo o cambio ==")

GUARDADO = {
    "brl": [
        {"_uid": "r1", "cl": "JUAN", "am": 100, "_por": "Jhoselin", "_porUlt": "Jhoselin"},
        {"_uid": "r2", "cl": "ANA", "am": 200, "_por": "Jhoselin", "_porUlt": "Jhoselin"},
    ],
    "egresos": [{"id": 1, "mot": "LUZ"}],
}

# El operador manda su copia entera y cambia UNA cosa, y crea otra.
entra = {
    "brl": [
        {"_uid": "r1", "cl": "JUAN", "am": 100, "_por": "Jhoselin", "_porUlt": "Jhoselin"},
        {"_uid": "r2", "cl": "ANA", "am": 999, "_por": "Jhoselin", "_porUlt": "Jhoselin"},
        {"_uid": "r3", "cl": "NUEVO", "am": 50},
    ],
    "egresos": [{"id": 1, "mot": "LUZ"}],
}
n = main.sellar_quien(entra, GUARDADO, OPE)
porUid = {r["_uid"]: r for r in entra["brl"]}
ok(n == 2, "se sellan 2 registros: el que cambio y el nuevo", n)
ok(porUid["r3"].get("_por") == "Carlos" and porUid["r3"].get("_porUlt") == "Carlos",
   "el nuevo lo creo Carlos", porUid["r3"])
ok(porUid["r2"].get("_por") == "Jhoselin", "el que cambio SIGUE creado por ella",
   porUid["r2"])
ok(porUid["r2"].get("_porUlt") == "Carlos", "pero lo toco Carlos", porUid["r2"])
ok(porUid["r1"].get("_porUlt") == "Jhoselin",
   "y el que NO cambio no se toca: mandar la copia entera no sella nada",
   porUid["r1"])
ok(entra["egresos"][0].get("_por") is None,
   "un registro sin sello que no cambio sigue sin sello", entra["egresos"][0])

print("\n== 2. No se puede falsear, que es para lo que existe esto ==")

# Alguien edita el bloque a mano y se pone otro nombre en un registro ajeno.
mentira = {"brl": [
    {"_uid": "r1", "cl": "JUAN", "am": 100, "_por": "EL JEFE", "_porUlt": "EL JEFE"},
    {"_uid": "r2", "cl": "ANA", "am": 200, "_por": "NADIE", "_porUlt": "NADIE"},
]}
main.sellar_quien(mentira, GUARDADO, OPE)
porUid = {r["_uid"]: r for r in mentira["brl"]}
ok(porUid["r1"].get("_por") == "Jhoselin" and porUid["r1"].get("_porUlt") == "Jhoselin",
   "un sello inventado sobre un registro que no cambio se deshace", porUid["r1"])
ok(porUid["r2"].get("_por") == "Jhoselin",
   "y el otro tambien", porUid["r2"])

# Y si de verdad cambia algo, el sello es el SUYO, no el que mando.
mentira2 = {"brl": [{"_uid": "r1", "cl": "OTRO", "am": 100,
                     "_por": "EL JEFE", "_porUlt": "EL JEFE"}]}
main.sellar_quien(mentira2, GUARDADO, OPE)
ok(mentira2["brl"][0]["_porUlt"] == "Carlos",
   "cambiando de verdad, queda sellado con QUIEN ES, no con lo que mando",
   mentira2["brl"][0])
ok(mentira2["brl"][0]["_por"] == "Jhoselin",
   "y no puede robarle la autoria a quien lo creo", mentira2["brl"][0])

print("\n== 3. Sellar no puede hacer que el registro parezca cambiado ==")

# Si _por o _porUlt contaran para decidir si algo cambio, sellar lo dejaria
# distinto y se volveria a sellar en cada guardado, para siempre.
yaSellado = {"brl": [{"_uid": "r1", "cl": "JUAN", "am": 100,
                      "_por": "Jhoselin", "_porUlt": "Jhoselin"}]}
ok(main.sellar_quien(yaSellado, GUARDADO, OPE) == 0,
   "un guardado que no cambia nada no sella nada")
conMod = {"brl": [{"_uid": "r1", "cl": "JUAN", "am": 100, "_mod": 123456,
                   "_por": "Jhoselin", "_porUlt": "Jhoselin"}]}
ok(main.sellar_quien(conMod, GUARDADO, OPE) == 0,
   "_mod tampoco cuenta: es la marca de la fusion, no un dato")
fuente = inspect.getsource(main._sin_sellos)
for campo in ('"_mod"', 'SELLO_CREO', 'SELLO_ULT'):
    ok(campo in fuente, "y %s queda fuera de la comparacion" % campo)

print("\n== 4. Sin nombre no se inventa nada ==")

sinNombre = {"brl": [{"_uid": "r9", "cl": "X"}]}
ok(main.sellar_quien(sinNombre, GUARDADO, {}) == 0,
   "sin usuario no se sella")
ok(sinNombre["brl"][0].get("_por") is None, "y el registro se queda limpio")
ok(main._nombre_de({"correo": "x@x.com"}) == "x@x.com",
   "sin nombre se usa el correo")

print("\n== 5. Quien borro viaja en la marca ==")

unidas = main.unir_marcas(
    {"brl": [{"id": "r1", "ts": 9999999999999, "por": "Carlos"}]},
    {"brl": [{"id": "r2", "ts": 9999999999999}]})
ok(unidas["brl"][0].get("por") == "Carlos",
   "unir_marcas conserva quien borro", unidas["brl"])
ok("por" not in unidas["brl"][1],
   "y no se lo inventa a la que no lo traia", unidas["brl"])

print("\n== 6. Esta enchufado, y despues de los permisos ==")

_src = ""
for _n in dir(main):
    _o = getattr(main, _n)
    if inspect.isclass(_o) and hasattr(_o, "do_PUT"):
        _src = inspect.getsource(_o.do_PUT)
        break
_i = _src.find("sellar_quien(fusionado")
_j = _src.find("aplicar_permisos(fusionado")
ok(_i > -1, "PUT /estado sella")
ok(_i > -1 and _j > -1 and _i > _j,
   "y sella DESPUES de los permisos: lo que no se dejo entrar no se sella",
   (_i, _j))

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
