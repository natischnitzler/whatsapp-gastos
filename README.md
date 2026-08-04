# Chat de gastos por WhatsApp

Versión en Python/FastAPI: un solo `main.py`, el estado vive en memoria
(`gastos = []`) y, si conectas un Google Sheet (opcional pero recomendado),
también se respalda ahí — así no se pierde nada si el servidor se reinicia.

**Sin Google Sheets conectado:** los gastos viven solo en la memoria del proceso.
Si el servidor se reinicia (nuevo deploy, o el hosting "duerme" por inactividad),
**la lista se vacía**. Usa `/export` para respaldar a mano cuando quieras.

**Con Google Sheets conectado (ver Paso 0 más abajo):** cada gasto se escribe
también como fila en tu planilla, y al arrancar el servidor **recarga todo el
historial desde ahí** — el reinicio deja de ser un problema.

## Cómo se registra un gasto

**Opción 1 — con botones (recomendado):**
1. Escribe `hola` (o `hi`, `menu`, `buenas`)
2. Te saluda por tu nombre y según la hora (🌅 Buenos días / ☀️ Buenas tardes / 🌙 Buenas noches,
   hora de Chile), y te muestra 3 botones: *Gastos Casa*, *Gastos Lindo*, *Gastos Linda*
   (el nombre sale de la variable `MIEMBROS`; si tu número no está ahí, usa el nombre de tu perfil de WhatsApp)
3. Tocas uno → te aparece una lista con las categorías
4. Eliges categoría → te dice el presupuesto y pregunta el monto
5. Escribes el monto (puedes agregar una descripción, ej: `almuerzo 8.500`) → queda guardado,
   y te dice cuánto les queda en esa categoría (o el aviso de sobregiro si ya se pasaron)

**Opción 2 — código directo (más rápido, ideal si ya se lo saben de memoria):**Cada categoría tiene un código corto (se genera solo, ej: `LN01` para la primera
categoría de Linda). Pueden mandarlo solo o con el monto en el mismo mensaje:
- `LN01` → el bot reconoce cuenta y categoría, y pregunta el monto
- `LN01 5.000 spinning` → queda registrado directo, sin preguntar nada
- Los códigos exactos (y se pueden renombrar) están en la pestaña "Presupuestos"
  del Excel — ver más abajo.

**Opción 3 — texto directo, sin botones ni códigos (detecta todo solo):**
Si mencionas la cuenta y la categoría en el mismo mensaje, el bot lo reconoce al
tiro y no hace falta tocar nada:
- `Lindo cafecitos 5.000 Starbucks`
- `Casa supermercado 15.000 verduras`
- `Starbucks 3.000, cafecitos de Lindo` (el orden no importa)

**Ni siquiera hace falta decir la cuenta**, si la categoría no es ambigua:
- `perros 12.000 vacuna` → reconoce Casa solo, porque "Perros" solo existe ahí

Si la categoría **sí existe en más de una cuenta** (ej: "Belleza" está en Linda
y en Lindo), el bot resuelve solo según **quién escribe**, usando
`CUENTA_PREDETERMINADA_POR_TELEFONO` en `main.py`:
```python
CUENTA_PREDETERMINADA_POR_TELEFONO = {
    "56985495930": "Linda",
    "56945676306": "Lindo",
}
```
Así, si Linda manda `belleza 20.000`, queda en su cuenta; si Lindo manda lo
mismo, queda en la suya — sin tener que aclarar nada. Si el número de quien
escribe no está en esa lista, el bot prefiere no adivinar (evita asignarlo mal)
y no lo reconoce por ese camino — en ese caso hay que decir la cuenta explícita,
usar el código, o los botones.

**Opción 4 — texto libre genérico (si no se reconoce cuenta+categoría):**
`<lo que quieras> <categoría> <monto>` — el parser busca el **último número**
como monto y una **palabra clave de categoría** en cualquier parte del mensaje.
Esta opción no pregunta "cuenta" (Casa/Lindo/Linda) — queda sin asignar.

- `comida almuerzo 5.000`
- `Lindo comida 5.000`
- `bencina 20.000`

Categorías, cuentas y presupuestos editables en `main.py` (`CUENTAS_CONFIG`) — ver detalle abajo.

## Eliminar o modificar un gasto

Escribe **`eliminar`** (o `borrar`) o **`modificar`** (o `editar`, `corregir`) y
el bot te manda los últimos 10 gastos, como lista tocable **y** numerada en el
mismo mensaje — puedes tocar la opción o simplemente responder con el número:

```
¿Cuál quieres eliminar? Tócalo de la lista, o mándame el número:

1. 💙 Lindo · ☕ Cafecitos $3.000 (starbucks)
2. 💛 Linda · 💅 Belleza $20.000 (corte)
3. 🏠 Casa · 🛒 Supermercado $30.000 (lider)
```

- **Eliminar**: lo saca de la lista, de la planilla (si Sheets está conectado)
  y recalcula el resumen del mes — todo al tiro.
