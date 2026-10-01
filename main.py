"""
main.py — Punto de entrada headless para Railway (sin UI)
=========================================================
Lee config.json, construye un logger liviano y lanza MLLiveBotWorker.
Soporta ejecución simultánea de múltiples símbolos vía Multithreading.
"""

import json
import logging
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime, timezone

# ─── Logger liviano que reemplaza el log_callback de la UI ───────────────────

class HeadlessLogger:
    """
    Reemplaza el objeto log de la UI.
    Expone los mismos métodos que usa el worker.
    Todo va a stdout para que Railway lo capture en su panel de logs.
    """

    def __init__(self, level: str = "INFO", prefix: str = ""):
        logging.basicConfig(
            stream=sys.stdout,
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self._log = logging.getLogger(f"BotV4{prefix}")
        self._prefix = f"[{prefix}] " if prefix else ""

    def info(self,     msg): self._log.info(f"{self._prefix}{msg}")
    def success(self,  msg): self._log.info(f"{self._prefix}✅ {msg}")
    def warning(self,  msg): self._log.warning(f"{self._prefix}{msg}")
    def error(self,    msg): self._log.error(f"{self._prefix}{msg}")
    def critical(self, msg): self._log.critical(f"{self._prefix}{msg}")

    def update_state(self, state: dict):
        now = int(time.time())
        if not hasattr(self, '_last_state_log') or now - self._last_state_log >= 60:
            price   = state.get('price',   0)
            balance = state.get('balance', 0)
            pnl     = state.get('pnl',     0)
            adx     = state.get('adx_val', 0)
            self._log.info(
                f"{self._prefix}📊 Estado | precio={price:.4f} | "
                f"balance=${balance:.2f} | pnl_dia=${pnl:.4f} | adx={adx:.1f}"
            )
            self._last_state_log = now

# ─── ui_ref mínimo que el worker necesita ────────────────────────────────────

class HeadlessUIRef:
    class _Widget:
        def __init__(self, value): self._v = str(value)
        def get(self):             return self._v

    def __init__(self, cfg: dict):
        self.ent_lev = self._Widget(cfg.get("leverage",  10))
        self.ent_mar = self._Widget(cfg.get("margen_pct", 30))
        self.ent_max = self._Widget(cfg.get("max_pos_size", 1500))
        self.ent_tp  = self._Widget(cfg.get("factor_tp",  3.50))
        self.ent_sl  = self._Widget(cfg.get("factor_sl",  1.00))
        self.ent_timeout = self._Widget(cfg.get("timeout_velas", 10))

# ─── Carga de config ──────────────────────────────────────────────────────────

def cargar_config(path: str = "config.json") -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        print(f"ERROR: No se encontró {path}. Copiá config.example.json a config.json y completá los valores.")
        sys.exit(1)

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    if os.environ.get("BINANCE_API_KEY"):
        cfg["api_key"] = os.environ["BINANCE_API_KEY"]
    if os.environ.get("BINANCE_SECRET_KEY"):
        cfg["secret_key"] = os.environ["BINANCE_SECRET_KEY"]

    required = ["api_key", "secret_key", "leverage",
                "margen_pct", "max_pos_size", "factor_tp", "factor_sl",
                "models_dir"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(f"ERROR: Faltan campos obligatorios en config.json o variables de entorno: {missing}")
        sys.exit(1)

    return cfg

# ─── Entry point (Hilo del Worker) ────────────────────────────────────────────

def bot_worker_thread(symbol: str, cfg: dict, delay_start: int, ws_manager=None):
    """
    Función que envuelve la creación y bucle de autoreinicio de MLLiveBotWorker para un solo símbolo.
    """
    if delay_start > 0:
        time.sleep(delay_start)
        
    log = HeadlessLogger(level=cfg.get("log_level", "INFO"), prefix=symbol)
    ui_ref = HeadlessUIRef(cfg)
    
    # Import aquí para aislar dependencias si falla
    from live_worker import MLLiveBotWorker

    bot = MLLiveBotWorker(
        api_key        = cfg["api_key"],
        secret_key     = cfg["secret_key"],
        symbol         = symbol,
        leverage       = int(cfg["leverage"]),
        margen         = float(cfg["margen_pct"]),
        max_pos        = float(cfg["max_pos_size"]),
        tp             = float(cfg["factor_tp"]),
        sl             = float(cfg["factor_sl"]),
        log_callback   = log,
        models_dir     = cfg["models_dir"],
        model_prefix   = cfg.get("model_prefix", "ethusdt"),
        use_short      = cfg.get("use_short", True),
        demo_mode      = cfg.get("demo_mode", False),
        tp_fijo_pct    = cfg.get("tp_fijo_pct"),
        timeout_velas  = cfg.get("timeout_velas", 10),
        ui_ref         = ui_ref,
        ws_manager     = ws_manager,
    )

    max_reinicios  = cfg.get("max_reinicios", 10)
    espera_reinicio = cfg.get("espera_reinicio_seg", 60)
    reinicios      = 0

    while reinicios < max_reinicios:
        try:
            log.info(f"▶ Iniciando bot para {symbol} (intento {reinicios + 1}/{max_reinicios})...")
            bot.run()
            reinicios += 1
            if reinicios < max_reinicios:
                log.warning(f"Bot detenido. Reintentando en {espera_reinicio}s ({reinicios}/{max_reinicios})...")
                time.sleep(espera_reinicio)
                bot.is_running = True
                bot.entry_lock = False
            else:
                log.critical("Máximo de reinicios alcanzado. Deteniendo.")
                break
        except Exception as e:
            reinicios += 1
            log.error(f"💥 Crash inesperado: {e}")
            if reinicios < max_reinicios:
                log.warning(f"Reiniciando en {espera_reinicio}s ({reinicios}/{max_reinicios} reinicios)...")
                time.sleep(espera_reinicio)
                bot.posicion_actual = None
                bot.entry_lock      = False
                bot.is_running      = True
            else:
                log.critical(f"Máximo de reinicios alcanzado ({max_reinicios}). Abortando.")
                break

# ─── Entry point (Main) ────────────────────────────────────────────────────────

def main():
    config_path = os.environ.get("BOT_CONFIG", "config.json")
    cfg         = cargar_config(config_path)

    global_log = HeadlessLogger(level=cfg.get("log_level", "INFO"), prefix="MAIN")

    global_log.info("═" * 60)
    global_log.info("  BEHAVIOR BOT v4 — MULTI-CRYPTO (Railway / servidor)")
    global_log.info(f"  Leverage:    {cfg['leverage']}x")
    global_log.info(f"  Margen:      {cfg['margen_pct']}%")
    global_log.info(f"  Max pos:     ${cfg['max_pos_size']}")
    global_log.info(f"  Factor TP:   {cfg['factor_tp']}")
    global_log.info(f"  Factor SL:   {cfg['factor_sl']}")
    global_log.info(f"  Models dir:  {cfg['models_dir']}")
    global_log.info(f"  Short:       {cfg.get('use_short', True)}")
    global_log.info("═" * 60)
    if cfg.get("demo_mode", False):
        global_log.info("  🧪🧪🧪  DEMO MODE — Binance Futures TESTNET  🧪🧪🧪")
        global_log.info("  Ninguna orden usa fondos reales. Requiere API keys de testnet.")
    else:
        global_log.critical("  ⚠️⚠️⚠️  LIVE MODE — DINERO REAL en Binance Futures  ⚠️⚠️⚠️")
        global_log.critical("  Las órdenes que ejecute este bot usan fondos reales.")
    global_log.info("═" * 60)

    try:
        import urllib.request
        ip = urllib.request.urlopen('https://api.ipify.org', timeout=5).read().decode()
        global_log.info(f"🌐 IP pública del servidor: {ip}")
        if not cfg.get("demo_mode", False):
            global_log.info("   → Agregá esta IP a la whitelist de tu API key en Binance (Seguridad Real)")
        else:
            global_log.info("   → (Modo Demo: no es necesario agregar IPs a la whitelist en Binance Testnet)")
    except Exception as e:
        global_log.warning(f"No se pudo obtener IP pública: {e}")

    # Extraer todos los símbolos configurados válidos (evitar vacíos y duplicados)
    symbols = []
    for key in ["symbol", "symbol_2", "symbol_3", "symbol_4"]:
        sym = str(cfg.get(key, "")).strip()
        if sym and sym.upper() not in [s.upper() for s in symbols]:
            symbols.append(sym.upper())

    if not symbols:
        global_log.critical("ERROR: No se especificó ningún símbolo válido en config.json (ej: 'symbol': 'ETHUSDT')")
        sys.exit(1)

    global_log.info(f"🚀 Iniciando {len(symbols)} instancias: {', '.join(symbols)}")

    # ── Iniciar WebSocket Manager Global ────────────────────────────────────
    # Import aquí para aislar dependencias si falla
    from ws_kline_manager import KlineWSManager
    ws_manager = KlineWSManager(symbols, demo_mode=cfg.get("demo_mode", False))
    ws_manager.start()

    threads = []
    # Lanzar un hilo por cada símbolo, con un retraso escalonado (stagger) para evitar golpear la API de golpe
    for i, symbol in enumerate(symbols):
        delay = i * 2  # 2 segundos de separación
        t = threading.Thread(target=bot_worker_thread, args=(symbol, cfg, delay, ws_manager), daemon=True)
        threads.append(t)
        t.start()

    # Mantener el proceso principal vivo mientras haya hilos activos
    try:
        while True:
            # Comprobar si al menos un hilo sigue vivo
            if not any(t.is_alive() for t in threads):
                global_log.critical("Todos los hilos han terminado. Cerrando aplicación.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        global_log.info("Interrupción manual recibida. Cerrando aplicación.")
        sys.exit(0)

if __name__ == "__main__":
    main()
