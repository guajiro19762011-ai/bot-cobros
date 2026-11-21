# -*- coding: utf-8 -*-
"""
BOT DE TELEGRAM PARA PUNTO DE COBRO
Menú con botones · Inventario · Compras · Ventas/pedidos de contado y a crédito
Abonos · Deudas · Reportes · Avisos diarios · Respaldo automático

Variables de entorno:
  BOT_TOKEN    -> token que te da @BotFather
  ALLOWED_IDS  -> IDs de Telegram autorizados, separados por coma (ej: 12345,67890)
  DB_PATH      -> (opcional) ruta del archivo de base de datos. Por defecto: cobros.db
"""
import os
import asyncio
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
                      ReplyKeyboardMarkup, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler, ConversationHandler,
                          CallbackQueryHandler, ContextTypes, filters)

warnings.filterwarnings("ignore", message=".*per_message.*")
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
DB = os.environ.get("DB_PATH", "cobros.db")
TZ = ZoneInfo("America/Bogota")
STOCK_BAJO = 3
END = ConversationHandler.END

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

# Columnas nuevas que se agregan solas a bases de datos viejas
MIGRACIONES = [("clientes", "apellido", "TEXT"), ("clientes", "direccion", "TEXT")]


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
        for tabla, col, tipo in MIGRACIONES:
            cols = [r["name"] for r in con.execute(f"PRAGMA table_info({tabla})")]
            if col not in cols:
                con.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")


# Clientes: "nombre" devuelve el nombre completo (nombre + apellido)
SQL_CLIENTES = """SELECT id, TRIM(nombre || ' ' || COALESCE(apellido,'')) AS nombre,
                  nombre AS nombres, apellido, direccion, telefono, creado FROM clientes"""
NOMBRE_CLI = "TRIM(c.nombre || ' ' || COALESCE(c.apellido,''))"

# ───────────────────────── BOTONES ─────────────────────────
B_VENTA, B_ABONO = "🛒 Venta / Pedido", "💰 Registrar abono"
B_CLIENTE, B_PRODUCTO = "👤 Nuevo cliente", "📦 Nuevo producto"
B_COMPRA, B_FIAR = "🚚 Compra de mercancía", "📒 Fiar / Préstamo"
B_DEUDAS, B_REPORTES = "💳 Deudas", "📊 Reportes"
B_BUSCAR, B_AYUDA = "🔎 Buscar cliente", "❓ Ayuda"
B_CANCELAR, B_OMITIR, B_HOY, B_LISTO = "❌ Cancelar", "⏭️ Omitir", "📅 Hoy", "✅ Listo"
B_SIN_CLIENTE, B_TODO = "🚶 Sin cliente (contado)", "💯 Paga todo"

MENU = ReplyKeyboardMarkup([
    [B_VENTA, B_ABONO],
    [B_CLIENTE, B_PRODUCTO],
    [B_COMPRA, B_FIAR],
    [B_DEUDAS, B_REPORTES],
    [B_BUSCAR, B_AYUDA],
], resize_keyboard=True, is_persistent=True)

BOTONES_MENU = [B_VENTA, B_ABONO, B_CLIENTE, B_PRODUCTO, B_COMPRA, B_FIAR,
                B_DEUDAS, B_REPORTES, B_BUSCAR, B_AYUDA]


def teclado(*filas):
    """Teclado de un paso: las opciones que se pasen + el botón Cancelar."""
    return ReplyKeyboardMarkup([list(f) for f in filas if f] + [[B_CANCELAR]], resize_keyboard=True)


def en_columnas(nombres, n=2, maximo=20):
    nombres = list(nombres)[:maximo]
    return [nombres[i:i + n] for i in range(0, len(nombres), n)]


# ───────────────────────── UTILIDADES ─────────────────────────
def hoy() -> date:
    return datetime.now(TZ).date()


def money(v) -> str:
    return "$" + f"{int(v or 0):,}".replace(",", ".")


def fd(iso) -> str:
    if not iso:
        return "-"
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


def esc(t) -> str:
    return html.escape(str(t if t is not None else ""))


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
    t = norm(t.replace("📅", ""))
    if t in ("hoy", "h"):
        return hoy()
    if t == "ayer":
        return hoy() - timedelta(days=1)
    if t == "manana":
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


def opcional(t: str):
    t = t.strip()
    return None if t in (B_OMITIR, "-", "") else t


async def enviar(update: Update, texto: str, **kw):
    """Envía en HTML, parte mensajes largos y deja el menú puesto si no se indica otro teclado."""
    kw.setdefault("reply_markup", MENU)
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


async def preguntar(update: Update, texto: str, kb=None):
    await update.effective_message.reply_text(texto, parse_mode="HTML", reply_markup=kb or teclado())


def autorizado(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **k):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in ALLOWED:
            if update.callback_query:
                await update.callback_query.answer()
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"⛔ No autorizado. Tu ID de Telegram es {uid}. Agrégalo en ALLOWED_IDS.")
            return END
        return await func(update, context, *a, **k)
    return wrapper


# ───────────────────────── BÚSQUEDAS ─────────────────────────
def todos(tipo):
    with db() as con:
        if tipo == "cliente":
            return con.execute(SQL_CLIENTES + " ORDER BY nombre, apellido").fetchall()
        return con.execute("SELECT * FROM productos ORDER BY nombre").fetchall()


def buscar(tipo: str, texto: str):
    n = norm(texto)
    return [f for f in todos(tipo) if n and n in norm(f["nombre"])]


def obtener(tipo: str, id_):
    with db() as con:
        if tipo == "cliente":
            return con.execute(SQL_CLIENTES + " WHERE id=?", (id_,)).fetchone()
        return con.execute("SELECT * FROM productos WHERE id=?", (id_,)).fetchone()


def teclado_lista(tipo, extra=None):
    """Teclado con los clientes o productos como botones (si no son demasiados)."""
    filas = todos(tipo)
    botones = en_columnas([f["nombre"] for f in filas]) if len(filas) <= 20 else []
    return teclado(*([extra] if extra else []), *botones)


