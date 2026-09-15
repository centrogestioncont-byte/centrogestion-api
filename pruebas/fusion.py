# -*- coding: utf-8 -*-
"""Pruebas de la fusion del estado — la parte que decide que dato gana.

No levanta el servidor ni toca Mongo: importa main.py y llama a las funciones
sueltas, igual que pruebas/prestamos.js hace con index.html en la app. Asi se
prueba el codigo que de verdad se despliega.

A mano:  python3 pruebas/fusion.py
"""
import os
import sys
import types

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

# pymongo arrastra cryptography, que no siempre esta. Aqui no se usa ninguna:
# la fusion es aritmetica pura. Se pone un doble para poder importar main.
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

import main  # noqa: E402

fallos = []


def ok(condicion, mensaje, dato=None):
    if condicion:
        print("  ok   " + mensaje)
    else:
        fallos.append(mensaje)
        print("  FALLA " + mensaje + ("  -> " + repr(dato) if dato is not None else ""))


T1 = 1789412461727          # el telefono marco aqui
T2 = T1 + 3600000           # la PC, una hora despues
APERTURA = ["aperturaUsdt", "aperturaFecha", "aperturaSaldos", "aperturaTs", "aperturaBase"]


def fusionar(entrante, guardado, marcas_ent=None, marcas_guar=None):
    """Como lo llama do_PUT: lo guardado hace de local, lo que llega de remoto."""
    ent = {"config": entrante}
    gua = {"config": guardado}
    if marcas_ent is not None:
        ent["_modCampos"] = {"config": marcas_ent}
    if marcas_guar is not None:
        gua["_modCampos"] = {"config": marcas_guar}
    return main.fusionar_estado(ent, gua, T2, T1)


print("\nLas marcas tienen que llegar al servidor")
ok("_modCampos" in main.DATA_KEYS,
   "_modCampos esta en DATA_KEYS: sin eso el servidor no guarda quien toco que")

print("\nLa apertura viaja entera o no viaja")
ok(isinstance(getattr(main, "MERGE_BLOQUES", None), dict) and "config" in main.MERGE_BLOQUES,
   "hay bloques de claves que viajan juntas en config")
bloque = (getattr(main, "MERGE_BLOQUES", {}) or {}).get("config", [[]])[0]
for k in APERTURA:
    ok(k in bloque, "  " + k + " va en el bloque de la apertura")

# El caso real del 14/09: el telefono con una apertura, la PC con otra.
tel = {"aperturaUsdt": 2544.79, "aperturaFecha": "2026-09-11"}
pc = {"aperturaUsdt": 2450.20, "aperturaFecha": "2026-09-12",
      "aperturaSaldos": {"c1": 10}, "aperturaTs": T2}
r = fusionar(tel, pc, {"aperturaUsdt": T2, "aperturaFecha": T1},
             {"aperturaUsdt": T1, "aperturaFecha": T2, "aperturaSaldos": T2, "aperturaTs": T2})
cfg = r["config"]
ok((cfg.get("aperturaFecha") == "2026-09-11" and cfg.get("aperturaUsdt") == 2544.79) or
   (cfg.get("aperturaFecha") == "2026-09-12" and cfg.get("aperturaUsdt") == 2450.20),
   "la fecha y el monto salen SIEMPRE del mismo aparato",
   (cfg.get("aperturaFecha"), cfg.get("aperturaUsdt")))

print("\nUn valor de config que ya existe se tiene que poder cambiar")
# Esta es la regla vieja: el servidor solo rellenaba lo que estaba vacio, asi
# que una comision ya puesta no habia forma de corregirla desde la app.
r = fusionar({"comision_binance": 0.5}, {"comision_binance": 0.2},
             {"comision_binance": T2}, {"comision_binance": T1})
ok(r["config"].get("comision_binance") == 0.5,
   "con marca mas nueva, el valor del aparato entra", r["config"].get("comision_binance"))
r = fusionar({"comision_binance": 0.5}, {"comision_binance": 0.2},
             {"comision_binance": T1}, {"comision_binance": T2})
ok(r["config"].get("comision_binance") == 0.2,
   "con marca mas vieja, se queda lo guardado", r["config"].get("comision_binance"))
