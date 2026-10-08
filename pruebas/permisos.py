# -*- coding: utf-8 -*-
"""Pruebas del paso 3: el servidor hace cumplir QUE puede escribir cada uno.

Como fusion.py y respaldo.py: importa main.py con una pymongo de mentira. No
levanta el servidor ni toca Mongo.

Lo que fija, y por que cada cosa:

  · la tabla de valores por omision de cada ROL, valor por valor. Es la misma
    que PERMISOS_POR_ROL en index.html y que sean iguales no lo puede
    comprobar ninguna prueba —los dos repositorios no se ven entre si— asi
    que al menos cambiarla tiene que costar tocar esta prueba a proposito.

  · las claves que NO se pueden guardar. Es la mitad importante: registrar
    una remesa mueve los saldos, consume lotes del FIFO y crea la fila por
    cobrar si queda pendiente. Guardar cualquiera de esas por un permiso que
    un operador no tiene le romperia justo su trabajo, y en silencio.

  · que fusionar_estado conserva una clave que no llega. Todo el diseno se
    apoya en eso: filtrar es QUITAR la clave del bloque, no mandarla vacia.

A mano:  python3 pruebas/permisos.py
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


print("== 1. El rol es un punto de partida, no el permiso ==")

ADMIN = {"rol": "admin", "permisos": {}}
OPE = {"rol": "brl", "permisos": {"editar": True}}

ok(main.tiene_permiso(ADMIN, "cierre") is True, "el administrador puede todo")
ok(main.tiene_permiso(ADMIN, "lo_que_sea") is True, "incluso una casilla que no existe")
ok(main.tiene_permiso(OPE, "nueva") is True, "un operador trae 'nueva' por su rol")
ok(main.tiene_permiso(OPE, "prestamos") is False, "y no trae 'prestamos'")
# Lo explicito manda sobre el rol, en los dos sentidos. Esto es lo que hace
# que quitar un permiso quite algo de verdad (FASE B: guardar manda la lista
# COMPLETA, con su true o su false).
ok(main.tiene_permiso({"rol": "brl", "permisos": {"nueva": False}}, "nueva") is False,
   "un false explicito le gana al valor por omision del rol")
ok(main.tiene_permiso({"rol": "brl", "permisos": {"prestamos": True}}, "prestamos") is True,
   "y un true explicito tambien")
ok(main.tiene_permiso({"rol": "lector", "permisos": {}}, "cierre") is True,
   "el supervisor ve todo")
ok(main.tiene_permiso({"rol": "lector", "permisos": {}}, "editar") is False,
   "menos editar: eso ES el rol")
ok(main.tiene_permiso({"rol": "inventado", "permisos": {}}, "nueva") is False,
   "un rol desconocido no trae nada puesto")
ok(main.tiene_permiso(None, "nueva") is False, "sin usuario no hay permiso")

print("\n== 2. La tabla de cada rol, valor por valor (espejo de index.html) ==")

ESPERADA = {
    "admin": "*",
    "lector": {"todo": True, "salvo": ["editar"]},
    "brl": {"si": ["editar", "mi_ganancia", "op_diario", "nueva", "clientes"]},
    "vzla": {"si": ["editar", "mi_ganancia", "op_diario", "nueva", "clientes"]},
    "eeuu": {"si": ["editar", "mi_ganancia", "op_diario", "nueva_eeuu",
                    "clientes", "dash_eeuu"]},
}
ok(main.PERMISOS_POR_ROL == ESPERADA,
   "los valores por omision son los mismos que en el navegador",
   main.PERMISOS_POR_ROL)

print("\n== 3. Lo que NUNCA se puede guardar ==")

# Medido recorriendo index.html y anotando que funciones escriben cada clave.
# Registrar una remesa (saveTx / saveTxEE) toca TODAS estas.
NO_GUARDABLES = {
    "cuentas":                "una remesa mueve los saldos",
    "capital":                "la fila de capital de cada cuenta va con la cuenta",
    "inventarioUsdt":         "una remesa consume lotes del FIFO",
    "inventarioUsdt_cerrado": "y cierra los que se agotan",
    "cuentasCobrar":          "una remesa PENDIENTE crea la fila por cobrar",
    "movimientosCapital":     "lo escribe la propia remesa",
    "brl":                    "es lo que el operador registra: su trabajo",
    "vzla":                   "igual",
    "clientes":               "van por /clientes y el bloque de estado se ignora",
    "_deleted":               "infraestructura de la fusion",
    "_deletedMerge":          "infraestructura de la fusion",
    "_modCampos":             "infraestructura de la fusion",
    "histBalance":            "historial indexado por fecha: se une",
    "histTasas":              "historial indexado por fecha: se une",
    "histSaldos":             "historial indexado por fecha: se une",
    "histApertura":           "historial indexado por fecha: se une",
    "histComp":               "historial indexado por fecha: se une",
}
for clave, porque in NO_GUARDABLES.items():
    ok(clave not in main.CLAVES_GUARDADAS,
       "'%s' no se guarda (%s)" % (clave, porque))

print("\n== 4. La tabla de claves guardadas esta bien escrita ==")

# Las 21 casillas de PERMISOS_APP en index.html. Una clave guardada por un
# permiso que no existe no guarda NADA: tiene_permiso devolveria False para
# todo el mundo menos el administrador y cerraria el modulo a todos.
PERMISOS_REALES = {
    "editar", "dash", "diario", "ops", "nueva", "nueva_eeuu", "clientes",
    "prestamos", "cobrar", "egresos", "inventario_usdt", "capital_total",
    "traspasos", "calculadora", "evolucion", "cierre", "dash_eeuu",
    "mi_ganancia", "op_diario", "auditoria", "config_admin",
}
for clave, permiso in main.CLAVES_GUARDADAS.items():
    ok(permiso in PERMISOS_REALES,
       "'%s' se guarda por un permiso que existe ('%s')" % (clave, permiso))
    ok(clave in main.DATA_KEYS,
       "'%s' esta en DATA_KEYS (si no, no se guardaria nunca)" % clave)

ok(len(main.CLAVES_GUARDADAS) >= 15,
   "estan las 15 claves guardadas", len(main.CLAVES_GUARDADAS))
for clave in ("prestamos", "egresos", "egresos_personales", "cierresMes",
              "config", "ajustesSaldo", "traspasos", "eeuu"):
    ok(clave in main.CLAVES_GUARDADAS, "'%s' SI se guarda" % clave)

print("\n== 5. Se deshace lo que no le tocaba, y solo se avisa de lo que CAMBIA ==")

GUARDADO = {
    "brl": [], "cuentas": [{"id": "c1", "saldo": 1000}],
    "prestamos": [{"id": 9, "monto": 100}],
    "egresos": [{"id": 8, "motivo": "LUZ"}],
    "config": {"pct_sueldo": 30},
    "cierresMes": [], "traspasos": [], "ajustesSaldo": [],
    "inventarioUsdt": [], "cuentasCobrar": [], "movimientosCapital": [],
    "clientes": [], "_deletedMerge": {}, "_modCampos": {},
}

def copia(d):
    import copy as _c
    return _c.deepcopy(d)

# EL CASO DE VERDAD, el que se vio con su primer operador: la app manda el
# bloque ENTERO en cada guardado, con las 36 claves, haya cambiado algo o no.
# Antes esto sacaba un aviso de 14 lineas sin que nadie hubiera tocado nada.
igual, rech = main.aplicar_permisos(copia(GUARDADO), GUARDADO, OPE)
ok(rech == [],
   "mandar el bloque entero SIN cambiar nada no avisa de nada", rech)
ok(igual["prestamos"] == GUARDADO["prestamos"], "y lo guardado sigue igual")

# Y cuando si intenta cambiar algo, se deshace y se avisa.
TOCADO = copia(GUARDADO)
TOCADO["prestamos"] = [{"id": 9, "monto": 0, "cliente": "BORRADO"}]
TOCADO["egresos"] = [{"id": 8, "motivo": "ROBADO"}]
TOCADO["brl"] = [{"_uid": "r1", "cliente": "JUAN"}]
TOCADO["cuentas"] = [{"id": "c1", "saldo": 900}]
limpio, rech = main.aplicar_permisos(copia(TOCADO), GUARDADO, OPE)
ok(rech == ["egresos", "prestamos"],
   "solo se avisa de las dos que de verdad intento cambiar", rech)
ok(limpio["prestamos"] == GUARDADO["prestamos"], "el prestamo vuelve a lo que habia")
ok(limpio["egresos"] == GUARDADO["egresos"], "el egreso tambien")
ok(limpio["brl"][0]["cliente"] == "JUAN", "pero SU remesa se queda")
ok(limpio["cuentas"][0]["saldo"] == 900, "y el saldo que movio la remesa tambien")

# A la administradora no se le deshace nada.
limpio, rech = main.aplicar_permisos(copia(TOCADO), GUARDADO, ADMIN)
ok(rech == [], "a la administradora no se le toca nada", rech)
ok(limpio["prestamos"][0]["cliente"] == "BORRADO", "y su cambio se queda")

# El permiso suelto: le activa egresos y los egresos le entran, sin que eso
# le abra los prestamos. Es lo que ella pidio con esas palabras.
CON_EGRESOS = {"rol": "brl", "permisos": {"editar": True, "egresos": True}}
limpio, rech = main.aplicar_permisos(copia(TOCADO), GUARDADO, CON_EGRESOS)
ok(rech == ["prestamos"], "con el permiso de egresos, solo cae el prestamo", rech)
ok(limpio["egresos"][0]["motivo"] == "ROBADO", "y el egreso entra")

# Una clave que todavia no existe guardada se quita entera.
limpio, rech = main.aplicar_permisos({"prestamos": [{"id": 1}]}, {}, OPE)
ok("prestamos" not in limpio, "una clave sin nada guardado se quita entera")
ok(rech == ["prestamos"], "y se avisa", rech)

limpio, rech = main.aplicar_permisos("no soy un dict", GUARDADO, OPE)
ok(rech == [] and limpio == "no soy un dict", "un cuerpo raro no revienta")

print("\n== 6. Quitar una clave CONSERVA lo guardado (en esto se apoya todo) ==")

# Ojo: brl/vzla/eeuu se identifican por _uid, no por id (_MERGE_ID_FIELD).
# Con un "id" la fila se descarta y la prueba pasaria por la razon equivocada.
guardado = {"prestamos": [{"id": 9, "monto": 100}], "brl": []}
fus = main.fusionar_estado({"brl": [{"_uid": "a1"}]}, guardado, 2000, 1000)
ok(fus.get("prestamos") == [{"id": 9, "monto": 100}],
   "una clave que no llega se queda TAL CUAL como estaba", fus.get("prestamos"))
ok(len(fus.get("brl") or []) == 1, "y la que si llega se fusiona")

print("\n== 7. El filtro esta enchufado, y el rechazo no es mudo ==")

_src = inspect.getsource(main.Manejador.do_PUT) if hasattr(main, "Manejador") else ""
if not _src:
    for _n in dir(main):
        _o = getattr(main, _n)
        if inspect.isclass(_o) and hasattr(_o, "do_PUT"):
            _src = inspect.getsource(_o.do_PUT)
            break
ok("aplicar_permisos" in _src,
   "PUT /estado pasa el estado por los permisos")
# Anclado en la LLAMADA, no en el nombre: un comentario que diga
# "antes de fusionar_estado" aparece antes y hacia pasar esta guardia
# midiendo otro trozo del archivo. Ya paso tres veces en este proyecto.
_i = _src.find("aplicar_permisos(fusionado")
_j = _src.find("fusionar_estado(entrante")
ok(_i > -1 and _j > -1 and _i > _j,
   "y lo aplica DESPUES de fusionar, que es lo que permite ver si cambia algo",
   (_i, _j))
ok("clavesRechazadas" in _src,
   "la respuesta dice que no se guardo (un fallo mudo obliga a adivinar)")
ok("escritura rechazada" in _src,
   "y queda anotado en la auditoria DEL SERVIDOR, que no es voluntaria")

print("\n== 8. Las tres claves que el servidor estaba tirando ==")

for clave in ("histApertura", "histComp", "mapaBinance"):
    ok(clave in main.DATA_KEYS,
       "'%s' esta en DATA_KEYS: guardar_estado recorre esa lista" % clave)

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
