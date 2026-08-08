import os
import re
import json
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def _al_iniciar():
    await asyncio.to_thread(_cargar_desde_sheet)
    await asyncio.to_thread(_cargar_presupuestos_desde_sheet)
    asyncio.create_task(_sync_periodico())

# ── Configuración ─────────────────────────────────────────────────────────────
WA_VERIFY_TOKEN    = os.environ.get("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN    = os.environ.get("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_API_BASE        = "https://graph.facebook.com/v19.0"
ADMIN_KEY          = os.environ.get("ADMIN_KEY", "cambia-esta-clave")
SYNC_INTERVAL_MINUTOS = float(os.environ.get("SYNC_INTERVAL_MINUTOS", "5"))

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

# ── Google Sheets (opcional): guarda cada gasto en una planilla de Drive ──────
# Esto también resuelve la pérdida de datos al reiniciar el servidor: al partir,
# el bot recarga todo el historial desde la planilla. Cada mes tiene su propia
# pestaña (ej: "Agosto 2026"), y dentro de cada una los gastos quedan agrupados
# por Cuenta y luego por Categoria.
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SHEET_HEADERS = ["Fecha", "Cuenta", "Categoria", "Monto", "Descripcion", "Quien"]
_hoja_cache = {}

def sheets_configurado() -> bool:
    return bool(GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON)

def _spreadsheet():
    """Cliente de la planilla completa (lazy, se conecta solo la primera vez)."""
    if "sh" not in _hoja_cache:
        import gspread
        from google.oauth2.service_account import Credentials
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)
        _hoja_cache["sh"] = gc.open_by_key(GOOGLE_SHEET_ID)
    return _hoja_cache["sh"]

def _nombre_pestana_mes(dt: datetime) -> str:
    return f"{MESES_ES.get(dt.month, dt.month).capitalize()} {dt.year}"

def _es_pestana_mes(nombre: str) -> bool:
    partes = nombre.strip().split()
    if len(partes) != 2:
        return False
    mes, anio = partes
    return mes.lower() in MESES_ES.values() and anio.isdigit()

def _hoja_mes(dt: datetime):
    """Pestaña del mes correspondiente a la fecha del gasto — se crea sola si no existe."""
    titulo = _nombre_pestana_mes(dt)
    cache_key = f"ws_mes::{titulo}"
    if cache_key not in _hoja_cache:
        import gspread
        sh = _spreadsheet()
        try:
            ws = sh.worksheet(titulo)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=titulo, rows=300, cols=len(SHEET_HEADERS))
            ws.append_row(SHEET_HEADERS)
        _hoja_cache[cache_key] = ws
    return _hoja_cache[cache_key]

def _hoja_resumen_mes(dt: datetime):
    """Pestaña 'Resumen <Mes> <Año>' — totales por Cuenta y Categoria de ese mes."""
    titulo = f"Resumen {_nombre_pestana_mes(dt)}"
    cache_key = f"ws_resumen::{titulo}"
    if cache_key not in _hoja_cache:
        import gspread
        sh = _spreadsheet()
        try:
            ws = sh.worksheet(titulo)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=titulo, rows=100, cols=3)
        _hoja_cache[cache_key] = ws
    return _hoja_cache[cache_key]

def _resumen_mes_filas(dt: datetime) -> list:
    """Arma [Cuenta, Categoria, Suma] agrupado, con un TOTAL por cuenta y un TOTAL GENERAL,
    usando lo que hay en memoria (ya incluye el gasto recién guardado)."""
    filas = []
    total_general = 0
    for cuenta in CUENTAS_CONFIG:
        por_categoria = {}
        for g in gastos:
            if g.get("cuenta") != cuenta:
                continue
            try:
                gdt = datetime.fromisoformat(g["created_at"])
            except ValueError:
                continue
            if gdt.year != dt.year or gdt.month != dt.month:
                continue
            cat = g.get("categoria") or "(sin categoría)"
            por_categoria[cat] = por_categoria.get(cat, 0) + g["monto"]
        if not por_categoria:
            continue
        subtotal = sum(por_categoria.values())
        for cat, suma in por_categoria.items():
            filas.append([cuenta, cat, suma])
        filas.append([cuenta, "TOTAL", subtotal])
        total_general += subtotal
    if filas:
        filas.append(["TOTAL GENERAL", "", total_general])
    return filas

def _actualizar_resumen_mes_sync(dt: datetime):
    ws = _hoja_resumen_mes(dt)
    ws.clear()
    ws.append_row(["Cuenta", "Categoria", "Suma"])
    filas = _resumen_mes_filas(dt)
    if filas:
        ws.append_rows(filas)

def _actualizar_resumenes_todos_los_meses():
    """Reconstruye la pestaña de resumen de cada mes que tenga gastos en memoria."""
    meses = {(g["created_at"][:7]) for g in gastos if g.get("created_at")}  # 'YYYY-MM'
    for ym in meses:
        try:
            anio, mes = int(ym[:4]), int(ym[5:7])
            _actualizar_resumen_mes_sync(datetime(anio, mes, 1, tzinfo=timezone.utc))
        except Exception as e:
            print(f"Error actualizando resumen de {ym}: {type(e).__name__}: {e!r}")

def _normalizar_cuenta_de_sheet(valor):
    """Tolera errores de tipeo típicos al escribir la cuenta a mano en el Excel:
    mayúsculas/minúsculas, espacios de más, o un plural de más (ej: 'Lindos' -> 'Lindo').
    Si no logra reconocerla, la deja tal cual (mejor que perderla en silencio)."""
    valor = (valor or "").strip()
    if not valor:
        return None
    for cuenta in CUENTAS_CONFIG:
        if cuenta.lower() == valor.lower():
            return cuenta
    if valor.lower().endswith("s"):
        sin_s = valor[:-1]
        for cuenta in CUENTAS_CONFIG:
            if cuenta.lower() == sin_s.lower():
                return cuenta
    return valor

