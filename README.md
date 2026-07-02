# Behavior Bot v4 — Deploy en Railway

## Estructura del repositorio

```
tu-repo/
├── main.py                          ← entry point headless
├── live_worker.py                   ← motor del bot
├── requirements.txt
├── railway.toml
├── config.json                      ← NO subir a GitHub público
├── .gitignore
├── utils/
│   └── security.py                  ← copiarlo de tu proyecto original
└── models/
    ├── ethusdt/
    │   ├── behavior_ethusdt_v4_summary.json
    │   └── pretrigger_ethusdt_setups.json
    └── (o directo en models/ sin subcarpeta)
```

## Pasos para el deploy

### 1. Preparar el repositorio local

```bash
# Crear repo nuevo
mkdir bot-ethusdt && cd bot-ethusdt
git init

# Copiar los archivos de este deploy
# + live_worker.py
# + utils/security.py  (del proyecto original)
# + models/ethusdt/behavior_ethusdt_v4_summary.json
# + models/ethusdt/pretrigger_ethusdt_setups.json
```

### 2. Configurar config.json

Completar los campos en config.json:
- `api_key` y `secret_key` → tus credenciales de Binance Futures
- Verificar `leverage`, `margen_pct`, `max_pos_size`
- `demo_mode: true` para pruebas sin operar en real

### 3. IMPORTANTE — API keys en Railway como variables de entorno

En lugar de subir config.json al repo (riesgo de seguridad),
configurar las API keys como variables de entorno en Railway:

En el panel de Railway → tu servicio → Variables:
```
BINANCE_API_KEY    = tu_api_key
BINANCE_SECRET_KEY = tu_secret_key
```

Y modificar config.json para leerlas:
```json
{
  "api_key":    "${BINANCE_API_KEY}",
  "secret_key": "${BINANCE_SECRET_KEY}",
  ...
}
```

O simplemente poner los valores directamente en config.json
y asegurarse que el .gitignore lo excluye (ya está configurado).

### 4. Subir a GitHub (repo PRIVADO)

```bash
git add .
git commit -m "bot v4 initial deploy"
git branch -M main
git remote add origin https://github.com/tu-usuario/bot-ethusdt.git
git push -u origin main
```

⚠️  Usar repositorio PRIVADO siempre.

### 5. Deploy en Railway

1. Ir a railway.com → New Project → Deploy from GitHub repo
2. Seleccionar el repo
3. Railway detecta railway.toml automáticamente
4. El bot arranca con `python main.py`

### 6. Verificar logs

En Railway → tu servicio → Logs, deberías ver:
```
✅ v4 cargado | Summary: X escenarios | Setups: Y...
✅ Sesión sincronizada (v4 — gatillo 15m)
📊 Estado | precio=... | balance=... | pnl_dia=...
```

## Parámetros en config.json

| Campo | Descripción | Valor recomendado |
|---|---|---|
| leverage | Apalancamiento | 10 |
| margen_pct | % del capital por trade | 30 |
| max_pos_size | Cap de posición en USD | 1500 |
| factor_tp | Multiplicador del MFE para TP | 3.50 |
| factor_sl | Multiplicador del MFE para SL | 1.00 |
| use_short | Operar en short | true |
| demo_mode | Modo demo (sin órdenes reales) | false |
| log_level | Nivel de log | INFO |

## Modificar parámetros sin redeploy

Cambiar los valores en config.json, hacer commit y push.
Railway redespliega automáticamente en segundos.
