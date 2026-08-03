import os
import re
import httpx
from datetime import datetime, timezone
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

# ── Estado global (en memoria — se pierde si el servidor se reinicia) ────────
gastos: list = []
_procesados_wa_ids: set = set()
_next_id = 1

# ── Categorías y palabras clave (edítalas a gusto) ────────────────────────────
CATEGORIAS = {
    "Comida":          ["comida", "super", "supermercado", "almuerzo", "cena", "delivery",
                         "restaurant", "restoran", "restorán", "cafe", "café"],
    "Transporte":      ["bencina", "auto", "uber", "taxi", "transporte", "metro", "bip",
                         "estacionamiento", "peaje", "autopista"],
    "Salud":           ["salud", "farmacia", "doctor", "medico", "médico", "isapre",
                         "remedios", "dentista"],
    "Hogar":           ["hogar", "arriendo", "gastos comunes", "luz", "agua", "gas",
                         "internet", "cuenta", "casa"],
    "Entretenimiento": ["entretenimiento", "salida", "cine", "streaming", "netflix",
                         "spotify", "bar", "trago", "plan"],
    "Mascotas":        ["perro", "gato", "mascota", "veterinario", "paseador"],
    "Otros":           [],
}

MONTO_REGEX = re.compile(
    r"\$?\s?(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s?(k)?",
    re.IGNORECASE,
)

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

def normalizar_texto(texto: str) -> str:
    t = texto.lower()
    for a, b in [("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n")]:
        t = t.replace(a, b)
    return t

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

def extraer_categoria(texto: str):
    lower = normalizar_texto(texto)
    for categoria, keywords in CATEGORIAS.items():
        for kw in keywords:
            if normalizar_texto(kw) in lower:
                return categoria, kw
    return "Otros", None

def parse_message(texto_original: str):
    texto = texto_original.strip()
    resultado_monto = extraer_monto(texto)
    if not resultado_monto:
        return None
    monto, texto_monto = resultado_monto
    categoria, keyword = extraer_categoria(texto)

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
    global _next_id
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

    if mensaje.get("type") != "text":
        await enviar_mensaje(numero, 'Por ahora solo entiendo mensajes de texto tipo "comida 5.000" 🙂')
        return {"status": "ok"}

    texto = mensaje["text"]["body"]

    profile_name = None
    try:
        profile_name = value["contacts"][0]["profile"]["name"]
    except (KeyError, IndexError):
        pass
    nombre = MIEMBROS.get(numero, profile_name or numero)

    parsed = parse_message(texto)
    if not parsed:
        await enviar_mensaje(
            numero,
            'No encontré un monto en tu mensaje 🤔\nMándalo así: "categoría descripción monto"\n'
            'Ej: "comida almuerzo 5.000"',
        )
        return {"status": "ok"}

    gasto = {
        "id": _next_id,
        "member_phone": numero,
        "member_name": nombre,
        "categoria": parsed["categoria"],
        "descripcion": parsed["descripcion"],
        "monto": parsed["monto"],
        "mensaje_original": texto,
        "wa_message_id": wa_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _next_id += 1
    gastos.insert(0, gasto)  # más nuevo primero

    confirmacion = f"✅ {parsed['categoria']} {fmt_monto(parsed['monto'])}"
    if parsed["descripcion"]:
        confirmacion += f" ({parsed['descripcion']})"
    await enviar_mensaje(numero, confirmacion)

    return {"status": "ok"}

# ── Lectura de gastos (para el dashboard) ─────────────────────────────────────
@app.get("/gastos")
def ver_gastos(limit: int = 200):
    return gastos[:limit]

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
    return {"status": "ok", "gastos_registrados": 0}
