# -*- coding: utf-8 -*-
"""Pruebas del lector del mercado P2P.

No llama a Binance: le pone un tablon de mentira delante. Lo que se prueba es
lo que de verdad decide -que anuncios cuentan y que precio sale-, no la red.

La llamada real no se puede probar aqui ni en el CI: hace falta salir a
internet. Esa la hace ella al desplegar, y por eso /mercado devuelve de
cuantos anuncios salio cada numero.

A mano:  python3 pruebas/mercado.py
"""
import os
import sys
import types

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

# Los mismos dobles que pruebas/fusion.py: pymongo y bson no hacen falta aqui.
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


def ok(condicion, titulo, extra=""):
    if condicion:
        print("  ok   " + titulo)
    else:
        fallos.append(titulo)
        print("  FALLA " + titulo + ("   -> " + str(extra) if extra else ""))


def anuncio(precio, minimo, maximo):
    return {"adv": {"price": str(precio),
                    "minSingleTransAmount": str(minimo),
                    "maxSingleTransAmount": str(maximo)}}


def con_tablon(filas, fallo=""):
    """Sustituye la llamada a Binance por una respuesta fija.

    Devuelve (lista, motivo) porque eso es lo que devuelve la de verdad: hay
    que poder distinguir "Binance no contesto" de "contesto y ninguno sirve".
    """
    main._pedir_tablon = lambda fiat, tipo, filas_n=20: (filas, fallo)


def tasa(fiat, tipo, monto):
    """Solo el dato, para las pruebas a las que el motivo les da igual."""
    return main._leer_mercado(fiat, tipo, monto)[0]


def porque(fiat, tipo, monto):
    """Solo el motivo."""
    return main._leer_mercado(fiat, tipo, monto)[1]


print("\n== El precio se lee A SU VOLUMEN, no el mejor del tablon ==")
# El primero es el mejor precio pero solo acepta hasta 200: ella mueve 1.000.
con_tablon([
    anuncio(5.00, 50, 200),      # fuera: no acepta 1.000
    anuncio(5.10, 500, 5000),
    anuncio(5.12, 800, 9000),
    anuncio(5.20, 100000, 500000),  # fuera: pide mas de lo que ella mueve
])
r = tasa("BRL", "BUY", 1000)
ok(r is not None and r["anuncios"] == 2,
   "solo cuentan los anuncios que aceptan su monto", r)
ok(r and abs(r["tasa"] - 5.11) < 1e-9,
   "y el precio es la MEDIANA de esos, no el mejor de la lista", r and r["tasa"])

# Con tres, la mediana es la del medio.
con_tablon([anuncio(5.10, 1, 9999), anuncio(5.12, 1, 9999), anuncio(5.30, 1, 9999)])
r = tasa("BRL", "BUY", 1000)
ok(r and r["tasa"] == 5.12, "con tres anuncios, el del medio", r and r["tasa"])

print("\n== Un anuncio sin limites NO se supone bueno ==")
# Sin minimo/maximo no hay forma de saber si acepta su monto. Contarlo seria
# meter en la mediana un precio que quiza no puede tomar.
con_tablon([{"adv": {"price": "5.00"}}, anuncio(5.40, 1, 9999)])
r = tasa("BRL", "BUY", 1000)
ok(r and r["anuncios"] == 1 and r["tasa"] == 5.40,
   "el anuncio sin limites se descarta en vez de suponer que sirve", r)

print("\n== Nada de esto puede reventar ==")
con_tablon(None, "Binance respondio 403")   # Binance no contesto
ok(tasa("BRL", "BUY", 1000) is None, "si no contesta, no hay dato")
con_tablon([])                        # contesto vacio
ok(tasa("BRL", "BUY", 1000) is None, "si viene vacio, tampoco")
con_tablon([anuncio(0, 1, 9999), anuncio("abc", 1, 9999), {"adv": "no soy un dict"}, "ni yo"])
ok(tasa("BRL", "BUY", 1000) is None,
   "con basura dentro no levanta: la descarta toda")
con_tablon([anuncio(5.10, 5000, 9000)])   # ninguno acepta su monto
ok(tasa("BRL", "BUY", 1000) is None,
   "si ninguno acepta su monto, no se inventa un precio")

print("\n== Y las dos razones de quedarse sin precio NO son la misma ==")
# Esto es lo que costo una tarde: la pantalla decia "sin lectura" para las dos
# y no habia forma de saber si el arreglo era del servidor o de bajar el monto.
con_tablon(None, "Binance respondio 403")
m = porque("BRL", "BUY", 1000)
ok("403" in m, "si Binance corta, se dice el numero que devolvio", m)
con_tablon([anuncio(5.10, 5000, 9000), anuncio(5.11, 6000, 9000)])
m = porque("BRL", "BUY", 1000)
ok("403" not in m and "1.000" in m and "2" in m,
   "si contesto pero ninguno acepta su monto, se dice cuantos habia y cuanto pidio", m)