async def resolver(update: Update, tipo: str, texto: str):
    """Devuelve la fila si hay una sola coincidencia; si hay varias muestra botones para elegir."""
    matches = buscar(tipo, texto)
    exactos = [m for m in matches if norm(m["nombre"]) == norm(texto)]
    if len(exactos) == 1:
        return exactos[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        boton = B_CLIENTE if tipo == "cliente" else B_PRODUCTO
        await update.message.reply_text(
            f"No encontré ningún {tipo} con «{texto}». Escribe otro nombre, "
            f"o toca {B_CANCELAR} y créalo con {boton}.")
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


# ───────────────────────── CÁLCULO DE DEUDAS ─────────────────────────
SALDO = "v.total - COALESCE((SELECT SUM(monto) FROM abono_aplicaciones ap WHERE ap.venta_id=v.id),0)"


def pendientes(con, cliente_id=None):
    q = f"""SELECT v.*, {NOMBRE_CLI} AS cliente, {SALDO} AS saldo
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
    resto, aplicado = monto, []
    for p in pendientes(con, cliente_id):
        if resto <= 0:
            break
        m = min(resto, p["saldo"])
        con.execute("INSERT INTO abono_aplicaciones VALUES(?,?,?)", (aid, p["id"], m))
        resto -= m
        pagada = m == p["saldo"]
        if pagada:
            con.execute("UPDATE ventas SET fecha_pago=? WHERE id=?", (fecha_iso, p["id"]))
        aplicado.append((p["id"], m, pagada))
    return aid, aplicado


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


# ───────────────────────── FICHAS (lo que quedó guardado) ─────────────────────────
def ficha_cliente(cid) -> str:
    c = obtener("cliente", cid)
    with db() as con:
        deuda = deuda_cliente(con, cid)
    return (f"📋 <b>Cliente #{c['id']} guardado</b>\n\n"
            f"👤 Nombre: <b>{esc(c['nombres'])}</b>\n"
            f"👤 Apellido: {esc(c['apellido'] or '-')}\n"
            f"🏠 Dirección: {esc(c['direccion'] or '-')}\n"
            f"📞 Teléfono: {esc(c['telefono'] or '-')}\n"
            f"📅 Registrado: {fd(c['creado'])}\n"
            f"💳 Deuda actual: {money(deuda)}")


def ficha_producto(pid, titulo="Producto") -> str:
    p = obtener("producto", pid)
    gan = p["precio_venta"] - p["precio_compra"]
    pct = f" ({gan * 100 // p['precio_compra']}%)" if p["precio_compra"] else ""
    return (f"📋 <b>{titulo} #{p['id']}</b>\n\n"
            f"📦 Nombre: <b>{esc(p['nombre'])}</b>\n"
            f"💵 Costo por unidad: {money(p['precio_compra'])}\n"
            f"🏷️ Precio de venta: {money(p['precio_venta'])}\n"
            f"📈 Ganancia por unidad: {money(gan)}{pct}\n"
            f"🔢 Unidades en stock: <b>{p['stock']}</b>"
            f"{' 🔴 stock bajo' if p['stock'] <= STOCK_BAJO else ''}\n"
            f"💼 Valor del inventario: {money(max(p['stock'], 0) * p['precio_compra'])}")


def ficha_venta(vid) -> str:
    with db() as con:
        v = con.execute(f"""SELECT v.*, {NOMBRE_CLI} AS cliente, {SALDO} AS saldo
                            FROM ventas v LEFT JOIN clientes c ON c.id=v.cliente_id WHERE v.id=?""",
                        (vid,)).fetchone()
        items = con.execute("""SELECT i.*, p.nombre FROM venta_items i JOIN productos p ON p.id=i.producto_id
                               WHERE i.venta_id=?""", (vid,)).fetchall()
        deuda = deuda_cliente(con, v["cliente_id"]) if v["cliente_id"] else 0
    titulo = f"Pedido #{v['id']} guardado" if items else f"Deuda #{v['id']} guardada"
    lin = [f"🧾 <b>{titulo}</b>", "",
           f"📅 Fecha: {fd(v['fecha'])}",
           f"👤 Cliente: {esc(v['cliente'] or 'Sin cliente')}"]
    if v["nota"]:
        lin.append(f"📝 Detalle: {esc(v['nota'])}")
    if items:
        lin.append("")
        lin += [f"• {i['cantidad']} × {esc(i['nombre'])} a {money(i['precio'])} = {money(i['cantidad'] * i['precio'])}"
                for i in items]
    lin += ["", f"💲 <b>Total: {money(v['total'])}</b>"]
    if v["tipo"] == "contado":
        lin.append("💵 Pago: de contado (pagado)")
    else:
        lin += ["📒 Pago: a crédito",
                f"⏳ Fecha límite de pago: <b>{fd(v['fecha_vence'])}</b>",
                f"💳 Deuda total del cliente: <b>{money(deuda)}</b>"]
    return "\n".join(lin)


# ───────────────────────── INICIO / AYUDA / CANCELAR ─────────────────────────
AYUDA = f"""🤖 <b>Bot de Cobros</b>

Usa los botones de abajo 👇

{B_VENTA} – vender de contado o a crédito
{B_ABONO} – cuando un cliente paga
{B_CLIENTE} – nombre, apellido, dirección y teléfono
{B_PRODUCTO} – nombre, costo, precio y cantidad
{B_COMPRA} – mercancía que compras (sube el stock)
{B_FIAR} – deuda sin productos o deuda vieja del cuaderno
{B_DEUDAS} – quién debe, cuánto y qué está vencido
{B_REPORTES} – ventas, cobros, ganancia e inventario
{B_BUSCAR} – ficha e historial de un cliente

En cualquier momento toca {B_CANCELAR} para salir.
También puedes escribir: <i>cuánto debe Juan</i>, <i>inventario</i>, <i>vencidos</i>.

<b>Correcciones:</b> /anularventa número · /anularabono número
<b>Copia de seguridad:</b> /respaldo"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED:
        await update.message.reply_text(f"Hola 👋 Tu ID de Telegram es {uid}.\nAgrégalo en ALLOWED_IDS para usar el bot.")
        return END
    context.user_data.clear()
    await enviar(update, AYUDA)
    return END


async def miid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("❌ Cancelado. ¿Qué quieres hacer?", reply_markup=MENU)
    return END


# Estados de la conversación (todos distintos)
(NC_NOMBRE, NC_APELLIDO, NC_DIR, NC_TEL,
 NP_NOMBRE, NP_COSTO, NP_PRECIO, NP_STOCK,
 C_PROD, C_CANT, C_COSTO, C_PROV, C_FECHA,
 V_CLI, V_PROD, V_CANT, V_TIPO, V_VENCE,
 F_CLI, F_MONTO, F_NOTA, F_FECHA, F_VENCE,
 A_CLI, A_MONTO, A_FECHA,
 Q_DEUDA, Q_CLIENTE) = range(28)


# ───────────────────────── NUEVO CLIENTE ─────────────────────────
@autorizado
async def nc_inicio(update, context):
    context.user_data["nc"] = {}
    await preguntar(update, "👤 <b>Nuevo cliente</b> (paso 1 de 4)\n\n¿Cuál es el <b>nombre</b>?")
    return NC_NOMBRE


async def nc_nombre(update, context):
    t = update.message.text.strip()
    if len(t) < 2:
        await preguntar(update, "Escribe un nombre válido.")
        return NC_NOMBRE
    context.user_data["nc"]["nombre"] = t.title()
    await preguntar(update, "Paso 2 de 4\n\n¿Cuál es el <b>apellido</b>?", teclado([B_OMITIR]))
    return NC_APELLIDO


async def nc_apellido(update, context):
    d = context.user_data["nc"]
    ap = opcional(update.message.text)
    d["apellido"] = ap.title() if ap else None
    completo = norm(f"{d['nombre']} {d['apellido'] or ''}")
    if any(norm(c["nombre"]) == completo for c in todos("cliente")):
        await preguntar(update, "⚠️ Ya existe un cliente con ese nombre y apellido.\n"
                                "Escribe otro apellido (o agrega un segundo apellido):", teclado([B_OMITIR]))
        return NC_APELLIDO
    await preguntar(update, "Paso 3 de 4\n\n🏠 ¿Cuál es la <b>dirección</b>? (puede ser barrio y casa)",
                    teclado([B_OMITIR]))
    return NC_DIR


async def nc_dir(update, context):
    context.user_data["nc"]["direccion"] = opcional(update.message.text)
    await preguntar(update, "Paso 4 de 4\n\n📞 ¿Cuál es el <b>teléfono</b>?", teclado([B_OMITIR]))
    return NC_TEL


async def nc_tel(update, context):
    tel = opcional(update.message.text)
    if tel and sum(ch.isdigit() for ch in tel) < 7:
        await preguntar(update, "Ese teléfono parece incompleto. Escríbelo de nuevo (ej: 3001234567):",
                        teclado([B_OMITIR]))
        return NC_TEL
    d = context.user_data.pop("nc")
    with db() as con:
        cid = con.execute("INSERT INTO clientes(nombre,apellido,direccion,telefono,creado) VALUES(?,?,?,?,?)",
                          (d["nombre"], d["apellido"], d["direccion"], tel, hoy().isoformat())).lastrowid
    await enviar(update, "✅ " + ficha_cliente(cid))
    return END


# ───────────────────────── NUEVO PRODUCTO ─────────────────────────
@autorizado
async def np_inicio(update, context):
    context.user_data["np"] = {}
    await preguntar(update, "📦 <b>Nuevo producto</b> (paso 1 de 4)\n\n¿Cuál es el <b>nombre</b> del producto?")
    return NP_NOMBRE


async def np_nombre(update, context):
    nombre = update.message.text.strip()
    if any(norm(p["nombre"]) == norm(nombre) for p in todos("producto")):
        await preguntar(update, "⚠️ Ese producto ya existe. Escribe otro nombre:")
        return NP_NOMBRE
    context.user_data["np"]["nombre"] = nombre[:1].upper() + nombre[1:]
    await preguntar(update, "Paso 2 de 4\n\n💵 ¿Cuánto te <b>cuesta</b> cada unidad? (lo que pagas al comprarla)",
                    teclado([B_OMITIR]))
    return NP_COSTO


async def np_costo(update, context):
    t = update.message.text.strip()
    if opcional(t) is None:
        v = 0
    else:
        v = parse_monto(t)
        if not v:
            await preguntar(update, "Escribe un valor válido, ej: 8000 o 8k", teclado([B_OMITIR]))
            return NP_COSTO
    context.user_data["np"]["costo"] = v
    await preguntar(update, "Paso 3 de 4\n\n🏷️ ¿A cuánto lo <b>vendes</b> por unidad?")
    return NP_PRECIO


async def np_precio(update, context):
    v = parse_monto(update.message.text)
    if not v:
        await preguntar(update, "Escribe un valor válido, ej: 15000 o 15k")
        return NP_PRECIO
    d = context.user_data["np"]
    if d["costo"] and v < d["costo"]:
        await update.message.reply_text(f"⚠️ Ojo: lo vendes por debajo de lo que te cuesta ({money(d['costo'])}).")
    d["precio"] = v
    await preguntar(update, "Paso 4 de 4\n\n🔢 ¿Cuántas <b>unidades</b> tienes ahora?", teclado(["0"]))
    return NP_STOCK


async def np_stock(update, context):
    t = update.message.text.strip()
    if not t.isdigit():
        await preguntar(update, "Escribe un número entero, ej: 10", teclado(["0"]))
        return NP_STOCK
    d = context.user_data.pop("np")
    with db() as con:
        pid = con.execute("INSERT INTO productos(nombre,precio_compra,precio_venta,stock) VALUES(?,?,?,?)",
                          (d["nombre"], d["costo"], d["precio"], int(t))).lastrowid
    await enviar(update, "✅ " + ficha_producto(pid, "Producto guardado"))
    return END


# ───────────────────────── COMPRA DE MERCANCÍA ─────────────────────────
@autorizado
async def c_inicio(update, context):
    if not todos("producto"):
        await enviar(update, f"Primero crea tus productos con {B_PRODUCTO}")
        return END
    context.user_data["compra"] = {}
    await preguntar(update, "🚚 <b>Compra de mercancía</b>\n\n¿Qué <b>producto</b> compraste?", teclado_lista("producto"))
    return C_PROD


async def _c_set_prod(update, context, p):
    context.user_data["compra"]["prod"] = dict(p)
    await preguntar(update, f"📦 {esc(p['nombre'])} (stock actual: {p['stock']})\n\n¿Cuántas <b>unidades</b> compraste?")
    return C_CANT


async def c_prod_txt(update, context):
    p = await resolver(update, "producto", update.message.text.strip())
    return await _c_set_prod(update, context, p) if p else C_PROD


async def c_prod_btn(update, context):
    return await _c_set_prod(update, context, await btn_elegido(update, "producto"))


async def c_cant(update, context):
    t = update.message.text.strip()
    if not t.isdigit() or int(t) <= 0:
        await preguntar(update, "Escribe un número mayor que 0")
        return C_CANT
    c = context.user_data["compra"]
    c["cant"] = int(t)
    anterior = c["prod"]["precio_compra"]
    kb = teclado([money(anterior)]) if anterior else teclado()
    await preguntar(update, "💵 ¿Cuánto te costó <b>cada unidad</b>?", kb)
    return C_COSTO


async def c_costo(update, context):
    v = parse_monto(update.message.text)
    if not v:
        await preguntar(update, "Escribe un valor válido, ej: 8000")
        return C_COSTO
    context.user_data["compra"]["costo"] = v
    await preguntar(update, "🏪 ¿A qué <b>proveedor</b> le compraste?", teclado([B_OMITIR]))
    return C_PROV


async def c_prov(update, context):
    context.user_data["compra"]["prov"] = opcional(update.message.text)
    await preguntar(update, "📅 ¿<b>Fecha</b> de la compra? (dd/mm/aaaa)", teclado([B_HOY, "Ayer"]))
    return C_FECHA


async def c_fecha(update, context):
    f = parse_fecha(update.message.text)
    if not f or f > hoy():
        await preguntar(update, "Fecha no válida. Usa dd/mm/aaaa o toca Hoy", teclado([B_HOY, "Ayer"]))
        return C_FECHA
    c = context.user_data.pop("compra")
    p = c["prod"]
    with db() as con:
        cid = con.execute("INSERT INTO compras(producto_id,cantidad,costo_unitario,proveedor,fecha) VALUES(?,?,?,?,?)",
                          (p["id"], c["cant"], c["costo"], c["prov"], f.isoformat())).lastrowid
        con.execute("UPDATE productos SET stock=stock+?, precio_compra=? WHERE id=?", (c["cant"], c["costo"], p["id"]))
    texto = (f"✅ 📋 <b>Compra #{cid} guardada</b>\n\n"
             f"📦 Producto: <b>{esc(p['nombre'])}</b>\n"
             f"🔢 Cantidad: {c['cant']}\n"
             f"💵 Costo por unidad: {money(c['costo'])}\n"
             f"💲 <b>Total pagado: {money(c['cant'] * c['costo'])}</b>\n"
             f"🏪 Proveedor: {esc(c['prov'] or '-')}\n"
             f"📅 Fecha: {f.strftime('%d/%m/%Y')}\n\n"
             f"🔢 Stock antes: {p['stock']} → ahora: <b>{p['stock'] + c['cant']}</b>")
    await enviar(update, texto)
    return END


# ───────────────────────── VENTA / PEDIDO ─────────────────────────
@autorizado
async def v_inicio(update, context):
    if not todos("producto"):
        await enviar(update, f"Primero crea tus productos con {B_PRODUCTO}")
        return END
    context.user_data["venta"] = {"cliente": None, "items": []}
    await preguntar(update, "🛒 <b>Nueva venta / pedido</b>\n\n¿A qué <b>cliente</b>? Toca uno o escribe el nombre:",
                    teclado_lista("cliente", [B_SIN_CLIENTE]))
    return V_CLI


async def v_cli_txt(update, context):
    t = update.message.text.strip()
    if t == B_SIN_CLIENTE:
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
        txt = "📦 Toca otro <b>producto</b> o <b>✅ Listo</b> para terminar:"
        kb = teclado_lista("producto", [B_LISTO])
    else:
        txt = "📦 ¿Qué <b>producto</b> lleva?"
        kb = teclado_lista("producto")
    await preguntar(update, txt, kb)
    return V_PROD


async def _v_set_prod(update, context, p):
    context.user_data["venta"]["actual"] = dict(p)
    await preguntar(update, f"📦 {esc(p['nombre'])} · precio {money(p['precio_venta'])} · stock {p['stock']}\n\n"
                            f"¿Cuántas <b>unidades</b>?\n<i>(Para otro precio escribe cantidad y precio: 2 15000)</i>",
                    teclado(["1", "2", "3", "5", "10"]))
    return V_CANT


async def v_prod_txt(update, context):
    t = update.message.text.strip()
    v = context.user_data["venta"]
    if t == B_LISTO or norm(t) == "listo":
        if not v["items"]:
            await preguntar(update, "Agrega al menos un producto.", teclado_lista("producto"))
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
        await preguntar(update, "Escribe la cantidad, ej: 2  (o 2 15000 para cambiar el precio)",
                        teclado(["1", "2", "3", "5", "10"]))
        return V_CANT
    cant = int(partes[0])
    precio = p["precio_venta"]
    if len(partes) > 1:
        precio = parse_monto(partes[1])
        if not precio:
            await preguntar(update, "Precio no válido. Ej: 2 15000")
            return V_CANT
    ya = sum(i["cantidad"] for i in v["items"] if i["producto_id"] == p["id"])
    if cant + ya > p["stock"]:
        await preguntar(update, f"⚠️ Solo hay {p['stock'] - ya} en stock. Pon una cantidad menor, "
                                f"o cancela y registra la mercancía con {B_COMPRA}.")
        return V_CANT
    v["items"].append({"producto_id": p["id"], "nombre": p["nombre"], "cantidad": cant,
                       "precio": precio, "costo": p["precio_compra"]})
    total = sum(i["cantidad"] * i["precio"] for i in v["items"])
    await update.message.reply_text(f"➕ {cant} × {p['nombre']} = {money(cant * precio)}\n🧮 Total hasta ahora: {money(total)}")
    return await v_pedir_prod(update, context)


async def v_pedir_tipo(update, context):
    v = context.user_data["venta"]
    if not v["cliente"]:
        return await v_guardar(update, context, "contado", None)
    total = sum(i["cantidad"] * i["precio"] for i in v["items"])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💵 Contado", callback_data="tipo:contado"),
                                InlineKeyboardButton("📒 Crédito", callback_data="tipo:credito")]])
    await preguntar(update, f"🧮 Total: <b>{money(total)}</b>")
    await update.effective_message.reply_text("¿Cómo <b>paga</b>?", parse_mode="HTML", reply_markup=kb)
    return V_TIPO