def _cargar_desde_sheet():
    """Al iniciar el servidor, restaura el historial de TODAS las pestañas de mes."""
    global gastos, _next_id
    if not sheets_configurado():
        return
    try:
        sh = _spreadsheet()
        pestanas_mes = [ws for ws in sh.worksheets() if _es_pestana_mes(ws.title)]
        todos = []
        for ws in pestanas_mes:
            for fila in ws.get_all_records():
                monto_raw = fila.get("Monto", 0)
                try:
                    monto = float(str(monto_raw).replace(",", "."))
                except ValueError:
                    continue
                todos.append({
                    "member_phone": "",
                    "member_name": fila.get("Quien", ""),
                    "cuenta": _normalizar_cuenta_de_sheet(fila.get("Cuenta")),
                    "categoria": fila.get("Categoria", ""),
                    "descripcion": fila.get("Descripcion") or None,
                    "monto": monto,
                    "mensaje_original": "",
                    "wa_message_id": None,
                    "created_at": fila.get("Fecha") or datetime.now(timezone.utc).isoformat(),
                })
        todos.sort(key=lambda g: g["created_at"], reverse=True)  # más nuevo primero
        for i, g in enumerate(todos, start=1):
            g["id"] = i
        gastos = todos
        _next_id = len(todos) + 1
        print(f"Google Sheets: {len(gastos)} gastos restaurados desde {len(pestanas_mes)} pestañas mensuales.")
    except Exception as e:
        import traceback
        print(f"Error cargando desde Google Sheets: {type(e).__name__}: {e!r}")
        traceback.print_exc()

    try:
        _actualizar_resumenes_todos_los_meses()
    except Exception as e:
        import traceback
        print(f"Error actualizando resúmenes mensuales: {type(e).__name__}: {e!r}")
        traceback.print_exc()

def _guardar_en_sheet_sync(gasto: dict):
    try:
        dt = datetime.fromisoformat(gasto["created_at"])
        ws = _hoja_mes(dt)
        ws.append_row([
            gasto["created_at"], gasto.get("cuenta") or "", gasto["categoria"],
            gasto["monto"], gasto.get("descripcion") or "", gasto["member_name"],
        ], value_input_option="USER_ENTERED")
        # Reordena la pestaña agrupada por Cuenta y luego por Categoria (deja la fecha como desempate)
        total_filas = len(ws.get_all_values())
        if total_filas > 2:
            ws.sort((2, "asc"), (3, "asc"), (1, "asc"), range=f"A2:F{total_filas}")
        _actualizar_resumen_mes_sync(dt)
    except Exception as e:
        import traceback
        print(f"Error guardando en Google Sheets: {type(e).__name__}: {e!r}")
        traceback.print_exc()

async def guardar_en_sheet(gasto: dict):
    """No bloquea el resto del bot si Google Sheets está lento o falla."""
    if not sheets_configurado():
        return
    await asyncio.to_thread(_guardar_en_sheet_sync, gasto)

def _fila_de_gasto_en_ws(ws, gasto: dict):
    """Busca la fila (índice base-1) que corresponde a este gasto por su Fecha exacta."""
    valores = ws.get_all_values()
    for idx, fila in enumerate(valores[1:], start=2):  # la fila 1 son los headers
        if fila and fila[0] == gasto["created_at"]:
            return idx
    return None

def _eliminar_de_sheet_sync(gasto: dict):
    try:
        dt = datetime.fromisoformat(gasto["created_at"])
        ws = _hoja_mes(dt)
        idx = _fila_de_gasto_en_ws(ws, gasto)
        if idx:
            ws.delete_rows(idx)
        _actualizar_resumen_mes_sync(dt)
    except Exception as e:
        import traceback
        print(f"Error eliminando de Google Sheets: {type(e).__name__}: {e!r}")
        traceback.print_exc()

async def eliminar_gasto_en_sheet(gasto: dict):
    if not sheets_configurado():
        return
    await asyncio.to_thread(_eliminar_de_sheet_sync, gasto)

def _actualizar_gasto_en_sheet_sync(gasto: dict):
    try:
        dt = datetime.fromisoformat(gasto["created_at"])
        ws = _hoja_mes(dt)
        idx = _fila_de_gasto_en_ws(ws, gasto)
        if idx:
            ws.update_cell(idx, 4, gasto["monto"])  # columna D = Monto
            ws.update_cell(idx, 5, gasto.get("descripcion") or "")  # columna E = Descripcion
        _actualizar_resumen_mes_sync(dt)
    except Exception as e:
        import traceback
        print(f"Error actualizando en Google Sheets: {type(e).__name__}: {e!r}")
        traceback.print_exc()

async def actualizar_gasto_en_sheet(gasto: dict):
    if not sheets_configurado():
        return
    await asyncio.to_thread(_actualizar_gasto_en_sheet_sync, gasto)

def _actualizar_presupuesto_en_sheet_sync(cuenta: str, categoria, nuevo_valor: float):
    """categoria=None → actualiza el tope general de la cuenta (fila con Categoria en blanco)."""
    try:
        ws = _hoja_presupuestos()
        filas = ws.get_all_values()
        for idx, fila in enumerate(filas[1:], start=2):
            fc = fila[0].strip() if len(fila) > 0 else ""
            fcat = fila[1].strip() if len(fila) > 1 else ""
            if fc == cuenta and fcat == (categoria or ""):
                ws.update_cell(idx, 3, nuevo_valor)  # columna C = Presupuesto
                return
        # No debería pasar (la fila siempre debería existir), pero por si acaso la agrega
        codigo = ""
        if categoria:
            codigo = next((c for c, (cu, ca) in CODIGOS.items() if cu == cuenta and ca == categoria), "")
        icono = icono_categoria(categoria) if categoria else icono_cuenta(cuenta)
        ws.append_row([cuenta, categoria or "", nuevo_valor, codigo, icono])
    except Exception as e:
        import traceback
        print(f"Error actualizando presupuesto en Google Sheets: {type(e).__name__}: {e!r}")
        traceback.print_exc()

async def actualizar_presupuesto_en_sheet(cuenta: str, categoria, nuevo_valor: float):
    if not sheets_configurado():
        return
    await asyncio.to_thread(_actualizar_presupuesto_en_sheet_sync, cuenta, categoria, nuevo_valor)

async def sincronizar_todo():
    """Recarga gastos y presupuestos desde la planilla (útil si editaron algo a mano en Sheets)."""
    if not sheets_configurado():
        return False
    await asyncio.to_thread(_cargar_desde_sheet)
    await asyncio.to_thread(_cargar_presupuestos_desde_sheet)
    return True

async def _sync_periodico():
    """Corre en segundo plano mientras el servidor esté despierto: resincroniza con la
    planilla cada SYNC_INTERVAL_MINUTOS, por si alguien editó algo a mano en Sheets."""
    if not sheets_configurado():
        return
    while True:
        await asyncio.sleep(SYNC_INTERVAL_MINUTOS * 60)
        try:
            await sincronizar_todo()
            print(f"Sincronización automática: {len(gastos)} gastos, {SYNC_INTERVAL_MINUTOS} min de intervalo.")
        except Exception as e:
            print(f"Error en sincronización periódica: {type(e).__name__}: {e!r}")

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

# Íconos para que se vea más lindo en WhatsApp y en el dashboard. Editables acá.
ICONOS_CUENTA = {
    "Casa": "🏠",
    "Linda": "💛",
    "Lindo": "💙",
}
ICONOS_CATEGORIA = {
    "Perros": "🐶",
    "Tali": "✨",
    "Farmacia": "💊",
    "Regalos": "🎁",
    "Supermercado": "🛒",
    "Spid": "⚡",
    "Deporte": "🏃",
    "Salir a comer": "🍽️",
    "Belleza": "💅",
    "Cafecitos": "☕",
    "Salidas": "🎉",
    "Otros": "🔹",
}