- **Modificar**: por ahora deja cambiar el **monto** (y de paso la descripción,
  si escribes algo más junto al número) — es el caso más común ("me equivoqué
  en el monto"). Si quieren cambiar la cuenta o categoría, por ahora es más
  simple eliminarlo y registrarlo de nuevo.
- En cualquier momento de este flujo pueden escribir **`cancelar`** para salir
  sin tocar nada.

## Cuentas, categorías y presupuestos

Todo se configura directamente en `main.py`, en el diccionario `CUENTAS_CONFIG`:

```python
CUENTAS_CONFIG = {
    "Casa": {
        "presupuesto_total": None,  # sin tope general, solo por categoría
        "categorias": {
            "Perros": 200000, "Tali": 100000, "Farmacia": 20000,
            "Regalos": 50000, "Supermercado": 300000, "Spid": 50000,
        },
    },
    "Linda": {
        "presupuesto_total": 350000,  # tope general de la cuenta
        "categorias": {"Deporte": 80000, "Salir a comer": 80000, "Belleza": 30000},
    },
    "Lindo": {
        "presupuesto_total": None,
        "categorias": {"Cafecitos": 40000, "Belleza": 40000, "Salidas": 35000, "Otros": None},
    },
}
```

- Cada cuenta tiene sus **propias categorías con su propio presupuesto** — por eso
  "Belleza" puede valer $30.000 en Linda y $40.000 en Lindo sin mezclarse.
- `presupuesto_total` (opcional): si lo pones, además avisa cuando la **cuenta completa**
  se pasa de ese monto, aunque ninguna categoría individual se haya pasado.
- Una categoría con `None` como presupuesto queda **sin límite** (solo se registra, nunca avisa).
- **Aviso al tiro, en 3 niveles:** apenas un gasto hace que la categoría (o la
  cuenta completa) cruce el 75%, el 90% o el 100% del presupuesto, llega un
  mensaje de advertencia — 🟡 a los 75%, 🟠 a los 90%, 🔴 al pasarse. Si ya están
  sobre un umbral, cada gasto nuevo en esa categoría lo recuerda de nuevo.
- **Resumen a pedido:** escribe `resumen` (o `presupuesto`, `como vamos`, `balance`) y el
  bot manda el estado completo, cuenta por cuenta, con el mismo semáforo:
  🟢 vas bien &nbsp;·&nbsp; 🟡 75% &nbsp;·&nbsp; 🟠 90% &nbsp;·&nbsp; 🔴 te pasaste &nbsp;·&nbsp; ⚪ sin límite
- El dashboard también muestra barras de progreso con la misma info.

⚠️ Los gastos registrados con el **formato libre** (sin pasar por los botones de "hola")
usan categorías genéricas aparte (`CATEGORIAS_LIBRES`: Comida, Transporte, etc.) y **no
cuentan para estos presupuestos** — quedan sin cuenta asignada. Para que sí cuenten, hay
que usar los botones.

## Cómo queda organizado el Excel

Cada gasto se guarda en la pestaña de **su mes** (ej: "Agosto 2026", "Septiembre
2026") — se crea sola la primera vez que hay un gasto ese mes. Columnas, en
este orden: `Fecha`, `Cuenta`, `Categoria`, `Monto`, `Descripcion`, `Quien`.

**El mes nuevo arranca solo, sin que haya que resetear nada:** como los
presupuestos y el `resumen` se calculan filtrando por mes actual, el 1° de
cada mes todo vuelve a $0 automáticamente apenas se crea la pestaña nueva —
no hay ningún paso manual que hacer.

Dentro de cada pestaña, las filas quedan **agrupadas por Cuenta y luego por
Categoria** (se reordena solo cada vez que se guarda un gasto nuevo), así es
fácil mirar de un vistazo cuánto se fue en cada cosa. Con un solo gasto no se
nota — el orden se ve a partir del segundo.

Además, el bot crea sola una pestaña **"Resumen \<Mes\> \<Año\>"** (ej: "Resumen
Agosto 2026") con la **suma total por Cuenta y por Categoria** de ese mes —
algo así:

| Cuenta | Categoria | Suma |
|---|---|---|
| Casa | Farmacia | 8.000 |
| Casa | Supermercado | 45.000 |
| Casa | **TOTAL** | **53.000** |
| Linda | Belleza | 25.000 |
| Linda | **TOTAL** | **25.000** |
| **TOTAL GENERAL** | | **78.000** |

Esta pestaña se reconstruye sola cada vez que se guarda un gasto nuevo, y
también al sincronizar (`sincronizar`, cada 5 min, o al arrancar el servidor)
— así que si editas algo a mano en la pestaña del mes, el resumen se pone al
día apenas se resincroniza.

⚠️ Si venías de una versión anterior del bot con una sola pestaña de gastos con
más columnas (Telefono, MensajeOriginal, WaMessageId), esa pestaña queda tal
cual como respaldo histórico — el bot deja de escribirle y empieza a usar las
pestañas por mes desde ahora. Si quieres, puedes copiar esas filas antiguas a
mano a la pestaña del mes que correspondía.

## Definir los presupuestos (y códigos) desde el Excel

Si conectaste Google Sheets (Paso 0), el bot crea automáticamente una **pestaña
llamada "Presupuestos"** en la misma planilla, prellenada con los montos y
códigos actuales. Columnas: `Cuenta`, `Categoria`, `Presupuesto`, `Codigo`.

- Una fila con **Categoria en blanco** = el tope general de esa cuenta (sin código)
- Dejar **Presupuesto en blanco** = sin límite para esa categoría
- La columna **Codigo** trae algo tipo `LN01`, `CA03`, etc. — pueden **cambiarlo
  por lo que quieran** (ej: renombrar `LD01` a `N01`) y el bot lo respeta apenas
  se sincronice
- Cualquier cambio de montos o códigos se relee:
  - automáticamente cada vez que alguien pide `resumen`
  - cada 5 minutos en segundo plano (ver más abajo)
  - o al tiro si escriben `sincronizar`
- Las categorías en sí (los nombres) siguen definiéndose en `main.py` — la
  planilla controla los **montos y códigos**, no agrega categorías nuevas.
- Si agregan una fila con una categoría que el bot no reconoce (ej: escrita
  mal, o de una cuenta que no existe), el bot la ignora sin problema — no
  rompe nada, simplemente no la toma en cuenta.
- Si ya tenías esta pestaña creada de una versión anterior del bot (sin columna
  Codigo), no te preocupes — el bot la detecta y **agrega la columna sola**, con
  los códigos ya rellenados, la próxima vez que sincronice.

## Compartido entre celulares, y sincronización con la planilla

- El presupuesto es **por cuenta**, no por teléfono: si tu pareja registra un
  gasto de "Casa" desde su celular, suma al mismo total de Casa que ves tú.
- Todo gasto (de cualquier celular) se escribe en la misma planilla, casi al
  tiro (menos de un segundo después de confirmarlo por WhatsApp).
- En el otro sentido — planilla → bot — se sincroniza:
  - **Automáticamente cada 5 minutos** (mientras el servidor esté despierto),
    configurable con la variable `SYNC_INTERVAL_MINUTOS`
  - Al **arrancar el servidor**
  - Cada vez que alguien pide **`resumen`** (refresca los presupuestos)
  - Al tiro si escriben **`sincronizar`** (o `actualizar`, `recargar`) — fuerza
    la recarga completa sin esperar el ciclo automático

---

## Paso 0 — (Opcional) Conectar Google Sheets

Esto hace que cada gasto quede guardado en una planilla de tu Drive, y que el
bot recupere todo el historial si el servidor se reinicia.

1. Crea una planilla nueva en [sheets.google.com](https://sheets.google.com), ponle
   un nombre (ej: "Gastos Familia"). No hace falta que le pongas encabezados, el
   bot los crea solo la primera vez.
2. Copia el **ID de la planilla**: es la parte de la URL entre `/d/` y `/edit`.
   Ej: `https://docs.google.com/spreadsheets/d/`**`1AbCdEfGhIjKlMnOpQrStUvWxYz`**`/edit`
3. Ve a [console.cloud.google.com](https://console.cloud.google.com) → crea un
   proyecto nuevo (o usa uno existente) → en el buscador escribe **"Google Sheets API"**
   → **Habilitar**.
4. En el menú lateral: **APIs y servicios → Credenciales → Crear credenciales →
   Cuenta de servicio**. Ponle un nombre (ej: "bot-gastos") → **Crear y continuar**
   → **Listo** (no hace falta darle roles).
5. Click en la cuenta de servicio recién creada → pestaña **Claves** → **Agregar
   clave → Crear clave nueva → JSON** → se descarga un archivo `.json`.
6. Abre ese archivo con un editor de texto y busca el campo `"client_email"` —
   copia ese correo (algo como `bot-gastos@tu-proyecto.iam.gserviceaccount.com`).
7. Vuelve a tu planilla de Google Sheets → botón **Compartir** → pega ese correo
   y dale permiso de **Editor**.
8. En Render (Paso 2 más abajo), agrega dos variables de entorno:
   - `GOOGLE_SHEET_ID`: el ID que copiaste en el punto 2
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: pega el **contenido completo** del archivo
     `.json` descargado, como una sola línea (ábrelo, selecciona todo, copia y pega)

Si no configuras esto, el bot sigue funcionando igual, solo que en memoria (ver
advertencia arriba).

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
   | `GOOGLE_SHEET_ID` | opcional, ver Paso 0 |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | opcional, ver Paso 0 |
   | `SYNC_INTERVAL_MINUTOS` | opcional, cada cuántos minutos resincroniza con Sheets (default: `5`) |

5. Deploy. Te da una URL tipo `https://gastos-whatsapp.onrender.com`.

> ⚠️ El plan gratis de Render "duerme" el servicio tras ~15 min sin tráfico y lo
> despierta en el próximo request (tarda unos segundos). Si conectaste Google
> Sheets (Paso 0), el historial se recupera solo al despertar; si no, se pierde.

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
| `GET /resumen` | Estado de presupuestos por cuenta y categoría en JSON (lo usa el dashboard) |
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