async def v_tipo_btn(update, context):
    q = update.callback_query
    await q.answer()
    tipo = q.data.split(":")[1]
    await q.edit_message_text("💵 Contado" if tipo == "contado" else "📒 Crédito")
    if tipo == "contado":
        return await v_guardar(update, context, "contado", None)
    await preguntar(update, "⏳ ¿Hasta cuándo tiene para <b>pagar</b>?\nToca un plazo o escribe la fecha (dd/mm/aaaa):",
                    teclado(["8 días", "15 días", "30 días"]))
    return V_VENCE


async def v_vence(update, context):
    t = update.message.text.replace("días", "").replace("dias", "").strip()
    f = parse_fecha(t, permitir_dias=True)
    if not f or f < hoy():
        await preguntar(update, "Fecha no válida (hoy o después). Ej: 30/12/2026 o 15",
                        teclado(["8 días", "15 días", "30 días"]))
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
    await enviar(update, "✅ " + ficha_venta(vid))
    return END


# ───────────────────────── FIAR / PRÉSTAMO (deuda sin productos) ─────────────────────────
@autorizado
async def f_inicio(update, context):
    if not todos("cliente"):
        await enviar(update, f"Primero crea el cliente con {B_CLIENTE}")
        return END
    context.user_data["fiar"] = {}
    await preguntar(update, "📒 <b>Fiar / préstamo / deuda vieja</b> (paso 1 de 5)\n\n¿A qué <b>cliente</b>?",
                    teclado_lista("cliente"))
    return F_CLI


