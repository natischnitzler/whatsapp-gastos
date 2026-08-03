import os
import re
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Configuración ─────────────────────────────────────────────────────────────
WA_VERIFY_TOKEN    = os.environ.get("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN    = os.environ.get("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_API_BASE        = "https://graph.facebook.com/v19.0"
ADMIN_KEY          = os.environ.get("ADMIN_KEY", "cambia-esta-clave")

# Miembros de la familia/pareja: "56912345678:Cata,56998765432:Tomas"
MIEMBROS_RAW = os.environ.get("MIEMBROS", "")

def _parsear_miembros(raw: str) -> dict:
    miembros = {}
    for par in raw.split(","):
        par = par.strip()
        if not par or ":" not in par:
            continue
        tel, nombre = par.split(":", 1)
        miembros[tel.strip()] = nombre.strip()
    return miembros

MIEMBROS = _parsear_miembros(MIEMBROS_RAW)

MESES_ES = {1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
            7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"}

# ── Cuentas, categorías y presupuestos (edítalo acá directamente) ────────────
# Cada cuenta tiene sus propias categorías con su propio presupuesto mensual.
# "presupuesto_total" es opcional: si lo pones, avisa cuando la CUENTA completa
# se pasa de ese monto, además de cada categoría individual.
# Una categoría sin presupuesto (None) queda sin límite, solo se registra.
CUENTAS_CONFIG = {
    "Casa": {
        "presupuesto_total": None,
        "categorias": {
            "Perros":       200000,
            "Tali":         100000,
            "Farmacia":      20000,
            "Regalos":       50000,
            "Supermercado": 300000,
            "Spid":          50000,
        },
    },
    "Linda": {
        "presupuesto_total": 350000,
        "categorias": {
            "Deporte":        80000,
            "Salir a comer":  80000,
            "Belleza":        30000,
        },
    },
    "Lindo": {
        "presupuesto_total": None,
        "categorias": {
            "Cafecitos": 40000,
            "Belleza":   40000,
            "Salidas":   35000,
            "Otros":     None,
        },
    },
}

# Categorías genéricas SOLO para cuando alguien escribe texto libre (sin pasar
# por los botones) — no están ligadas a presupuestos, es un fallback aparte.
CATEGORIAS_LIBRES = {
    "Comida":          ["comida", "super", "supermercado", "almuerzo", "cena", "delivery",
                         "restaurant", "restoran", "restorán", "cafe", "café"],
    "Transporte":      ["bencina", "auto", "uber", "taxi", "transporte", "metro", "bip",
                         "estacionamiento", "peaje", "autopista"],
    "Salud":           ["salud", "farmacia", "doctor", "medico", "médico", "isapre",
                         "remedios", "dentista"],
    "Hogar":           ["hogar", "arriendo", "gastos comunes", "luz", "agua", "gas", "internet"],
    "Entretenimiento": ["entretenimiento", "salida", "cine", "streaming", "netflix",
                         "spotify", "bar", "trago", "plan"],
    "Mascotas":        ["perro", "gato", "mascota", "veterinario", "paseador"],
    "Otros":           [],
}

MONTO_REGEX = re.compile(
    r"\$?\s?(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s?(k)?",
    re.IGNORECASE,
)

# ── Estado global (en memoria — se pierde si el servidor se reinicia) ────────
gastos: list = []
_procesados_wa_ids: set = set()
_next_id = 1
sesiones: dict = {}  # numero -> {"cuenta": "Casa", "categoria": "Perros"}

# ── Utilidades ────────────────────────────────────────────────────────────────
def normalizar_numero(n: str) -> str:
    if not n:
        return ""
    n = re.sub(r"[\s\-\(\)]", "", n)
    if n.startswith("+"):
        n = n[1:]
    return n

def fmt_monto(n) -> str:
    return "$" + f"{int(n):,}".replace(",", ".")

def saludo_hora() -> str:
    hora = datetime.now(timezone(timedelta(hours=-4))).hour  # hora de Chile
    if hora < 12:
        return "🌅 Buenos días"
    if hora < 19:
        return "☀️ Buenas tardes"
    return "🌙 Buenas noches"

def normalizar_texto(texto: str) -> str:
    t = texto.lower()
    for a, b in [("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n")]:
        t = t.replace(a, b)
    return t

def _slug(texto: str) -> str:
    t = normalizar_texto(texto)
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")

def _cuenta_por_slug(slug: str):
    for cuenta in CUENTAS_CONFIG:
        if _slug(cuenta) == slug:
            return cuenta
    return None

def _categoria_por_slug(cuenta: str, slug: str):
    if not cuenta:
        return None
    for cat in CUENTAS_CONFIG.get(cuenta, {}).get("categorias", {}):
        if _slug(cat) == slug:
            return cat
    return None

def extraer_monto(texto: str):
    matches = list(MONTO_REGEX.finditer(texto))
    if not matches:
        return None
    ultimo = matches[-1]
    num_str = ultimo.group(1)
    es_k = bool(ultimo.group(2))

    if "." in num_str and "," in num_str:
        num_str = num_str.replace(".", "").replace(",", ".")
    elif "." in num_str:
        if re.search(r"\.\d{3}\b", num_str):
            num_str = num_str.replace(".", "")
    elif "," in num_str:
        num_str = num_str.replace(",", ".")

    try:
        monto = float(num_str)
    except ValueError:
        return None
    if es_k:
        monto *= 1000
    return monto, ultimo.group(0)

def extraer_categoria_libre(texto: str):
    lower = normalizar_texto(texto)
    for categoria, keywords in CATEGORIAS_LIBRES.items():
        for kw in keywords:
            if normalizar_texto(kw) in lower:
                return categoria, kw
    return "Otros", None

def parse_message(texto_original: str):
    """Fallback de texto libre: 'categoría descripción monto'. Sin cuenta asignada."""
    texto = texto_original.strip()
    resultado_monto = extraer_monto(texto)
    if not resultado_monto:
        return None
    monto, texto_monto = resultado_monto
    categoria, keyword = extraer_categoria_libre(texto)

    descripcion = texto.replace(texto_monto, "")
    if keyword:
        descripcion = re.sub(re.escape(keyword), "", descripcion, flags=re.IGNORECASE)
    descripcion = re.sub(r"\s+", " ", descripcion).strip()

    return {"categoria": categoria, "monto": monto, "descripcion": descripcion or None}

# ── Meta Cloud API ────────────────────────────────────────────────────────────
async def enviar_mensaje(to: str, texto: str):
    url = f"{WA_API_BASE}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": texto, "preview_url": False},
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            print(f"Error Meta API {r.status_code}: {r.text}")

async def enviar_botones(to: str, texto: str, botones: list):
    """Envía un mensaje con hasta 3 botones. botones = [{'id':..., 'title':...}, ...]"""
    url = f"{WA_API_BASE}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                    for b in botones[:3]
                ]
            },
        },
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            print(f"Error botones Meta API {r.status_code}: {r.text}")
            fallback = texto + "\n\n" + "\n".join([f"• {b['title']}" for b in botones])
            await enviar_mensaje(to, fallback)