r = fusionar({"comision_binance": 0.5}, {"comision_binance": 0.2}, {}, {})
ok(r["config"].get("comision_binance") == 0.2,
   "sin marcas de ningun lado, se queda lo guardado", r["config"].get("comision_binance"))
r = fusionar({"algoNuevo": 7}, {}, {}, {})
ok(r["config"].get("algoNuevo") == 7, "una clave que el servidor no tiene, entra")
r = fusionar({"comision_binance": 0.5}, {"comision_binance": 0},
             {}, {})
ok(r["config"].get("comision_binance") == 0.5,
   "y lo que esta vacio en el servidor se sigue rellenando")
r = fusionar({"moduloX": True}, {"moduloX": False}, {}, {})
ok(r["config"].get("moduloX") is False,
   "un false puesto a proposito no se deja pisar sin marca")

print("\nLas marcas se unen, no se reemplazan")
ent = {"config": {"a": 1}, "_modCampos": {"config": {"a": T2}}}
gua = {"config": {"b": 2}, "_modCampos": {"config": {"b": T1}}}
r = main.fusionar_estado(ent, gua, T2, T1)
m = (r.get("_modCampos") or {}).get("config") or {}
ok(m.get("a") == T2 and m.get("b") == T1,
   "la marca del otro aparato no se pierde al guardar desde este", m)
ent = {"config": {"a": 1}, "_modCampos": {"config": {"a": T1}}}
gua = {"config": {"a": 1}, "_modCampos": {"config": {"a": T2}}}
r = main.fusionar_estado(ent, gua, T2, T1)
ok(((r.get("_modCampos") or {}).get("config") or {}).get("a") == T2,
   "y de cada clave se queda la marca mas nueva")

print("\nEl resto de la fusion no cambia de comportamiento")
# Las listas se siguen fusionando por id y por marca de registro.
ent = {"cuentas": [{"id": "c1", "saldo": 10, "_mod": 200}]}
gua = {"cuentas": [{"id": "c1", "saldo": 5, "_mod": 100},
                   {"id": "c2", "saldo": 7, "_mod": 100}]}
r = main.fusionar_estado(ent, gua, T2, T1)
porid = {c["id"]: c for c in r["cuentas"]}
ok(porid.get("c1", {}).get("saldo") == 10, "una cuenta marcada mas nueva gana", porid.get("c1"))
ok("c2" in porid, "y la que solo tiene el servidor no se pierde")

# Una clave que no esta en MERGE_FIELDS ni es config se sigue reemplazando.
r = main.fusionar_estado({"tasasCambio": {"BRL": 5.3}}, {"tasasCambio": {"BRL": 5.1}}, T2, T1)
ok(r["tasasCambio"].get("BRL") == 5.3, "las claves sueltas se siguen reemplazando")

# Lo que el aparato NO manda, no se toca.
r = main.fusionar_estado({"config": {"a": 1}}, {"config": {"a": 1}, "clientes": [{"cod": "X"}]}, T2, T1)
ok(len(r.get("clientes") or []) == 1, "lo que el aparato no manda se queda como estaba")

# Y las claves prohibidas siguen saliendo de config.
r = main.fusionar_estado({"config": {"usuarios": [{"pin": "1234"}], "a": 1}}, {"config": {}}, T2, T1)
ok("usuarios" not in r["config"], "los perfiles con PIN se siguen borrando de config")

# FASE B: los permisos por ROL ya no deciden nada. Se borran aunque esten
# guardados desde antes: merge_config_safe arranca de lo GUARDADO, asi que una
# clave que el servidor todavia tenga vuelve sola en el guardado siguiente.
r = main.fusionar_estado({"config": {"a": 1}},
                         {"config": {"modulos": {"brl_nueva": False}}}, T2, T1)
ok("modulos" not in r["config"],
   "los permisos por rol se borran de config aunque ya estuvieran guardados")
# Y si un aparato viejo los vuelve a mandar, tampoco entran.
r = main.fusionar_estado({"config": {"modulos": {"brl_nueva": False}}},
                         {"config": {}}, T2, T1)
ok("modulos" not in r["config"], "ni aunque los mande un aparato viejo")

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