async def _f_set_cli(update, context, c):
    context.user_data["fiar"]["cli"] = dict(c)
    await preguntar(update, f"👤 {esc(c['nombre'])}\n\nPaso 2 de 5\n💲 ¿De cuánto es la <b>deuda</b>?")
    return F_MONTO


async def f_cli_txt(update, context):
    c = await resolver(update, "cliente", update.message.text.strip())
    return await _f_set_cli(update, context, c) if c else F_CLI


async def f_cli_btn(update, context):
    return await _f_set_cli(update, context, await btn_elegido(update, "cliente"))


async def f_monto(update, context):
    v = parse_monto(update.message.text)
    if not v:
        await preguntar(update, "Escribe un valor válido, ej: 200000 o 200k")
        return F_MONTO
    context.user_data["fiar"]["monto"] = v
    await preguntar(update, "Paso 3 de 5\n📝 ¿Qué es? (detalle corto)",
                    teclado(["Préstamo", "Mercado", "Deuda vieja"], [B_OMITIR]))
    return F_NOTA


async def f_nota(update, context):
    context.user_data["fiar"]["nota"] = opcional(update.message.text)
    await preguntar(update, "Paso 4 de 5\n📅 ¿En qué <b>fecha</b> se dio el crédito? (dd/mm/aaaa)",
                    teclado([B_HOY, "Ayer"]))
    return F_FECHA