async def enviar_lista(to: str, texto: str, boton_titulo: str, filas: list):
    """Envía una lista interactiva. filas = [{'id':..., 'title':...}, ...] (máx 10)"""
    url = f"{WA_API_BASE}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": texto},
            "action": {
                "button": boton_titulo,
                "sections": [{"title": "Categorías", "rows": filas[:10]}],
            },
        },
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            print(f"Error lista Meta API {r.status_code}: {r.text}")
            fallback = texto + "\n\n" + "\n".join([f"• {f['title']}" for f in filas])
            await enviar_mensaje(to, fallback)

async def enviar_bienvenida(to: str, nombre: str):
    sesiones[to] = {}
    saludo = saludo_hora()
    texto = (
        f"{saludo}, {nombre}! 👋\n"
        f"Escribe *resumen* si quieres ver el presupuesto, o elige una cuenta para registrar un gasto:"
    )
    botones = [{"id": f"cuenta_{_slug(c)}", "title": f"Gastos {c}"} for c in CUENTAS_CONFIG]
    await enviar_botones(to, texto, botones)

async def enviar_categorias(to: str, cuenta: str):
    categorias = CUENTAS_CONFIG[cuenta]["categorias"]
    filas = [{"id": f"cat__{_slug(cuenta)}__{_slug(cat)}", "title": cat} for cat in categorias]
    await enviar_lista(to, f"Gastos {cuenta} — ¿en qué categoría?", "Elegir categoría", filas)

