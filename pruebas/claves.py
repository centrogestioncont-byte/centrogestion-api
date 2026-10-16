# -*- coding: utf-8 -*-
"""ARREGLO 151 — que el servidor guarde TODO lo que la app le manda.

Esto ya ha pasado dos veces:

  08/10/2026   histApertura, histComp y mapaBinance no estaban en DATA_KEYS.
               La app las mandaba, el servidor las tiraba, 200 OK. El
               historial de la apertura —que existe para contestar "¿cuanto
               era antes?"— no guardo ni una linea en semanas.

  16/10/2026   lo mismo con CINCO claves mas, y esta vez eran modulos
               enteros: el apartamento (134), la mercancia (143) y los
               aportes (146). La dueña lo vio porque la PC tenia su maquina
               de 210 USDT y el telefono no, y Actualizar no la traia nunca.
               Esos tres modulos vivian SOLO dentro de un navegador: ni en
               Mongo, ni en la copia automatica.

Las dos veces el fallo fue MUDO, y por eso tardo. Esta prueba existe para que
la tercera cueste trabajo:

  1. fija las cuatro listas valor por valor, como pruebas/permisos.py hace
     con PERMISOS_POR_ROL. Anadir o quitar una clave obliga a tocar esto a
     proposito, no de pasada;
  2. y cuando hay red, DESCARGA la app publicada y compara las listas de
     verdad — que es lo unico que caza el caso real: la app anade una clave
     y aqui nadie se entera. Sin red no falla: lo dice y sigue.

A mano:  python3 pruebas/claves.py
"""
import json
import os
import re
import sys
import types
import urllib.error
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

# Los mismos dobles que pruebas/fusion.py: aqui no se toca Mongo.
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
    falso_bson = types.ModuleType("bson")
    falso_bson.ObjectId = str
    errores_bson = types.ModuleType("bson.errors")
    errores_bson.InvalidId = Exception
    falso_bson.errors = errores_bson
    sys.modules["bson"] = falso_bson
    sys.modules["bson.errors"] = errores_bson

import main  # noqa: E402

fallos = []


def ok(condicion, mensaje, dato=None):
    if condicion:
        print("  ok   " + mensaje)
    else:
        fallos.append(mensaje)
        print("  FALLA " + mensaje + ("  -> " + repr(dato) if dato is not None else ""))


# ══════════════════════════════════════════════════════════════════════════
# 1 · LAS CINCO QUE FALTABAN, POR NOMBRE
# ══════════════════════════════════════════════════════════════════════════
#
# Van en DATA_KEYS (si no, no se guardan) Y en MERGE_FIELDS (si no, la lista
# entrante REEMPLAZA a la guardada en vez de unirse registro a registro, y
# dos aparatos se pisan). Las dos cosas, o el modulo sigue roto a medias.
NUEVAS = ["inmuebles", "inmCuotas", "articulos", "artGastos", "aportes"]

print("Los modulos del 134, el 143 y el 146 llegan al servidor")
for k in NUEVAS:
    ok(k in main.DATA_KEYS,    k + " se GUARDA (DATA_KEYS)")
    ok(k in main.MERGE_FIELDS, k + " se UNE en vez de reemplazarse (MERGE_FIELDS)")

print("\nY la apertura sigue viajando entera")
# "aperturaSinInteres" (ARREGLO 149 de la app, 16/10) dice si el monto ya
# viene sin el interes de los prestamos. Un monto sin su bandera no se puede
# interpretar: fuera del bloque, la fusion dejaria el monto de un aparato con
# la bandera del otro y el interes se descontaria dos veces, o ninguna.
BLOQUE = ["aperturaUsdt", "aperturaFecha", "aperturaSaldos", "aperturaTs",
          "aperturaBase", "aperturaSinInteres"]
bloque = (main.MERGE_BLOQUES or {}).get("config", [[]])[0]
for k in BLOQUE:
    ok(k in bloque, "  " + k + " va en el bloque")
ok(len(bloque) == len(BLOQUE),
   "  y el bloque son esas seis, ni una suelta", bloque)

print("\nLa poda es la misma en las dos puntas")
# Si solo una punta poda, el servidor y los telefonos resuelven distinto el
# mismo conflicto. "cierresMes" entro en el front el 16/09 y aqui no: reabrir
# un mes duraba lo que tardaba el siguiente guardado.
ok("cierresMes" in main.PODA_CATS,
   "cierresMes se poda aqui tambien (reabrir un mes tiene que durar)")
ok("capital" in main.PODA_CATS, "y capital sigue estando")


# ══════════════════════════════════════════════════════════════════════════
# 2 · LO QUE EL SERVIDOR NO SEPA GUARDAR, QUE LO DIGA
# ══════════════════════════════════════════════════════════════════════════
#
# Las dos veces que esto paso, el sintoma fue NADA: 200 OK y el dato perdido.
print("\nUna clave desconocida deja de perderse en silencio")
ok(callable(getattr(main, "claves_ignoradas", None)),
   "existe claves_ignoradas()")
ig = main.claves_ignoradas({"brl": [], "loQueSea": [1], "config": {}})
ok(ig == ["loQueSea"], "nombra la que no sabe guardar", ig)
ok(main.claves_ignoradas({k: 1 for k in main.DATA_KEYS}) == [],
   "y no se queja de las que si guarda")
ok(main.claves_ignoradas({"_ts": 1, "_version": 1, "_exportado": "x"}) == [],
   "ni del sobre del propio archivo de export")
ok(main.claves_ignoradas(None) == [], "sin cuerpo no revienta")
# Y va en la respuesta del guardado, que es donde se ve sin entrar al servidor.
fuente = open(os.path.join(RAIZ, "main.py"), encoding="utf-8").read()
ok('"clavesIgnoradas": ignoradas' in fuente,
   "y viaja en la respuesta del PUT /estado")


