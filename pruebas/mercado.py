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

from urllib.error import HTTPError  # noqa: E402

import main  # noqa: E402

# La de verdad, guardada ANTES de que las pruebas la sustituyan por dobles:
# mas abajo se mira que manda a Binance, y para eso hace falta la original.
_TABLON_REAL = main._pedir_tablon


def _sin_red(*a, **k):
    raise RuntimeError("las pruebas no salen a internet")


# Por omision NADIE llega a Binance. Las que miran el mercado normal ponen su
# propio doble y lo quitan; sin esto, una prueba se iba a la red de verdad y
# contestaba distinto segun donde corriera.
main.urlopen = _sin_red

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
    main._pedir_tablon = lambda fiat, tipo, *a, **k: (filas, fallo)


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
con_tablon([{"adv": {"price": "5.10"}}])   # sin limites: no se puede saber
ok(tasa("BRL", "BUY", 1000) is None,
   "si ningun anuncio dice sus limites, no se inventa un precio")

print("\n== Y las dos razones de quedarse sin precio NO son la misma ==")
# Esto es lo que costo una tarde: la pantalla decia "sin lectura" para las dos
# y no habia forma de saber si el arreglo era del servidor o de bajar el monto.
con_tablon(None, "Binance respondio 403")
m = porque("BRL", "BUY", 1000)
ok("403" in m, "si Binance corta, se dice el numero que devolvio", m)
con_tablon([{"adv": {"price": "5.10"}}, {"adv": {"price": "5.11"}}])
m = porque("BRL", "BUY", 1000)
ok("403" not in m and "2" in m and "límites" in m,
   "si contesto pero ninguno dice sus limites, se dice cuantos habia", m)
con_tablon([])
m = porque("BRL", "BUY", 1000)
ok("BRL" in m and "1.000" not in m,
   "un tablon vacio no se disfraza de problema de monto", m)
ok(not m.startswith(" ") and not m.startswith("·"),
   "y el motivo nunca empieza a media frase, aunque el primer intento no traiga texto", m)
con_tablon([anuncio(5.12, 1, 999999)])
ok(porque("BRL", "BUY", 1000) == "",
   "y cuando sale bien no hay motivo que contar")

print("\n== El precio de los reales: el primer sitio que conteste ==")
# Su servidor esta en Railway EE.UU. y Binance le devuelve 451 —"bloqueado por
# tu pais"— tanto en el mercado normal como vaciandole el tablon P2P. Cambiar
# de region es de pago y su plan no lo tiene, asi que hay que ir a otro sitio.


class _RespFalsa:
    def __init__(self, cuerpo):
        self.cuerpo = cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.cuerpo


_urlopen_guardado = main.urlopen


def con_fuentes(respuestas):
    """respuestas: {trozo de la url -> cuerpo en bytes, o una excepcion}."""
    visitadas = []

    def falso(pedido, timeout=None):
        url = pedido.full_url
        visitadas.append(url)
        for trozo, que in respuestas.items():
            if trozo in url:
                if isinstance(que, Exception):
                    raise que
                return _RespFalsa(que)
        raise RuntimeError("nadie contesta")

    main.urlopen = falso
    return visitadas


# Las tres formas en que cada sitio envuelve el precio.
ok(main._precio_de({"price": "5.27"}) == 5.27, "se entiende la forma de Binance")
ok(main._precio_de({"tether": {"brl": 5.2713}}) == 5.2713,
   "y la de CoinGecko, que lo mete dentro")
ok(main._precio_de([{"pair": "USDT-BRL", "last": "5.28"}]) == 5.28,
   "y la de Mercado Bitcoin, que manda una lista")
ok(main._precio_de({"algo": "otra cosa"}) is None,
   "y una forma que no se conoce no se adivina")

# Binance contesta -> se queda con Binance y no molesta a los demas.
visitadas = con_fuentes({"binance": b'{"price":"5.27"}'})
r, motivo = main._precio_spot("BRL")
main.urlopen = _urlopen_guardado
ok(r and r["tasa"] == 5.27 and r["fuente"] == "Binance",
   "con Binance sano, el precio es el suyo", r)
ok(len(visitadas) == 1, "y a los otros sitios ni se les pregunta", visitadas)

