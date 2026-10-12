# Cuentas 🧾 — tu contador personal en Telegram

Bot de Telegram que lleva tus finanzas personales a partir de **notas de voz o mensajes escritos**:

- Registra gastos e ingresos y **los clasifica** (aprende de tus correcciones: si le dices que "Rappi" es supermercado, la próxima vez lo clasifica así).
- Lleva tus **suscripciones y pagos fijos**: los cobra solo en cada ciclo y te avisa antes de cada cobro.
- Te **avisa de gastos críticos**: presupuesto por categoría superado, gasto del mes por encima de lo que puedes gastar sin tocar tu meta de ahorro, un ritmo que te deja en rojo a fin de mes, compras muy por encima de tu ticket habitual o categorías disparadas respecto a tu promedio.
- Responde preguntas: *¿cuánto llevo en restaurantes?*, *¿en qué gasté más el mes pasado?*, **¿debería salir hoy?** (veredicto con tu disponible, presupuesto diario y lo que sueles gastar al salir).
- **Se adapta a ti**: aprende tu ingreso típico, promedios por categoría, qué días gastas más, si prefieres voz o texto, y usa eso al responder y al alertar.
- **Mini App de Telegram** con un dashboard completo: KPIs, presupuestos, gasto acumulado contra disponible, categorías contra tu promedio, historial de 6 meses, días de la semana, movimientos editables y ajustes.

En el chat las respuestas son cortas (registro, veredicto, resumen de 5 líneas); el detalle, las gráficas y la edición viven en la Mini App.

## Arquitectura (AWS, serverless)

```
Telegram ──webhook──► API Gateway (HTTP) ──► λ webhook ──► SQS FIFO (orden por chat, dedupe por update_id)
                                                              │
                                                              ▼
                                                     λ processor ──► Claude en Amazon Bedrock (herramientas)
                                                       │    │                    │
                                     audio .ogg ──► S3 │    └──► DynamoDB ◄──────┘
                                                       ▼
                                             Amazon Transcribe ──(EventBridge: job terminado)──► λ transcribe_done ──► mismo flujo que el texto

EventBridge Scheduler (diario) ──► λ scheduler: cobra suscripciones, recordatorios, re-aprende hábitos, cierre de mes

Mini App: CloudFront ──► S3 (HTML/JS estático)
                    └──► /api/* ──► API Gateway ──► λ api (valida initData firmado por Telegram) ──► DynamoDB
```

| Pieza | Por qué |
|---|---|
| **SQS FIFO** entre webhook y procesamiento | Telegram exige respuesta rápida; el LLM y la transcripción tardan. FIFO mantiene el orden por chat y descarta reintentos duplicados de Telegram. |
| **Amazon Transcribe** (batch + EventBridge) | Las notas de voz de Telegram son OGG/Opus, formato soportado. Sin polling: EventBridge avisa cuando termina. Los audios se borran apenas se procesan (y hay lifecycle de 1 día). |
| **Claude en Bedrock** con herramientas | El modelo interpreta lenguaje natural ("20 lucas en el súper") y llama herramientas (`registrar_movimientos`, `evaluar_salida_o_compra`, `consultar_gastos`, …). **Los números siempre los calcula el código**, nunca el modelo. Si Bedrock falla, un parser de respaldo registra frases simples. |
| **DynamoDB** tabla única | Perfil, movimientos (`TX#fecha#id`), suscripciones, historial corto (TTL) y deduplicación de alertas. GSI para listar usuarios y suscripciones por fecha de cobro. |
| **Alertas deterministas** | Se calculan en `finance.py` y se envían como mensaje aparte; cada alerta se manda una sola vez (clave condicional en DynamoDB). |

### Código

```
src/cuentas/
  assistant.py     bucle de herramientas con Claude (Bedrock, SDK de Anthropic)
  tools.py         definición y ejecución de herramientas (con validación)
  ledger.py        registrar / corregir / cobrar suscripciones
  finance.py       resúmenes, alertas, "¿puedo salir?", datos del dashboard (puro, testeado)
  learning.py      aprendizaje del perfil (incremental + recálculo diario)
  categories.py    catálogo y clasificador por palabras clave + lo aprendido
  parsing.py       parser de respaldo sin LLM
  bot.py           comandos, botones (deshacer / cambiar categoría), envío de alertas
  handlers/        webhook, processor, transcribe_done, api, scheduler
webapp/            Mini App (HTML + JS sin dependencias, gráficas SVG, tema claro/oscuro de Telegram)
template.yaml      infraestructura (AWS SAM)
```

## Despliegue

Requisitos: AWS CLI, [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html), Python 3.12.

1. Crea el bot con [@BotFather](https://t.me/BotFather) y guarda el token.
2. Habilita el acceso a Claude en Amazon Bedrock (consola de Bedrock → *Model access*) en la región que uses (`BedrockRegion`, por defecto `us-east-1`).
3. Guarda los secretos en SSM:
   ```bash
   aws ssm put-parameter --name /cuentas-bot/telegram-token --type SecureString --value "<TOKEN>"
   aws ssm put-parameter --name /cuentas-bot/webhook-secret --type SecureString --value "$(openssl rand -hex 32)"
   ```
4. Despliega todo:
   ```bash
   make all STACK=cuentas-bot REGION=us-east-1
   # equivale a: sam build && sam deploy ... && subir webapp/ a S3 && registrar webhook, comandos y botón de menú
   ```
   Parámetros útiles (`sam deploy --parameter-overrides ...`): `DefaultCurrency` (ej. `COP`, `MXN`, `EUR`), `DefaultTimezone`, `TranscribeLanguage` (`es-US` o `es-ES`), `ModelId`, `LlmEffort`, `DailyScheduleUtc`.
5. Escríbele `/start` a tu bot.

Cada usuario puede cambiar su moneda, zona horaria, ingreso, día de pago y meta de ahorro hablándole al bot ("gano 4 millones y me pagan el 30") o desde *Ajustes* en la Mini App.

### Modelo

Por defecto usa `anthropic.claude-opus-5-5` mediante el cliente `AnthropicBedrockMantle` del SDK oficial de Anthropic, con esfuerzo `medium` (parámetro `LlmEffort`). Tiene activado un **respaldo ante rechazos** (`BetaRefusalFallbackMiddleware`, modelo `FallbackModelId`): si el modelo principal declina una petición por política, el SDK la reintenta con el de respaldo. El rol de las Lambdas tiene `bedrock-mantle:CreateInference`.

## Desarrollo local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
make test      # pytest (finanzas, herramientas, aprendizaje, auth de la Mini App, scheduler, bucle del asistente)
make lint      # cfn-lint del template
```

La Mini App se puede ver sin Telegram con datos de ejemplo:

```bash
cd webapp && python -m http.server 8000
# abre http://localhost:8000/?demo=1  (o ?demo=1&theme=dark)
```

## Privacidad

- Solo funciona en chats privados.
- Los audios y transcripciones se eliminan apenas se procesan; el historial de conversación expira a los 7 días.
- La API de la Mini App solo acepta peticiones con el `initData` firmado por Telegram (HMAC con el token del bot) y de menos de 24 h.
- DynamoDB y S3 cifrados en reposo; la tabla tiene PITR y `DeletionPolicy: Retain`.