def icono_cuenta(cuenta: str) -> str:
    return ICONOS_CUENTA.get(cuenta, "💰")

def icono_categoria(categoria: str) -> str:
    return ICONOS_CATEGORIA.get(categoria, "🔹")

# Si alguien escribe una categoría ambigua (que existe en más de una cuenta, ej:
# "Belleza" en Linda y en Lindo) SIN decir la cuenta, se resuelve solo según quién
# escribe. Números en formato normalizado (sin '+', sin espacios).
CUENTA_PREDETERMINADA_POR_TELEFONO = {
    "56985495930": "Linda",
    "56945676306": "Lindo",
}


# Prefijo de código por cuenta (para que Linda y Lindo no choquen aunque empiecen igual).
# Los códigos reales que use el bot terminan siendo los que estén en la pestaña
# "Presupuestos" del Excel — esto es solo el punto de partida.
PREFIJOS_CODIGO = {"Casa": "CA", "Linda": "LN", "Lindo": "LD"}

def _generar_codigos_iniciales() -> dict:
    codigos = {}
    for cuenta, config in CUENTAS_CONFIG.items():
        prefijo = PREFIJOS_CODIGO[cuenta] if cuenta in PREFIJOS_CODIGO else cuenta[:2].upper()
        for i, cat in enumerate(config["categorias"], start=1):
            codigos[f"{prefijo}{i:02d}"] = (cuenta, cat)
    return codigos

# Mapa código -> (cuenta, categoria). Funciona sin Sheets con estos valores por
# defecto; si Sheets está conectado, la columna "Codigo" de la pestaña
# "Presupuestos" puede agregar o renombrar códigos (ver _cargar_presupuestos_desde_sheet).
CODIGOS = _generar_codigos_iniciales()

def _migrar_columna_codigo(ws):
    """Si la pestaña 'Presupuestos' ya existía de antes (sin columna Codigo), la agrega
    y la rellena con los códigos generados por defecto para las filas que reconozca."""
    headers = ws.row_values(1)
    if "Codigo" in headers:
        return
    col_codigo = len(headers) + 1
    if ws.col_count < col_codigo:
        ws.add_cols(col_codigo - ws.col_count)  # la grilla puede ser más angosta que los headers reales
    ws.update_cell(1, col_codigo, "Codigo")

    inverso = {}
    for codigo, (cu, ca) in _generar_codigos_iniciales().items():
        inverso[(cu, ca)] = codigo

    filas = ws.get_all_records()
    for idx, fila in enumerate(filas, start=2):  # la fila 1 son los headers
        cuenta = str(fila.get("Cuenta", "")).strip()
        categoria = str(fila.get("Categoria", "")).strip()
        if categoria and (cuenta, categoria) in inverso:
            ws.update_cell(idx, col_codigo, inverso[(cuenta, categoria)])

def _migrar_columna_icono(ws):
    """Si la pestaña 'Presupuestos' ya existía de antes (sin columna Icono), la agrega
    y la rellena con los íconos actuales, para que categoría e ícono vivan juntos."""
    headers = ws.row_values(1)
    if "Icono" in headers:
        return
    col_icono = len(headers) + 1
    if ws.col_count < col_icono:
        ws.add_cols(col_icono - ws.col_count)
    ws.update_cell(1, col_icono, "Icono")

    filas = ws.get_all_records()
    for idx, fila in enumerate(filas, start=2):
        cuenta = str(fila.get("Cuenta", "")).strip()
        categoria = str(fila.get("Categoria", "")).strip()
        icono = ICONOS_CATEGORIA.get(categoria, "") if categoria else ICONOS_CUENTA.get(cuenta, "")
        if icono:
            ws.update_cell(idx, col_icono, icono)

def _hoja_presupuestos():
    """Pestaña 'Presupuestos' — se crea sola la primera vez, prellenada con los valores actuales."""
    if "ws_presupuestos" not in _hoja_cache:
        import gspread
        sh = _spreadsheet()
        try:
            ws = sh.worksheet("Presupuestos")
            _migrar_columna_codigo(ws)
            _migrar_columna_icono(ws)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="Presupuestos", rows=50, cols=5)
            ws.append_row(["Cuenta", "Categoria", "Presupuesto", "Codigo", "Icono"])
            filas = []
            for cuenta, config in CUENTAS_CONFIG.items():
                filas.append([cuenta, "", config.get("presupuesto_total") or "", "", icono_cuenta(cuenta)])
                for cat, limite in config["categorias"].items():
                    codigo = next((c for c, (cu, ca) in CODIGOS.items() if cu == cuenta and ca == cat), "")
                    filas.append([cuenta, cat, limite if limite else "", codigo, icono_categoria(cat)])
            ws.append_rows(filas)
        _hoja_cache["ws_presupuestos"] = ws
    return _hoja_cache["ws_presupuestos"]

def _cargar_presupuestos_desde_sheet():
    """La pestaña 'Presupuestos' pasa a ser la fuente de verdad de las categorías de
    cada cuenta (no solo los montos): agregar una fila nueva crea la categoría, sacar
    una fila la elimina, y cambiar el texto de Categoria la renombra. Deja la fila de
    Categoria en blanco para el tope general de la cuenta. La columna Codigo es lo que
    la gente puede escribir directo en el chat (ej: 'LN01 5.000 starbucks')."""
    global CODIGOS
    if not sheets_configurado():
        return
    try:
        ws = _hoja_presupuestos()
        filas = ws.get_all_records()

        nuevas_categorias = {cuenta: {} for cuenta in CUENTAS_CONFIG}
        nuevos_totales = {cuenta: CUENTAS_CONFIG[cuenta].get("presupuesto_total") for cuenta in CUENTAS_CONFIG}
        codigos_hoja = {}
        iconos_categoria_hoja = {}
        iconos_cuenta_hoja = {}

        for fila in filas:
            cuenta = _normalizar_cuenta_de_sheet(fila.get("Cuenta", "")) or ""
            categoria = str(fila.get("Categoria", "")).strip()
            presupuesto_raw = str(fila.get("Presupuesto", "")).strip()
            codigo_raw = str(fila.get("Codigo", "")).strip()
            icono_raw = str(fila.get("Icono", "")).strip()
            if cuenta not in CUENTAS_CONFIG:
                continue
            try:
                presupuesto = float(presupuesto_raw.replace(",", ".")) if presupuesto_raw else None
            except ValueError:
                continue
            if categoria:
                nuevas_categorias[cuenta][categoria] = presupuesto
                if codigo_raw:
                    codigos_hoja[re.sub(r"\s+", "", codigo_raw).upper()] = (cuenta, categoria)
                if icono_raw:
                    iconos_categoria_hoja[categoria] = icono_raw
            else:
                nuevos_totales[cuenta] = presupuesto
                if icono_raw:
                    iconos_cuenta_hoja[cuenta] = icono_raw

        ICONOS_CATEGORIA.update(iconos_categoria_hoja)
        ICONOS_CUENTA.update(iconos_cuenta_hoja)

        actualizadas = 0
        for cuenta in CUENTAS_CONFIG:
            if nuevas_categorias[cuenta]:  # si la hoja no traía ninguna fila para esta cuenta, no la vaciamos (evita perder todo por una hoja vacía/con error)
                CUENTAS_CONFIG[cuenta]["categorias"] = nuevas_categorias[cuenta]
                actualizadas += len(nuevas_categorias[cuenta])
            CUENTAS_CONFIG[cuenta]["presupuesto_total"] = nuevos_totales[cuenta]

        codigos_generados = _generar_codigos_iniciales()  # basado en las categorías ya actualizadas
        codigos_generados.update(codigos_hoja)  # los códigos explícitos de la hoja mandan por sobre los generados
        CODIGOS = codigos_generados

        print(f"Presupuestos: {actualizadas} categorías activas, {len(CODIGOS)} códigos.")
    except Exception as e:
        import traceback
        print(f"Error cargando presupuestos desde Google Sheets: {type(e).__name__}: {e!r}")
        traceback.print_exc()

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
    r"\$?\s?(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?:\s?(k))?",
    re.IGNORECASE,
)