# Binance da 451 -> sigue al siguiente.
visitadas = con_fuentes({
    "binance": HTTPError("u", 451, "Unavailable For Legal Reasons", {}, None),
    "coingecko": b'{"tether":{"brl":5.2713}}',
})
r, motivo = main._precio_spot("BRL")
main.urlopen = _urlopen_guardado
ok(r and r["tasa"] == 5.2713 and r["fuente"] == "CoinGecko",
   "si Binance bloquea por pais, se va al siguiente sitio", r)
ok(any("coingecko" in u for u in visitadas), "que si se le pregunta", visitadas)

# Los dos primeros caen -> el tercero.
visitadas = con_fuentes({
    "binance": HTTPError("u", 451, "x", {}, None),
    "coingecko": HTTPError("u", 429, "x", {}, None),
    "mercadobitcoin": b'[{"pair":"USDT-BRL","last":"5.29"}]',
})
r, motivo = main._precio_spot("BRL")
main.urlopen = _urlopen_guardado
ok(r and r["tasa"] == 5.29 and r["fuente"] == "Mercado Bitcoin",
   "y si caen dos, el tercero", r)

# Si caen las tres, se dice lo que contesto CADA una: eso es lo que decide que
# hacer despues, y el 451 hay que poder leerlo tal cual.
con_fuentes({
    "binance": HTTPError("u", 451, "x", {}, None),
    "coingecko": HTTPError("u", 403, "x", {}, None),
    "mercadobitcoin": HTTPError("u", 500, "x", {}, None),
})
r, motivo = main._precio_spot("BRL")
main.urlopen = _urlopen_guardado
ok(r is None, "sin ninguna, no se inventa precio", r)
ok("Binance respondio 451" in motivo, "y se lee el 451 tal cual", motivo)
ok("CoinGecko respondio 403" in motivo and "Mercado Bitcoin respondio 500" in motivo,
   "con lo que dijo cada uno, no solo el primero", motivo)

# Bolivares no tiene mercado fuera del P2P.
r, motivo = main._precio_spot("VES")
ok(r is None and "fuera del P2P" in motivo,
   "para bolivares no hay mercado normal, y se dice", motivo)

print("\n== Y el P2P de reales solo se prueba si el mercado falla ==")
main._mercado_cache.clear()
pedidos_p2p = []


def anota_p2p(fiat, tipo, filas_n=20, sencillo=False):
    pedidos_p2p.append(fiat)
    return [anuncio(957, 1, 999999)], ""


main._pedir_tablon = anota_p2p
con_fuentes({"binance": b'{"price":"5.2713"}'})
r = main.mercado_p2p()
main.urlopen = _urlopen_guardado
ok(r["compraBRL"]["tasa"] == 5.2713 and r["compraBRL"]["fuente"] == "Binance",
   "los reales vienen del mercado", r["compraBRL"])
ok("BRL" not in pedidos_p2p,
   "y al P2P de reales ni se le pregunta: esa puerta esta cerrada", pedidos_p2p)
ok("VES" in pedidos_p2p, "los bolivares si siguen saliendo del P2P", pedidos_p2p)
ok(r["disponible"] is True and r["ventaVES"]["tasa"] == 957,
   "con los dos lados, disponible", r)

# Si el mercado falla, se cae al P2P y se cuentan los DOS motivos.
main._mercado_cache.clear()
pedidos_p2p2 = []


def solo_ves(fiat, tipo, filas_n=20, sencillo=False):
    pedidos_p2p2.append(fiat)
    if fiat == "BRL":
        return [], "Binance devolvio 0 anuncios de BRL"
    return [anuncio(957, 1, 999999)], ""


main._pedir_tablon = solo_ves
con_fuentes({})          # ningun sitio contesta
r = main.mercado_p2p()
main.urlopen = _urlopen_guardado
ok("BRL" in pedidos_p2p2, "si el mercado falla, SI se prueba el P2P", pedidos_p2p2)
m = r.get("motivoBRL", "")
ok("Binance" in m and "CoinGecko" in m,
   "y se cuenta lo que dijo cada sitio del mercado", m)
ok("y el P2P:" in m, "y tambien lo que dijo el P2P", m)

