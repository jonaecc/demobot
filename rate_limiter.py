"""
╔══════════════════════════════════════════════════════════════════════╗
║     RATE LIMITER GLOBAL — Protección contra bans de Binance          ║
║     Compartido entre todos los hilos de worker                       ║
╚══════════════════════════════════════════════════════════════════════╝

PROBLEMA:
  Cada instancia de ccxt tiene su propio rate limiter interno.
  Con 4 símbolos (= 4 instancias ccxt), cada una cree que puede usar
  2400 weight/min, pero Binance lo cuenta por IP → total real = 2400.

SOLUCIÓN:
  Singleton global thread-safe que todos los hilos consultan ANTES
  de hacer cualquier request REST. Implementa:
    1. Token bucket con ventana deslizante de 60 segundos
    2. Umbral preventivo al 75% (1800 weight) para frenar antes del ban
    3. Estado de ban compartido — un hilo detecta ban, todos se detienen
    4. Tracking del peso real vía header X-MBX-USED-WEIGHT-1m
"""

import threading
import time
import logging

logger = logging.getLogger("RateLimiter")


# ══════════════════════════════════════════════════════════════════
# PESOS APROXIMADOS DE ENDPOINTS DE BINANCE FUTURES (USDS-M)
# ══════════════════════════════════════════════════════════════════

# Referencia: https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
WEIGHT_KLINES         = 5    # GET /fapi/v1/klines (limit ≤ 499)
WEIGHT_KLINES_LARGE   = 10   # GET /fapi/v1/klines (limit 500-999)
WEIGHT_BALANCE        = 5    # GET /fapi/v2/balance
WEIGHT_POSITION       = 5    # GET /fapi/v2/positionRisk
WEIGHT_OPEN_ORDERS    = 1    # GET /fapi/v1/openOrders (con symbol)
WEIGHT_ALL_ORDERS     = 5    # GET /fapi/v1/allOrders
WEIGHT_ORDER          = 1    # POST /fapi/v1/order
WEIGHT_CANCEL_ORDER   = 1    # DELETE /fapi/v1/order
WEIGHT_EXCHANGE_INFO  = 40   # GET /fapi/v1/exchangeInfo (load_markets)
WEIGHT_ACCOUNT        = 5    # GET /fapi/v2/account
WEIGHT_LEVERAGE       = 1    # POST /fapi/v1/leverage
WEIGHT_MARGIN_TYPE    = 1    # POST /fapi/v1/marginType
WEIGHT_POSITION_MODE  = 1    # GET/POST /fapi/v1/positionSide/dual
WEIGHT_ALGO_ORDERS    = 1    # GET /fapi/v1/openAlgoOrders
WEIGHT_FETCH_ORDER    = 1    # GET /fapi/v1/order
WEIGHT_DEFAULT        = 2    # Fallback para endpoints no clasificados


def weight_for_klines(limit: int) -> int:
    """Calcula el peso de un request de klines según el limit."""
    if limit <= 99:
        return 1
    elif limit <= 499:
        return 5  # WEIGHT_KLINES
    elif limit <= 999:
        return 10  # WEIGHT_KLINES_LARGE
    else:
        return 20


