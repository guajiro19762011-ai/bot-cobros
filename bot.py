# -*- coding: utf-8 -*-
"""
BOT DE TELEGRAM PARA PUNTO DE COBRO
Inventario, compras, ventas de contado y a crédito, abonos, deudas y reportes.

Variables de entorno:
  BOT_TOKEN    -> token que te da @BotFather
  ALLOWED_IDS  -> IDs de Telegram autorizados, separados por coma (ej: 12345,67890)
  DB_PATH      -> (opcional) ruta del archivo de base de datos. Por defecto: cobros.db
"""
import os
import html
import logging
import sqlite3
import functools
import warnings
import unicodedata
from contextlib import contextmanager
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler, ConversationHandler,
                          CallbackQueryHandler, ContextTypes, filters)

warnings.filterwarnings("ignore", message=".*per_message.*")
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
DB = os.environ.get("DB_PATH", "cobros.db")
TZ = ZoneInfo("America/Bogota")
STOCK_BAJO = 3

# ───────────────────────── BASE DE DATOS ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS clientes(
  id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, telefono TEXT, creado TEXT);
CREATE TABLE IF NOT EXISTS productos(
  id INTEGER PRIMARY KEY, nombre TEXT NOT NULL,
  precio_venta INTEGER NOT NULL DEFAULT 0, precio_compra INTEGER NOT NULL DEFAULT 0,
  stock INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS compras(
  id INTEGER PRIMARY KEY, producto_id INTEGER REFERENCES productos(id),
  cantidad INTEGER, costo_unitario INTEGER, proveedor TEXT, fecha TEXT);
CREATE TABLE IF NOT EXISTS ventas(
  id INTEGER PRIMARY KEY, cliente_id INTEGER REFERENCES clientes(id),
  fecha TEXT, total INTEGER, tipo TEXT, fecha_vence TEXT, fecha_pago TEXT, nota TEXT);
CREATE TABLE IF NOT EXISTS venta_items(
  id INTEGER PRIMARY KEY, venta_id INTEGER REFERENCES ventas(id) ON DELETE CASCADE,
  producto_id INTEGER REFERENCES productos(id), cantidad INTEGER, precio INTEGER, costo INTEGER);
CREATE TABLE IF NOT EXISTS abonos(
  id INTEGER PRIMARY KEY, cliente_id INTEGER REFERENCES clientes(id), monto INTEGER, fecha TEXT);
CREATE TABLE IF NOT EXISTS abono_aplicaciones(
  abono_id INTEGER REFERENCES abonos(id) ON DELETE CASCADE,
  venta_id INTEGER REFERENCES ventas(id) ON DELETE CASCADE, monto INTEGER);
"""


@contextmanager
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript(SCHEMA)


# ───────────────────────── UTILIDADES ─────────────────────────
def hoy() -> date:
    return datetime.now(TZ).date()


def money(v) -> str:
    return "$" + f"{int(v or 0):,}".replace(",", ".")


def fd(iso) -> str:
    """ISO -> dd/mm/aaaa"""
    if not iso:
        return "-"
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


def esc(t) -> str:
    return html.escape(str(t or ""))


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return " ".join(t.lower().replace("?", " ").replace("¿", " ").split())


def parse_monto(t: str):
    """Acepta 50000, 50.000, $50.000, 50k, 1.5m"""
    t = t.lower().replace("$", "").replace(" ", "").strip()
    mult = 1
    if t.endswith("k"):
        mult, t = 1_000, t[:-1]
    elif t.endswith("m"):
        mult, t = 1_000_000, t[:-1]
    try:
        v = float(t.replace(",", ".")) * mult if mult > 1 else float(t.replace(".", "").replace(",", ""))
    except ValueError:
        return None
    return int(round(v)) if v > 0 else None


def parse_fecha(t: str, permitir_dias=False):
    t = norm(t)
    if t in ("hoy", "h"):
        return hoy()
    if t in ("ayer",):
        return hoy() - timedelta(days=1)
    if t in ("manana",):
        return hoy() + timedelta(days=1)
    if permitir_dias and t.isdigit():
        return hoy() + timedelta(days=int(t))
    for f in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d/%m"):
        try:
            d = datetime.strptime(t, f).date()
            return d.replace(year=hoy().year) if f == "%d/%m" else d
        except ValueError:
            pass
    return None


async def enviar(update: Update, texto: str, **kw):
    """Envía en HTML y parte mensajes largos (límite de Telegram 4096)."""
    msg = update.effective_message
    trozos, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > 3900:
            trozos.append(actual)
            actual = ""
        actual += linea + "\n"
    trozos.append(actual)
    for i, tr in enumerate(trozos):
        extra = kw if i == len(trozos) - 1 else {}
        await msg.reply_text(tr, parse_mode="HTML", **extra)


def autorizado(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **k):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in ALLOWED:
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"⛔ No autorizado. Tu ID de Telegram es {uid}. Agrégalo en ALLOWED_IDS.")
            return ConversationHandler.END
        return await func(update, context, *a, **k)
    return wrapper


TABLAS = {"cliente": "clientes", "producto": "productos"}


def buscar(tipo: str, texto: str):
    with db() as con:
        filas = con.execute(f"SELECT * FROM {TABLAS[tipo]} ORDER BY nombre").fetchall()
    n = norm(texto)
    return [f for f in filas if n in norm(f["nombre"])]


def obtener(tipo: str, id_):
    with db() as con:
        return con.execute(f"SELECT * FROM {TABLAS[tipo]} WHERE id=?", (id_,)).fetchone()


async def resolver(update: Update, tipo: str, texto: str):
    """Devuelve la fila si hay una sola coincidencia; si hay varias muestra botones."""
    matches = buscar(tipo, texto)
    exactos = [m for m in matches if norm(m["nombre"]) == norm(texto)]
    if len(exactos) == 1:
        return exactos[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sug = "/nuevocliente" if tipo == "cliente" else "/nuevoproducto"
        await update.message.reply_text(
            f"No encontré ningún {tipo} con «{texto}». Escribe otro nombre, o /cancelar y créalo con {sug}.")
        return None
    pref = "cli" if tipo == "cliente" else "prod"
    kb = [[InlineKeyboardButton(m["nombre"], callback_data=f"{pref}:{m['id']}")] for m in matches[:10]]
    await update.message.reply_text("Encontré varios, elige uno:", reply_markup=InlineKeyboardMarkup(kb))
    return None


async def btn_elegido(update: Update, tipo: str):
    q = update.callback_query
    await q.answer()
    fila = obtener(tipo, int(q.data.split(":")[1]))
    await q.edit_message_text(f"✔️ {fila['nombre']}")
    return fila


KB_HOY = ReplyKeyboardMarkup([["Hoy"]], resize_keyboard=True, one_time_keyboard=True)

# ───────────────────────── CÁLCULO DE DEUDAS ─────────────────────────
SALDO = "v.total - COALESCE((SELECT SUM(monto) FROM abono_aplicaciones ap WHERE ap.venta_id=v.id),0)"


def pendientes(con, cliente_id=None):
    q = f"""SELECT v.*, c.nombre AS cliente, {SALDO} AS saldo
            FROM ventas v JOIN clientes c ON c.id=v.cliente_id
            WHERE v.tipo='credito' AND v.fecha_pago IS NULL"""
    args = ()
    if cliente_id:
        q += " AND v.cliente_id=?"
        args = (cliente_id,)
    return con.execute(q + " ORDER BY v.fecha, v.id", args).fetchall()


def deuda_cliente(con, cliente_id) -> int:
    return sum(p["saldo"] for p in pendientes(con, cliente_id))


def aplicar_abono(con, cliente_id, monto, fecha_iso):
    """Registra el abono y lo reparte empezando por la deuda más antigua."""
    aid = con.execute("INSERT INTO abonos(cliente_id,monto,fecha) VALUES(?,?,?)",
                      (cliente_id, monto, fecha_iso)).lastrowid
    resto, pagadas = monto, []
    for p in pendientes(con, cliente_id):
        if resto <= 0:
            break
        m = min(resto, p["saldo"])
        con.execute("INSERT INTO abono_aplicaciones VALUES(?,?,?)", (aid, p["id"], m))
        resto -= m
        if m == p["saldo"]:
            con.execute("UPDATE ventas SET fecha_pago=? WHERE id=?", (fecha_iso, p["id"]))
            pagadas.append(p["id"])
    return aid, pagadas


def texto_deuda(con, cli) -> str:
    pend = pendientes(con, cli["id"])
    if not pend:
        return f"✅ <b>{esc(cli['nombre'])}</b> no debe nada."
    total = sum(p["saldo"] for p in pend)
    h = hoy().isoformat()
    lin = [f"👤 <b>{esc(cli['nombre'])}</b> debe <b>{money(total)}</b>", ""]
    for p in pend:
        vencida = p["fecha_vence"] and p["fecha_vence"] < h
        desc = f" ({esc(p['nota'])})" if p["nota"] else ""
        lin.append(f"• #{p['id']}{desc} · crédito {fd(p['fecha'])} · vence {fd(p['fecha_vence'])}"
                   f"{' ⚠️ <b>VENCIDA</b>' if vencida else ''}\n   Saldo {money(p['saldo'])} de {money(p['total'])}")
    ab = con.execute("SELECT * FROM abonos WHERE cliente_id=? ORDER BY fecha DESC, id DESC LIMIT 3",
                     (cli["id"],)).fetchall()
    if ab:
        lin.append("\n💰 Últimos abonos:")
        lin += [f"• {fd(a['fecha'])}: {money(a['monto'])} (abono #{a['id']})" for a in ab]
    return "\n".join(lin)


# ───────────────────────── COMANDOS BÁSICOS ─────────────────────────
AYUDA = """🤖 <b>Bot de Cobros</b>

<b>Registrar</b>
/nuevocliente – crear cliente
/nuevoproducto – crear producto
/compra – compra de mercancía (sube inventario)
/venta – venta de contado o a crédito
/fiar – deuda sin productos (fiado, préstamo o deuda anterior)
/abono – registrar pago o abono de un cliente

<b>Consultar</b>
/deuda nombre – cuánto debe un cliente
/cliente nombre – historial del cliente
/deudores – todos los que deben
/vencidos – créditos vencidos
/inventario – productos y stock
/clientes – lista de clientes
/resumen [hoy|semana|mes] – ventas, cobros y ganancia

<b>Corregir</b>
/anularventa número – anula una venta
/anularabono número – anula un abono
/respaldo – te envío la base de datos
/cancelar – cancela lo que estés haciendo

💬 También puedes escribir normal: <i>cuánto debe Juan</i>, <i>inventario</i>, <i>vencidos</i>, <i>deudores</i>."""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED:
        await update.message.reply_text(f"Hola 👋 Tu ID de Telegram es {uid}.\nAgrégalo en ALLOWED_IDS para usar el bot.")
        return
    await enviar(update, AYUDA)


async def miid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("❌ Cancelado.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ───────────────────────── NUEVO CLIENTE ─────────────────────────
NC_NOMBRE, NC_TEL = range(2)


@autorizado
async def nc_inicio(update, context):
    await update.message.reply_text("👤 Nombre del cliente:", reply_markup=ReplyKeyboardRemove())
    return NC_NOMBRE


async def nc_nombre(update, context):
    nombre = update.message.text.strip()
    if any(norm(c["nombre"]) == norm(nombre) for c in buscar("cliente", nombre)):
        await update.message.reply_text("Ya existe un cliente con ese nombre. Escribe otro (ej: agrega el apellido).")
        return NC_NOMBRE
    context.user_data["nc"] = nombre
    await update.message.reply_text("📞 Teléfono (o escribe - si no tiene):")
    return NC_TEL


async def nc_tel(update, context):
    tel = update.message.text.strip()
    tel = None if tel == "-" else tel
    nombre = context.user_data.pop("nc")
    with db() as con:
        con.execute("INSERT INTO clientes(nombre,telefono,creado) VALUES(?,?,?)", (nombre, tel, hoy().isoformat()))
    await enviar(update, f"✅ Cliente <b>{esc(nombre)}</b> creado.")
    return ConversationHandler.END


# ───────────────────────── NUEVO PRODUCTO ─────────────────────────
NP_NOMBRE, NP_PRECIO, NP_STOCK = range(3)


@autorizado
async def np_inicio(update, context):
    await update.message.reply_text("📦 Nombre del producto:", reply_markup=ReplyKeyboardRemove())
    return NP_NOMBRE


async def np_nombre(update, context):
    nombre = update.message.text.strip()
    if any(norm(p["nombre"]) == norm(nombre) for p in buscar("producto", nombre)):
        await update.message.reply_text("Ese producto ya existe. Escribe otro nombre.")
        return NP_NOMBRE
    context.user_data["np"] = {"nombre": nombre}
    await update.message.reply_text("💲 Precio de VENTA por unidad (ej: 15000 o 15k):")
    return NP_PRECIO


async def np_precio(update, context):
    v = parse_monto(update.message.text)
    if not v:
        await update.message.reply_text("Escribe un valor válido, ej: 15000")
        return NP_PRECIO
    context.user_data["np"]["precio"] = v
    await update.message.reply_text("🔢 ¿Cuántas unidades tienes ahora? (0 si las vas a registrar con /compra)")
    return NP_STOCK


async def np_stock(update, context):
    t = update.message.text.strip()
    if not t.isdigit():
        await update.message.reply_text("Escribe un número entero, ej: 10")
        return NP_STOCK
    d = context.user_data.pop("np")
    with db() as con:
        con.execute("INSERT INTO productos(nombre,precio_venta,stock) VALUES(?,?,?)", (d["nombre"], d["precio"], int(t)))
    await enviar(update, f"✅ Producto <b>{esc(d['nombre'])}</b> creado · precio {money(d['precio'])} · stock {t}")
    return ConversationHandler.END


# ───────────────────────── COMPRA (entrada de inventario) ─────────────────────────
C_PROD, C_CANT, C_COSTO, C_PROV, C_FECHA = range(5)


@autorizado
async def c_inicio(update, context):
    context.user_data["compra"] = {}
    await update.message.reply_text("🚚 Nueva compra. ¿Qué producto compraste?", reply_markup=ReplyKeyboardRemove())
    return C_PROD


async def _c_set_prod(update, context, p):
    context.user_data["compra"]["prod"] = dict(p)
    await update.effective_message.reply_text(f"{p['nombre']} (stock actual {p['stock']}). ¿Cuántas unidades compraste?")
    return C_CANT


async def c_prod_txt(update, context):
    p = await resolver(update, "producto", update.message.text.strip())
    return await _c_set_prod(update, context, p) if p else C_PROD


async def c_prod_btn(update, context):
    return await _c_set_prod(update, context, await btn_elegido(update, "producto"))


async def c_cant(update, context):
    t = update.message.text.strip()
    if not t.isdigit() or int(t) <= 0:
        await update.message.reply_text("Escribe un número mayor que 0")
        return C_CANT
    context.user_data["compra"]["cant"] = int(t)
    await update.message.reply_text("💲 Costo por UNIDAD (lo que te costó cada una):")
    return C_COSTO


async def c_costo(update, context):
    v = parse_monto(update.message.text)
    if not v:
        await update.message.reply_text("Escribe un valor válido, ej: 8000")
        return C_COSTO
    context.user_data["compra"]["costo"] = v
    await update.message.reply_text("🏪 ¿A qué proveedor? (o - si no importa)")
    return C_PROV


async def c_prov(update, context):
    t = update.message.text.strip()
    context.user_data["compra"]["prov"] = None if t == "-" else t
    await update.message.reply_text("📅 Fecha de la compra (dd/mm/aaaa) o toca Hoy:", reply_markup=KB_HOY)
    return C_FECHA


async def c_fecha(update, context):
    f = parse_fecha(update.message.text)
    if not f:
        await update.message.reply_text("Fecha no válida. Usa dd/mm/aaaa o Hoy")
        return C_FECHA
    c = context.user_data.pop("compra")
    p = c["prod"]
    with db() as con:
        con.execute("INSERT INTO compras(producto_id,cantidad,costo_unitario,proveedor,fecha) VALUES(?,?,?,?,?)",
                    (p["id"], c["cant"], c["costo"], c["prov"], f.isoformat()))
        con.execute("UPDATE productos SET stock=stock+?, precio_compra=? WHERE id=?", (c["cant"], c["costo"], p["id"]))
        nuevo = con.execute("SELECT stock FROM productos WHERE id=?", (p["id"],)).fetchone()["stock"]
    await enviar(update, f"✅ Compra registrada: {c['cant']} × <b>{esc(p['nombre'])}</b> a {money(c['costo'])}"
                         f" = {money(c['cant'] * c['costo'])}\n📅 {f.strftime('%d/%m/%Y')} · Stock ahora: {nuevo}",
                 reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ───────────────────────── VENTA ─────────────────────────
V_CLI, V_PROD, V_CANT, V_TIPO, V_VENCE = range(5)
SIN_CLIENTE = "Sin cliente (contado)"


@autorizado
async def v_inicio(update, context):
    context.user_data["venta"] = {"cliente": None, "items": []}
    kb = ReplyKeyboardMarkup([[SIN_CLIENTE]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("🛒 Nueva venta. ¿A qué cliente? Escribe el nombre:", reply_markup=kb)
    return V_CLI


async def v_cli_txt(update, context):
    t = update.message.text.strip()
    if t == SIN_CLIENTE:
        return await v_pedir_prod(update, context)
    c = await resolver(update, "cliente", t)
    if not c:
        return V_CLI
    context.user_data["venta"]["cliente"] = dict(c)
    return await v_pedir_prod(update, context)


async def v_cli_btn(update, context):
    context.user_data["venta"]["cliente"] = dict(await btn_elegido(update, "cliente"))
    return await v_pedir_prod(update, context)


async def v_pedir_prod(update, context):
    v = context.user_data["venta"]
    if v["items"]:
        kb = ReplyKeyboardMarkup([["✅ Listo"]], resize_keyboard=True)
        txt = "📦 Escribe otro producto o toca ✅ Listo para terminar."
    else:
        kb, txt = ReplyKeyboardRemove(), "📦 ¿Qué producto lleva?"
    await update.effective_message.reply_text(txt, reply_markup=kb)
    return V_PROD


async def _v_set_prod(update, context, p):
    context.user_data["venta"]["actual"] = dict(p)
    await update.effective_message.reply_text(
        f"{p['nombre']} · precio {money(p['precio_venta'])} · stock {p['stock']}\n"
        f"¿Cuántas unidades? (para otro precio escribe: 2 15000)")
    return V_CANT


async def v_prod_txt(update, context):
    t = update.message.text.strip()
    v = context.user_data["venta"]
    if "listo" in t.lower():
        if not v["items"]:
            await update.message.reply_text("Agrega al menos un producto.")
            return V_PROD
        return await v_pedir_tipo(update, context)
    p = await resolver(update, "producto", t)
    return await _v_set_prod(update, context, p) if p else V_PROD


async def v_prod_btn(update, context):
    return await _v_set_prod(update, context, await btn_elegido(update, "producto"))


async def v_cant(update, context):
    v = context.user_data["venta"]
    p = v["actual"]
    partes = update.message.text.split()
    if not partes or not partes[0].isdigit() or int(partes[0]) <= 0:
        await update.message.reply_text("Escribe la cantidad, ej: 2  (o 2 15000 para cambiar el precio)")
        return V_CANT
    cant = int(partes[0])
    precio = p["precio_venta"]
    if len(partes) > 1:
        precio = parse_monto(partes[1])
        if not precio:
            await update.message.reply_text("Precio no válido. Ej: 2 15000")
            return V_CANT
    ya = sum(i["cantidad"] for i in v["items"] if i["producto_id"] == p["id"])
    if cant + ya > p["stock"]:
        await update.message.reply_text(
            f"⚠️ Solo hay {p['stock'] - ya} en stock. Pon menos, o /cancelar y registra la mercancía con /compra.")
        return V_CANT
    v["items"].append({"producto_id": p["id"], "nombre": p["nombre"], "cantidad": cant,
                       "precio": precio, "costo": p["precio_compra"]})
    total = sum(i["cantidad"] * i["precio"] for i in v["items"])
    await update.message.reply_text(f"➕ {cant} × {p['nombre']} = {money(cant * precio)}\nTotal: {money(total)}")
    return await v_pedir_prod(update, context)


async def v_pedir_tipo(update, context):
    v = context.user_data["venta"]
    total = sum(i["cantidad"] * i["precio"] for i in v["items"])
    det = "\n".join(f"• {i['cantidad']} × {esc(i['nombre'])} = {money(i['cantidad'] * i['precio'])}" for i in v["items"])
    cli = esc(v["cliente"]["nombre"]) if v["cliente"] else "Sin cliente"
    await enviar(update, f"🧾 <b>Resumen</b>\nCliente: {cli}\n{det}\n<b>Total: {money(total)}</b>",
                 reply_markup=ReplyKeyboardRemove())
    if not v["cliente"]:
        return await v_guardar(update, context, "contado", None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💵 Contado", callback_data="tipo:contado"),
                                InlineKeyboardButton("📒 Crédito", callback_data="tipo:credito")]])
    await update.effective_message.reply_text("¿Cómo paga?", reply_markup=kb)
    return V_TIPO


async def v_tipo_btn(update, context):
    q = update.callback_query
    await q.answer()
    tipo = q.data.split(":")[1]
    await q.edit_message_text("💵 Contado" if tipo == "contado" else "📒 Crédito")
    if tipo == "contado":
        return await v_guardar(update, context, "contado", None)
    await q.message.reply_text("📅 ¿Hasta cuándo tiene para pagar?\nEscribe la fecha (dd/mm/aaaa) o los días de plazo (ej: 15).")
    return V_VENCE


async def v_vence(update, context):
    f = parse_fecha(update.message.text, permitir_dias=True)
    if not f or f < hoy():
        await update.message.reply_text("Fecha no válida (debe ser hoy o futura). Ej: 30/12/2026 o 15")
        return V_VENCE
    return await v_guardar(update, context, "credito", f)


async def v_guardar(update, context, tipo, vence):
    v = context.user_data.pop("venta")
    total = sum(i["cantidad"] * i["precio"] for i in v["items"])
    h = hoy().isoformat()
    cid = v["cliente"]["id"] if v["cliente"] else None
    with db() as con:
        vid = con.execute(
            "INSERT INTO ventas(cliente_id,fecha,total,tipo,fecha_vence,fecha_pago) VALUES(?,?,?,?,?,?)",
            (cid, h, total, tipo, vence.isoformat() if vence else None, h if tipo == "contado" else None)).lastrowid
        for i in v["items"]:
            con.execute("INSERT INTO venta_items(venta_id,producto_id,cantidad,precio,costo) VALUES(?,?,?,?,?)",
                        (vid, i["producto_id"], i["cantidad"], i["precio"], i["costo"]))
            con.execute("UPDATE productos SET stock=stock-? WHERE id=?", (i["cantidad"], i["producto_id"]))
        extra = ""
        if tipo == "credito":
            extra = (f"\n📅 Vence: {vence.strftime('%d/%m/%Y')}"
                     f"\n💳 Deuda total de {esc(v['cliente']['nombre'])}: {money(deuda_cliente(con, cid))}")
    await enviar(update, f"✅ Venta #{vid} guardada ({'contado' if tipo == 'contado' else 'crédito'}) · "
                         f"{money(total)}{extra}", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ───────────────────────── FIAR (deuda sin productos) ─────────────────────────
F_CLI, F_MONTO, F_NOTA, F_FECHA, F_VENCE = range(5)


@autorizado
async def f_inicio(update, context):
    context.user_data["fiar"] = {}
    await update.message.reply_text("📒 Registrar deuda sin productos (fiado, préstamo o deuda anterior).\n¿Qué cliente?",
                                    reply_markup=ReplyKeyboardRemove())
    return F_CLI


async def _f_set_cli(update, context, c):
    context.user_data["fiar"]["cli"] = dict(c)
    await update.effective_message.reply_text(f"Cliente: {c['nombre']}\n💲 ¿Cuánto es la deuda?")
    return F_MONTO


async def f_cli_txt(update, context):
    c = await resolver(update, "cliente", update.message.text.strip())
    return await _f_set_cli(update, context, c) if c else F_CLI


async def f_cli_btn(update, context):
    return await _f_set_cli(update, context, await btn_elegido(update, "cliente"))


async def f_monto(update, context):
    v = parse_monto(update.message.text)
    if not v:
        await update.message.reply_text("Escribe un valor válido, ej: 200000 o 200k")
        return F_MONTO
    context.user_data["fiar"]["monto"] = v
    await update.message.reply_text("📝 Descripción corta (ej: préstamo, mercado, deuda vieja) o -")
    return F_NOTA


async def f_nota(update, context):
    t = update.message.text.strip()
    context.user_data["fiar"]["nota"] = None if t == "-" else t
    await update.message.reply_text("📅 ¿En qué fecha se dio el crédito? (dd/mm/aaaa) o toca Hoy", reply_markup=KB_HOY)
    return F_FECHA


async def f_fecha(update, context):
    f = parse_fecha(update.message.text)
    if not f or f > hoy():
        await update.message.reply_text("Fecha no válida (no puede ser futura). Ej: 01/11/2026 o Hoy")
        return F_FECHA
    context.user_data["fiar"]["fecha"] = f
    await update.message.reply_text("⏳ ¿Fecha límite de pago? (dd/mm/aaaa) o días de plazo (ej: 30)",
                                    reply_markup=ReplyKeyboardRemove())
    return F_VENCE


async def f_vence(update, context):
    d = context.user_data["fiar"]
    t = update.message.text.strip()
    if t.isdigit():
        v = d["fecha"] + timedelta(days=int(t))
    else:
        v = parse_fecha(t)
    if not v or v < d["fecha"]:
        await update.message.reply_text("Fecha no válida. Debe ser igual o posterior a la fecha del crédito.")
        return F_VENCE
    context.user_data.pop("fiar")
    with db() as con:
        vid = con.execute("INSERT INTO ventas(cliente_id,fecha,total,tipo,fecha_vence,nota) VALUES(?,?,?,?,?,?)",
                          (d["cli"]["id"], d["fecha"].isoformat(), d["monto"], "credito", v.isoformat(), d["nota"])).lastrowid
        total = deuda_cliente(con, d["cli"]["id"])
    await enviar(update, f"✅ Deuda #{vid} registrada a <b>{esc(d['cli']['nombre'])}</b>: {money(d['monto'])}\n"
                         f"📅 Crédito {d['fecha'].strftime('%d/%m/%Y')} · vence {v.strftime('%d/%m/%Y')}\n"
                         f"💳 Deuda total: {money(total)}")
    return ConversationHandler.END


# ───────────────────────── ABONO ─────────────────────────
A_CLI, A_MONTO, A_FECHA = range(3)


@autorizado
async def a_inicio(update, context):
    context.user_data["abono"] = {}
    await update.message.reply_text("💰 Registrar abono. ¿Qué cliente paga?", reply_markup=ReplyKeyboardRemove())
    return A_CLI


async def _a_set_cli(update, context, c):
    with db() as con:
        deuda = deuda_cliente(con, c["id"])
    if deuda <= 0:
        await update.effective_message.reply_text(f"✅ {c['nombre']} no tiene deudas pendientes.")
        context.user_data.pop("abono", None)
        return ConversationHandler.END
    context.user_data["abono"] = {"cli": dict(c), "deuda": deuda}
    kb = ReplyKeyboardMarkup([[f"Todo ({money(deuda)})"]], resize_keyboard=True, one_time_keyboard=True)
    await update.effective_message.reply_text(f"{c['nombre']} debe {money(deuda)}.\n💲 ¿Cuánto abona?", reply_markup=kb)
    return A_MONTO


async def a_cli_txt(update, context):
    c = await resolver(update, "cliente", update.message.text.strip())
    return await _a_set_cli(update, context, c) if c else A_CLI


async def a_cli_btn(update, context):
    return await _a_set_cli(update, context, await btn_elegido(update, "cliente"))


async def a_monto(update, context):
    d = context.user_data["abono"]
    t = update.message.text.strip()
    v = d["deuda"] if t.lower().startswith("todo") else parse_monto(t)
    if not v:
        await update.message.reply_text("Escribe un valor válido, ej: 50000")
        return A_MONTO
    if v > d["deuda"]:
        await update.message.reply_text(f"⚠️ El abono supera la deuda ({money(d['deuda'])}). Escribe un valor menor o igual.")
        return A_MONTO
    d["monto"] = v
    await update.message.reply_text("📅 Fecha del pago (dd/mm/aaaa) o toca Hoy", reply_markup=KB_HOY)
    return A_FECHA


async def a_fecha(update, context):
    f = parse_fecha(update.message.text)
    if not f or f > hoy():
        await update.message.reply_text("Fecha no válida (no puede ser futura).")
        return A_FECHA
    d = context.user_data.pop("abono")
    with db() as con:
        aid, pagadas = aplicar_abono(con, d["cli"]["id"], d["monto"], f.isoformat())
        resta = deuda_cliente(con, d["cli"]["id"])
    txt = (f"✅ Abono #{aid} de {money(d['monto'])} de <b>{esc(d['cli']['nombre'])}</b> ({f.strftime('%d/%m/%Y')})\n")
    if pagadas:
        txt += f"🎉 Créditos pagados completos: {', '.join('#' + str(p) for p in pagadas)}\n"
    txt += f"💳 Queda debiendo: <b>{money(resta)}</b>" if resta else "🏁 ¡Quedó a paz y salvo!"
    await enviar(update, txt, reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ───────────────────────── CONSULTAS ─────────────────────────
async def consulta_deuda(update, nombre):
    if not nombre:
        await update.effective_message.reply_text("Escribe el nombre, ej: /deuda Juan")
        return
    m = buscar("cliente", nombre)
    if not m:
        await update.effective_message.reply_text(f"No encontré clientes con «{nombre}».")
        return
    with db() as con:
        textos = [texto_deuda(con, c) for c in m[:5]]
    await enviar(update, "\n\n──────────\n\n".join(textos))


async def consulta_cliente(update, nombre):
    if not nombre:
        await update.effective_message.reply_text("Escribe el nombre, ej: /cliente Juan")
        return
    m = buscar("cliente", nombre)
    if not m:
        await update.effective_message.reply_text(f"No encontré clientes con «{nombre}».")
        return
    c = m[0]
    with db() as con:
        ventas = con.execute("SELECT * FROM ventas WHERE cliente_id=? ORDER BY fecha DESC, id DESC LIMIT 8", (c["id"],)).fetchall()
        tot = con.execute("SELECT COALESCE(SUM(total),0) t, COUNT(*) n FROM ventas WHERE cliente_id=?", (c["id"],)).fetchone()
        deuda_txt = texto_deuda(con, c)
    lin = [f"📇 <b>{esc(c['nombre'])}</b>", f"📞 {esc(c['telefono'] or '-')} · cliente desde {fd(c['creado'])}",
           f"🛒 {tot['n']} compras por {money(tot['t'])} en total", "", "<b>Últimos movimientos:</b>"]
    for v in ventas:
        estado = "pagada " + fd(v["fecha_pago"]) if v["fecha_pago"] else "pendiente"
        lin.append(f"• #{v['id']} {fd(v['fecha'])} · {v['tipo']} · {money(v['total'])} · {estado}")
    if len(m) > 1:
        lin.append(f"\n(También coinciden: {', '.join(esc(x['nombre']) for x in m[1:6])})")
    await enviar(update, "\n".join(lin) + "\n\n" + deuda_txt)


async def consulta_deudores(update):
    with db() as con:
        pend = pendientes(con)
    if not pend:
        await enviar(update, "✅ Nadie debe nada.")
        return
    h = hoy().isoformat()
    por = {}
    for p in pend:
        d = por.setdefault(p["cliente"], {"saldo": 0, "vencido": 0})
        d["saldo"] += p["saldo"]
        if p["fecha_vence"] and p["fecha_vence"] < h:
            d["vencido"] += p["saldo"]
    orden = sorted(por.items(), key=lambda x: -x[1]["saldo"])
    lin = [f"📋 <b>Deudores ({len(orden)})</b>", ""]
    for n, d in orden:
        lin.append(f"• {esc(n)}: <b>{money(d['saldo'])}</b>" + (f" (⚠️ vencido {money(d['vencido'])})" if d["vencido"] else ""))
    total = sum(d["saldo"] for _, d in orden)
    venc = sum(d["vencido"] for _, d in orden)
    lin += ["", f"💳 Cartera total: <b>{money(total)}</b>", f"⚠️ Vencido: <b>{money(venc)}</b>"]
    await enviar(update, "\n".join(lin))


def lista_vencidos():
    h = hoy()
    with db() as con:
        return [p for p in pendientes(con) if p["fecha_vence"] and p["fecha_vence"] < h.isoformat()]


async def consulta_vencidos(update):
    v = lista_vencidos()
    if not v:
        await enviar(update, "✅ No hay créditos vencidos.")
        return
    h = hoy()
    lin = ["⚠️ <b>Créditos vencidos</b>", ""]
    for p in sorted(v, key=lambda x: x["fecha_vence"]):
        dias = (h - date.fromisoformat(p["fecha_vence"])).days
        lin.append(f"• {esc(p['cliente'])} · #{p['id']} · {money(p['saldo'])} · venció {fd(p['fecha_vence'])} ({dias} días)")
    lin.append(f"\nTotal vencido: <b>{money(sum(p['saldo'] for p in v))}</b>")
    await enviar(update, "\n".join(lin))


async def consulta_inventario(update):
    with db() as con:
        prods = con.execute("SELECT * FROM productos ORDER BY nombre").fetchall()
    if not prods:
        await enviar(update, "No hay productos. Crea uno con /nuevoproducto")
        return
    lin = ["📦 <b>Inventario</b>", ""]
    valor = 0
    for p in prods:
        alerta = " 🔴" if p["stock"] <= STOCK_BAJO else ""
        lin.append(f"• {esc(p['nombre'])}: <b>{p['stock']}</b> und · venta {money(p['precio_venta'])}{alerta}")
        valor += max(p["stock"], 0) * p["precio_compra"]
    lin.append(f"\n💼 Valor del inventario (a costo): {money(valor)}\n🔴 = quedan {STOCK_BAJO} o menos")
    await enviar(update, "\n".join(lin))


async def consulta_resumen(update, periodo="hoy"):
    h = hoy()
    periodo = norm(periodo) or "hoy"
    if periodo.startswith("sem"):
        ini, titulo = h - timedelta(days=h.weekday()), "esta semana"
    elif periodo.startswith("mes"):
        ini, titulo = h.replace(day=1), "este mes"
    else:
        ini, titulo = h, "hoy"
    a, b = ini.isoformat(), h.isoformat()
    with db() as con:
        r = con.execute("""SELECT COUNT(*) n,
                 COALESCE(SUM(CASE WHEN tipo='contado' THEN total END),0) contado,
                 COALESCE(SUM(CASE WHEN tipo='credito' THEN total END),0) credito
                 FROM ventas WHERE fecha BETWEEN ? AND ?""", (a, b)).fetchone()
        gan = con.execute("""SELECT COALESCE(SUM(i.cantidad*(i.precio-i.costo)),0) g
                 FROM venta_items i JOIN ventas v ON v.id=i.venta_id WHERE v.fecha BETWEEN ? AND ?""", (a, b)).fetchone()["g"]
        abonos = con.execute("SELECT COALESCE(SUM(monto),0) s FROM abonos WHERE fecha BETWEEN ? AND ?", (a, b)).fetchone()["s"]
        compras = con.execute("SELECT COALESCE(SUM(cantidad*costo_unitario),0) s FROM compras WHERE fecha BETWEEN ? AND ?", (a, b)).fetchone()["s"]
        cartera = sum(p["saldo"] for p in pendientes(con))
    txt = (f"📊 <b>Resumen de {titulo}</b> ({ini.strftime('%d/%m')} – {h.strftime('%d/%m')})\n\n"
           f"🛒 Ventas: {r['n']}\n"
           f"💵 De contado: {money(r['contado'])}\n"
           f"📒 A crédito / fiado: {money(r['credito'])}\n"
           f"💰 Abonos cobrados: {money(abonos)}\n"
           f"🏦 <b>Dinero que entró: {money(r['contado'] + abonos)}</b>\n"
           f"🚚 Compras de mercancía: {money(compras)}\n"
           f"📈 Ganancia aprox. en productos: {money(gan)}\n\n"
           f"💳 Cartera pendiente total: {money(cartera)}")
    await enviar(update, txt)


async def consulta_clientes(update):
    with db() as con:
        cl = con.execute("SELECT * FROM clientes ORDER BY nombre").fetchall()
        deudas = {c["id"]: deuda_cliente(con, c["id"]) for c in cl}
    if not cl:
        await enviar(update, "No hay clientes. Crea uno con /nuevocliente")
        return
    lin = [f"👥 <b>Clientes ({len(cl)})</b>", ""]
    lin += [f"• {esc(c['nombre'])}" + (f" — debe {money(deudas[c['id']])}" if deudas[c["id"]] else "") for c in cl]
    await enviar(update, "\n".join(lin))


# Comandos que llaman a las consultas
@autorizado
async def cmd_deuda(update, context): await consulta_deuda(update, " ".join(context.args))
@autorizado
async def cmd_cliente(update, context): await consulta_cliente(update, " ".join(context.args))
@autorizado
async def cmd_deudores(update, context): await consulta_deudores(update)
@autorizado
async def cmd_vencidos(update, context): await consulta_vencidos(update)
@autorizado
async def cmd_inventario(update, context): await consulta_inventario(update)
@autorizado
async def cmd_clientes(update, context): await consulta_clientes(update)
@autorizado
async def cmd_resumen(update, context): await consulta_resumen(update, " ".join(context.args))


# ───────────────────────── CORRECCIONES ─────────────────────────
@autorizado
async def cmd_anular_venta(update, context):
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Uso: /anularventa 15")
        return
    vid = int(context.args[0].lstrip("#"))
    with db() as con:
        v = con.execute("SELECT * FROM ventas WHERE id=?", (vid,)).fetchone()
        if not v:
            await update.message.reply_text(f"No existe la venta #{vid}.")
            return
        ab = con.execute("SELECT DISTINCT abono_id FROM abono_aplicaciones WHERE venta_id=?", (vid,)).fetchall()
        if ab:
            await update.message.reply_text("Esta venta tiene abonos. Anula primero: " +
                                            ", ".join(f"/anularabono {a['abono_id']}" for a in ab))
            return
        for i in con.execute("SELECT * FROM venta_items WHERE venta_id=?", (vid,)).fetchall():
            con.execute("UPDATE productos SET stock=stock+? WHERE id=?", (i["cantidad"], i["producto_id"]))
        con.execute("DELETE FROM ventas WHERE id=?", (vid,))
    await update.message.reply_text(f"🗑️ Venta #{vid} anulada y el inventario devuelto.")


@autorizado
async def cmd_anular_abono(update, context):
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Uso: /anularabono 7")
        return
    aid = int(context.args[0].lstrip("#"))
    with db() as con:
        a = con.execute("SELECT * FROM abonos WHERE id=?", (aid,)).fetchone()
        if not a:
            await update.message.reply_text(f"No existe el abono #{aid}.")
            return
        afectadas = [r["venta_id"] for r in con.execute("SELECT venta_id FROM abono_aplicaciones WHERE abono_id=?", (aid,))]
        con.execute("DELETE FROM abonos WHERE id=?", (aid,))
        for vid in afectadas:
            con.execute("UPDATE ventas SET fecha_pago=NULL WHERE id=? AND tipo='credito'", (vid,))
    await update.message.reply_text(f"🗑️ Abono #{aid} de {money(a['monto'])} anulado. La deuda volvió a quedar pendiente.")


@autorizado
async def cmd_respaldo(update, context):
    with open(DB, "rb") as f:
        await update.message.reply_document(f, filename=f"respaldo_{hoy().isoformat()}.db",
                                            caption="💾 Respaldo de tu base de datos. Guárdalo bien.")


# ───────────────────────── TEXTO LIBRE ─────────────────────────
PALABRAS_RELLENO = {"cuanto", "cuanta", "debe", "deuda", "de", "del", "la", "el", "me", "mi", "que", "esta", "tiene", "a", "le"}


@autorizado
async def texto_libre(update, context):
    original = update.message.text.strip()
    t = norm(original)
    palabras = t.split()
    if not palabras:
        return
    if "debe" in palabras or "deuda" in palabras or "deben" in palabras:
        if "deben" in palabras or "quienes" in palabras or "quien" in palabras:
            return await consulta_deudores(update)
        nombre = " ".join(p for p in palabras if p not in PALABRAS_RELLENO)
        return await (consulta_deuda(update, nombre) if nombre else consulta_deudores(update))
    if palabras[0] == "cliente" and len(palabras) > 1:
        return await consulta_cliente(update, " ".join(palabras[1:]))
    if any(p in t for p in ("inventario", "stock", "productos")):
        return await consulta_inventario(update)
    if "deudores" in t or "cartera" in t:
        return await consulta_deudores(update)
    if "vencid" in t:
        return await consulta_vencidos(update)
    if "resumen" in t or "ventas" in t:
        per = "semana" if "semana" in t else "mes" if "mes" in t else "hoy"
        return await consulta_resumen(update, per)
    if "clientes" in t:
        return await consulta_clientes(update)
    # Si escribió solo un nombre de cliente, mostrar su deuda
    if buscar("cliente", original):
        return await consulta_deuda(update, original)
    await enviar(update, "No entendí 🤔\n\n" + AYUDA)


# ───────────────────────── RECORDATORIO DIARIO ─────────────────────────
async def recordatorio(context: ContextTypes.DEFAULT_TYPE):
    h = hoy()
    with db() as con:
        pend = pendientes(con)
    vencen_hoy = [p for p in pend if p["fecha_vence"] == h.isoformat()]
    manana = [p for p in pend if p["fecha_vence"] == (h + timedelta(days=1)).isoformat()]
    vencidos = [p for p in pend if p["fecha_vence"] and p["fecha_vence"] < h.isoformat()]
    if not (vencen_hoy or manana or vencidos):
        return
    lin = [f"⏰ <b>Cobros del día {h.strftime('%d/%m/%Y')}</b>"]
    if vencen_hoy:
        lin += ["", "📌 Vencen HOY:"] + [f"• {esc(p['cliente'])}: {money(p['saldo'])}" for p in vencen_hoy]
    if manana:
        lin += ["", "🔜 Vencen mañana:"] + [f"• {esc(p['cliente'])}: {money(p['saldo'])}" for p in manana]
    if vencidos:
        lin += ["", f"⚠️ Vencidos: {len(vencidos)} créditos por {money(sum(p['saldo'] for p in vencidos))} (ver /vencidos)"]
    for uid in ALLOWED:
        try:
            await context.bot.send_message(uid, "\n".join(lin), parse_mode="HTML")
        except Exception as e:
            log.warning("No pude enviar recordatorio a %s: %s", uid, e)


async def respaldo_diario(context: ContextTypes.DEFAULT_TYPE):
    """Todas las noches manda la base de datos por Telegram como copia de seguridad."""
    for uid in ALLOWED:
        try:
            with open(DB, "rb") as f:
                await context.bot.send_document(uid, f, filename=f"respaldo_{hoy().isoformat()}.db",
                                                caption="💾 Respaldo automático del día. Guárdalo, no lo borres.")
        except Exception as e:
            log.warning("No pude enviar respaldo a %s: %s", uid, e)


# ───────────────────────── ARRANQUE ─────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("venta", "Registrar venta"), BotCommand("abono", "Registrar abono/pago"),
        BotCommand("deuda", "Deuda de un cliente"), BotCommand("deudores", "Todos los que deben"),
        BotCommand("vencidos", "Créditos vencidos"), BotCommand("fiar", "Deuda sin productos"),
        BotCommand("compra", "Compra de mercancía"), BotCommand("inventario", "Ver inventario"),
        BotCommand("resumen", "Resumen hoy/semana/mes"), BotCommand("cliente", "Historial de cliente"),
        BotCommand("clientes", "Lista de clientes"), BotCommand("nuevocliente", "Crear cliente"),
        BotCommand("nuevoproducto", "Crear producto"), BotCommand("respaldo", "Copia de la base de datos"),
        BotCommand("cancelar", "Cancelar operación"), BotCommand("ayuda", "Ver ayuda"),
    ])


def main():
    if not TOKEN:
        raise SystemExit("Falta la variable BOT_TOKEN")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    TXT = filters.TEXT & ~filters.COMMAND
    fb = [CommandHandler("cancelar", cancelar)]

    def conv(cmd, inicio, estados):
        app.add_handler(ConversationHandler(entry_points=[CommandHandler(cmd, inicio)], states=estados,
                                            fallbacks=fb, allow_reentry=True))

    conv("nuevocliente", nc_inicio, {NC_NOMBRE: [MessageHandler(TXT, nc_nombre)], NC_TEL: [MessageHandler(TXT, nc_tel)]})
    conv("nuevoproducto", np_inicio, {NP_NOMBRE: [MessageHandler(TXT, np_nombre)],
                                      NP_PRECIO: [MessageHandler(TXT, np_precio)], NP_STOCK: [MessageHandler(TXT, np_stock)]})
    conv("compra", c_inicio, {C_PROD: [MessageHandler(TXT, c_prod_txt), CallbackQueryHandler(c_prod_btn, "^prod:")],
                              C_CANT: [MessageHandler(TXT, c_cant)], C_COSTO: [MessageHandler(TXT, c_costo)],
                              C_PROV: [MessageHandler(TXT, c_prov)], C_FECHA: [MessageHandler(TXT, c_fecha)]})
    conv("venta", v_inicio, {V_CLI: [MessageHandler(TXT, v_cli_txt), CallbackQueryHandler(v_cli_btn, "^cli:")],
                             V_PROD: [MessageHandler(TXT, v_prod_txt), CallbackQueryHandler(v_prod_btn, "^prod:")],
                             V_CANT: [MessageHandler(TXT, v_cant)],
                             V_TIPO: [CallbackQueryHandler(v_tipo_btn, "^tipo:")],
                             V_VENCE: [MessageHandler(TXT, v_vence)]})
    conv("fiar", f_inicio, {F_CLI: [MessageHandler(TXT, f_cli_txt), CallbackQueryHandler(f_cli_btn, "^cli:")],
                            F_MONTO: [MessageHandler(TXT, f_monto)], F_NOTA: [MessageHandler(TXT, f_nota)],
                            F_FECHA: [MessageHandler(TXT, f_fecha)], F_VENCE: [MessageHandler(TXT, f_vence)]})
    conv("abono", a_inicio, {A_CLI: [MessageHandler(TXT, a_cli_txt), CallbackQueryHandler(a_cli_btn, "^cli:")],
                             A_MONTO: [MessageHandler(TXT, a_monto)], A_FECHA: [MessageHandler(TXT, a_fecha)]})

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", start))
    app.add_handler(CommandHandler("miid", miid))
    app.add_handler(CommandHandler("deuda", cmd_deuda))
    app.add_handler(CommandHandler("cliente", cmd_cliente))
    app.add_handler(CommandHandler("deudores", cmd_deudores))
    app.add_handler(CommandHandler("vencidos", cmd_vencidos))
    app.add_handler(CommandHandler("inventario", cmd_inventario))
    app.add_handler(CommandHandler("clientes", cmd_clientes))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("anularventa", cmd_anular_venta))
    app.add_handler(CommandHandler("anularabono", cmd_anular_abono))
    app.add_handler(CommandHandler("respaldo", cmd_respaldo))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(MessageHandler(TXT, texto_libre))

    if app.job_queue:
        app.job_queue.run_daily(recordatorio, time=time(7, 30, tzinfo=TZ))     # aviso diario 7:30 a.m.
        app.job_queue.run_daily(respaldo_diario, time=time(22, 0, tzinfo=TZ))  # respaldo 10:00 p.m.

    log.info("Bot iniciado ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