print("\n== La respuesta al navegador nunca deja la app sin datos ==")
main._mercado_cache.clear()
con_tablon(None, "Binance respondio 403")
r = main.mercado_p2p()
ok(r["ok"] is True and r["disponible"] is False,
   "Binance caido -> ok=True y disponible=False, nunca un error", r)
ok("403" in r.get("motivo", ""), "y dice por que, con el numero", r.get("motivo"))
ok(r.get("motivoBRL") and r.get("motivoVES"),
   "cada lado trae el suyo: los reales y los bolivares salen de sitios distintos", r)
ok("reales:" in r["motivo"] and "bolivares:" in r["motivo"],
   "y el resumen nombra los dos, porque ya no fallan por lo mismo", r["motivo"])
ok("y el P2P:" in r["motivoBRL"],
   "el de los reales cuenta el mercado Y el P2P, que son dos intentos", r["motivoBRL"])

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


def contar(fiat, tipo, *a, **k):
    llamadas["n"] += 1
    return [anuncio(5.12, 1, 999999)], ""


main._pedir_tablon = contar
main.mercado_p2p()
primero = llamadas["n"]
main.mercado_p2p()
ok(llamadas["n"] == primero, "la segunda lectura sale del cache", llamadas["n"])
main.mercado_p2p({"BRL": "9999"})
ok(llamadas["n"] > primero, "pero otro monto es otra lectura", llamadas["n"])

print("\n== Un fallo NO se guarda cinco minutos ==")
# El boton de la tarjeta dice "reintentar": con el cache largo devolvia el
# mismo fallo durante cinco minutos y pulsarlo parecia no hacer nada.
main._mercado_cache.clear()
fallos_n = {"n": 0}


def fallar(fiat, tipo, *a, **k):
    fallos_n["n"] += 1
    return None, "Binance respondio 403"


main._pedir_tablon = fallar
main.mercado_p2p()
antes = fallos_n["n"]
main.mercado_p2p()
ok(fallos_n["n"] == antes, "seguidas sale del cache: no se machaca a Binance")
# Se envejece el guardado justo por encima del cache corto y por debajo del largo.
clave = list(main._mercado_cache.keys())[0]
momento, resp = main._mercado_cache[clave]
main._mercado_cache[clave] = (momento - (main.MERCADO_CACHE_FALLO_SEG + 1), resp)
main.mercado_p2p()
ok(fallos_n["n"] > antes,
   "pero a los %ss se vuelve a preguntar, aunque el cache bueno sea de %ss"
   % (main.MERCADO_CACHE_FALLO_SEG, main.MERCADO_CACHE_SEG), fallos_n["n"])

# Y al reves: una lectura BUENA si aguanta los cinco minutos completos.
main._mercado_cache.clear()
main._pedir_tablon = contar
main.mercado_p2p()
buenas = llamadas["n"]
clave = list(main._mercado_cache.keys())[0]
momento, resp = main._mercado_cache[clave]
main._mercado_cache[clave] = (momento - (main.MERCADO_CACHE_FALLO_SEG + 1), resp)
main.mercado_p2p()
ok(llamadas["n"] == buenas,
   "una lectura buena no caduca a los %ss: esa si dura los %ss"
   % (main.MERCADO_CACHE_FALLO_SEG, main.MERCADO_CACHE_SEG), llamadas["n"])

print("\n== Las dos direcciones no se confunden ==")
# Ella COMPRA USDT con reales -> mira a quien VENDE (BUY).
# Ella VENDE USDT por bolivares -> mira a quien COMPRA (SELL).
vistos = []


def anotar(fiat, tipo, *a, **k):
    vistos.append((fiat, tipo))
    return [anuncio(1.0, 1, 999999999)], ""


main._pedir_tablon = anotar
main._mercado_cache.clear()
main.mercado_p2p()
ok(("BRL", "BUY") in vistos, "los reales se leen del lado de quien vende USDT", vistos)
ok(("VES", "SELL") in vistos, "y los bolivares del lado de quien los compra", vistos)

print("\n== Un tablon VACIO se cuenta con lo que dijo Binance ==")
# Brasil tiene cientos de anuncios a cualquier hora: un tablon vacio no es
# creible, y "no tiene anuncios" no se lo cree nadie. Se repite lo que dijo EL.
m = main._porque_vacio({"success": False, "code": "000002",
                        "message": "rate limited", "total": 0}, "BRL")
