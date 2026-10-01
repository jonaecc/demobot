"""
╔══════════════════════════════════════════════════════════════════════╗
║     WEBSOCKET KLINE MANAGER — Precio en tiempo real sin REST         ║
║     Elimina ~240 weight/min de polling                               ║
╚══════════════════════════════════════════════════════════════════════╝

PROBLEMA:
  El bot hace fetch_ohlcv(limit=2) cada 5 segundos por símbolo para
  detectar nuevas velas y obtener el precio actual. Con 4 símbolos:
    4 × 12/min × peso 5 = 240 weight/min → ~10% del límite total.

SOLUCIÓN:
  Un solo WebSocket combinado suscrito a <symbol>@kline_15m para
  todos los símbolos. Provee:
    - Precio actual (OHLC de la vela en formación) en tiempo real
    - Detección de cierre de vela (evento x=true) sin latencia
    - Cero peso en la API REST

DEGRADACIÓN GRACEFUL:
  Si websocket-client no está instalado o la conexión falla,
  el bot sigue funcionando con REST polling (protegido por el
  rate limiter global), solo que con mayor latencia.
"""

import json
import threading
import time
import logging

logger = logging.getLogger("WSKline")

# URLs de WebSocket de Binance Futures (USDS-M)
WS_URL_PRODUCTION = "wss://fstream.binance.com"
WS_URL_TESTNET    = "wss://demo-fstream.binance.com"