# Sinónimos para que el bot reconozca la categoría aunque no escriban el nombre exacto.
# Se buscan dentro de la cuenta ya identificada, así "Belleza" de Linda y de Lindo no se mezclan.
SINONIMOS_CATEGORIA = {
    "Deporte":        ["gimnasio", "gym", "pilates", "yoga", "crossfit", "entrenamiento", "zapatillas"],
    "Salir a comer":  ["restaurant", "restaurante", "almuerzo afuera", "cena afuera", "delivery"],
    "Belleza":        ["pelo", "peluqueria", "peluquería", "manicure", "pedicure", "spa",
                        "corte de pelo", "unas", "uñas", "maquillaje"],
    "Cafecitos":      ["cafe", "café", "starbucks", "cafeteria", "cafetería", "capuccino", "latte"],
    "Salidas":        ["cine", "bar", "trago", "junta", "juntarse"],
    "Supermercado":   ["super", "lider", "líder", "jumbo", "unimarc", "tottus", "santa isabel"],
    "Farmacia":       ["remedios", "doctor", "cruz verde", "salcobrand", "ahumada", "medicamentos"],
    "Perros":         ["veterinario", "paseador", "vacuna"],
    "Regalos":        ["regalo", "cumpleanos", "cumpleaños", "cumple", "aniversario"],
}


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

def _match_categoria_en_cuenta(cuenta: str, texto_norm: str, texto_original: str):
    """Busca el ícono, el nombre, o un sinónimo de cualquier categoría de esa cuenta en el
    texto. El ícono va primero porque es inequívoco (a diferencia de sinónimos como
    'starbucks', que a veces el usuario quiere dejar como parte de la descripción)."""
    for cat in CUENTAS_CONFIG[cuenta]["categorias"]:
        icono = ICONOS_CATEGORIA.get(cat)
        if icono and icono in texto_original:
            idx = texto_original.find(icono)
            return cat, texto_original[idx:idx + len(icono)]

    for cat in CUENTAS_CONFIG[cuenta]["categorias"]:
        candidatos = [cat] + SINONIMOS_CATEGORIA.get(cat, [])
        for candidato in candidatos:
            cand_norm = normalizar_texto(candidato)
            m = re.search(rf"\b{re.escape(cand_norm)}\b", texto_norm)
            if m:
                return cat, texto_original[m.start():m.end()]
    return None, None

def parse_directo(texto_original: str, numero: str = None):
    """Detecta 'cuenta categoría monto [comentario]' en cualquier orden dentro del texto,
    ej: 'Lindo cafecitos 5.000 Starbucks' o 'Starbucks 5.000, cafecitos de Lindo'.
    También reconoce un código directo de la pestaña Presupuestos (ej: 'LN01 5.000 Starbucks'),
    y si no dicen la cuenta pero la categoría es ambigua (ej: 'Belleza' existe en más de una
    cuenta), la resuelve solo según quién escribe (CUENTA_PREDETERMINADA_POR_TELEFONO)."""
    texto_norm = normalizar_texto(texto_original)

    cuenta_encontrada = None
    categoria_encontrada = None
    cuenta_match = None
    categoria_match = None

    # 1) ¿Hay un código de presupuesto en el mensaje? (ej: "LN01", "CA03")
    for token in re.findall(r"\b[A-Za-z]{1,4}\d{1,3}\b", texto_original):
        codigo = token.upper()
        if codigo in CODIGOS:
            cuenta_encontrada, categoria_encontrada = CODIGOS[codigo]
            cuenta_match = categoria_match = token  # se descuenta una sola vez de la descripción
            break

    # 2) Si no hay código, busca por nombre o ícono de cuenta explícito en el texto
    if not cuenta_encontrada:
        for cuenta in CUENTAS_CONFIG:
            icono_c = ICONOS_CUENTA.get(cuenta)
            if icono_c and icono_c in texto_original:
                idx = texto_original.find(icono_c)
                cuenta_encontrada = cuenta
                cuenta_match = texto_original[idx:idx + len(icono_c)]
                break
            cuenta_norm = normalizar_texto(cuenta)
            m = re.search(rf"\b{re.escape(cuenta_norm)}\b", texto_norm)
            if m:
                cuenta_encontrada = cuenta
                cuenta_match = texto_original[m.start():m.end()]
                break

        if cuenta_encontrada:
            # Dijeron la cuenta explícita → busca la categoría SOLO dentro de esa cuenta
            categoria_encontrada, categoria_match = _match_categoria_en_cuenta(
                cuenta_encontrada, texto_norm, texto_original
            )
            if not categoria_encontrada:
                return None
        else:
            # No dijeron ninguna cuenta → busca la categoría en TODAS las cuentas
            coincidencias = []  # [(cuenta, categoria, texto_match), ...]
            for cuenta in CUENTAS_CONFIG:
                cat, match = _match_categoria_en_cuenta(cuenta, texto_norm, texto_original)
                if cat:
                    coincidencias.append((cuenta, cat, match))

            if not coincidencias:
                return None
            elif len(coincidencias) == 1:
                cuenta_encontrada, categoria_encontrada, categoria_match = coincidencias[0]
            else:
                # Categoría ambigua (ej: "Belleza" en Linda y en Lindo) → resuelve por quién escribe
                cuentas_posibles = {c for c, _, _ in coincidencias}
                dueño = CUENTA_PREDETERMINADA_POR_TELEFONO.get(numero) if numero else None
                if dueño and dueño in cuentas_posibles:
                    cuenta_encontrada, categoria_encontrada, categoria_match = next(
                        c for c in coincidencias if c[0] == dueño
                    )
                else:
                    return None  # ambiguo y no sabemos de quién es → mejor no adivinar

    resultado_monto = extraer_monto(texto_original)
    if not resultado_monto:
        return None
    monto, texto_monto = resultado_monto

    descripcion = texto_original
    patrones = [p for p in {cuenta_match, categoria_match, texto_monto} if p]
    for patron in patrones:
        descripcion = re.sub(re.escape(patron), " ", descripcion, count=1, flags=re.IGNORECASE)
    descripcion = re.sub(r"[,\.]+", " ", descripcion)
    descripcion = re.sub(r"\s+", " ", descripcion).strip(" ,.-")
    for _ in range(3):
        nueva = re.sub(r"^(de|del|en|la|el)\s+", "", descripcion, flags=re.IGNORECASE)
        nueva = re.sub(r"\s+(de|del|en|la|el)$", "", nueva, flags=re.IGNORECASE)
        if nueva == descripcion:
            break
        descripcion = nueva.strip()

    return {
        "cuenta": cuenta_encontrada,
        "categoria": categoria_encontrada,
        "monto": monto,
        "descripcion": descripcion or None,
    }

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

