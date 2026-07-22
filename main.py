"""
main.py — Punto de entrada headless para Railway (sin UI)
=========================================================
Lee config.json, construye un logger liviano y lanza MLLiveBotWorker.
No depende de tkinter ni de ninguna interfaz gráfica.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# ─── Logger liviano que reemplaza el log_callback de la UI ───────────────────

class HeadlessLogger:
    """
    Reemplaza el objeto log de la UI.
    Expone los mismos métodos que usa el worker:
      .info()  .success()  .warning()  .error()  .critical()  .update_state()
    Todo va a stdout para que Railway lo capture en su panel de logs.
    """

    def __init__(self, level: str = "INFO"):
        logging.basicConfig(
            stream=sys.stdout,
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self._log = logging.getLogger("BotV4")

    def info(self,     msg): self._log.info(msg)
    def success(self,  msg): self._log.info(f"✅ {msg}")
    def warning(self,  msg): self._log.warning(msg)
    def error(self,    msg): self._log.error(msg)
    def critical(self, msg): self._log.critical(msg)

    def update_state(self, state: dict):
        """
        En la UI actualiza los widgets. Acá imprime cada 60 segundos
        para no saturar los logs de Railway.
        """
        now = int(time.time())
        if not hasattr(self, '_last_state_log') or now - self._last_state_log >= 60:
            price   = state.get('price',   0)
            balance = state.get('balance', 0)
            pnl     = state.get('pnl',     0)
            adx     = state.get('adx_val', 0)
            self._log.info(
                f"📊 Estado | precio={price:.4f} | "
                f"balance=${balance:.2f} | pnl_dia=${pnl:.4f} | adx={adx:.1f}"
            )
            self._last_state_log = now


# ─── ui_ref mínimo que el worker necesita ────────────────────────────────────

class HeadlessUIRef:
    """
    El worker accede a ui_ref para:
      ui_ref.ent_lev / ent_mar / ent_max / ent_tp / ent_sl  → parámetros
      (hot-reload desde config en cada ciclo)

    Los widgets de tkinter tienen .get() — emulamos eso con una clase simple.
    """

    class _Widget:
        def __init__(self, value): self._v = str(value)
        def get(self):             return self._v

    def __init__(self, cfg: dict):
        # Widgets emulados — el worker los lee en cada ciclo para hot-reload
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
        print(f"ERROR: No se encontró {path}. "
              f"Copiá config.example.json a config.json y completá los valores.")
        sys.exit(1)

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    # Las API keys pueden venir de variables de entorno (Railway)
    # y tienen prioridad sobre config.json
    if os.environ.get("BINANCE_API_KEY"):
        cfg["api_key"] = os.environ["BINANCE_API_KEY"]
    if os.environ.get("BINANCE_SECRET_KEY"):
        cfg["secret_key"] = os.environ["BINANCE_SECRET_KEY"]

    required = ["api_key", "secret_key", "symbol", "leverage",
                "margen_pct", "max_pos_size", "factor_tp", "factor_sl",
                "models_dir"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(f"ERROR: Faltan campos obligatorios en config.json o variables de entorno: {missing}")
        sys.exit(1)

    return cfg


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    # Railway permite pasar la ruta del config por variable de entorno
    config_path = os.environ.get("BOT_CONFIG", "config.json")
    cfg         = cargar_config(config_path)

    log    = HeadlessLogger(level=cfg.get("log_level", "INFO"))
    ui_ref = HeadlessUIRef(cfg)

    log.info("═" * 60)
    log.info("  BEHAVIOR BOT v4 — modo headless (Railway / servidor)")
    log.info(f"  Symbol:      {cfg['symbol']}")
    log.info(f"  Leverage:    {cfg['leverage']}x")
    log.info(f"  Margen:      {cfg['margen_pct']}%")
    log.info(f"  Max pos:     ${cfg['max_pos_size']}")
    log.info(f"  Factor TP:   {cfg['factor_tp']}")
    log.info(f"  Factor SL:   {cfg['factor_sl']}")
    log.info(f"  Models dir:  {cfg['models_dir']}")
    log.info(f"  Short:       {cfg.get('use_short', True)}")
    log.info("═" * 60)
    if cfg.get("demo_mode", False):
        log.info("  🧪🧪🧪  DEMO MODE — Binance Futures TESTNET  🧪🧪🧪")
        log.info("  Ninguna orden usa fondos reales. Requiere API keys de testnet.")
    else:
        log.critical("  ⚠️⚠️⚠️  LIVE MODE — DINERO REAL en Binance Futures  ⚠️⚠️⚠️")
        log.critical("  Las órdenes que ejecute este bot usan fondos reales.")
    log.info("═" * 60)

    # Mostrar IP pública del servidor (útil para whitelist en Binance)
    try:
        import urllib.request
        ip = urllib.request.urlopen('https://api.ipify.org', timeout=5).read().decode()
        log.info(f"🌐 IP pública del servidor: {ip}")
        log.info("   → Agregá esta IP a la whitelist de tu API key en Binance")
    except Exception as e:
        log.warning(f"No se pudo obtener IP pública: {e}")

    # Import aquí para que los errores de dependencias sean claros
    from live_worker import MLLiveBotWorker

    bot = MLLiveBotWorker(
        api_key        = cfg["api_key"],
        secret_key     = cfg["secret_key"],
        symbol         = cfg["symbol"],
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
    )

    # Reinicio automático ante crash inesperado
    max_reinicios  = cfg.get("max_reinicios", 10)
    espera_reinicio = cfg.get("espera_reinicio_seg", 60)
    reinicios      = 0

    while reinicios < max_reinicios:
        try:
            log.info(f"▶ Iniciando bot (intento {reinicios + 1}/{max_reinicios})...")
            bot.run()
            # Si run() retorna limpiamente (stop() llamado), salir sin reintentar
            # run() retornó sin excepción — puede ser fallo de resync o stop() manual
            # Si no es stop() manual, esperar antes de reintentar
            reinicios += 1
            if reinicios < max_reinicios:
                log.warning(
                    f"Bot detenido. Reintentando en {espera_reinicio}s "
                    f"({reinicios}/{max_reinicios})..."
                )
                time.sleep(espera_reinicio)
                bot.is_running = True
                bot.entry_lock = False
            else:
                log.critical("Máximo de reinicios alcanzado. Deteniendo.")
                break
        except KeyboardInterrupt:
            log.info("Interrupción manual. Cerrando.")
            break
        except Exception as e:
            reinicios += 1
            log.error(f"💥 Crash inesperado: {e}")
            if reinicios < max_reinicios:
                log.warning(
                    f"Reiniciando en {espera_reinicio}s "
                    f"({reinicios}/{max_reinicios} reinicios)..."
                )
                time.sleep(espera_reinicio)
                # Resetear estado del bot para reinicio limpio
                bot.posicion_actual = None
                bot.entry_lock      = False
                bot.is_running      = True
            else:
                log.critical(f"Máximo de reinicios alcanzado ({max_reinicios}). Abortando.")
                sys.exit(1)


if __name__ == "__main__":
    main()