class KlineWSManager:
    """
    Manager de WebSocket para streams de klines de Binance Futures.

    Mantiene una conexión combinada a múltiples símbolos y provee:
      - Último precio / OHLCV de la vela en formación
      - Timestamp de la última vela cerrada (para detección de nueva vela)

    Thread-safe: los workers pueden leer el estado desde cualquier hilo.
    """

    def __init__(self, symbols: list[str], demo_mode: bool = False):
        """
        Args:
            symbols: Lista de símbolos en formato limpio (ej: ["ETHUSDT", "BTCUSDT"])
            demo_mode: True para usar testnet WebSocket URL
        """
        # Normalizar símbolos a formato Binance (sin / ni :USDT)
        self._symbols = []
        for s in symbols:
            clean = s.upper().replace("/", "").split(":")[0]
            if clean not in self._symbols:
                self._symbols.append(clean)

        self._demo_mode = demo_mode
        self._base_url = WS_URL_TESTNET if demo_mode else WS_URL_PRODUCTION
        self._lock = threading.RLock()
        self._running = False
        self._connected = False
        self._thread = None
        self._ws = None
        self._reconnect_count = 0
        self._max_reconnects = 50  # máximo de reconexiones antes de rendirse

        # Estado por símbolo
        self._latest: dict[str, dict | None] = {}
        self._last_closed_ts: dict[str, int] = {}
        self._new_candle_events: dict[str, threading.Event] = {}

        for sym in self._symbols:
            self._latest[sym] = None
            self._last_closed_ts[sym] = 0
            self._new_candle_events[sym] = threading.Event()

    # ──────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ──────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Inicia la conexión WebSocket en un hilo daemon.
        Retorna True si se pudo importar websocket-client.
        """
        if self._running:
            return True

        try:
            import websocket  # noqa: F401
        except ImportError:
            logger.error(
                "❌ websocket-client no instalado. "
                "Ejecutá: pip install websocket-client"
            )
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="WSKlineManager"
        )
        self._thread.start()
        logger.info(f"🔌 WS Manager iniciado para {len(self._symbols)} símbolos")
        return True

    def stop(self):
        """Detiene la conexión WebSocket."""
        self._running = False
        self._connected = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info("🔌 WS Manager detenido")

    @property
    def is_connected(self) -> bool:
        """True si el WebSocket está conectado y recibiendo datos."""
        return self._connected and self._running

    # ──────────────────────────────────────────────────────────────
    # LECTURA DE DATOS (thread-safe)
    # ──────────────────────────────────────────────────────────────

    def get_latest(self, symbol: str) -> dict | None:
        """
        Retorna los datos más recientes del kline 15m para un símbolo.

        Returns:
            dict con keys: open, high, low, close, volume, ts, is_closed
            None si no hay datos disponibles aún.

        El símbolo se normaliza automáticamente (acepta ETHUSDT, ETH/USDT:USDT, etc).
        """
        sym = symbol.upper().replace("/", "").split(":")[0]
        with self._lock:
            data = self._latest.get(sym)
            return dict(data) if data else None

    def get_last_closed_ts(self, symbol: str) -> int:
        """
        Retorna el timestamp (ms) de apertura de la última vela 15m cerrada.
        Equivale al campo bars[-2][0] del REST polling.

        Retorna 0 si no se ha recibido ningún cierre de vela aún.
        """
        sym = symbol.upper().replace("/", "").split(":")[0]
        with self._lock:
            return self._last_closed_ts.get(sym, 0)

    def wait_for_new_candle(self, symbol: str, timeout: float = 30) -> bool:
        """
        Bloquea hasta que se detecte un cierre de vela para el símbolo,
        o hasta que expire el timeout.

        Returns:
            True si se detectó nueva vela, False si timeout.
        """
        sym = symbol.upper().replace("/", "").split(":")[0]
        event = self._new_candle_events.get(sym)
        if event is None:
            return False
        event.clear()
        return event.wait(timeout=timeout)

    def get_poll_data(self, symbol: str) -> tuple[int, float, float, float] | None:
        """
        Retorna datos equivalentes a lo que fetch_ohlcv(limit=2) provee
        para el loop principal:
          (last_closed_ts, current_price, current_high, current_low)

        Retorna None si no hay datos disponibles (WS no conectado).
        """
        sym = symbol.upper().replace("/", "").split(":")[0]
        with self._lock:
            data = self._latest.get(sym)
            closed_ts = self._last_closed_ts.get(sym, 0)

        if data is None or closed_ts == 0:
            return None

        return (closed_ts, data["close"], data["high"], data["low"])

    # ──────────────────────────────────────────────────────────────
    # CONEXIÓN Y RECONEXIÓN
    # ──────────────────────────────────────────────────────────────

    def _reconnect_loop(self):
        """Loop de reconexión automática con backoff exponencial."""
        backoff = 1  # segundos

        while self._running and self._reconnect_count < self._max_reconnects:
            try:
                self._connect()
            except Exception as e:
                logger.warning(f"WS error de conexión: {e}")

            if not self._running:
                break

            self._connected = False
            self._reconnect_count += 1

            # Backoff exponencial: 1s, 2s, 4s, 8s, 16s, 30s (cap)
            backoff = min(backoff * 2, 30)
            logger.info(
                f"🔄 WS reconectando en {backoff}s "
                f"(intento {self._reconnect_count}/{self._max_reconnects})"
            )
            time.sleep(backoff)

        if self._reconnect_count >= self._max_reconnects:
            logger.error(
                f"❌ WS máximo de reconexiones ({self._max_reconnects}) alcanzado. "
                f"Cayendo a REST polling con rate limiter."
            )
            self._running = False

    def _connect(self):
        """Establece la conexión WebSocket con streams combinados."""
        import websocket

        # Construir URL de streams combinados
        # Formato: wss://fstream.binance.com/stream?streams=ethusdt@kline_15m/btcusdt@kline_15m
        streams = "/".join(f"{s.lower()}@kline_15m" for s in self._symbols)
        url = f"{self._base_url}/stream?streams={streams}"

        logger.info(f"🔌 Conectando WS a {url}")

        def on_message(ws, message):
            try:
                self._process_message(message)
            except Exception as e:
                logger.warning(f"WS error procesando mensaje: {e}")

        def on_error(ws, error):
            logger.warning(f"WS error: {error}")

        def on_close(ws, close_status_code, close_msg):
            self._connected = False
            logger.info(f"WS cerrado: code={close_status_code} msg={close_msg}")

        def on_open(ws):
            self._connected = True
            self._reconnect_count = 0  # reset backoff en conexión exitosa
            logger.info(
                f"✅ WS conectado — streams: {', '.join(self._symbols)}"
            )

        self._ws = websocket.WebSocketApp(
            url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open,
        )

        # run_forever bloquea hasta desconexión
        # ping_interval mantiene la conexión viva
        self._ws.run_forever(
            ping_interval=30,
            ping_timeout=10,
            reconnect=0,  # reconexión la manejamos nosotros
        )

    def _process_message(self, raw_message: str):
        """Procesa un mensaje del stream combinado de klines."""
        data = json.loads(raw_message)

        # Stream combinado envuelve en {"stream": "...", "data": {...}}
        if "data" in data:
            data = data["data"]

        # Solo procesamos eventos de kline
        if data.get("e") != "kline":
            return

        k   = data["k"]
        sym = k["s"].upper()  # ej: "ETHUSDT"

        # Solo procesar klines de 15m (por si acaso)
        if k.get("i") != "15m":
            return

        info = {
            "open":      float(k["o"]),
            "high":      float(k["h"]),
            "low":       float(k["l"]),
            "close":     float(k["c"]),
            "volume":    float(k["v"]),
            "ts":        int(k["t"]),      # timestamp de apertura de la vela
            "is_closed": bool(k["x"]),     # ¿la vela cerró?
        }

        with self._lock:
            self._latest[sym] = info

            if info["is_closed"]:
                prev_ts = self._last_closed_ts.get(sym, 0)
                if info["ts"] > prev_ts:
                    self._last_closed_ts[sym] = info["ts"]
                    # Señalizar nueva vela a los hilos que estén esperando
                    event = self._new_candle_events.get(sym)
                    if event:
                        event.set()
                    logger.info(
                        f"🕯️ {sym} vela 15m cerrada | "
                        f"O={info['open']:.2f} H={info['high']:.2f} "
                        f"L={info['low']:.2f} C={info['close']:.2f}"
                    )