RECORDATORIOS = [
    "✨ Recuerda ahorrar para la casa",
    "🏡 Un poquito cada día suma para la casa",
    "💛 Vamos bien, sigan cuidando la plata de la casa",
]

async def enviar_bienvenida(to: str, nombre: str):
    sesiones[to] = {}
    saludo = saludo_hora()
    recordatorio = RECORDATORIOS[datetime.now(timezone.utc).day % len(RECORDATORIOS)]
    texto = (
        f"{saludo}, {nombre} 💛\n"
        f"¿En qué gastaste? Elige una cuenta, o escribe *resumen* para ver cómo van "
        f"(o *categorias* para ver todas con su código).\n\n"
        f"{recordatorio}"
    )
    botones = [{"id": f"cuenta_{_slug(c)}", "title": f"{icono_cuenta(c)} Gastos {c}"} for c in CUENTAS_CONFIG]
    await enviar_botones(to, texto, botones)

async def enviar_categorias(to: str, cuenta: str):
    categorias = CUENTAS_CONFIG[cuenta]["categorias"]
    filas = [{"id": f"cat__{_slug(cuenta)}__{_slug(cat)}", "title": f"{icono_categoria(cat)} {cat}"} for cat in categorias]
    await enviar_lista(to, f"Buenísimo, ¿en qué se fue la plata de {icono_cuenta(cuenta)} {cuenta}?", "Elegir categoría", filas)

SALUDOS = {"hola", "hi", "hey", "buenas", "menu", "menú", "hello", "buenos dias",
           "buenos días", "buenas tardes", "buenas noches"}

RESUMEN_PALABRAS = {"resumen", "presupuesto", "presupuestos", "como vamos", "cómo vamos", "balance"}

CATEGORIAS_PALABRAS = {"categorias", "categoria", "ver categorias", "lista de categorias", "codigos"}

SYNC_PALABRAS = {"sincronizar", "actualizar", "recargar"}

ELIMINAR_PALABRAS = {"eliminar", "borrar", "borra", "elimina"}
MODIFICAR_PALABRAS = {"modificar", "editar", "corregir", "modifica", "edita"}
CANCELAR_PALABRAS = {"cancelar", "cancela"}

def _nombre_de(numero: str, value: dict) -> str:
    profile_name = None
    try:
        profile_name = value["contacts"][0]["profile"]["name"]
    except (KeyError, IndexError):
        pass
    return MIEMBROS.get(numero, profile_name or numero)