async def f_fecha(update, context):
    f = parse_fecha(update.message.text)
    if not f or f > hoy():
        await preguntar(update, "Fecha no válida (no puede ser futura). Ej: 01/11/2026", teclado([B_HOY, "Ayer"]))
        return F_FECHA
    context.user_data["fiar"]["fecha"] = f
    await preguntar(update, "Paso 5 de 5\n⏳ ¿<b>Fecha límite</b> de pago? Toca un plazo o escribe la fecha:",
                    teclado(["8 días", "15 días", "30 días"]))
    return F_VENCE


async def f_vence(update, context):
    d = context.user_data["fiar"]
    t = update.message.text.replace("días", "").replace("dias", "").strip()
    v = d["fecha"] + timedelta(days=int(t)) if t.isdigit() else parse_fecha(t)
    if not v or v < d["fecha"]:
        await preguntar(update, "Fecha no válida. Debe ser igual o posterior a la fecha del crédito.",
                        teclado(["8 días", "15 días", "30 días"]))
        return F_VENCE
    context.user_data.pop("fiar")
    with db() as con:
        vid = con.execute("INSERT INTO ventas(cliente_id,fecha,total,tipo,fecha_vence,nota) VALUES(?,?,?,?,?,?)",
                          (d["cli"]["id"], d["fecha"].isoformat(), d["monto"], "credito",
                           v.isoformat(), d["nota"])).lastrowid
    await enviar(update, "✅ " + ficha_venta(vid))
    return END