ok("BRL" in m, "se dice de que moneda era el tablon", m)
ok("no tuvo exito" in m, "y que Binance dijo que no tuvo exito", m)
ok("000002" in m, "con su codigo", m)
ok("rate limited" in m, "y su mensaje", m)
ok("total 0" in m, "y el total que declaro", m)
# Lo normal cuando de verdad no hay nada: exito, sin codigo raro, total 0.
m = main._porque_vacio({"success": True, "code": "000000",
                        "message": None, "total": 0}, "VES")
ok("000000" not in m and "VES" in m and "total 0" in m,
   "un vacio limpio no inventa codigos que no vinieron", m)
# Un total booleano no es un total.
m = main._porque_vacio({"total": True}, "BRL")
ok("total" not in m, "un true no se cuenta como un total", m)

# Y ese motivo tiene que llegar hasta arriba, no quedarse por el camino.
main._pedir_tablon = lambda fiat, tipo, *a, **k: (
    [], "Binance devolvio 0 anuncios de BRL (codigo 000002)")
ok("000002" in main._leer_mercado("BRL", "BUY", 1000)[1],
   "y el motivo del tablon vacio llega hasta la tarjeta", main._leer_mercado("BRL", "BUY", 1000)[1])

print("\n== Si ninguno acepta su monto, se baja al mayor que SI esten dando ==")
# El tablon SE MUEVE: el 29/09 por la noche dos anuncios de VES aceptaban sus
# 112.000 Bs; a la mañana siguiente habia 20 y ninguno. Quedarse sin numero es
# peor que dar uno diciendo a que monto se midio.
con_tablon([anuncio(950, 1000, 40000), anuncio(957, 1000, 45000),
            anuncio(960, 1000, 50000), anuncio(970, 1000, 30000)])
r = tasa("VES", "SELL", 112000)
ok(r is not None, "ya no se queda sin lectura", r)
ok(r and r["montoPedido"] == 112000,
   "y dice cual era el monto que se pidio", r)
# 50.000 lo acepta uno, 45.000 dos, 40.000 tres: gana 40.000, que es el mas
# cercano a sus 112.000 de los que llegan a la cuenta.
ok(r and r["monto"] == 40000,
   "se baja al monto mas CERCANO al suyo que acepten tres", r)
ok(r and r["anuncios"] == 3,
   "que son los que de verdad lo aceptan", r)
ok(main.MERCADO_MIN_ANUNCIOS == 3, "y el minimo son 3 anuncios")

# EL CASO DEL 30/09, que es el que costo: su monto era demasiado PEQUEÑO, no
# grande. Los anuncios de VES pedian minimos por encima de sus 112.000 Bs y la
# primera version se fue al techo del tablon —36.450.000 Bs, unos 38.000 USDT—,
# midiendole un mercado en el que no opera.
con_tablon([anuncio(950, 200000, 20000000), anuncio(952, 200000, 30000000),
            anuncio(954, 200000, 50000000), anuncio(956, 5000000, 50000000),
            anuncio(958, 5000000, 50000000)])
r = tasa("VES", "SELL", 112000)
ok(r and r["monto"] == 200000,
   "si su monto es demasiado PEQUEÑO se sube lo justo, no al techo del tablon", r)
ok(r and r["anuncios"] == 3, "con los que de verdad lo aceptan ahi", r)

# Si ni acercandose llegan a 3, manda el que tenga MAS anuncios; entre iguales,
# el mas cercano al suyo.
con_tablon([anuncio(950, 1000, 40000), anuncio(957, 1000, 45000)])
r = tasa("VES", "SELL", 112000)
ok(r and r["monto"] == 40000 and r["anuncios"] == 2,
   "con menos de tres, manda el que tenga MAS anuncios", r)
con_tablon([anuncio(950, 1000, 40000)])
r = tasa("VES", "SELL", 112000)
ok(r and r["monto"] == 40000 and r["anuncios"] == 1,
   "y con uno solo, ese, pero con su monto a la vista", r)
# Con la misma cantidad de anuncios arriba y abajo, gana el lado mas cercano.
con_tablon([anuncio(950, 10000, 50000), anuncio(951, 10000, 50000),
            anuncio(952, 10000, 50000),
            anuncio(960, 900000, 9000000), anuncio(961, 900000, 9000000),
            anuncio(962, 900000, 9000000)])