SALUDOS = {"hola", "hi", "hey", "buenas", "menu", "menú", "hello", "buenos dias",
           "buenos días", "buenas tardes", "buenas noches"}

RESUMEN_PALABRAS = {"resumen", "presupuesto", "presupuestos", "como vamos", "cómo vamos", "balance"}

def _nombre_de(numero: str, value: dict) -> str:
    profile_name = None
    try:
        profile_name = value["contacts"][0]["profile"]["name"]
    except (KeyError, IndexError):
        pass
    return MIEMBROS.get(numero, profile_name or numero)

def _guardar_gasto(numero: str, nombre: str, cuenta, categoria: str, monto: float,
                    descripcion, texto_original: str, wa_id: str) -> dict:
    global _next_id
    gasto = {
        "id": _next_id,
        "member_phone": numero,
        "member_name": nombre,
        "cuenta": cuenta,
        "categoria": categoria,
        "descripcion": descripcion,
        "monto": monto,
        "mensaje_original": texto_original,
        "wa_message_id": wa_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _next_id += 1
    gastos.insert(0, gasto)
    return gasto

def _confirmacion(gasto: dict) -> str:
    partes = []
    if gasto.get("cuenta"):
        partes.append(gasto["cuenta"])
    partes.append(gasto["categoria"])
    texto = f"✅ {' · '.join(partes)} {fmt_monto(gasto['monto'])}"
    if gasto.get("descripcion"):
        texto += f" ({gasto['descripcion']})"
    return texto

# ── Cálculo de presupuestos ────────────────────────────────────────────────────
def _mismo_mes(iso_str: str, ahora: datetime) -> bool:
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return False
    return dt.year == ahora.year and dt.month == ahora.month

def total_cuenta(cuenta: str) -> float:
    ahora = datetime.now(timezone.utc)
    return sum(g["monto"] for g in gastos if g.get("cuenta") == cuenta and _mismo_mes(g["created_at"], ahora))

def total_categoria_en_cuenta(cuenta: str, categoria: str) -> float:
    ahora = datetime.now(timezone.utc)
    return sum(g["monto"] for g in gastos
               if g.get("cuenta") == cuenta and g.get("categoria") == categoria and _mismo_mes(g["created_at"], ahora))

def _emoji_progreso(total: float, limite) -> str:
    if not limite or limite <= 0:
        return "⚪"
    pct = total / limite
    if pct >= 1:
        return "🔴"
    if pct >= 0.8:
        return "🟡"
    return "🟢"

def _avisos_presupuesto(gasto: dict) -> list:
    avisos = []
    cuenta = gasto.get("cuenta")
    categoria = gasto.get("categoria")
    if not cuenta or cuenta not in CUENTAS_CONFIG:
        return avisos

    config = CUENTAS_CONFIG[cuenta]

    limite_cat = config["categorias"].get(categoria)
    if limite_cat:
        total_cat = total_categoria_en_cuenta(cuenta, categoria)
        if total_cat > limite_cat:
            avisos.append(
                f"⚠️ Se pasaron del presupuesto de *{categoria}* ({cuenta}): "
                f"{fmt_monto(total_cat)} / {fmt_monto(limite_cat)} (+{fmt_monto(total_cat - limite_cat)})"
            )

    limite_total = config.get("presupuesto_total")
    if limite_total:
        total = total_cuenta(cuenta)
        if total > limite_total:
            avisos.append(
                f"⚠️ Se pasaron del presupuesto general de *{cuenta}*: "
                f"{fmt_monto(total)} / {fmt_monto(limite_total)} (+{fmt_monto(total - limite_total)})"
            )
    return avisos

def _saldo_texto(gasto: dict):
    """Si no se pasaron del presupuesto, dice cuánto les queda en esa categoría."""
    cuenta = gasto.get("cuenta")
    categoria = gasto.get("categoria")
    if not cuenta or cuenta not in CUENTAS_CONFIG:
        return None
    limite = CUENTAS_CONFIG[cuenta]["categorias"].get(categoria)
    if not limite:
        return None
    total = total_categoria_en_cuenta(cuenta, categoria)
    restante = limite - total
    if restante < 0:
        return None  # ya se pasaron, eso lo cubre el aviso de sobregiro
    return f"💰 Quedan {fmt_monto(restante)} de {fmt_monto(limite)} en {categoria}"

def construir_resumen() -> str:
    ahora = datetime.now(timezone.utc)
    mes_label = f"{MESES_ES.get(ahora.month, ahora.month)} {ahora.year}"
    lineas = [f"📊 Resumen de {mes_label}"]

    for cuenta, config in CUENTAS_CONFIG.items():
        lineas.append(f"\n*{cuenta}*")
        total = total_cuenta(cuenta)
        limite_total = config.get("presupuesto_total")
        if limite_total:
            emoji = _emoji_progreso(total, limite_total)
            extra = f" (+{fmt_monto(total - limite_total)})" if total > limite_total else ""
            lineas.append(f"{emoji} Total: {fmt_monto(total)} / {fmt_monto(limite_total)}{extra}")
        else:
            lineas.append(f"Total: {fmt_monto(total)}")

        for cat, limite in config["categorias"].items():
            total_cat = total_categoria_en_cuenta(cuenta, cat)
            if limite:
                emoji = _emoji_progreso(total_cat, limite)
                extra = f" (+{fmt_monto(total_cat - limite)})" if total_cat > limite else ""
                lineas.append(f"  {emoji} {cat}: {fmt_monto(total_cat)} / {fmt_monto(limite)}{extra}")
            else:
                lineas.append(f"  ⚪ {cat}: {fmt_monto(total_cat)}")

    return "\n".join(lineas)

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.get("/webhook")
async def verificar_webhook(request: Request):
    params = request.query_params
    modo = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if modo == "subscribe" and token == WA_VERIFY_TOKEN:
        return int(challenge)
    raise HTTPException(status_code=403, detail="Forbidden")

@app.post("/webhook")
async def recibir_mensaje(request: Request):
    body = await request.json()

    try:
        entry = body["entry"][0]
        change = entry["changes"][0]
        value = change["value"]
        mensajes = value.get("messages")
    except (KeyError, IndexError):
        return {"status": "ok"}

    if not mensajes:
        # Notificación de status (entregado/leído), no es un mensaje nuevo
        return {"status": "ok"}

    mensaje = mensajes[0]
    numero = normalizar_numero(mensaje["from"])
    wa_id = mensaje.get("id")

    if wa_id in _procesados_wa_ids:
        return {"status": "ok"}  # evita duplicados si Meta reenvía el webhook
    _procesados_wa_ids.add(wa_id)

    nombre = _nombre_de(numero, value)
    sesion = sesiones.get(numero, {})

    # ── Botones y listas (respuestas interactivas) ──
    if mensaje.get("type") == "interactive":
        interactive = mensaje["interactive"]
        reply_id = None
        if interactive.get("type") == "button_reply":
            reply_id = interactive["button_reply"]["id"]
        elif interactive.get("type") == "list_reply":
            reply_id = interactive["list_reply"]["id"]

        if reply_id and reply_id.startswith("cuenta_"):
            slug = reply_id[len("cuenta_"):]
            cuenta = _cuenta_por_slug(slug)
            if not cuenta:
                await enviar_mensaje(numero, "No reconocí esa cuenta, escribe *hola* para intentar de nuevo.")
                return {"status": "ok"}
            sesiones[numero] = {"cuenta": cuenta}
            await enviar_categorias(numero, cuenta)
            return {"status": "ok"}

        if reply_id and reply_id.startswith("cat__"):
            resto = reply_id[len("cat__"):]
            cuenta_slug, _, cat_slug = resto.partition("__")
            cuenta = _cuenta_por_slug(cuenta_slug)
            categoria = _categoria_por_slug(cuenta, cat_slug)
            if not cuenta or not categoria:
                await enviar_mensaje(numero, "No reconocí esa categoría, escribe *hola* para intentar de nuevo.")
                return {"status": "ok"}
            sesiones[numero] = {"cuenta": cuenta, "categoria": categoria}
            limite = CUENTAS_CONFIG[cuenta]["categorias"].get(categoria)
            extra = f" (presupuesto {fmt_monto(limite)})" if limite else ""
            await enviar_mensaje(
                numero,
                f"¿Cuánto gastaron en {categoria}{extra}? Puedes agregar un comentario, ej: \"8.500 cena con amigas\"",
            )
            return {"status": "ok"}

        await enviar_mensaje(numero, "No entendí esa opción, escribe *hola* para empezar de nuevo.")
        return {"status": "ok"}

    if mensaje.get("type") != "text":
        await enviar_mensaje(numero, 'Por ahora solo entiendo mensajes de texto tipo "comida 5.000" 🙂')
        return {"status": "ok"}

    texto = mensaje["text"]["body"]
    texto_lower = normalizar_texto(texto.strip())

    # ── Saludo → muestra los botones de cuenta ──
    if texto_lower in SALUDOS:
        await enviar_bienvenida(numero, nombre)
        return {"status": "ok"}

    # ── Pedir el resumen de presupuestos ──
    if texto_lower in RESUMEN_PALABRAS:
        await enviar_mensaje(numero, construir_resumen())
        return {"status": "ok"}

    # ── Si ya eligió cuenta + categoría, este mensaje es el monto (+ comentario opcional) ──
    if sesion.get("cuenta") and sesion.get("categoria"):
        resultado_monto = extraer_monto(texto)
        if not resultado_monto:
            await enviar_mensaje(numero, "No encontré un monto ahí 🤔 Mándame el número, ej: 5.000 (puedes agregar un comentario)")
            return {"status": "ok"}
        monto, texto_monto = resultado_monto
        descripcion = re.sub(r"\s+", " ", texto.replace(texto_monto, "")).strip() or None

        gasto = _guardar_gasto(numero, nombre, sesion["cuenta"], sesion["categoria"],
                                monto, descripcion, texto, wa_id)
        await enviar_mensaje(numero, _confirmacion(gasto))
        avisos = _avisos_presupuesto(gasto)
        for aviso in avisos:
            await enviar_mensaje(numero, aviso)
        if not avisos:
            saldo = _saldo_texto(gasto)
            if saldo:
                await enviar_mensaje(numero, saldo)
        sesiones[numero] = {}  # limpia la sesión, listo para el próximo gasto
        return {"status": "ok"}

    # ── Formato libre de siempre: "categoría descripción monto" (sin cuenta asignada) ──
    parsed = parse_message(texto)
    if not parsed:
        await enviar_mensaje(
            numero,
            'No encontré un monto en tu mensaje 🤔\nEscribe *hola* para usar los botones, o mándalo así: '
            '"categoría descripción monto" (ej: "comida almuerzo 5.000")',
        )
        return {"status": "ok"}

    gasto = _guardar_gasto(numero, nombre, None, parsed["categoria"], parsed["monto"],
                            parsed["descripcion"], texto, wa_id)
    await enviar_mensaje(numero, _confirmacion(gasto))

    return {"status": "ok"}

# ── Lectura de gastos (para el dashboard) ─────────────────────────────────────
@app.get("/gastos")
def ver_gastos(limit: int = 200):
    return gastos[:limit]

@app.get("/resumen")
def resumen_json():
    ahora = datetime.now(timezone.utc)
    cuentas = []
    for cuenta, config in CUENTAS_CONFIG.items():
        categorias = [
            {"nombre": cat, "gastado": total_categoria_en_cuenta(cuenta, cat), "presupuesto": limite}
            for cat, limite in config["categorias"].items()
        ]
        cuentas.append({
            "nombre": cuenta,
            "gastado": total_cuenta(cuenta),
            "presupuesto": config.get("presupuesto_total"),
            "categorias": categorias,
        })
    return {"mes": f"{MESES_ES.get(ahora.month, ahora.month)} {ahora.year}", "cuentas": cuentas}

@app.get("/health")
async def health():
    return {"status": "ok", "bot": "gastos", "gastos_registrados": len(gastos), "miembros": len(MIEMBROS)}

# ── Admin ─────────────────────────────────────────────────────────────────────
@app.get("/export")
def exportar(key: str = ""):
    """Respaldo manual: copia este JSON a un archivo si quieres guardar historial
    antes de un reinicio del servidor (no hay base de datos)."""
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403)
    return gastos

@app.get("/reset")
def reset(key: str = ""):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403)
    global gastos, _next_id
    gastos = []
    _next_id = 1
    _procesados_wa_ids.clear()
    sesiones.clear()
    return {"status": "ok", "gastos_registrados": 0}