# ───────────────────────── ABONO ─────────────────────────
@autorizado
async def a_inicio(update, context):
    with db() as con:
        deudores = sorted({p["cliente"] for p in pendientes(con)})
    if not deudores:
        await enviar(update, "✅ Ningún cliente tiene deudas pendientes.")
        return END
    context.user_data["abono"] = {}
    await preguntar(update, "💰 <b>Registrar abono</b>\n\n¿Qué <b>cliente</b> paga? (estos son los que deben)",
                    teclado(*en_columnas(deudores)))
    return A_CLI


async def _a_set_cli(update, context, c):
    with db() as con:
        deuda = deuda_cliente(con, c["id"])
    if deuda <= 0:
        context.user_data.pop("abono", None)
        await enviar(update, f"✅ {esc(c['nombre'])} no tiene deudas pendientes.")
        return END
    context.user_data["abono"] = {"cli": dict(c), "deuda": deuda}
    await preguntar(update, f"👤 {esc(c['nombre'])} debe <b>{money(deuda)}</b>\n\n💲 ¿Cuánto <b>abona</b>?",
                    teclado([f"{B_TODO} ({money(deuda)})"]))
    return A_MONTO


async def a_cli_txt(update, context):
    c = await resolver(update, "cliente", update.message.text.strip())
    return await _a_set_cli(update, context, c) if c else A_CLI


async def a_cli_btn(update, context):
    return await _a_set_cli(update, context, await btn_elegido(update, "cliente"))


async def a_monto(update, context):
    d = context.user_data["abono"]
    t = update.message.text.strip()
    v = d["deuda"] if t.startswith(B_TODO) or norm(t).startswith("todo") else parse_monto(t)
    if not v:
        await preguntar(update, "Escribe un valor válido, ej: 50000", teclado([f"{B_TODO} ({money(d['deuda'])})"]))
        return A_MONTO
    if v > d["deuda"]:
        await preguntar(update, f"⚠️ El abono supera la deuda ({money(d['deuda'])}). Escribe un valor menor o igual.",
                        teclado([f"{B_TODO} ({money(d['deuda'])})"]))
        return A_MONTO
    d["monto"] = v
    await preguntar(update, "📅 ¿<b>Fecha</b> del pago? (dd/mm/aaaa)", teclado([B_HOY, "Ayer"]))
    return A_FECHA


async def a_fecha(update, context):
    f = parse_fecha(update.message.text)
    if not f or f > hoy():
        await preguntar(update, "Fecha no válida (no puede ser futura).", teclado([B_HOY, "Ayer"]))
        return A_FECHA
    d = context.user_data.pop("abono")
    with db() as con:
        aid, aplicado = aplicar_abono(con, d["cli"]["id"], d["monto"], f.isoformat())
        resta = deuda_cliente(con, d["cli"]["id"])
    lin = [f"✅ 📋 <b>Abono #{aid} guardado</b>", "",
           f"👤 Cliente: <b>{esc(d['cli']['nombre'])}</b>",
           f"💲 Valor: <b>{money(d['monto'])}</b>",
           f"📅 Fecha: {f.strftime('%d/%m/%Y')}",
           f"💳 Debía: {money(d['deuda'])}", "", "Se aplicó a:"]
    lin += [f"• #{vid}: {money(m)}{' — ✅ pagada completa' if pagada else ''}" for vid, m, pagada in aplicado]
    lin.append("")
    lin.append(f"💳 <b>Queda debiendo: {money(resta)}</b>" if resta else "🏁 <b>¡Quedó a paz y salvo!</b>")
    await enviar(update, "\n".join(lin))
    return END


# ───────────────────────── CONSULTAS ─────────────────────────
async def consulta_deuda(update, nombre):
    if not nombre:
        await enviar(update, "Escribe el nombre, ej: /deuda Juan")
        return
    m = buscar("cliente", nombre)
    if not m:
        await enviar(update, f"No encontré clientes con «{esc(nombre)}».")
        return
    with db() as con:
        textos = [texto_deuda(con, c) for c in m[:5]]
    await enviar(update, "\n\n──────────\n\n".join(textos))


async def consulta_cliente(update, nombre):
    m = buscar("cliente", nombre) if nombre else []
    if not m:
        await enviar(update, f"No encontré clientes con «{esc(nombre)}».")
        return
    c = m[0]
    with db() as con:
        ventas = con.execute("SELECT * FROM ventas WHERE cliente_id=? ORDER BY fecha DESC, id DESC LIMIT 8", (c["id"],)).fetchall()
        tot = con.execute("SELECT COALESCE(SUM(total),0) t, COUNT(*) n FROM ventas WHERE cliente_id=?", (c["id"],)).fetchone()
        deuda_txt = texto_deuda(con, c)
    lin = [f"📇 <b>{esc(c['nombre'])}</b>",
           f"🏠 {esc(c['direccion'] or '-')}",
           f"📞 {esc(c['telefono'] or '-')} · cliente desde {fd(c['creado'])}",
           f"🛒 {tot['n']} compras por {money(tot['t'])} en total", "", "<b>Últimos movimientos:</b>"]
    for v in ventas:
        estado = "pagada " + fd(v["fecha_pago"]) if v["fecha_pago"] else "pendiente"
        lin.append(f"• #{v['id']} {fd(v['fecha'])} · {v['tipo']} · {money(v['total'])} · {estado}")
    if not ventas:
        lin.append("• Sin movimientos todavía")
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
    lin += ["", f"💳 Cartera total: <b>{money(sum(d['saldo'] for _, d in orden))}</b>",
            f"⚠️ Vencido: <b>{money(sum(d['vencido'] for _, d in orden))}</b>"]
    await enviar(update, "\n".join(lin))