r = tasa("VES", "SELL", 112000)
ok(r and r["monto"] == 50000,
   "y empatados en anuncios, gana el monto que menos se aleja del suyo", r)

# Lo que NO puede pasar: que una lectura normal arrastre montoPedido, ni que se
# baje el monto cuando el suyo si se puede tomar.
con_tablon([anuncio(950, 1000, 999999), anuncio(957, 1000, 999999),
            anuncio(960, 1000, 999999)])
r = tasa("VES", "SELL", 112000)
ok(r and r["monto"] == 112000 and "montoPedido" not in r,
   "si su monto SI se puede tomar, no se baja ni se marca nada", r)
ok(r and r["tasa"] == 957, "y el precio sigue siendo la mediana", r)

print("\n== Un lado vacio se vuelve a pedir UNA vez, con la pregunta simple ==")
vistas = []


def sencillo_funciona(fiat, tipo, filas_n=20, sencillo=False):
    vistas.append(sencillo)
    return ([anuncio(5.12, 1, 999999)], "") if sencillo else ([], "vacio")


main._pedir_tablon = sencillo_funciona
r, motivo = main._leer_mercado("BRL", "BUY", 1000)
ok(vistas == [False, True], "primero como siempre, y si viene vacio, simple", vistas)
ok(r and r["tasa"] == 5.12, "si con la simple si hay anuncios, se usan", r)
ok(motivo == "", "y no queda motivo que contar: salio bien", motivo)

# Si tampoco, se dice que se intento: si no, parece que no se probo.
vistas2 = []


def vacio_siempre(fiat, tipo, filas_n=20, sencillo=False):
    if tipo == "SELL":          # el sondeo del otro lado, que no se cuenta aqui
        return [], ""
    vistas2.append(sencillo)
    return [], "Binance devolvio 0 anuncios de BRL"


main._pedir_tablon = vacio_siempre
r, motivo = main._leer_mercado("BRL", "BUY", 1000)
ok(vistas2 == [False, True], "se intenta una vez, no dos ni diez", vistas2)
ok("con la pregunta simple tampoco" in motivo,
   "y se dice que la simple tampoco: si no, parece que no se probo", motivo)

# Lo que NO puede pasar: gastar una llamada de mas cuando ya habia anuncios,
# ni cuando Binance no contesto (ahi el problema no es el cuerpo).
vistas3 = []


def con_anuncios(fiat, tipo, filas_n=20, sencillo=False):
    vistas3.append(sencillo)
    return [anuncio(5.12, 1, 999999)], ""


main._pedir_tablon = con_anuncios
main._leer_mercado("BRL", "BUY", 1000)
ok(vistas3 == [False], "una lectura buena no cuesta ni una llamada mas", vistas3)

vistas4 = []


def no_contesta(fiat, tipo, filas_n=20, sencillo=False):
    vistas4.append(sencillo)
    return None, "Binance respondio 403"


main._pedir_tablon = no_contesta
main._leer_mercado("BRL", "BUY", 1000)
ok(vistas4 == [False],
   "y si Binance no contesta no se reintenta: ahi el problema no es el cuerpo", vistas4)

print("\n== Un tablon vacio se sondea por el otro lado ==")
# El 30/09 los reales vinieron con CERO anuncios y "total 0" mientras los
# bolivares, desde el MISMO servidor, traian 20. Desde fuera "no hay tablon" y
# "solo se vacia este sentido" se ven igual; esto los separa.
pedidos = []


def solo_un_lado(fiat, tipo, filas_n=20, sencillo=False):
    pedidos.append((fiat, tipo, sencillo))
    return ([anuncio(5.12, 1, 999999)], "") if tipo == "SELL" else ([], "vacio")


main._pedir_tablon = solo_un_lado
m = porque("BRL", "BUY", 1000)
ok("SI trae 1 anuncios" in m,
   "si el otro sentido si trae anuncios, se dice: el tablon existe", m)
ok(("BRL", "SELL", True) in pedidos,
   "se sondea la MISMA moneda, al reves, y con el cuerpo simple", pedidos)