class BinanceRateLimiter:
    """
    Rate limiter global thread-safe para la API de Binance.

    Binance impone un límite de 2400 request weight por minuto por IP.
    Este limiter:
      - Trackea el peso acumulado en una ventana de 60 segundos
      - Bloquea (con sleep) cuando se acerca al límite
      - Propaga el estado de ban entre todos los hilos
      - Se actualiza con el peso real del header X-MBX-USED-WEIGHT-1m
    """

    MAX_WEIGHT_PER_MIN = 2400
    SAFE_WEIGHT_LIMIT  = 1800   # 75% — umbral de frenado preventivo
    WARN_WEIGHT_LIMIT  = 1200   # 50% — umbral de warning

    def __init__(self):
        self._lock = threading.Lock()
        self._weight_used = 0
        self._window_start = time.time()
        self._ban_until_ms = 0          # timestamp UNIX ms hasta que dura el ban
        self._last_header_weight = 0    # último valor del header X-MBX-USED-WEIGHT-1m
        self._last_header_time = 0      # cuándo se actualizó el header
        self._total_requests = 0        # contador de requests totales (debug)
        self._total_waits = 0           # veces que tuvimos que esperar

    # ──────────────────────────────────────────────────────────────
    # ESTADO DE BAN
    # ──────────────────────────────────────────────────────────────

    def is_banned(self) -> bool:
        """Devuelve True si la IP sigue baneada por Binance."""
        return time.time() * 1000 < self._ban_until_ms

    def set_ban(self, until_ms: int):
        """Registra un ban. Se usa el máximo entre el ban actual y el nuevo."""
        with self._lock:
            self._ban_until_ms = max(self._ban_until_ms, until_ms)
            logger.warning(
                f"🚫 Ban global registrado hasta {until_ms} "
                f"({self.get_ban_remaining_secs():.0f}s restantes)"
            )

    def clear_ban(self):
        """Limpia el ban (si expiró)."""
        with self._lock:
            if not self.is_banned():
                self._ban_until_ms = 0

    def get_ban_remaining_secs(self) -> float:
        """Segundos restantes del ban actual."""
        return max(0, (self._ban_until_ms / 1000) - time.time())

    # ──────────────────────────────────────────────────────────────
    # ADQUISICIÓN DE PERMISO
    # ──────────────────────────────────────────────────────────────

    def acquire(self, weight: int = WEIGHT_DEFAULT, timeout: float = 120) -> bool:
        """
        Adquiere permiso para hacer un API call con el peso dado.
        Bloquea si estamos cerca del límite o baneados.

        Args:
            weight:  peso del endpoint (ver constantes WEIGHT_*)
            timeout: máximo de segundos a esperar

        Returns:
            True si se obtuvo permiso, False si timeout o ban irrecuperable.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            # Si estamos baneados, esperar a que expire
            if self.is_banned():
                ban_remaining = self.get_ban_remaining_secs()
                if ban_remaining > timeout:
                    return False
                time.sleep(min(ban_remaining + 1, deadline - time.time()))
                continue

            with self._lock:
                now = time.time()

                # Resetear ventana si pasaron 60+ segundos
                if now - self._window_start >= 60:
                    self._weight_used = 0
                    self._window_start = now

                # Usar el mayor entre nuestro tracking y el header real
                effective_weight = self._weight_used
                if self._last_header_time > self._window_start:
                    effective_weight = max(effective_weight, self._last_header_weight)

                # ¿Hay espacio?
                if effective_weight + weight <= self.SAFE_WEIGHT_LIMIT:
                    self._weight_used += weight
                    self._total_requests += 1

                    # Log de warning cuando se acerca al límite
                    if effective_weight + weight > self.WARN_WEIGHT_LIMIT:
                        logger.info(
                            f"⚠️ Rate limit al {(effective_weight + weight) / self.MAX_WEIGHT_PER_MIN * 100:.0f}% "
                            f"({effective_weight + weight}/{self.MAX_WEIGHT_PER_MIN} weight)"
                        )
                    return True

                # No hay espacio — calcular cuánto esperar
                wait_until_reset = max(0.5, 60 - (now - self._window_start) + 0.5)

            self._total_waits += 1
            wait = min(wait_until_reset, deadline - time.time(), 15)
            if wait <= 0:
                return False

            logger.info(
                f"⏳ Rate limiter: esperando {wait:.1f}s "
                f"(weight={self._weight_used}/{self.SAFE_WEIGHT_LIMIT})"
            )
            time.sleep(wait)

        return False

    # ──────────────────────────────────────────────────────────────
    # ACTUALIZACIÓN DESDE RESPUESTAS HTTP
    # ──────────────────────────────────────────────────────────────

    def update_from_header(self, used_weight: int):
        """
        Actualiza el peso usado desde el header X-MBX-USED-WEIGHT-1m
        de una respuesta de la API de Binance.
        Es más preciso que nuestro tracking local.
        """
        with self._lock:
            self._last_header_weight = used_weight
            self._last_header_time = time.time()

            # Si el header reporta más peso del que trackeamos, corregir
            if used_weight > self._weight_used:
                self._weight_used = used_weight

    def get_stats(self) -> dict:
        """Retorna estadísticas del rate limiter (para debug/logging)."""
        with self._lock:
            return {
                "weight_used": self._weight_used,
                "safe_limit": self.SAFE_WEIGHT_LIMIT,
                "max_limit": self.MAX_WEIGHT_PER_MIN,
                "pct_used": round(self._weight_used / self.MAX_WEIGHT_PER_MIN * 100, 1),
                "is_banned": self.is_banned(),
                "ban_remaining_s": round(self.get_ban_remaining_secs(), 0),
                "total_requests": self._total_requests,
                "total_waits": self._total_waits,
            }


# ══════════════════════════════════════════════════════════════════
# SINGLETON GLOBAL
# ══════════════════════════════════════════════════════════════════

_global_limiter = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> BinanceRateLimiter:
    """
    Retorna la instancia singleton del rate limiter global.
    Thread-safe. Todos los hilos de worker deben usar esta misma instancia.
    """
    global _global_limiter
    if _global_limiter is None:
        with _limiter_lock:
            if _global_limiter is None:
                _global_limiter = BinanceRateLimiter()
    return _global_limiter