async def consulta_vencidos(update):
    h = hoy()
    with db() as con:
        v = [p for p in pendientes(con) if p["fecha_vence"] and p["fecha_vence"] < h.isoformat()]
    if not v:
        await enviar(update, "✅ No hay créditos vencidos.")
        return
    lin = ["⚠️ <b>Créditos vencidos</b>", ""]
    for p in sorted(v, key=lambda x: x["fecha_vence"]):
        dias = (h - date.fromisoformat(p["fecha_vence"])).days
        lin.append(f"• {esc(p['cliente'])} · #{p['id']} · {money(p['saldo'])} · venció {fd(p['fecha_vence'])} ({dias} días)")
    lin.append(f"\nTotal vencido: <b>{money(sum(p['saldo'] for p in v))}</b>")
    await enviar(update, "\n".join(lin))


async def consulta_inventario(update):
    prods = todos("producto")
    if not prods:
        await enviar(update, f"No hay productos. Crea uno con {B_PRODUCTO}")
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
    await enviar(update,
                 f"📊 <b>Resumen de {titulo}</b> ({ini.strftime('%d/%m')} – {h.strftime('%d/%m')})\n\n"
                 f"🛒 Ventas: {r['n']}\n"
                 f"💵 De contado: {money(r['contado'])}\n"
                 f"📒 A crédito / fiado: {money(r['credito'])}\n"
                 f"💰 Abonos cobrados: {money(abonos)}\n"
                 f"🏦 <b>Dinero que entró: {money(r['contado'] + abonos)}</b>\n"
                 f"🚚 Compras de mercancía: {money(compras)}\n"
                 f"📈 Ganancia aprox. en productos: {money(gan)}\n\n"
                 f"💳 Cartera pendiente total: {money(cartera)}")


async def consulta_clientes(update):
    cl = todos("cliente")
    if not cl:
        await enviar(update, f"No hay clientes. Crea uno con {B_CLIENTE}")
        return
    with db() as con:
        deudas = {c["id"]: deuda_cliente(con, c["id"]) for c in cl}
    lin = [f"👥 <b>Clientes ({len(cl)})</b>", ""]
    lin += [f"• {esc(c['nombre'])}" + (f" · 📞 {esc(c['telefono'])}" if c["telefono"] else "")
            + (f" — debe {money(deudas[c['id']])}" if deudas[c["id"]] else "") for c in cl]
    await enviar(update, "\n".join(lin))


# ───────────────────────── SUBMENÚS (Deudas, Reportes, Buscar) ─────────────────────────
@autorizado
async def menu_deudas(update, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Deuda de un cliente", callback_data="m:deuda")],
        [InlineKeyboardButton("📋 Todos los que deben", callback_data="m:deudores")],
        [InlineKeyboardButton("⚠️ Créditos vencidos", callback_data="m:vencidos")],
    ])
    await update.message.reply_text("💳 <b>Deudas</b> — ¿qué quieres ver?", parse_mode="HTML", reply_markup=kb)
    return END


@autorizado
async def menu_reportes(update, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Hoy", callback_data="m:res:hoy"),
         InlineKeyboardButton("📆 Semana", callback_data="m:res:semana"),
         InlineKeyboardButton("🗓️ Mes", callback_data="m:res:mes")],
        [InlineKeyboardButton("📦 Inventario", callback_data="m:inventario")],
        [InlineKeyboardButton("👥 Lista de clientes", callback_data="m:clientes")],
    ])
    await update.message.reply_text("📊 <b>Reportes</b> — ¿qué quieres ver?", parse_mode="HTML", reply_markup=kb)
    return END


@autorizado
async def menu_buscar(update, context):
    if not todos("cliente"):
        await enviar(update, f"Todavía no hay clientes. Crea uno con {B_CLIENTE}")
        return END
    await preguntar(update, "🔎 ¿Qué <b>cliente</b> buscas? Toca uno o escribe el nombre:", teclado_lista("cliente"))
    return Q_CLIENTE


@autorizado
async def menu_cb(update, context):
    q = update.callback_query
    await q.answer()
    accion = q.data.split(":")[1:]
    if accion[0] == "deuda":
        await preguntar(update, "👤 ¿De qué <b>cliente</b>? Toca uno o escribe el nombre:", teclado_lista("cliente"))
        return Q_DEUDA
    if accion[0] == "deudores":
        await consulta_deudores(update)
    elif accion[0] == "vencidos":
        await consulta_vencidos(update)
    elif accion[0] == "res":
        await consulta_resumen(update, accion[1])
    elif accion[0] == "inventario":
        await consulta_inventario(update)
    elif accion[0] == "clientes":
        await consulta_clientes(update)
    return END


async def q_deuda(update, context):
    await consulta_deuda(update, update.message.text.strip())
    return END


async def q_cliente(update, context):
    await consulta_cliente(update, update.message.text.strip())
    return END


@autorizado
async def ayuda(update, context):
    await enviar(update, AYUDA)
    return END


# Comandos escritos (siguen funcionando además de los botones)
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
        await enviar(update, "Uso: /anularventa 15")
        return
    vid = int(context.args[0].lstrip("#"))
    with db() as con:
        v = con.execute("SELECT * FROM ventas WHERE id=?", (vid,)).fetchone()
        if not v:
            await enviar(update, f"No existe la venta #{vid}.")
            return
        ab = con.execute("SELECT DISTINCT abono_id FROM abono_aplicaciones WHERE venta_id=?", (vid,)).fetchall()
        if ab:
            await enviar(update, "Esta venta tiene abonos. Anula primero: " +
                                 ", ".join(f"/anularabono {a['abono_id']}" for a in ab))
            return
        for i in con.execute("SELECT * FROM venta_items WHERE venta_id=?", (vid,)).fetchall():
            con.execute("UPDATE productos SET stock=stock+? WHERE id=?", (i["cantidad"], i["producto_id"]))
        con.execute("DELETE FROM ventas WHERE id=?", (vid,))
    await enviar(update, f"🗑️ Venta #{vid} anulada y el inventario devuelto.")


