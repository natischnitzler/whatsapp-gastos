# Chat de gastos por WhatsApp (sin base de datos)

Versión en Python/FastAPI, mismo estilo que el bot de Aviv: un solo `main.py`,
todo el estado en memoria (`gastos = []`), sin base de datos externa.

**Trade-off importante:** como no hay base de datos, los gastos viven solo en la
memoria del proceso. Si el servidor se reinicia (nuevo deploy, o si el hosting
"duerme" por inactividad y despierta de nuevo), **la lista de gastos se vacía**.
Usa `/export` para respaldar cuando quieras. Si en algún momento quieres que no
se pierda nada, dímelo y le agregamos un archivo en disco o una base de datos —
por ahora queda tal como lo pediste.

## Formato de mensaje

`<lo que quieras> <categoría> <monto>` — el parser busca el **último número**
como monto y una **palabra clave de categoría** en cualquier parte del mensaje.

- `comida almuerzo 5.000`
- `Lindo comida 5.000`
- `bencina 20.000`

Categorías editables en `main.py`, diccionario `CATEGORIAS`.

---

## Paso 1 — Meta WhatsApp Cloud API

1. [developers.facebook.com](https://developers.facebook.com) → **Mis Apps → Crear app → Negocio**.
2. Agrega el producto **WhatsApp**.
3. En **WhatsApp → Introducción** anota:
   - `Phone number ID` → `WA_PHONE_NUMBER_ID`
   - `Temporary access token` → `WA_ACCESS_TOKEN` (dura 24h; luego se puede hacer permanente con un System User)
4. Agrega tu número y el de tu pareja como **destinatarios de prueba**.

## Paso 2 — Desplegar el backend

Como no usamos Vercel (esto es Python con estado en memoria, necesita un proceso
corriendo siempre, no funciones serverless), la forma más simple es **Render**:

1. Sube esta carpeta a un repo de GitHub.
2. Ve a [render.com](https://render.com) → **New → Web Service** → conecta el repo.
3. Configuración:
   - **Runtime:** Python (detecta `runtime.txt` automáticamente)
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. En **Environment**, agrega:

   | Variable | Valor |
   |---|---|
   | `WA_VERIFY_TOKEN` | invéntate un string, ej: `gastos2026secreto` |
   | `WA_ACCESS_TOKEN` | el token de Meta |
   | `WA_PHONE_NUMBER_ID` | el Phone number ID de Meta |
   | `ADMIN_KEY` | otra clave para los endpoints de admin (`/export`, `/reset`) |
   | `MIEMBROS` | `56912345678:Cata,56998765432:Tomas` (sin espacios extra) |

5. Deploy. Te da una URL tipo `https://gastos-whatsapp.onrender.com`.

> ⚠️ El plan gratis de Render "duerme" el servicio tras ~15 min sin tráfico y lo
> despierta en el próximo request (tarda unos segundos) — y al despertar, si fue
> un reinicio real, la memoria se resetea. Si esto te complica, aviso y vemos
> plan pago o agregar persistencia.

## Paso 3 — Conectar el webhook en Meta

1. **developers.facebook.com → tu app → WhatsApp → Configuración → Webhook → Editar**:
   - Callback URL: `https://gastos-whatsapp.onrender.com/webhook`
   - Verify token: el mismo `WA_VERIFY_TOKEN`
2. Suscríbete al campo `messages`.

Prueba mandando `comida almuerzo 5.000` al número de WhatsApp — debería confirmarte.

## Paso 4 — El dashboard

1. Abre `dashboard/index.html`, reemplaza:
   ```js
   const API_BASE = 'https://tu-backend.onrender.com';
   ```
   por la URL real de tu backend en Render.
2. Ábrelo con doble clic en tu navegador, o súbelo a cualquier hosting estático
   (GitHub Pages, Vercel, Netlify) — es un solo archivo HTML, no necesita build.
3. Como no hay base de datos con "tiempo real", el dashboard hace **polling cada
   15 segundos** al endpoint `/gastos` para refrescarse.

---

## Endpoints disponibles

| Endpoint | Qué hace |
|---|---|
| `GET /webhook` | Verificación del webhook (la usa Meta una vez) |
| `POST /webhook` | Recibe los mensajes entrantes de WhatsApp |
| `GET /gastos` | Lista de gastos en memoria (lo usa el dashboard) |
| `GET /health` | Estado del servicio |
| `GET /export?key=TU_ADMIN_KEY` | Respaldo completo en JSON |
| `GET /reset?key=TU_ADMIN_KEY` | Borra todo (úsalo con cuidado) |

## Probar el parser localmente

```bash
pip install -r requirements.txt
python3 -c "from main import parse_message; print(parse_message('Lindo comida 5.000'))"
```

## Correr el servidor localmente

```bash
export WA_VERIFY_TOKEN=test123
export MIEMBROS="56912345678:Cata,56998765432:Tomas"
uvicorn main:app --reload --port 8000
```

Para probar el webhook desde afuera (Meta necesita una URL pública) puedes usar
[ngrok](https://ngrok.com) mientras desarrollas: `ngrok http 8000`.