async def _guardar_gasto(numero: str, nombre: str, cuenta, categoria: str, monto: float,
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
    await guardar_en_sheet(gasto)
    return gasto

def _confirmacion(gasto: dict) -> str:
    partes = []
    if gasto.get("cuenta"):
        partes.append(f"{icono_cuenta(gasto['cuenta'])} {gasto['cuenta']}")
    partes.append(f"{icono_categoria(gasto['categoria'])} {gasto['categoria']}")
    texto = f"✅ ¡Anotado! {' · '.join(partes)} {fmt_monto(gasto['monto'])}"
    if gasto.get("descripcion"):
        texto += f" ({gasto['descripcion']})"
    return texto

def _fecha_corta(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return f"{dt.day:02d} {MESES_ES.get(dt.month, '')[:3]}"
    except (ValueError, TypeError):
        return ""

def _buscar_gasto_por_id(gasto_id):
    for g in gastos:
        if g["id"] == gasto_id:
            return g
    return None

def _resumen_gasto_corto(gasto: dict) -> str:
    partes = []
    if gasto.get("cuenta"):
        partes.append(f"{icono_cuenta(gasto['cuenta'])} {gasto['cuenta']}")
    partes.append(f"{icono_categoria(gasto['categoria'])} {gasto['categoria']}")
    texto = f"{' · '.join(partes)} {fmt_monto(gasto['monto'])}"
    if gasto.get("descripcion"):
        texto += f" ({gasto['descripcion']})"
    return texto

async def enviar_lista_gastos(to: str, accion: str):
    """accion = 'eliminar' o 'modificar'. Muestra los últimos 10 gastos, tocables
    O elegibles escribiendo el número — lo que sea más cómodo."""
    recientes = gastos[:10]
    if not recientes:
        sesiones[to] = {}
        await enviar_mensaje(to, "Todavía no hay ningún gasto registrado 🤷")
        return

    prefijo = "del" if accion == "eliminar" else "mod"
    filas = []
    lineas_numeradas = []
    for i, g in enumerate(recientes, start=1):
        icat = icono_categoria(g["categoria"])
        cuenta_txt = f"{g['cuenta']} · " if g.get("cuenta") else ""
        titulo = f"{icat} {g['categoria']} {fmt_monto(g['monto'])}"
        descripcion_fila = f"{cuenta_txt}{_fecha_corta(g['created_at'])}"
        if g.get("descripcion"):
            descripcion_fila += f" · {g['descripcion']}"
        filas.append({"id": f"{prefijo}__{g['id']}", "title": titulo[:24], "description": descripcion_fila[:72]})
        lineas_numeradas.append(f"{i}. {_resumen_gasto_corto(g)}")

    sesiones[to] = {"accion": accion, "candidatos": [g["id"] for g in recientes]}
    verbo = "eliminar" if accion == "eliminar" else "modificar"
    intro = f"¿Cuál quieres {verbo}? Tócalo de la lista, o mándame el número:\n\n" + "\n".join(lineas_numeradas)
    await enviar_lista(to, intro, "Elegir gasto", filas)

async def procesar_eliminacion(to: str, gasto_id: int):
    gasto = _buscar_gasto_por_id(gasto_id)
    sesiones[to] = {}
    if not gasto:
        await enviar_mensaje(to, "Mmm, ese gasto ya no está — puede que alguien más lo haya borrado antes 🤷")
        return
    gastos.remove(gasto)
    await eliminar_gasto_en_sheet(gasto)
    await enviar_mensaje(to, f"🗑️ Listo, eliminé {_resumen_gasto_corto(gasto)}.")

async def procesar_seleccion_modificar(to: str, gasto_id: int):
    gasto = _buscar_gasto_por_id(gasto_id)
    if not gasto:
        sesiones[to] = {}
        await enviar_mensaje(to, "Mmm, ese gasto ya no está — puede que alguien más lo haya borrado antes 🤷")
        return
    sesiones[to] = {"accion": "modificar_monto", "gasto_id": gasto_id}
    await enviar_mensaje(
        to,
        f"Dale, {_resumen_gasto_corto(gasto)} — mándame el monto correcto (puedes agregar un "
        f"comentario nuevo también), o escribe *cancelar*.",
    )

async def procesar_modificacion_monto(to: str, gasto_id: int, monto_nuevo: float, descripcion_nueva):
    gasto = _buscar_gasto_por_id(gasto_id)
    sesiones[to] = {}
    if not gasto:
        await enviar_mensaje(to, "Mmm, ese gasto ya no está — puede que alguien más lo haya borrado antes 🤷")
        return
    gasto["monto"] = monto_nuevo
    if descripcion_nueva:
        gasto["descripcion"] = descripcion_nueva
    await actualizar_gasto_en_sheet(gasto)
    await enviar_mensaje(to, f"✏️ ¡Listo! Quedó en {_resumen_gasto_corto(gasto)}")
    estado = _estado_texto(gasto)
    if estado:
        await enviar_mensaje(to, estado)

SUMAR_PALABRAS_INICIO = {"sumar", "agregar", "anadir", "aumentar"}

def _detectar_comando_sumar(texto: str):
    """Si el mensaje empieza con 'sumar/agregar/añadir/aumentar [a] ...', devuelve el resto
    del texto (lo que sigue). Si no empieza con ninguna de esas palabras, devuelve None."""
    palabras = texto.strip().split(None, 1)
    if not palabras or normalizar_texto(palabras[0]) not in SUMAR_PALABRAS_INICIO:
        return None
    resto = palabras[1] if len(palabras) > 1 else ""
    resto_palabras = resto.split(None, 1)
    if resto_palabras and normalizar_texto(resto_palabras[0]) == "a":  # "sumar A Linda..."
        resto = resto_palabras[1] if len(resto_palabras) > 1 else ""
    return resto.strip()

async def procesar_comando_sumar(numero: str, resto: str):
    if not resto:
        await enviar_mensaje(numero, 'Cuéntame a qué le sumamos plata, ej: "sumar Linda belleza 3.000"')
        return

    directo = parse_directo(resto, numero)
    if directo:
        cuenta, categoria, monto = directo["cuenta"], directo["categoria"], directo["monto"]
        config = CUENTAS_CONFIG[cuenta]
        anterior = config["categorias"].get(categoria) or 0
        nuevo = anterior + monto
        config["categorias"][categoria] = nuevo
        await actualizar_presupuesto_en_sheet(cuenta, categoria, nuevo)
        await enviar_mensaje(
            numero,
            f"➕ ¡Sumé {fmt_monto(monto)}! {icono_categoria(categoria)} {categoria} ({icono_cuenta(cuenta)} {cuenta}) "
            f"ahora tiene {fmt_monto(nuevo)} de presupuesto 💛",
        )
        return

    # Sin categoría → intenta 'cuenta monto', para el tope general de la cuenta
    texto_norm = normalizar_texto(resto)
    cuenta_total = None
    for cuenta in CUENTAS_CONFIG:
        if re.search(rf"\b{re.escape(normalizar_texto(cuenta))}\b", texto_norm):
            cuenta_total = cuenta
            break
    resultado_monto = extraer_monto(resto)

    if cuenta_total and resultado_monto:
        monto, _ = resultado_monto
        config = CUENTAS_CONFIG[cuenta_total]
        anterior = config.get("presupuesto_total") or 0
        nuevo = anterior + monto
        config["presupuesto_total"] = nuevo
        await actualizar_presupuesto_en_sheet(cuenta_total, None, nuevo)
        await enviar_mensaje(
            numero,
            f"➕ ¡Sumé {fmt_monto(monto)}! El presupuesto general de {icono_cuenta(cuenta_total)} {cuenta_total} "
            f"ahora es {fmt_monto(nuevo)} 💛",
        )
        return

    await enviar_mensaje(
        numero,
        'No pesqué a qué categoría o cuenta sumarle 🤔 Prueba así: "sumar Linda belleza 3.000"',
    )

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
    if pct >= 0.90:
        return "🟠"
    if pct >= 0.75:
        return "🟡"
    return "🟢"

# Umbrales de aviso, de mayor a menor — se manda solo el más alto que corresponda,
# para no repetir 3 mensajes por el mismo gasto.
UMBRALES_AVISO = [
    (1.00, "🔴", "se pasaron un poquito del presupuesto"),
    (0.90, "🟠", "ojo, ya van en el 90% del presupuesto"),
    (0.75, "🟡", "cariño, ya llevan el 75% del presupuesto"),
]

def _mensaje_umbral(nombre: str, total: float, limite) -> str:
    if not limite or limite <= 0:
        return None
    pct = total / limite
    for umbral, emoji, texto in UMBRALES_AVISO:
        if pct >= umbral:
            frase = f"{texto[0].upper()}{texto[1:]}"
            if umbral >= 1.0:
                return (f"{emoji} {frase} de *{nombre}*: {fmt_monto(total)} / {fmt_monto(limite)} "
                         f"(+{fmt_monto(total - limite)})")
            return f"{emoji} {frase} de *{nombre}*: {fmt_monto(total)} / {fmt_monto(limite)}"
    return None

def _avisos_presupuesto(gasto: dict) -> list:
    avisos = []
    cuenta = gasto.get("cuenta")
    categoria = gasto.get("categoria")
    if not cuenta or cuenta not in CUENTAS_CONFIG:
        return avisos

    config = CUENTAS_CONFIG[cuenta]

    limite_cat = config["categorias"].get(categoria)
    total_cat = total_categoria_en_cuenta(cuenta, categoria)
    msg_cat = _mensaje_umbral(categoria, total_cat, limite_cat)
    if msg_cat:
        avisos.append(msg_cat)

    limite_total = config.get("presupuesto_total")
    total_c = total_cuenta(cuenta)
    msg_cuenta = _mensaje_umbral(f"la cuenta {cuenta}", total_c, limite_total)
    if msg_cuenta:
        avisos.append(msg_cuenta)

    return avisos

def _estado_texto(gasto: dict):
    """Línea de estado tras guardar: progreso de la categoría + cuánto lleva la cuenta en total."""
    cuenta = gasto.get("cuenta")
    categoria = gasto.get("categoria")
    if not cuenta or cuenta not in CUENTAS_CONFIG:
        return None

    config = CUENTAS_CONFIG[cuenta]
    partes = []
    icat = icono_categoria(categoria)
    icu = icono_cuenta(cuenta)

    limite_cat = config["categorias"].get(categoria)
    total_cat = total_categoria_en_cuenta(cuenta, categoria)
    if limite_cat:
        emoji = _emoji_progreso(total_cat, limite_cat)
        restante = limite_cat - total_cat
        extra = f" (quedan {fmt_monto(restante)})" if restante >= 0 else f" (pasado por {fmt_monto(-restante)})"
        partes.append(f"{emoji}{icat} {categoria}: {fmt_monto(total_cat)}/{fmt_monto(limite_cat)}{extra}")
    else:
        partes.append(f"{icat} {categoria}: {fmt_monto(total_cat)}")

    total_c = total_cuenta(cuenta)
    limite_total = config.get("presupuesto_total")
    if limite_total:
        emoji_total = _emoji_progreso(total_c, limite_total)
        restante_total = limite_total - total_c
        extra_total = f" (quedan {fmt_monto(restante_total)})" if restante_total >= 0 else f" (pasado por {fmt_monto(-restante_total)})"
        partes.append(f"{emoji_total}{icu} Total {cuenta}: {fmt_monto(total_c)}/{fmt_monto(limite_total)}{extra_total}")
    else:
        partes.append(f"{icu} Total {cuenta}: {fmt_monto(total_c)}")

    return " · ".join(partes)

def construir_lista_categorias() -> str:
    """Chuleta con todas las categorías: ícono, nombre, código y a qué cuenta pertenecen."""
    inverso = {}
    for codigo, (cu, ca) in CODIGOS.items():
        inverso[(cu, ca)] = codigo  # si hay más de un código para el mismo par, gana el último (el más nuevo)

    lineas = ["📋 Estas son las categorías:"]
    for cuenta, config in CUENTAS_CONFIG.items():
        lineas.append(f"\n*{icono_cuenta(cuenta)} {cuenta}*")
        if not config["categorias"]:
            lineas.append("  (sin categorías todavía)")
            continue
        for cat in config["categorias"]:
            codigo = inverso.get((cuenta, cat))
            sufijo = f" — {codigo}" if codigo else ""
            lineas.append(f"{icono_categoria(cat)} {cat}{sufijo}")
    return "\n".join(lineas)

def construir_resumen() -> str:
    ahora = datetime.now(timezone.utc)
    mes_label = f"{MESES_ES.get(ahora.month, ahora.month)} {ahora.year}"
    lineas = [f"📊 Así vamos en {mes_label}"]

    for cuenta, config in CUENTAS_CONFIG.items():
        lineas.append(f"\n*{icono_cuenta(cuenta)} {cuenta}*")
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
            icat = icono_categoria(cat)
            if limite:
                emoji = _emoji_progreso(total_cat, limite)
                extra = f" (+{fmt_monto(total_cat - limite)})" if total_cat > limite else ""
                lineas.append(f"  {emoji}{icat} {cat}: {fmt_monto(total_cat)} / {fmt_monto(limite)}{extra}")
            else:
                lineas.append(f"  {icat} {cat}: {fmt_monto(total_cat)}")

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
                await enviar_mensaje(numero, "Mmm, esa cuenta no la tengo 😅 escríbeme *hola* y probamos de nuevo.")
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
                await enviar_mensaje(numero, "Uy, esa categoría no la pesqué 😅 escríbeme *hola* y probamos de nuevo.")
                return {"status": "ok"}
            sesiones[numero] = {"cuenta": cuenta, "categoria": categoria}
            limite = CUENTAS_CONFIG[cuenta]["categorias"].get(categoria)
            extra = f" (presupuesto {fmt_monto(limite)})" if limite else ""
            await enviar_mensaje(
                numero,
                f"Dale, {icono_categoria(categoria)} {categoria}{extra} 💛 ¿Cuánto fue? (cuéntame en qué si quieres, ej: \"8.500 cena con amigas\")",
            )
            return {"status": "ok"}

        if reply_id and reply_id.startswith("del__"):
            try:
                gasto_id = int(reply_id[len("del__"):])
            except ValueError:
                gasto_id = None
            if gasto_id is not None:
                await procesar_eliminacion(numero, gasto_id)
            return {"status": "ok"}

        if reply_id and reply_id.startswith("mod__"):
            try:
                gasto_id = int(reply_id[len("mod__"):])
            except ValueError:
                gasto_id = None
            if gasto_id is not None:
                await procesar_seleccion_modificar(numero, gasto_id)
            return {"status": "ok"}

        await enviar_mensaje(numero, "Uy, no entendí esa opción 😅 escríbeme *hola* y empezamos de nuevo.")
        return {"status": "ok"}

    if mensaje.get("type") != "text":
        await enviar_mensaje(numero, 'Por ahora solo entiendo mensajes de texto, amor — mándame algo tipo "comida 5.000" 🙂')
        return {"status": "ok"}

    texto = mensaje["text"]["body"]
    texto_lower = normalizar_texto(texto.strip())

    # ── Saludo → muestra los botones de cuenta ──
    if texto_lower in SALUDOS:
        await enviar_bienvenida(numero, nombre)
        return {"status": "ok"}

    # ── Pedir el resumen de presupuestos (refresca gastos Y presupuestos desde Sheets primero) ──
    if texto_lower in RESUMEN_PALABRAS:
        await sincronizar_todo()
        await enviar_mensaje(numero, construir_resumen())
        return {"status": "ok"}

    # ── Pedir la lista de categorías (con ícono, código y cuenta) ──
    if texto_lower in CATEGORIAS_PALABRAS:
        if sheets_configurado():
            await asyncio.to_thread(_cargar_presupuestos_desde_sheet)
        await enviar_mensaje(numero, construir_lista_categorias())
        return {"status": "ok"}

    # ── Forzar resincronización manual con la planilla (por si editaron algo a mano) ──
    if texto_lower in SYNC_PALABRAS:
        if not sheets_configurado():
            await enviar_mensaje(numero, "No tengo Google Sheets conectado todavía 🤔")
            return {"status": "ok"}
        await sincronizar_todo()
        await enviar_mensaje(numero, f"🔄 Listo, sincronicé todo con la planilla — llevamos {len(gastos)} gastos anotados.")
        return {"status": "ok"}

    # ── Pedir eliminar o modificar un gasto → manda la lista de los últimos 10 ──
    if texto_lower in ELIMINAR_PALABRAS:
        await enviar_lista_gastos(numero, "eliminar")
        return {"status": "ok"}

    if texto_lower in MODIFICAR_PALABRAS:
        await enviar_lista_gastos(numero, "modificar")
        return {"status": "ok"}

    # ── Cancelar un eliminar/modificar en curso ──
    if texto_lower in CANCELAR_PALABRAS and sesion.get("accion"):
        sesiones[numero] = {}
        await enviar_mensaje(numero, "Ya, quedó como estaba 🙂 escribe *hola* si quieres registrar algo.")
        return {"status": "ok"}

    # ── Eligieron un gasto de la lista escribiendo el número en vez de tocarlo ──
    if sesion.get("accion") in ("eliminar", "modificar") and texto.strip().isdigit():
        candidatos = sesion.get("candidatos", [])
        idx = int(texto.strip())
        if 1 <= idx <= len(candidatos):
            gasto_id = candidatos[idx - 1]
            if sesion["accion"] == "eliminar":
                await procesar_eliminacion(numero, gasto_id)
            else:
                await procesar_seleccion_modificar(numero, gasto_id)
        else:
            await enviar_mensaje(numero, f"Ese número no está en la lista 😅 elige uno del 1 al {len(candidatos)}, o escribe *cancelar*.")
        return {"status": "ok"}

    # ── Ya eligieron qué gasto modificar → este mensaje trae el monto nuevo ──
    if sesion.get("accion") == "modificar_monto":
        resultado_monto = extraer_monto(texto)
        if not resultado_monto:
            await enviar_mensaje(numero, "No pesqué el monto ahí 🤔 mándame el número nuevo, o escribe *cancelar*.")
            return {"status": "ok"}
        monto_nuevo, texto_monto = resultado_monto
        descripcion_nueva = re.sub(r"\s+", " ", texto.replace(texto_monto, "")).strip() or None
        await procesar_modificacion_monto(numero, sesion["gasto_id"], monto_nuevo, descripcion_nueva)
        return {"status": "ok"}

    # ── "Sumar/agregar/añadir/aumentar" → sube un presupuesto, NO registra un gasto ──
    resto_sumar = _detectar_comando_sumar(texto)
    if resto_sumar is not None:
        await procesar_comando_sumar(numero, resto_sumar)
        return {"status": "ok"}

    # ── Código de presupuesto solo, sin monto (ej: "LN01") → arma la sesión y pide el monto ──
    codigo_directo = texto.strip().upper()
    if codigo_directo in CODIGOS:
        cuenta, categoria = CODIGOS[codigo_directo]
        sesiones[numero] = {"cuenta": cuenta, "categoria": categoria}
        limite = CUENTAS_CONFIG[cuenta]["categorias"].get(categoria)
        extra = f" (presupuesto {fmt_monto(limite)})" if limite else ""
        await enviar_mensaje(
            numero,
            f"Dale, {icono_categoria(categoria)} {categoria} de {icono_cuenta(cuenta)} {cuenta}{extra} 💛 ¿Cuánto fue? (puedes contarme en qué, ej: \"8.500 cena con amigas\")",
        )
        return {"status": "ok"}

    # ── Atajo: detecta 'cuenta categoría monto' directo en el texto (ej: "Lindo cafecitos 5.000 Starbucks") ──
    directo = parse_directo(texto, numero)
    if directo:
        gasto = await _guardar_gasto(numero, nombre, directo["cuenta"], directo["categoria"],
                                      directo["monto"], directo["descripcion"], texto, wa_id)
        await enviar_mensaje(numero, _confirmacion(gasto))
        avisos = _avisos_presupuesto(gasto)
        for aviso in avisos:
            await enviar_mensaje(numero, aviso)
        estado = _estado_texto(gasto)
        if estado:
            await enviar_mensaje(numero, estado)
        sesiones[numero] = {}
        return {"status": "ok"}

    # ── Si ya eligió cuenta + categoría, este mensaje es el monto (+ comentario opcional) ──
    if sesion.get("cuenta") and sesion.get("categoria"):
        resultado_monto = extraer_monto(texto)
        if not resultado_monto:
            await enviar_mensaje(numero, "No pesqué el monto ahí 🤔 mándame el número, ej: 5.000 (puedes contarme en qué fue igual)")
            return {"status": "ok"}
        monto, texto_monto = resultado_monto
        descripcion = re.sub(r"\s+", " ", texto.replace(texto_monto, "")).strip() or None

        gasto = await _guardar_gasto(numero, nombre, sesion["cuenta"], sesion["categoria"],
                                      monto, descripcion, texto, wa_id)
        await enviar_mensaje(numero, _confirmacion(gasto))
        avisos = _avisos_presupuesto(gasto)
        for aviso in avisos:
            await enviar_mensaje(numero, aviso)
        estado = _estado_texto(gasto)
        if estado:
            await enviar_mensaje(numero, estado)
        sesiones[numero] = {}  # limpia la sesión, listo para el próximo gasto
        return {"status": "ok"}

    # ── Formato libre de siempre: "categoría descripción monto" (sin cuenta asignada) ──
    parsed = parse_message(texto)
    if not parsed:
        await enviar_mensaje(
            numero,
            'Mmm, no pesqué el monto ahí 🤔 Escríbeme *hola* para usar los botones, o mándalo así: '
            '"categoría descripción monto" (ej: "comida almuerzo 5.000")',
        )
        return {"status": "ok"}

    gasto = await _guardar_gasto(numero, nombre, None, parsed["categoria"], parsed["monto"],
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
            {"nombre": cat, "gastado": total_categoria_en_cuenta(cuenta, cat), "presupuesto": limite,
             "icono": icono_categoria(cat)}
            for cat, limite in config["categorias"].items()
        ]
        cuentas.append({
            "nombre": cuenta,
            "gastado": total_cuenta(cuenta),
            "presupuesto": config.get("presupuesto_total"),
            "icono": icono_cuenta(cuenta),
            "categorias": categorias,
        })
    return {"mes": f"{MESES_ES.get(ahora.month, ahora.month)} {ahora.year}", "cuentas": cuentas}

@app.get("/iconos")
def iconos_json():
    return {"cuentas": ICONOS_CUENTA, "categorias": ICONOS_CATEGORIA}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "bot": "gastos",
        "gastos_registrados": len(gastos),
        "miembros": len(MIEMBROS),
        "google_sheets": sheets_configurado(),
    }

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