@autorizado
async def cmd_anular_abono(update, context):
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await enviar(update, "Uso: /anularabono 7")
        return
    aid = int(context.args[0].lstrip("#"))
    with db() as con:
        a = con.execute("SELECT * FROM abonos WHERE id=?", (aid,)).fetchone()
        if not a:
            await enviar(update, f"No existe el abono #{aid}.")
            return
        afectadas = [r["venta_id"] for r in con.execute("SELECT venta_id FROM abono_aplicaciones WHERE abono_id=?", (aid,))]
        con.execute("DELETE FROM abonos WHERE id=?", (aid,))
        for vid in afectadas:
            con.execute("UPDATE ventas SET fecha_pago=NULL WHERE id=? AND tipo='credito'", (vid,))
    await enviar(update, f"🗑️ Abono #{aid} de {money(a['monto'])} anulado. La deuda volvió a quedar pendiente.")


@autorizado
async def cmd_respaldo(update, context):
    with open(DB, "rb") as f:
        await update.message.reply_document(f, filename=f"respaldo_{hoy().isoformat()}.db",
                                            caption="💾 Respaldo de tu base de datos. Guárdalo bien.",
                                            reply_markup=MENU)


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
        return await consulta_resumen(update, "semana" if "semana" in t else "mes" if "mes" in t else "hoy")
    if "clientes" in t:
        return await consulta_clientes(update)
    if buscar("cliente", original):
        return await consulta_deuda(update, original)
    await enviar(update, "No entendí 🤔 Usa los botones de abajo 👇")


# ───────────────────────── TAREAS AUTOMÁTICAS ─────────────────────────
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
        lin += ["", f"⚠️ Vencidos: {len(vencidos)} créditos por {money(sum(p['saldo'] for p in vencidos))}"]
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
        BotCommand("start", "Mostrar el menú"), BotCommand("cancelar", "Cancelar lo que estoy haciendo"),
        BotCommand("deuda", "Deuda de un cliente"), BotCommand("deudores", "Todos los que deben"),
        BotCommand("respaldo", "Copia de la base de datos"),
    ])


def boton(texto):
    return filters.Text([texto])


def construir_app():
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Texto normal que no sea comando ni botón del menú
    TXT = filters.TEXT & ~filters.COMMAND & ~filters.Text(BOTONES_MENU + [B_CANCELAR])

    def t(fn):
        return MessageHandler(TXT, fn)

    # Una sola conversación: cualquier botón del menú inicia (o reinicia) su formulario
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(boton(B_CLIENTE), nc_inicio), CommandHandler("nuevocliente", nc_inicio),
            MessageHandler(boton(B_PRODUCTO), np_inicio), CommandHandler("nuevoproducto", np_inicio),
            MessageHandler(boton(B_COMPRA), c_inicio), CommandHandler("compra", c_inicio),
            MessageHandler(boton(B_VENTA), v_inicio), CommandHandler("venta", v_inicio),
            MessageHandler(boton(B_FIAR), f_inicio), CommandHandler("fiar", f_inicio),
            MessageHandler(boton(B_ABONO), a_inicio), CommandHandler("abono", a_inicio),
            MessageHandler(boton(B_DEUDAS), menu_deudas),
            MessageHandler(boton(B_REPORTES), menu_reportes),
            MessageHandler(boton(B_BUSCAR), menu_buscar),
            MessageHandler(boton(B_AYUDA), ayuda), CommandHandler("ayuda", ayuda),
            CallbackQueryHandler(menu_cb, pattern="^m:"),
        ],
        states={
            NC_NOMBRE: [t(nc_nombre)], NC_APELLIDO: [t(nc_apellido)], NC_DIR: [t(nc_dir)], NC_TEL: [t(nc_tel)],
            NP_NOMBRE: [t(np_nombre)], NP_COSTO: [t(np_costo)], NP_PRECIO: [t(np_precio)], NP_STOCK: [t(np_stock)],
            C_PROD: [t(c_prod_txt), CallbackQueryHandler(c_prod_btn, "^prod:")],
            C_CANT: [t(c_cant)], C_COSTO: [t(c_costo)], C_PROV: [t(c_prov)], C_FECHA: [t(c_fecha)],
            V_CLI: [t(v_cli_txt), CallbackQueryHandler(v_cli_btn, "^cli:")],
            V_PROD: [t(v_prod_txt), CallbackQueryHandler(v_prod_btn, "^prod:")],
            V_CANT: [t(v_cant)], V_TIPO: [CallbackQueryHandler(v_tipo_btn, "^tipo:")], V_VENCE: [t(v_vence)],
            F_CLI: [t(f_cli_txt), CallbackQueryHandler(f_cli_btn, "^cli:")],
            F_MONTO: [t(f_monto)], F_NOTA: [t(f_nota)], F_FECHA: [t(f_fecha)], F_VENCE: [t(f_vence)],
            A_CLI: [t(a_cli_txt), CallbackQueryHandler(a_cli_btn, "^cli:")],
            A_MONTO: [t(a_monto)], A_FECHA: [t(a_fecha)],
            Q_DEUDA: [t(q_deuda)], Q_CLIENTE: [t(q_cliente)],
        },
        fallbacks=[MessageHandler(boton(B_CANCELAR), cancelar), CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(conv)

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
    app.add_handler(MessageHandler(boton(B_CANCELAR), cancelar))
    app.add_handler(MessageHandler(TXT, texto_libre))

    if app.job_queue:
        app.job_queue.run_daily(recordatorio, time=time(7, 30, tzinfo=TZ))     # aviso diario 7:30 a.m.
        app.job_queue.run_daily(respaldo_diario, time=time(22, 0, tzinfo=TZ))  # respaldo 10:00 p.m.
    return app


def main():
    if not TOKEN:
        raise SystemExit("Falta la variable BOT_TOKEN")
    # Python 3.14 ya no crea el "event loop" solo; lo creamos aquí para que funcione en Termux
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = construir_app()
    log.info("Bot iniciado ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