ok(porque("BRL", "BUY", 1000) != main._leer_mercado("BRL", "BUY", 99999999)[1],
   "y los dos motivos no son el mismo texto")
con_tablon([])
m = porque("BRL", "BUY", 1000)
ok("BRL" in m and "1.000" not in m,
   "un tablon vacio no se disfraza de problema de monto", m)
con_tablon([anuncio(5.12, 1, 999999)])
ok(porque("BRL", "BUY", 1000) == "",
   "y cuando sale bien no hay motivo que contar")

print("\n== La respuesta al navegador nunca deja la app sin datos ==")
main._mercado_cache.clear()
con_tablon(None, "Binance respondio 403")
r = main.mercado_p2p()
ok(r["ok"] is True and r["disponible"] is False,
   "Binance caido -> ok=True y disponible=False, nunca un error", r)
ok("403" in r.get("motivo", ""), "y dice por que, con el numero", r.get("motivo"))
ok("403" in r.get("motivoBRL", "") and "403" in r.get("motivoVES", ""),
   "cada lado trae el suyo: los reales y los bolivares pueden fallar distinto", r)
ok(r["motivo"] == r["motivoBRL"],
   "si los dos fallaron por lo mismo, no se dice dos veces", r["motivo"])

# Media lectura tambien es un fallo: sin las DOS tasas no hay suelo.
main._mercado_cache.clear()
main._pedir_tablon = lambda fiat, tipo, filas_n=20: (
    ([anuncio(5.12, 1, 999999)], "") if fiat == "BRL" else (None, "Binance respondio 429"))
r = main.mercado_p2p()
ok(r["disponible"] is True and "motivoBRL" not in r,
   "el lado que si leyo no arrastra un motivo que no existe", r)
ok("429" in r.get("motivoVES", ""),
   "y el que fallo lo dice aunque el otro haya salido bien", r.get("motivoVES"))
main._mercado_cache.clear()
con_tablon([anuncio(5.12, 1, 999999)])
r = main.mercado_p2p()
ok(r["disponible"] is True and r["compraBRL"]["tasa"] == 5.12,
   "con lectura buena, disponible=True", r)
ok(r["compraBRL"]["anuncios"] == 1 and r["compraBRL"]["monto"] == 1000.0,
   "y dice de cuantos anuncios salio y a que monto: si no cuadra, se ve", r["compraBRL"])

print("\n== El monto se puede cambiar, y una basura no lo rompe ==")
main._mercado_cache.clear()
con_tablon([anuncio(5.12, 1, 999999)])
r = main.mercado_p2p({"BRL": "2500"})
ok(r["compraBRL"]["monto"] == 2500.0, "se puede pedir otro monto", r["compraBRL"])
main._mercado_cache.clear()
r = main.mercado_p2p({"BRL": "hola"})
ok(r["compraBRL"]["monto"] == 1000.0,
   "y si llega basura se usa el suyo medido, no cero", r["compraBRL"])

print("\n== El cache evita preguntarle a Binance en cada pantalla ==")
main._mercado_cache.clear()
llamadas = {"n": 0}


def contar(fiat, tipo, filas_n=20):
    llamadas["n"] += 1
    return [anuncio(5.12, 1, 999999)], ""


main._pedir_tablon = contar
main.mercado_p2p()
primero = llamadas["n"]
main.mercado_p2p()
ok(llamadas["n"] == primero, "la segunda lectura sale del cache", llamadas["n"])
main.mercado_p2p({"BRL": "9999"})
ok(llamadas["n"] > primero, "pero otro monto es otra lectura", llamadas["n"])

print("\n== Las dos direcciones no se confunden ==")
# Ella COMPRA USDT con reales -> mira a quien VENDE (BUY).
# Ella VENDE USDT por bolivares -> mira a quien COMPRA (SELL).
vistos = []


def anotar(fiat, tipo, filas_n=20):
    vistos.append((fiat, tipo))
    return [anuncio(1.0, 1, 999999999)], ""


main._pedir_tablon = anotar
main._mercado_cache.clear()
main.mercado_p2p()
ok(("BRL", "BUY") in vistos, "los reales se leen del lado de quien vende USDT", vistos)
ok(("VES", "SELL") in vistos, "y los bolivares del lado de quien los compra", vistos)

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