ok(len([p for p in pedidos if p[1] == "SELL"]) == 1,
   "una sola vez: es un diagnostico, no un dato", pedidos)


def ningun_lado(fiat, tipo, filas_n=20, sencillo=False):
    return [], "vacio"


main._pedir_tablon = ningun_lado
m = porque("BRL", "BUY", 1000)
ok("tambien viene vacio" in m,
   "y si el otro lado tambien esta vacio, eso es otra cosa y se dice", m)

# El sondeo NUNCA puede tumbar la lectura: es lo ultimo que se hace y lo menos
# importante de todo lo que hay aqui.
def sondeo_revienta(fiat, tipo, filas_n=20, sencillo=False):
    if tipo == "SELL":
        raise RuntimeError("me cai")
    return [], "vacio"


main._pedir_tablon = sondeo_revienta
m = porque("BRL", "BUY", 1000)
ok("no se pudo mirar" in m, "y si el sondeo revienta, se dice y se sigue", m)

# Y no se gasta una llamada de mas cuando hay lectura.
pedidos2 = []


def siempre_lleno(fiat, tipo, filas_n=20, sencillo=False):
    pedidos2.append(tipo)
    return [anuncio(5.12, 1, 999999)], ""


main._pedir_tablon = siempre_lleno
main._leer_mercado("BRL", "BUY", 1000)
ok(pedidos2 == ["BUY"], "con lectura buena no se sondea nada", pedidos2)

print("\n== El servidor no se presenta ante Binance como un robot ==")
# Aqui no hay internet, asi que se mira lo que SE IBA A MANDAR. Basta: lo que
# el filtro de Binance corta es esto, no lo que conteste despues.
import json as _json  # noqa: E402

capturado = {}


class _RespuestaFalsa:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"data": []}'


def _urlopen_falso(pedido, timeout=None):
    capturado["cabeceras"] = dict(pedido.headers)
    capturado["cuerpo"] = _json.loads(pedido.data.decode("utf-8"))
    capturado["url"] = pedido.full_url
    return _RespuestaFalsa()


_urlopen_real = main.urlopen
main.urlopen = _urlopen_falso
_TABLON_REAL("BRL", "BUY")
main.urlopen = _urlopen_real

# Request pone las cabeceras en Capitalizado, no como se escribieron.
cab = {k.lower(): v for k, v in capturado.get("cabeceras", {}).items()}
ua = cab.get("user-agent", "")
ok("Mozilla" in ua and "centrogestion" not in ua.lower(),
   "se presenta como un navegador, no con el nombre de la app", ua)
ok(cab.get("origin", "").endswith("binance.com"),
   "y manda el Origin que mandaria su propia pagina", cab.get("origin"))
ok("referer" in cab and "accept-language" in cab,
   "con Referer e idioma, como los manda un navegador de verdad", sorted(cab))
ok(capturado.get("cuerpo", {}).get("clientType") == "web",
   "y el cuerpo dice que viene de la web", capturado.get("cuerpo"))
ok(capturado.get("url") == main.BINANCE_P2P,
   "sin cambiar la direccion del tablon", capturado.get("url"))

# La pregunta simple quita los tres campos, pero NO las cabeceras: lo que se
# esta probando con ella es el cuerpo, y cambiar dos cosas a la vez no diria
# cual fue.
main.urlopen = _urlopen_falso
_TABLON_REAL("BRL", "BUY", sencillo=True)
main.urlopen = _urlopen_real
c2 = capturado.get("cuerpo", {})
ok("clientType" not in c2 and "payTypes" not in c2 and "publisherType" not in c2,
   "la pregunta simple va sin los tres campos de adorno", c2)
ok(c2.get("fiat") == "BRL" and c2.get("tradeType") == "BUY" and c2.get("asset") == "USDT",
   "pero sigue preguntando lo mismo", c2)
cab2 = {k.lower(): v for k, v in capturado.get("cabeceras", {}).items()}
ok("Mozilla" in cab2.get("user-agent", ""),
   "y con las mismas cabeceras: se prueba el cuerpo, no las dos cosas a la vez", cab2.get("user-agent"))

print("\n" + ("FALLARON %d prueba(s)" % len(fallos) if fallos else "Todo en orden."))
sys.exit(1 if fallos else 0)