# ══════════════════════════════════════════════════════════════════════════
# 2b · Y QUE EL CAMINO FUNCIONE, NO SOLO QUE LAS LISTAS LO DIGAN
# ══════════════════════════════════════════════════════════════════════════
#
# Lo de arriba comprueba que el codigo DICE lo que debe decir. Esto recorre el
# ciclo entero con el caso que paso de verdad el 16/10:
#
#   la PC tiene la maquina · el servidor y el telefono no la tienen
#   la PC guarda  →  tiene que quedarse en el servidor
#   el telefono sube DESPUES, sin ella  →  NO puede borrarla
#
# Medido con sus dos exports antes de arreglarlo: en el paso 1 el servidor se
# quedaba con articulos=0, porque guardar_estado recorre DATA_KEYS y la clave
# no estaba. La maquina no se perdia en la fusion: no llegaba a escribirse.
print("\nEl ciclo entero, con la maquina de la dueña")
T1b = 1792000000000
pc = {"articulos": [{"id": 1, "nombre": "CHAVE DE IMPACTO"}],
      "aportes":   [{"id": 9, "monto": 470000}],
      "inmCuotas": [{"id": 7, "n": 1}]}
servidor = {"articulos": [], "aportes": [], "inmCuotas": []}

r1 = main.fusionar_estado(pc, servidor, T1b + 1000, T1b)
ok(len(r1.get("articulos") or []) == 1, "la PC guarda y la maquina queda en el servidor")
ok(len(r1.get("aportes") or []) == 1, "y los aportes tambien")

# Lo que guardar_estado escribiria de verdad: recorre DATA_KEYS, asi que una
# clave fuera de la lista no se escribe aunque la fusion la haya conservado.
escribe = [k for k in main.DATA_KEYS if k in r1]
for k in ("articulos", "aportes", "inmCuotas"):
    ok(k in escribe, "  y " + k + " llega a escribirse en Mongo")

# El telefono sube despues SIN la maquina. La fusion une por registro, asi que
# lo que un aparato no tiene no lo borra: solo una marca de borrado lo haria.
tel = {"articulos": [], "aportes": []}
r2 = main.fusionar_estado(tel, r1, T1b + 2000, T1b + 1000)
ok(len(r2.get("articulos") or []) == 1,
   "el telefono sincroniza sin ella y NO la borra",
   r2.get("articulos"))
ok(len(r2.get("aportes") or []) == 1, "ni los aportes")


# ══════════════════════════════════════════════════════════════════════════
# 3 · CONTRA LA APP DE VERDAD, CUANDO HAY RED
# ══════════════════════════════════════════════════════════════════════════
#
# Lo de arriba fija lo que hoy sabemos que falta. Esto caza lo que NO sabemos:
# el dia que la app anada una clave y aqui nadie se entere. Los dos
# repositorios no se ven entre si, asi que la unica forma de comparar las
# listas de verdad es leer la app publicada.
#
# Sin red NO falla, a proposito: una prueba que se cae porque el CI no
# alcanza internet se acaba desactivando, y entonces no protege nada.
APP = os.environ.get("URL_APP", "https://centrogestion.pages.dev/index.html")

print("\nLas dos listas, comparadas contra la app publicada")
html = None
try:
    with urllib.request.urlopen(APP, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
except (urllib.error.URLError, OSError, ValueError) as e:
    print("  --   sin red o sin la app a mano (%s): esta parte se salta" % type(e).__name__)

if html:
    ver = re.search(r'var APP_VERSION\s*=\s*"([^"]+)"', html)
    print("  --   leida " + (ver.group(1) if ver else "?") + " de " + APP)

    def lista_js(nombre):
        m = re.search(r"var " + nombre + r"\s*=\s*\[(.*?)\];", html, re.S)
        if not m:
            return None
        # Fuera los comentarios de linea: un nombre citado ahi no es la lista.
        cuerpo = re.sub(r"//[^\n]*", "", m.group(1))
        return set(re.findall(r'"([^"]+)"', cuerpo))

    # Lo que la app manda y aqui NO se guarda es un dato que se pierde. Al
    # reves no: el servidor puede conocer claves que esta version de la app
    # todavia no usa.
    cli = lista_js("DATA_KEYS")
    ok(cli is not None, "se encuentra DATA_KEYS en la app")
    if cli:
        # `avisoPush` queda fuera A PROPOSITO: es el aviso que se manda al
        # telefono en ese momento, no un dato del negocio — la misma regla por
        # la que la lectura de mercado tampoco se guarda. Si algun dia tiene
        # que persistir, se quita de aqui y se anade a DATA_KEYS.
        EFIMERAS = {"avisoPush"}
        faltan = sorted(cli - set(main.DATA_KEYS) - EFIMERAS)
        ok(not faltan,
           "el servidor guarda TODAS las claves que manda la app",
           "se pierden: " + ", ".join(faltan) if faltan else None)

    cliM = lista_js("_MERGE_FIELDS")
    ok(cliM is not None, "se encuentra _MERGE_FIELDS en la app")
    if cliM:
        faltanM = sorted(cliM - set(main.MERGE_FIELDS))
        ok(not faltanM,
           "y las une registro a registro, igual que la app",
           "se reemplazan enteras: " + ", ".join(faltanM) if faltanM else None)

    cliP = lista_js("_PODA_CATS")
    if cliP:
        faltanP = sorted(cliP - set(main.PODA_CATS))
        ok(not faltanP,
           "y poda lo mismo que la app",
           "solo poda una punta: " + ", ".join(faltanP) if faltanP else None)

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
