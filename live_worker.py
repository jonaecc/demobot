"""
╔══════════════════════════════════════════════════════════════════════╗
║         BEHAVIOR + PRETRIGGER LIVE WORKER  v4                        ║
║         Gatillo en 15m · Contexto 1D / 4H / 1H · Entrada limit      ║
╚══════════════════════════════════════════════════════════════════════╝

ARQUITECTURA:
─────────────────────────────────────────────────────────────────────
  Cascada de contexto (de mayor a menor temporalidad):

    1D  → contexto macro   (UU / UD / DU / DD)
    4H  → contexto medio   (UU / UD / DU / DD)
    1H  → contexto inmediato (UU / UD / DU / DD)
    15m → GATILLO  (T1 / T2 / T3 / T4)

  Escenario resultante:  T2__UU_DD_UD
                           ^   ^  ^  ^
                        gatillo 1D 4H 1H

  Hasta 4 × 64 = 256 combinaciones posibles.

CAMBIOS RESPECTO A v3 (live_worker_2.py):
  - Gatillo detectado en cierre de vela 15m (antes: 1H)
  - 1H pasa a ser un nivel de contexto más (igual que 4H y 1D)
  - Features pretrigger incluyen indicadores 15m + 1H + 4H + 1D
  - TP/SL desde post_15m del summary (antes: post_1h)
  - Entrada con orden LIMIT al bid/ask con timeout configurable
    → elimina deslizamiento de las órdenes market
  - Tamaño de posición por interés compuesto:
      pos_nominal = min(balance × margen × leverage, max_pos_size)
  - Detección de nueva vela: polling cada 5s sobre 15m (antes: 15s/1H)
  - Caché: 15m, 1H, 4H, 1D  (antes: 1H, 4H, 1D)

INTERFAZ PÚBLICA:
  Idéntica al worker anterior → la UI no necesita cambios.
  Alias load_ml_models() → load_behavior_data()
  Alias _ml_signal()     → _behavior_signal()
"""

import ccxt
import os
import math
import time
import pandas as pd
import pandas_ta as ta
import numpy as np
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from binance.client import Client

# Integración opcional con Instagram (ver instagram_poster.py).
# Si el módulo no está presente o falla al importar, el bot sigue
# funcionando normalmente — los notificadores quedan como no-ops.
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from instagram_poster import notificar_apertura, notificar_cierre, post_apertura
except Exception as e:
    print(f"Aviso: Integracion con Instagram deshabilitada por error de importacion: {e}")
    def notificar_apertura(**kwargs): pass
    def notificar_cierre(**kwargs): pass
    def post_apertura(**kwargs): pass
# SecurityManager eliminado — bot de uso personal


# ══════════════════════════════════════════════════════════════════
# PARÁMETROS DEL SISTEMA
# ══════════════════════════════════════════════════════════════════

FACTOR_TP_DEFAULT    = 3.50   # factor TP óptimo OOS (timeout-driven)
FACTOR_SL_DEFAULT    = 1.00   # factor SL óptimo OOS

# Escenarios excluidos — 50 peores por PnL OOS (backtest v4, engine corregido)
# Análisis: 2024-05-01 → presente | Resultado con exclusión: PF=2.030, WR=57.5%, MDD=0.7%
ESCENARIOS_EXCLUIDOS = [
    # Peores por PnL total OOS (ordenados de peor a mejor)
    "T1__UD_UU_UD", "T1__DD_DD_DU", "T3__UD_DD_DU", "T2__UU_UD_DD",
    "T4__DD_DU_DD", "T2__DU_UD_DU", "T3__DD_UU_DU", "T1__UU_DU_DD",
    "T1__UD_DU_DD", "T4__UD_DU_UU", "T1__DU_UU_UD", "T2__DU_UD_DD",
    "T1__DD_UD_UD", "T2__UU_DU_DD", "T2__UD_DU_UD", "T3__DD_DU_DU",
    "T4__UU_DU_DU", "T2__DU_DD_DD", "T1__DU_DD_DU", "T4__UU_DD_DU",
    "T4__DU_UD_DU", "T4__DD_UD_DD", "T1__DU_UD_UD", "T3__DU_DD_DU",
    "T1__DD_UD_UU", "T3__DD_DD_UD", "T1__DU_DD_DD", "T3__DD_DU_DD",
    "T4__UU_DD_UD", "T3__DD_DU_UD", "T2__UU_UU_UU", "T4__DD_DU_UD",
    "T4__DD_DD_UU", "T1__UD_DU_UU", "T4__UU_DU_UU", "T3__DD_DD_UU",
    "T1__UD_UD_DD", "T4__DU_UD_UU", "T3__UU_UD_UD", "T4__DD_DD_DD",
    "T2__DU_UU_DU", "T4__UD_DU_UD", "T3__DU_DU_UU", "T3__UU_UU_UD",
    "T3__UD_DU_UD", "T2__UU_DD_UU", "T3__DD_UD_DD", "T2__UD_DU_DD",
    "T3__UU_DD_UU", "T2__UU_DU_UU",
]


MIN_N_ESCENARIO    = 5     # mínimo de casos históricos para confiar en TP/SL
LIMIT_TIMEOUT_SEG  = 30    # segundos para que se llene la orden limit
LIMIT_OFFSET_PCT   = 0.02  # % de margen sobre bid/ask para asegurar llenado


# ══════════════════════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════════════════════

def _sf(val):
    if val is None:
        return None
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# CARGA DE ARCHIVOS BEHAVIOR + PRETRIGGER
# ══════════════════════════════════════════════════════════════════

def cargar_behavior_data(summary_path: str, setups_path: str) -> tuple:
    with open(summary_path, encoding="utf-8") as f:
        summary_raw = json.load(f)
    summary = summary_raw.get("escenarios", {})

    with open(setups_path, encoding="utf-8") as f:
        setups_raw = json.load(f)
    setups = setups_raw.get("escenarios", {})

    return summary, setups


# ══════════════════════════════════════════════════════════════════
# CÁLCULO DE INDICADORES POR TIMEFRAME
# ══════════════════════════════════════════════════════════════════

def _calcular_indicadores(df: pd.DataFrame, min_velas: int = 55) -> dict:

    result = {}
    if df is None or len(df) < min_velas:
        return result

    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)

    # ADX + DI+/DI-
    adx_df = ta.adx(high, low, close, length=14)
    if adx_df is not None and not adx_df.empty:
        adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
        dmp_col = [c for c in adx_df.columns if c.startswith("DMP_")]
        dmn_col = [c for c in adx_df.columns if c.startswith("DMN_")]
        if adx_col:
            adx_s = adx_df[adx_col[0]].dropna()
            if len(adx_s) >= 3:
                result["adx"]            = float(adx_s.iloc[-1])
                result["adx_prev"]       = float(adx_s.iloc[-2])
                result["adx_prev2"]      = float(adx_s.iloc[-3])
                result["adx_slope"]      = result["adx"]       - result["adx_prev"]
                result["adx_slope_prev"] = result["adx_prev"]  - result["adx_prev2"]
                result["adx_accel"]      = result["adx_slope"] - result["adx_slope_prev"]
        if dmp_col:
            result["di_plus"]  = _sf(adx_df[dmp_col[0]].iloc[-1])
        if dmn_col:
            result["di_minus"] = _sf(adx_df[dmn_col[0]].iloc[-1])
        if result.get("di_plus") is not None and result.get("di_minus") is not None:
            result["di_spread"] = result["di_plus"] - result["di_minus"]

    # SQZ Momentum (LazyBear)
    length      = 20
    basis       = ta.sma(close, length=length)
    ma_high_low = (high.rolling(length).max() + low.rolling(length).min()) / 2
    val         = (ma_high_low + basis) / 2
    sqz_hist    = ta.linreg(close - val, length=length)
    if sqz_hist is not None and not sqz_hist.empty:
        sqz_s = sqz_hist.dropna()
        if len(sqz_s) >= 3:
            result["sqz_hist"]      = float(sqz_s.iloc[-1])
            result["sqz_hist_prev"] = float(sqz_s.iloc[-2])
            result["sqz_dir"]       = float(sqz_s.iloc[-1]) - float(sqz_s.iloc[-2])
            result["sqz_dir_prev"]  = float(sqz_s.iloc[-2]) - float(sqz_s.iloc[-3])

    # RSI
    rsi = ta.rsi(close, length=14)
    if rsi is not None and not rsi.empty:
        result["rsi"] = _sf(rsi.iloc[-1])

    # EMAs y distancias
    last_close = float(close.iloc[-1])
    for ema_len, key in [(20, "ema20"), (55, "ema55"), (200, "ema200")]:
        ema = ta.ema(close, length=ema_len)
        if ema is not None and not ema.empty:
            ema_val = float(ema.iloc[-1])
            result[key] = ema_val
            if ema_val > 0:
                result[f"{key}_dist"] = (last_close - ema_val) / ema_val

    # ATR normalizado
    atr = ta.atr(high, low, close, length=14)
    if atr is not None and not atr.empty:
        atr_val = _sf(atr.iloc[-1])
        result["atr"] = atr_val
        if atr_val and last_close > 0:
            result["atr_norm"] = atr_val / last_close

    result["close"] = last_close
    return result


# ══════════════════════════════════════════════════════════════════
# CLASIFICACIÓN DE CÓDIGO DE RÉGIMEN (UU / UD / DU / DD)
# ══════════════════════════════════════════════════════════════════

def _clasificar_codigo(ind: dict) -> str:
    """
    Primer carácter = ADX slope (U = subiendo, D = bajando)
    Segundo carácter = SQZ dir  (U = subiendo, D = bajando)
    """
    adx_up = (ind.get("adx_slope", 0) or 0) > 0
    sqz_up = (ind.get("sqz_dir",   0) or 0) > 0
    return ("U" if adx_up else "D") + ("U" if sqz_up else "D")


# ══════════════════════════════════════════════════════════════════
# DETECCIÓN DE GATILLO T1 / T2 / T3 / T4  (en 15m)
# ══════════════════════════════════════════════════════════════════

def _detectar_gatillo(ind_15m: dict) -> str | None:
    """
    Detecta el tipo de gatillo en la vela 15m actual.

    T1 — SQZ gira positivo  AND ADX gira negativo   → Reversión
    T2 — ADX gira positivo  AND SQZ gira positivo   → Impulso confirmado   (no T1)
    T3 — ADX positivo       AND SQZ gira negativo   → Agotamiento          (no T1/T2)
    T4 — SQZ negativo       AND ADX gira negativo   → Continuación bajista (no T1/T2/T3)
    """
    adx_slope      = ind_15m.get("adx_slope",      0) or 0
    adx_slope_prev = ind_15m.get("adx_slope_prev", 0) or 0
    sqz_dir        = ind_15m.get("sqz_dir",        0) or 0
    sqz_dir_prev   = ind_15m.get("sqz_dir_prev",   0) or 0
    sqz_hist       = ind_15m.get("sqz_hist",       0) or 0

    adx_gira_pos = (adx_slope > 0) and (adx_slope_prev <= 0)
    adx_gira_neg = (adx_slope < 0) and (adx_slope_prev >= 0)
    sqz_gira_pos = (sqz_dir   > 0) and (sqz_dir_prev   <= 0)
    sqz_gira_neg = (sqz_dir   < 0) and (sqz_dir_prev   >= 0)

    if sqz_gira_pos and adx_gira_neg:
        return "T1"
    if adx_gira_pos and sqz_gira_pos:
        return "T2"
    if adx_slope > 0 and sqz_gira_neg:
        return "T3"
    if sqz_hist < 0 and adx_gira_neg:
        return "T4"
    return None


# ══════════════════════════════════════════════════════════════════
# FEATURES PRETRIGGER
# ══════════════════════════════════════════════════════════════════

def _extraer_features_pretrigger(ind_15m: dict, ind_1h: dict,
                                  ind_4h: dict, ind_1d: dict,
                                  df_15m: pd.DataFrame) -> dict:
    """
    Construye el dict de features para evaluar la condición pretrigger.
    Espejo exacto del pretrigger_setup_finder_v3.py.
    """
    dip = _sf(ind_15m.get("di_plus"))
    dim = _sf(ind_15m.get("di_minus"))

    # Estadísticas de las 10 velas 15m previas al gatillo
    ret_pre = pend_pre = rango_pre = vol_pre = None
    if df_15m is not None and len(df_15m) >= 11:
        pre = df_15m["close"].astype(float).iloc[-11:-1]  # 10 velas anteriores
        hi  = df_15m["high"].astype(float).iloc[-11:-1]
        lo  = df_15m["low"].astype(float).iloc[-11:-1]
        if len(pre) >= 2 and float(pre.iloc[0]) > 0:
            ret_pre   = _sf((float(pre.iloc[-1]) - float(pre.iloc[0])) / float(pre.iloc[0]) * 100)
            rets_1    = [(float(pre.iloc[i]) - float(pre.iloc[i-1])) / float(pre.iloc[i-1]) * 100
                         for i in range(1, len(pre)) if float(pre.iloc[i-1]) != 0]
            # ddof=0 (poblacional), igual que behavior_analysis_engine: np.std(rets_1)
            vol_pre   = _sf(float(np.std(rets_1))) if len(rets_1) > 1 else None
            rango_pre = _sf(float((hi.max() - lo.min()) / float(pre.iloc[0])) * 100)
            try:
                y = pre.values.astype(float)
                x = np.arange(len(y), dtype=float)
                slope = np.polyfit(x, y, 1)[0]
                # Normalizar por closes[0] (igual que behavior_analysis_engine)
                pend_pre = _sf(slope / float(pre.iloc[0]) * 100) if float(pre.iloc[0]) > 0 else None
            except Exception:
                pass

    return {
        # 15m — gatillo
        "adx":            _sf(ind_15m.get("adx")),
        "adx_slope":      _sf(ind_15m.get("adx_slope")),
        "adx_accel":      _sf(ind_15m.get("adx_accel")),
        "sqz_hist":       _sf(ind_15m.get("sqz_hist")),
        "sqz_dir":        _sf(ind_15m.get("sqz_dir")),
        "ema55_dist":     _sf(ind_15m.get("ema55_dist")),
        "ema20_dist":     _sf(ind_15m.get("ema20_dist")),
        "di_plus":        dip,
        "di_minus":       dim,
        "di_spread":      (dip - dim) if (dip is not None and dim is not None) else None,
        "rsi":            _sf(ind_15m.get("rsi")),
        "atr_norm":       _sf(ind_15m.get("atr_norm")),
        # 1H — contexto inmediato superior
        "adx_1h":         _sf(ind_1h.get("adx")),
        "adx_slope_1h":   _sf(ind_1h.get("adx_slope")),
        "sqz_hist_1h":    _sf(ind_1h.get("sqz_hist")),
        "ema55_dist_1h":  _sf(ind_1h.get("ema55_dist")),
        "rsi_1h":         _sf(ind_1h.get("rsi")),
        # 4H — contexto medio
        "adx_4h":         _sf(ind_4h.get("adx")),
        "sqz_hist_4h":    _sf(ind_4h.get("sqz_hist")),
        "ema55_dist_4h":  _sf(ind_4h.get("ema55_dist")),
        # 1D — contexto macro
        "ema55_dist_1d":  _sf(ind_1d.get("ema55_dist")),
        "adx_1d":         _sf(ind_1d.get("adx")),
        # Pre-gatillo — últimas 10 velas 15m
        "ret_pre":        ret_pre,
        "rango_pre":      rango_pre,
        "pendiente_pre":  pend_pre,
        "vol_pre":        vol_pre,
    }


# ══════════════════════════════════════════════════════════════════
# EVALUACIÓN DE CONDICIÓN PRETRIGGER
# ══════════════════════════════════════════════════════════════════

def _check_pretrigger(features: dict, esc_data: dict) -> tuple[bool, str]:

    hallazgos = esc_data.get("hallazgos", []) or []
    if not hallazgos:
        base_rate = esc_data.get("base_rate", 0.0)
        if base_rate < 0.54:
            return False, "sin_hallazgos_y_tasa_baja"
        return True, "sin_condicion"

    for i, h in enumerate(hallazgos):
        feat   = h.get("feature", "")
        op     = h.get("operador", ">=")
        umbral = h.get("umbral", 0)
        val    = features.get(feat)

        # Feature no disponible: saltar este hallazgo sin aprobar ni rechazar.
        # Un dato faltante no es motivo de aprobación automática.
        if val is None:
            continue

        cumple = (val >= umbral) if op == ">=" else (val <= umbral)
        if cumple:
            return True, f"ok_h{i+1} (feat={feat}, lift={h.get('lift', 0):.2f})"
        # Si no cumple: continuar con el siguiente hallazgo (no retornar False aquí)

    # Ningún hallazgo cumplió (o todos tenían val=None)
    return False, "sin_hallazgo_cumplido"


# ══════════════════════════════════════════════════════════════════
# TP / SL DESDE EL SUMMARY  (post_15m — sin lookahead)
# ══════════════════════════════════════════════════════════════════

def _get_tp_sl_summary(escenario_key: str, summary: dict,
                        direccion: str,
                        factor_tp: float,
                        factor_sl: float,
                        tp_fijo_pct: float = None) -> tuple[float, float] | None:
    """
    Calcula TP y SL desde los promedios históricos del summary (post_15m).
    Retorna (tp_pct, sl_pct) o None si no hay datos suficientes.
    Fallback: (0.8, 0.4) — valores conservadores para 15m.
    """
    esc   = summary.get(escenario_key, {}) or {}
    post  = esc.get("post_15m", {}) or {}
    mfe_u = post.get("mfe_up_medio_pct")
    mfe_d = post.get("mfe_down_medio_pct")
    n     = post.get("n", 0)

    if mfe_u is None or mfe_d is None or n < MIN_N_ESCENARIO:
        return (0.8, 0.4)   # fallback conservador

    mfe_u = float(mfe_u)
    mfe_d = float(mfe_d)

    if mfe_u <= 0 or mfe_d <= 0:
        return (0.8, 0.4)

    if direccion == "LONG":
        sl = max(mfe_d * factor_sl, 0.05)
        if tp_fijo_pct is not None and tp_fijo_pct > 0:
            tp = float(tp_fijo_pct)
        else:
            tp = max(mfe_u * factor_tp, 0.10) if factor_tp and factor_tp > 0 else None
    else:
        sl = max(mfe_u * factor_sl, 0.05)
        if tp_fijo_pct is not None and tp_fijo_pct > 0:
            tp = float(tp_fijo_pct)
        else:
            tp = max(mfe_d * factor_tp, 0.10) if factor_tp and factor_tp > 0 else None

    tp_final = min(round(tp, 4), 8.0) if tp is not None else None
    return tp_final, min(round(sl, 4), 3.0)


# ══════════════════════════════════════════════════════════════════
# WORKER PRINCIPAL
# ══════════════════════════════════════════════════════════════════

class MLLiveBotWorker:
    """
    Worker de trading que implementa la cascada v4:
        1D → 4H → 1H (contexto) → 15m (gatillo)

    Interfaz pública idéntica al worker anterior — la UI no cambia.
    """

    def __init__(
        self,
        api_key,
        secret_key,
        symbol,
        leverage,
        margen,
        max_pos,
        tp,
        sl,
        log_callback,
        models_dir: str = "./models",
        model_prefix: str = "ethusdt",
        confidence_thr: float = 0.75,   # conservado por compatibilidad
        use_short: bool = True,
        use_backtest_tp: bool = False,  # conservado por compatibilidad
        ui_ref=None,
        demo_mode: bool = False,
        **kwargs,
    ):
        # ── Conexión ───────────────────────────────────────────────
        self.api_key    = api_key
        self.secret_key = secret_key

        raw = symbol.upper().replace(" ", "")
        if "/" in raw:
            base, quote = raw.split("/")
            self.symbol = f"{base}/{quote}:USDT"
        else:
            base = raw.replace("USDT", "")
            self.symbol = f"{base}/USDT:USDT"

        # ── Riesgo ────────────────────────────────────────────────
        self.leverage     = int(leverage)
        self.margen_usado = float(margen) / 100 if float(margen) > 1 else float(margen)
        self.max_pos_size = float(max_pos)
        self.use_short    = use_short
        self.demo_mode    = bool(demo_mode)

        # Factores TP/SL (se actualizan desde UI en el loop)
        self.factor_tp = float(tp)
        self.factor_sl = float(sl)
        self.tp_fijo_pct = kwargs.get("tp_fijo_pct")
        self.timeout_velas = kwargs.get("timeout_velas", 10)

        # ── Behavior + pretrigger ─────────────────────────────────
        self.models_dir   = models_dir
        self.model_prefix = model_prefix
        self.escenarios_excluidos = set(ESCENARIOS_EXCLUIDOS)
        self.summary     = {}
        self.setups      = {}
        self._strategy_loaded = False
        # Conservados por compatibilidad con versiones anteriores y la UI
        self.detail           = {}
        self.confidence_thr   = confidence_thr
        self.use_backtest_tp  = use_backtest_tp
        self._last_closed_candle_ts = None


        # ── Estado y circuit breaker ──────────────────────────────
        self.capital_inicio_dia     = 0.0
        self.ultimo_dia_reset       = None
        self.max_daily_loss_pct     = 0.05
        self.max_consecutive_losses = 6
        self.daily_pnl              = 0.0
        self.loss_streak            = 0
        self.last_position_check    = 0.0
        self.position_check_interval = 15   # segundos de throttle para consultas a la API
        self.is_paused              = False
        self.is_running             = False
        self.max_balance_historico  = 0.0

        # ── Comunicación y persistencia ───────────────────────────
        self.log              = log_callback
        self.exchange         = None
        self.client           = None
        self.posicion_actual  = None
        self.cache_balance    = 0.0
        self.ui_ref           = ui_ref
        self.state_file       = "bot_state_v4.json"
        self.entry_lock       = False
        self.lock_time        = None
        self.last_5m_timestamp = None

        # Caché de DataFrames (15m = gatillo, 1H/4H/1D = contextos)
        self._df_cache = {"15m": None, "1h": None, "4h": None, "1d": None}

        # Throttle de auditoría: se ejecuta máx. 1 vez por minuto
        self.last_audit_time = 0

        # Throttle de sync de balance: cada 60 segundos, independiente de posición
        self.last_balance_sync = 0

        # True si la cuenta está en Hedge Mode (detectado en resync_session)
        self._hedge_mode = False

        # Ban de IP por rate-limit Binance (-1003): timestamp UNIX (ms) hasta el que dura
        self._api_ban_until_ms = 0

    # ──────────────────────────────────────────────────────────────
    # UTILIDADES
    # ──────────────────────────────────────────────────────────────

    def _is_banned(self) -> bool:
        """Devuelve True si el IP sigue baneado por Binance (-1003)."""
        return time.time() * 1000 < self._api_ban_until_ms

    def _handle_rate_limit_error(self, e: Exception) -> bool:
        """
        Detecta el error -1003 (IP ban) de Binance y almacena el timestamp
        de fin del ban para evitar más requests durante ese período.
        Devuelve True si era un error de ban/rate-limit.
        """
        err_str = str(e)
        if "-1003" in err_str or "Way too many requests" in err_str:
            # Intentar extraer el timestamp de fin del ban del mensaje
            import re
            m = re.search(r'banned until (\d+)', err_str)
            if m:
                ban_until_ms = int(m.group(1))
                self._api_ban_until_ms = ban_until_ms
                ban_secs = max(0, (ban_until_ms - time.time() * 1000) / 1000)
                self.log.warning(
                    f"🚫 IP baneada por Binance. Pausando requests REST por {ban_secs:.0f}s "
                    f"(hasta {ban_until_ms}). Usando datos en caché."
                )
            else:
                # Sin timestamp explícito: esperar 60 s por precaución
                self._api_ban_until_ms = (time.time() + 60) * 1000
                self.log.warning("🚫 Rate limit -1003 sin timestamp. Pausando 60s.")
            return True
        return False

    def get_clean_symbol(self):
        return self.symbol.replace("/", "").split(":")[0]

    # ──────────────────────────────────────────────────────────────
    # CARGA DE DATOS
    # ──────────────────────────────────────────────────────────────

    def load_behavior_data(self) -> bool:
        try:
            base   = Path(self.models_dir)
            prefix = self.model_prefix.lower()
            subdir = base / prefix

            summary_candidates = [
                subdir / f"behavior_{prefix}_v4_summary.json",
                subdir / f"behavior_{prefix}_summary.json",
                base   / f"behavior_{prefix}_v4_summary.json",
                base   / f"behavior_{prefix}_summary.json",
                base   / f"behavior_{prefix}usdt_v4_summary.json",
            ]
            setups_candidates = [
                subdir / f"pretrigger_{prefix}_v3_setups.json",
                subdir / f"pretrigger_{prefix}_setups.json",
                base   / f"pretrigger_{prefix}_v3_setups.json",
                base   / f"pretrigger_{prefix}_setups.json",
                base   / f"pretrigger_{prefix}usdt_setups.json",
            ]

            summary_path = next((p for p in summary_candidates if p.exists()), None)
            setups_path  = next((p for p in setups_candidates  if p.exists()), None)

            if summary_path is None:
                self.log.error(
                    f"❌ No se encontró behavior summary en '{base}/'. "
                    f"Archivos buscados: {[p.name for p in summary_candidates]}"
                )
                return False

            if setups_path is None:
                self.log.error(
                    f"❌ No se encontró pretrigger setups en '{base}/'. "
                    f"Archivos buscados: {[p.name for p in setups_candidates]}"
                )
                return False

            self.summary, self.setups = cargar_behavior_data(
                str(summary_path), str(setups_path)
            )

            n_cond    = sum(1 for v in self.setups.values() if v.get("hallazgos"))
            n_excl    = len(self.escenarios_excluidos)
            n_activos = sum(
                1 for k in self.setups
                if k not in self.escenarios_excluidos and self.setups[k].get("hallazgos")
            )

            self.log.success(
                f"📊 v4 cargado | Summary: {len(self.summary)} escenarios | "
                f"Setups: {len(self.setups)} ({n_cond} con condición, "
                f"{n_excl} excluidos, {n_activos} activos) | "
                f"summary={summary_path.name}"
            )
            self._strategy_loaded = True
            return True

        except Exception as e:
            self.log.error(f"❌ Error cargando behavior data v4: {e}")
            self._strategy_loaded = False
            return False

    # Alias de compatibilidad con la UI
    def load_ml_models(self) -> bool:
        return self.load_behavior_data()

    def load_strategy_files(self) -> bool:
        return self.load_behavior_data()

    # ──────────────────────────────────────────────────────────────
    # SEÑAL BEHAVIOR v4  (gatillo 15m, contexto 1H/4H/1D)
    # ──────────────────────────────────────────────────────────────

    def _behavior_signal(self) -> tuple:
        """
        Evalúa si hay señal en la vela 15m recién cerrada.
        Retorna (signal, tp_pct, sl_pct, escenario_key) donde:
          signal = 'buy' | 'sell' | None
        """
        if not self._strategy_loaded:
            self.log.warning("⚠️ Datos no cargados. Recargando...")
            if not self.load_behavior_data():
                return None, None, None, None

        df_15m = self._df_cache.get("15m")
        df_1h  = self._df_cache.get("1h")
        df_4h  = self._df_cache.get("4h")
        df_1d  = self._df_cache.get("1d")

        if df_15m is None or len(df_15m) < 55:
            return None, None, None, None
        if df_1h  is None or len(df_1h)  < 55:
            return None, None, None, None
        if df_4h  is None or len(df_4h)  < 55:
            return None, None, None, None

        # Calcular indicadores en cada TF
        ind_15m = _calcular_indicadores(df_15m, min_velas=55)
        ind_1h  = _calcular_indicadores(df_1h,  min_velas=55)
        ind_4h  = _calcular_indicadores(df_4h,  min_velas=55)
        ind_1d  = _calcular_indicadores(df_1d,  min_velas=30) if df_1d is not None else {}

        if not ind_15m or "adx_slope" not in ind_15m:
            return None, None, None, None

        # Detectar gatillo en 15m
        gatillo = _detectar_gatillo(ind_15m)
        if gatillo is None:
            return None, None, None, None

        # Clasificar códigos de contexto
        cod_1d  = _clasificar_codigo(ind_1d)  if ind_1d  else "DD"
        cod_4h  = _clasificar_codigo(ind_4h)  if ind_4h  else "DD"
        cod_1h  = _clasificar_codigo(ind_1h)  if ind_1h  else "DD"

        escenario_key = f"{gatillo}__{cod_1d}_{cod_4h}_{cod_1h}"

        self.log.info(
            f"🔍 Gatillo {gatillo} | 15m:{_clasificar_codigo(ind_15m)} "
            f"1H:{cod_1h} 4H:{cod_4h} 1D:{cod_1d} → {escenario_key}"
        )

        # ¿Escenario excluido?
        if escenario_key in self.escenarios_excluidos:
            self.log.info(f"⏭️  Excluido: {escenario_key}")
            return None, None, None, None

        # ¿Tiene setup pretrigger?
        esc_data = self.setups.get(escenario_key)
        if esc_data is None:
            self.log.info(f"⏭️  Sin setup para {escenario_key}")
            return None, None, None, None

        direccion = str(esc_data.get("direccion", "LONG")).upper()

        if direccion == "SHORT" and not self.use_short:
            self.log.info("⏭️  SHORT desactivado")
            return None, None, None, None

        # Evaluar condición pretrigger
        features = _extraer_features_pretrigger(ind_15m, ind_1h, ind_4h, ind_1d, df_15m)
        passed, razon = _check_pretrigger(features, esc_data)

        if not passed:
            self.log.info(f"⏭️  Pretrigger no cumplido: {razon}")
            return None, None, None, None

        # TP/SL desde summary post_15m
        tp_pct, sl_pct = _get_tp_sl_summary(
            escenario_key, self.summary, direccion,
            self.factor_tp, self.factor_sl, getattr(self, "tp_fijo_pct", None)
        )

        signal = "buy" if direccion == "LONG" else "sell"

        self.log.success(
            f"✅ Señal {direccion} | {escenario_key} | "
            f"TP={tp_pct:.2f}% SL={sl_pct:.2f}% | {razon}"
        )
        return signal, tp_pct, sl_pct, escenario_key

    # Alias de compatibilidad
    def _ml_signal(self, ts_actual=None) -> tuple:
        signal, tp_pct, sl_pct, _ = self._behavior_signal()
        return signal, tp_pct, sl_pct

    def _strategy_signal(self):
        return self._behavior_signal()

    # ──────────────────────────────────────────────────────────────
    # PERSISTENCIA
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _serialize_posicion(pos):
        """Convierte posicion_actual a tipos JSON-serializables."""
        if pos is None:
            return None
        out = {}
        for k, v in pos.items():
            if hasattr(v, "isoformat"):          # datetime / pd.Timestamp
                out[k] = v.isoformat()
            elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                out[k] = None
            else:
                out[k] = v
        return out

    def save_internal_state(self):
        state = {
            "posicion_actual"   : self._serialize_posicion(self.posicion_actual),
            "daily_pnl"         : self.daily_pnl,
            "ultimo_dia_reset"  : str(self.ultimo_dia_reset),
            "capital_inicio_dia": self.capital_inicio_dia,
            "loss_streak"       : self.loss_streak,
            "is_paused"         : self.is_paused,
            "lock_time"         : self.lock_time.isoformat() if self.lock_time else None,
            "max_balance_hist"  : self.max_balance_historico,
        }
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.state_file)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)

    def load_internal_state(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.state_file)
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            self.posicion_actual        = None   # siempre se resincroniza
            self.daily_pnl              = state.get("daily_pnl", 0.0)
            self.capital_inicio_dia     = state.get("capital_inicio_dia", 0.0)
            self.loss_streak            = state.get("loss_streak", 0)
            self.entry_lock             = False
            self.is_paused              = state.get("is_paused", False)
            self.max_balance_historico  = state.get("max_balance_hist", 0.0)
            lock_str = state.get("lock_time")
            self.lock_time = (
                datetime.fromisoformat(lock_str)
                if lock_str and lock_str != "None" else None
            )
            date_str = state.get("ultimo_dia_reset")
            if date_str and date_str != "None":
                self.ultimo_dia_reset = datetime.strptime(date_str, "%Y-%m-%d").date()
            self.log.success("📂 Estado v4 cargado.")
        except Exception as e:
            self.log.error(f"Error cargando estado: {e}")

    def logger_csv(self, event, details, pnl=None, balance=None, indicators=None):
        file_exists = os.path.isfile("bot_v4_log.csv")
        with open("bot_v4_log.csv", mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "Evento", "Detalles", "PnL_Neto", "Balance", "Indicadores"])
            writer.writerow([datetime.now(timezone.utc), event, details, pnl, balance, indicators])

    def log_trade_export(self, action, side, qty, entry_price, exit_price=None, pnl=None, scenario=None):
        export_dir = "export"
        os.makedirs(export_dir, exist_ok=True)
        filepath    = os.path.join(export_dir, "trades_v4_log.csv")
        file_exists = os.path.isfile(filepath)
        with open(filepath, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "Symbol", "Action", "Side", "Qty",
                                  "Entry_Price", "Exit_Price", "PnL_Neto", "Balance", "Scenario"])
            writer.writerow([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                self.symbol, action.upper(), side.upper(), qty,
                entry_price, exit_price or "", pnl or "",
                self.cache_balance, scenario or "",
            ])

    # ──────────────────────────────────────────────────────────────
    # CONEXIÓN Y DATOS DE MERCADO
    # ──────────────────────────────────────────────────────────────

    def sync_market_precision(self):
        try:
            markets = self.exchange.load_markets()
            if self.symbol in markets:
                self.log.success(f"📈 Precision synced: {self.symbol}")
                return True
            self.log.error(f"❌ Símbolo no encontrado: {self.symbol}")
            return False
        except Exception as e:
            self.log.error(f"Error sync precision: {e}")
            return False

    # URL oficial vigente del testnet de Binance Futures (USDS-M).
    # NOTA: Binance migró la URL de testnet.binancefuture.com a demo-fapi.binance.com
    # (anuncio del 27/ago/2025). Algunas versiones de ccxt/python-binance todavía
    # apuntan internamente a la URL vieja con testnet=True / set_sandbox_mode(True),
    # así que la forzamos explícitamente para no depender de que la librería
    # esté actualizada — evita el fallo silencioso de "funciona pero enruta mal".
    DEMO_FAPI_REST_URL = "https://demo-fapi.binance.com"
    DEMO_FAPI_WS_URL   = "wss://demo-fstream.binance.com"

    def resync_session(self):
        if self._is_banned():
            ban_secs = max(0, (self._api_ban_until_ms / 1000) - time.time())
            self.log.warning(f"🚫 IP baneada — esperando {ban_secs:.0f}s antes de resync.")
            time.sleep(min(ban_secs + 1, 120))

        try:
            self.exchange = ccxt.binanceusdm({
                "apiKey"         : self.api_key,
                "secret"         : self.secret_key,
                "enableRateLimit": True,
                "options"        : {
                    "defaultType"              : "future",
                    "adjustForTimeDifference"  : True,
                    "recvWindow"               : 10000,
                },
            })

            if self.demo_mode:
                # Testnet de Binance Futures — requiere API keys generadas en
                # https://testnet.binancefuture.com (distintas a las de producción).
                # set_sandbox_mode() como base, más override explícito de las URLs
                # fapi para blindarnos ante versiones de ccxt con la URL vieja.
                self.exchange.set_sandbox_mode(True)
                try:
                    self.exchange.urls["api"]["fapiPublic"]  = self.DEMO_FAPI_REST_URL + "/fapi/v1"
                    self.exchange.urls["api"]["fapiPrivate"] = self.DEMO_FAPI_REST_URL + "/fapi/v1"
                    self.exchange.urls["api"]["fapiPrivateV2"] = self.DEMO_FAPI_REST_URL + "/fapi/v2"
                    self.exchange.urls["api"]["fapiPrivateV3"] = self.DEMO_FAPI_REST_URL + "/fapi/v3"
                except Exception as e:
                    self.log.warning(f"No se pudo forzar override de URLs ccxt: {e}")
                self.log.warning(f"🧪 DEMO MODE activo — ccxt apuntando a {self.DEMO_FAPI_REST_URL}")

            self.client = Client(self.api_key, self.secret_key, testnet=self.demo_mode)

            if self.demo_mode:
                # Override explícito del atributo interno de python-binance —
                # mismo motivo que arriba: no confiar en que testnet=True
                # resuelva a la URL vigente en la versión instalada.
                try:
                    self.client.FUTURES_URL = self.DEMO_FAPI_REST_URL + "/fapi"
                except Exception as e:
                    self.log.warning(f"No se pudo forzar FUTURES_URL en python-binance: {e}")
                self.log.warning(f"🧪 DEMO MODE activo — python-binance FUTURES_URL={getattr(self.client, 'FUTURES_URL', '?')}")

                # Self-test de conectividad — confirma que la URL realmente
                # responde antes de seguir, en vez de fallar silenciosamente
                # más adelante en medio de una orden.
                try:
                    server_time = self.client.futures_time()
                    self.log.success(f"✅ Testnet respondiendo — server_time={server_time.get('serverTime')}")
                except Exception as e:
                    self.log.error(f"❌ Testnet no responde en {self.DEMO_FAPI_REST_URL}: {e}")
                    return False

            if not self.sync_market_precision():
                return False

            symbol_clean = self.get_clean_symbol()

            # Detectar / forzar One-way Mode y guardar el resultado real
            # para que set_protections pueda añadir positionSide si es Hedge Mode.
            self._hedge_mode = False
            if not self.posicion_actual:
                try:
                    self.client.futures_change_position_mode(dualSidePosition=False)
                    self._hedge_mode = False
                    self.log.info("⚙️ Position mode: One-way")
                except Exception as e:
                    err = str(e)
                    if "-4067" in err or "No need to change" in err:
                        # Ya estaba en One-way — OK
                        self._hedge_mode = False
                    else:
                        # No se pudo cambiar (posiciones abiertas, etc.)
                        # Consultar el modo actual para saberlo de verdad
                        self.log.warning(f"Position Mode no cambiado: {e}")
                        try:
                            pm = self.client._request_futures_api('get', 'positionSide/dual', True)
                            self._hedge_mode = bool(pm.get("dualSidePosition", False))
                            self.log.warning(
                                f"⚠️ Hedge Mode = {self._hedge_mode} "
                                f"(no se pudo forzar One-way con posición abierta)"
                            )
                        except Exception as e2:
                            if not self._handle_rate_limit_error(e2):
                                pass
            else:
                # Con posición abierta no intentamos cambiar el modo,
                # pero necesitamos saber en cuál estamos
                try:
                    pm = self.client._request_futures_api('get', 'positionSide/dual', True)
                    self._hedge_mode = bool(pm.get("dualSidePosition", False))
                except Exception as e:
                    if not self._handle_rate_limit_error(e):
                        pass

            try:
                self.client.futures_change_margin_type(symbol=symbol_clean, marginType="ISOLATED")
            except Exception as e:
                if not self._handle_rate_limit_error(e):
                    pass

            try:
                self.client.futures_change_leverage(symbol=symbol_clean, leverage=self.leverage)
                self.log.info(f"⚙️ Leverage: {self.leverage}x")
            except Exception as e:
                if not self._handle_rate_limit_error(e):
                    if "-4161" not in str(e):
                        self.log.warning(f"Leverage: {e}")

            balance_data = self.client.futures_account_balance()
            total_usdt   = next((float(b["balance"]) for b in balance_data if b["asset"] == "USDT"), 0.0)
            self.cache_balance = total_usdt

            if self.max_balance_historico == 0:
                self.max_balance_historico = total_usdt

            now_date = datetime.now(timezone.utc).date()
            if self.ultimo_dia_reset is None or now_date > self.ultimo_dia_reset:
                if total_usdt > 0:
                    self.capital_inicio_dia = total_usdt
                    self.ultimo_dia_reset   = now_date
                    self.daily_pnl          = 0.0
                    if self.is_paused:
                        self.is_paused = False

            self.log.success("✅ Sesión sincronizada (v4 — gatillo 15m).")
            return True

        except Exception as e:
            self.log.error(f"resync_session failed: {e}")
            return False

    def fetch_ohlcv_df(self, tf: str, limit: int = 300) -> pd.DataFrame | None:
        for attempt in range(3):
            try:
                bars = self.exchange.fetch_ohlcv(self.symbol, timeframe=tf, limit=limit)
                df   = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "vol"])
                df   = df.iloc[:-1].copy()   # descarta vela en formación
                if df.empty:
                    return None
                df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                df = df.set_index("ts", drop=False)
                df["ts"] = (df.index.view("int64") // 10**6).astype("int64")
                return df
            except Exception as e:
                err_str = str(e)
                is_rate_limit = "429" in err_str or "Too Many Requests" in err_str
                if attempt < 2:
                    # Backoff más largo si es rate limit (30s), normal si es error de red (5s)
                    wait = 30 if is_rate_limit else 5
                    if is_rate_limit:
                        self.log.warning(f"⏳ Rate limit en fetch {tf} — esperando {wait}s (intento {attempt+1}/3)")
                    time.sleep(wait)
                else:
                    self.log.error(f"Error fetching {tf} tras 3 intentos: {e}")
        return None

    def fetch_confirmed_data_with_cache(self, tf: str, limit: int = 300):
        """
        Descarga OHLCV, guarda el DF en caché y retorna dict
        {current, prev, prev2} compatible con monitor_position y UI.
        """
        df = self.fetch_ohlcv_df(tf, limit)
        if df is None or len(df) < 3:
            return None

        # Guardar DF completo para el motor de señal
        self._df_cache[tf] = df.copy()

        # Indicadores mínimos para UI y monitor_position
        df = df.copy()
        df["EMA_55"]  = ta.ema(df["close"].astype(float), length=55)
        df["EMA_200"] = ta.ema(df["close"].astype(float), length=200)
        df["ATR"]     = ta.atr(df["high"].astype(float), df["low"].astype(float),
                               df["close"].astype(float), length=14)

        adx_df = ta.adx(df["high"].astype(float), df["low"].astype(float),
                        df["close"].astype(float), length=14)
        if adx_df is not None and not adx_df.empty:
            adx_col = [c for c in adx_df.columns if "ADX_" in c]
            if adx_col:
                df["ADX"]       = adx_df[adx_col[0]]
                df["ADX_SLOPE"] = df["ADX"].diff()

        length      = 20
        basis       = ta.sma(df["close"].astype(float), length=length)
        ma_high_low = (df["high"].astype(float).rolling(length).max() +
                       df["low"].astype(float).rolling(length).min()) / 2
        val         = (ma_high_low + basis) / 2
        df["SQZ_HIST"] = ta.linreg(df["close"].astype(float) - val, length=length)
        df["SQZ_DIR"]  = df["SQZ_HIST"].diff()

        if df["EMA_55"].notna().any():
            df["PRICE_EMA55_DIST"] = ((df["close"].astype(float) - df["EMA_55"]) /
                                      df["EMA_55"])

        return {"current": df.iloc[-1], "prev": df.iloc[-2], "prev2": df.iloc[-3]}

    def fetch_market_state(self):
        """
        Descarga los 4 timeframes. 15m es el gatillo; 1H/4H/1D son contextos.
        """
        data   = {}
        limits = {"15m": 400, "1h": 300, "4h": 200, "1d": 100}
        for tf in ["15m", "1h", "4h", "1d"]:
            val = self.fetch_confirmed_data_with_cache(tf, limit=limits[tf])
            if val is None:
                self.log.warning(f"⚠️ Datos {tf} no disponibles.")
            data[tf] = val
        return data

    # ──────────────────────────────────────────────────────────────
    # AUDITORÍA Y CIRCUIT BREAKER
    # ──────────────────────────────────────────────────────────────

    def sync_balance(self, force: bool = False):
        """
        Sincroniza cache_balance desde la API de Binance.
        Se llama cada 60s desde el loop principal — independiente de si
        hay posición abierta o no, y de si el bot fue quien cerró o no.
        También actualiza max_balance_historico y daily_pnl del día actual.
        """
        now = time.time()
        if not force and now - self.last_balance_sync < 60:
            return
        # No intentar si el IP está baneado
        if self._is_banned():
            return
        try:
            balance_data   = self.client.futures_account_balance()
            balance_actual = next(
                (float(b["balance"]) for b in balance_data if b["asset"] == "USDT"), None
            )
            if balance_actual is None:
                return

            prev = self.cache_balance
            self.cache_balance = balance_actual
            self.last_balance_sync = now

            if balance_actual > self.max_balance_historico:
                self.max_balance_historico = balance_actual

            # Recalcular daily_pnl real desde el capital de inicio del día
            # Esto corrige la deriva cuando se cierran posiciones manualmente.
            if self.capital_inicio_dia and self.capital_inicio_dia > 0:
                self.daily_pnl = balance_actual - self.capital_inicio_dia

            if abs(balance_actual - prev) > 0.01:
                self.log.info(
                    f"💰 Balance sincronizado: ${balance_actual:.2f} "
                    f"(Δ{balance_actual - prev:+.2f} | pnl_dia={self.daily_pnl:+.2f})"
                )

        except Exception as e:
            if not self._handle_rate_limit_error(e):
                self.log.warning(f"sync_balance error: {e}")

    def _get_open_orders_robust(self, symbol_clean: str) -> list:
        """
        Obtiene órdenes abiertas usando tres fuentes en cascada:
          1. futures_get_open_orders  — endpoint estándar, siempre disponible
          2. openAlgoOrders           — GET /fapi/v1/openAlgoOrders (endpoint correcto)
          3. futures_get_all_orders   — historial reciente filtrado por estado NEW
        Retorna lista unificada (deduplicada por orderId).
        openAlgoOrders es la fuente autoritativa para algo orders (STOP_MARKET, etc)
        creadas desde el 2025-12-09 en adelante.
        """
        SL_TYPES = {"STOP_MARKET", "STOP", "STOP_LOSS_MARKET"}
        seen     = {}   # orderId → orden

        # ── Fuente 1: órdenes normales (siempre funciona) ──────────────────
        try:
            for o in (self.client.futures_get_open_orders(symbol=symbol_clean) or []):
                oid = o.get("orderId") or o.get("clientOrderId", id(o))
                seen[oid] = o
            self.log.info(f"[audit] futures_get_open_orders → {len(seen)} órdenes")
        except Exception as _e:
            if self._handle_rate_limit_error(_e):
                return list(seen.values())
            self.log.warning(f"[audit] futures_get_open_orders falló: {_e}")

        # ── Fuente 2: GET /fapi/v1/openAlgoOrders (endpoint correcto según docs) ──
        # NOTA: el endpoint es openAlgoOrders (open primero), NO algoOpenOrders.
        # algoOpenOrders devuelve 404 — nombre incorrecto que estaba en el código.
        if self._is_banned():
            return list(seen.values())
        try:
            result      = self.client._request_futures_api(
                'get', 'openAlgoOrders', True, data={"symbol": symbol_clean}
            )
            # La respuesta es directamente una lista según la doc oficial
            algo_orders = result if isinstance(result, list) else result.get("orders", [])
            added = 0
            for o in algo_orders:
                oid = o.get("algoId") or o.get("orderId") or o.get("clientOrderId", id(o))
                if oid not in seen:
                    seen[oid] = o
                    added += 1
            self.log.info(f"[audit] openAlgoOrders → {len(algo_orders)} algo ({added} nuevas)")
        except Exception as _e:
            if self._handle_rate_limit_error(_e):
                return list(seen.values())
            self.log.warning(f"[audit] openAlgoOrders falló: {_e}")

        # ── Fuente 3: historial reciente si aún no tenemos SL ──────────────
        has_sl_so_far = any(
            (o.get("orderType") or o.get("type", "")) in SL_TYPES
            and (o.get("algoStatus") or o.get("status", "NEW")) in ("NEW", "WORKING")
            for o in seen.values()
        )
        if not has_sl_so_far and not self._is_banned():
            try:
                # Últimas 10 órdenes del símbolo; filtramos por estado NEW
                all_orders = self.client.futures_get_all_orders(
                    symbol=symbol_clean, limit=10
                ) or []
                added = 0
                for o in all_orders:
                    st  = o.get("status", "")
                    oid = o.get("orderId") or o.get("clientOrderId", id(o))
                    if st == "NEW" and oid not in seen:
                        seen[oid] = o
                        added += 1
                if added:
                    self.log.info(f"[audit] get_all_orders fallback (+{added} NEW)")
            except Exception as _e3:
                self._handle_rate_limit_error(_e3)
                self.log.warning(f"[audit] get_all_orders falló: {_e3}")

        return list(seen.values())

    def audit_persistence(self):
        # Throttle: ejecutar máximo una vez cada 60 segundos para evitar
        # el ciclo cancelar→recrear SL (has_sl=False en cada poll de 5s).
        now = time.time()
        if now - self.last_audit_time < 60:
            return
        # No auditar si el IP está baneado — evita acumular más requests
        if self._is_banned():
            return
        self.last_audit_time = now

        try:
            symbol_clean = self.get_clean_symbol()
            pos_info     = self.client.futures_position_information(symbol=symbol_clean)
            active_pos   = next((p for p in pos_info if float(p["positionAmt"]) != 0), None)

            if active_pos:
                SL_TYPES    = {"STOP_MARKET", "STOP", "STOP_LOSS_MARKET"}
                open_orders = self._get_open_orders_robust(symbol_clean)

                # ── Determinar si hay SL activo ────────────────────────────
                has_sl = False
                for o in open_orders:
                    o_type = o.get("orderType") or o.get("type", "")
                    status = o.get("algoStatus") or o.get("status", "NEW")
                    if o_type in SL_TYPES and status in ("NEW", "WORKING"):
                        has_sl = True
                        break

                self.log.info(f"[audit] has_sl={has_sl} | orders_totales={len(open_orders)}")

                # ── Reconstruir posicion_actual si no la tenemos ───────────
                if not self.posicion_actual:
                    self.log.warning("⚠️ Sincronizando posición externa...")
                    pos_amt = float(active_pos["positionAmt"])
                    side    = "buy" if pos_amt > 0 else "sell"
                    entry_p = float(active_pos["entryPrice"])

                    # ── Prioridad 1: leer triggerPrice desde las órdenes activas ──
                    sl_p = None
                    for o in open_orders:
                        o_type = o.get("orderType") or o.get("type", "")
                        trig   = _sf(
                            o.get("triggerPrice") or o.get("stopPrice") or o.get("price")
                        )
                        if trig and o_type in SL_TYPES:
                            sl_p = trig
                            break

                    # ── Prioridad 2: reconstruir desde sl_pct guardado en estado ──
                    # sl_pct es el % calculado por _get_tp_sl_summary con el summary
                    # del escenario (mismo que usa el backtest). Es el valor correcto.
                    if sl_p is None:
                        sl_pct_guardado = None
                        if self.posicion_actual:
                            sl_pct_guardado = self.posicion_actual.get("sl_pct")
                        if sl_pct_guardado and sl_pct_guardado > 0:
                            sl_p = (entry_p * (1 - sl_pct_guardado / 100) if side == "buy"
                                    else entry_p * (1 + sl_pct_guardado / 100))
                            self.log.warning(
                                f"⚠️ SL no visible en órdenes — reconstruido desde sl_pct={sl_pct_guardado:.4f}% "
                                f"(entry={entry_p:.4f} → sl={sl_p:.4f})"
                            )

                    # ── Prioridad 3: recalcular desde summary con factor_sl actual ──
                    if sl_p is None and self.posicion_actual:
                        scenario_saved = self.posicion_actual.get("scenario")
                        if scenario_saved and self.summary:
                            direccion_saved = "LONG" if side == "buy" else "SHORT"
                            tp_sl = _get_tp_sl_summary(
                                scenario_saved, self.summary, direccion_saved,
                                self.factor_tp, self.factor_sl
                            )
                            if tp_sl:
                                sl_pct_recap = tp_sl[1]
                                sl_p = (entry_p * (1 - sl_pct_recap / 100) if side == "buy"
                                        else entry_p * (1 + sl_pct_recap / 100))
                                self.log.warning(
                                    f"⚠️ SL recalculado desde summary escenario={scenario_saved} "
                                    f"sl_pct={sl_pct_recap:.4f}% → sl={sl_p:.4f}"
                                )

                    # ── Prioridad 4: fallback conservador ──────────────────────────
                    # Último recurso: 1%. Se loguea como ERROR para que sea visible.
                    if sl_p is None:
                        fallback_pct = 0.01
                        sl_p = (entry_p * (1 - fallback_pct) if side == "buy"
                                else entry_p * (1 + fallback_pct))
                        self.log.error(
                            f"🚨 No se pudo reconstruir SL desde ninguna fuente — "
                            f"usando fallback emergencia 1% (entry={entry_p:.4f} sl={sl_p:.4f}). "
                            f"Revisar summary y estado guardado."
                        )

                    # ── Reconstrucción de TP ────────────────────────────────────────
                    # Mismo sistema de prioridades que el SL.
                    # Sin esto, audit_persistence llama a set_protections con
                    # tp_precio_input=None y el TP nunca se coloca tras un restart.
                    TP_TYPES = {"TAKE_PROFIT_MARKET", "TAKE_PROFIT"}
                    tp_p = None

                    # Prioridad 1: leer desde algo orders activas
                    for o in open_orders:
                        o_type = o.get("orderType") or o.get("type", "")
                        trig   = _sf(
                            o.get("triggerPrice") or o.get("stopPrice") or o.get("price")
                        )
                        if trig and o_type in TP_TYPES:
                            tp_p = trig
                            break

                    # Prioridad 2: recalcular desde summary con factores actuales
                    # (única fuente fiable cuando no hay órdenes activas en el exchange)
                    if tp_p is None and self.summary:
                        scenario_saved  = (self.posicion_actual or {}).get("scenario")
                        direccion_saved = "LONG" if side == "buy" else "SHORT"
                        # Si no tenemos scenario guardado, no podemos recalcular — se omite TP
                        if scenario_saved:
                            tp_sl_recap = _get_tp_sl_summary(
                                scenario_saved, self.summary, direccion_saved,
                                self.factor_tp, self.factor_sl
                            )
                            if tp_sl_recap and tp_sl_recap[0] is not None:
                                tp_pct_recap = tp_sl_recap[0]
                                tp_p = (entry_p * (1 + tp_pct_recap / 100) if side == "buy"
                                        else entry_p * (1 - tp_pct_recap / 100))
                                self.log.warning(
                                    f"⚠️ TP reconstruido desde summary escenario={scenario_saved} "
                                    f"tp_pct={tp_pct_recap:.4f}% → tp={tp_p:.4f}"
                                )
                        else:
                            self.log.warning(
                                "⚠️ No hay scenario guardado — TP no puede reconstruirse. "
                                "La posición se cerrará solo por SL o timeout."
                            )

                    # ts_timeout como string ISO para que save_internal_state no falle
                    from datetime import timezone as _tz
                    ts_to = (datetime.now(_tz.utc) + pd.Timedelta(minutes=getattr(self, "timeout_velas", 10) * 15)).isoformat()
                    self.posicion_actual = {
                        "qty"          : abs(pos_amt),
                        "side"         : side,
                        "entry_price"  : entry_p,
                        "sl_precio"    : sl_p,
                        "tp_precio"    : tp_p,
                        "max_alcanzado": entry_p,
                        "entry_balance": self.cache_balance,
                        "ts_timeout"   : ts_to,
                    }
                    self.save_internal_state()
                    
                    try:
                        import traceback as _tb
                        _tp_pct = abs(tp_p - entry_p) / entry_p * 100 if tp_p else None
                        _sl_pct = abs(sl_p - entry_p) / entry_p * 100 if sl_p else None
                        self.log.info("Posicion abierta detectada al inicio. Notificando a Instagram...")
                        self.log.info(f"  symbol={self.symbol} side={side} entry={entry_p} tp_pct={_tp_pct} sl_pct={_sl_pct} lev={self.leverage}")
                        post_apertura(
                            side=side,
                            symbol=self.symbol,
                            entry_price=entry_p,
                            tp_pct=_tp_pct,
                            sl_pct=_sl_pct,
                            leverage=self.leverage,
                            timeout_velas=getattr(self, "timeout_velas", 10),
                            scenario=None,
                            log=self.log
                        )
                    except Exception as e:
                        self.log.error(f"Error Instagram startup: {e}")
                        self.log.error(_tb.format_exc())

                # ── Re-colocar SL solo si realmente falta ─────────────────
                # Doble-verificación de posición antes de tocar órdenes.
                if not has_sl and self.posicion_actual:
                    sl_para_resync = self.posicion_actual["sl_precio"]
                    entry_para_resync = float(self.posicion_actual["entry_price"])
                    lado_resync = self.posicion_actual["side"]

                    # Guardia: no colocar SL en entry_price (sería inútil/peligroso)
                    sl_en_entry = abs(sl_para_resync - entry_para_resync) / entry_para_resync < 0.0005
                    if sl_en_entry:
                        self.log.error(
                            f"🚨 sl_precio ({sl_para_resync:.4f}) está en entry ({entry_para_resync:.4f}) "
                            f"— reconstrucción fallida, NO se coloca SL para evitar cierre inmediato"
                        )
                    elif not self._is_banned():
                        pos_check  = self.client.futures_position_information(symbol=symbol_clean)
                        still_open = next((p for p in pos_check if float(p["positionAmt"]) != 0), None)
                        if still_open:
                            self.log.warning("❗ Re-sincronizando protecciones (SL ausente confirmado)...")
                            tp_para_resync = self.posicion_actual.get("tp_precio")
                            self.set_protections(
                                lado_resync,
                                self.posicion_actual["qty"],
                                sl_para_resync,
                                tp_precio_input=tp_para_resync,
                                skip_cancel=True,   # no cancelar lo que no existe
                            )
            else:
                if self.posicion_actual:
                    self._handle_position_closed(source="audit")

        except Exception as e:
            if not self._handle_rate_limit_error(e):
                self.log.error(f"Error auditoría: {e}")

    def _manage_circuit_breaker(self):
        # No intentar si el IP está baneado
        if self._is_banned():
            return
        try:
            balance_data   = self.client.futures_account_balance()
            balance_actual = next((float(b["balance"]) for b in balance_data if b["asset"] == "USDT"), 0.0)
            self.cache_balance = balance_actual

            if balance_actual > self.max_balance_historico:
                self.max_balance_historico = balance_actual

            if self.max_balance_historico > 0:
                dd = (self.max_balance_historico - balance_actual) / self.max_balance_historico
                if dd >= 0.05:
                    self.is_paused = True
                    self.lock_time = datetime.now(timezone.utc)
                    self.log.critical(f"🛑 CIRCUIT BREAKER: DD {dd:.2%} desde pico ${self.max_balance_historico:.2f}")
                    self.save_internal_state()

        except Exception as e:
            if not self._handle_rate_limit_error(e):
                self.log.error(f"Error Circuit Breaker: {e}")

    # ──────────────────────────────────────────────────────────────
    # PROTECCIONES SL / TP EN EL EXCHANGE
    # ──────────────────────────────────────────────────────────────

    def _cancel_all_orders(self, symbol_clean):
        """
        Cancela tanto órdenes normales como algo orders.
        Binance requiere endpoints separados para cada tipo.
        """
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol_clean)
        except Exception:
            pass
        try:
            # DELETE /fapi/v1/algoOpenOrders — cancela todas las algo orders del símbolo
            self.client._request_futures_api(
                'delete', 'algoOpenOrders', True,
                data={"symbol": symbol_clean}
            )
        except Exception:
            pass

    def set_protections(self, side, qty, sl_precio_input, tp_precio_input=None,
                        skip_cancel: bool = False):
        """
        Coloca SL y TP en el exchange via POST /fapi/v1/algoOrder.

        skip_cancel=True: no cancela órdenes existentes antes de colocar el SL.
        Usar cuando audit_persistence ya confirmó que no hay SL (evita destruir
        una orden que pudiera haberse creado en la ventana entre la lectura y
        esta llamada).
        """
        try:
            symbol_clean = self.get_clean_symbol()
            opp_side     = "SELL" if side == "buy" else "BUY"
            ticker       = self.exchange.fetch_ticker(self.symbol)
            last_price   = ticker["last"]

            sl_price = float(sl_precio_input)

            # Sanidad: ajustar si el SL cruzó el precio de mercado
            if side == "buy":
                if sl_price >= last_price: sl_price = last_price * 0.999
            else:
                if sl_price <= last_price: sl_price = last_price * 1.001

            sl_p = self.exchange.price_to_precision(self.symbol, sl_price)

            if not skip_cancel:
                try:
                    self._cancel_all_orders(symbol_clean)
                except Exception:
                    pass

            # ── FIX v7: POST /fapi/v1/algoOrder  ─────────────────────────────────
            # Desde 2025-12-09 Binance migró las órdenes condicionales al Algo Service.
            # STOP_MARKET / TAKE_PROFIT_MARKET / STOP / TAKE_PROFIT / TRAILING_STOP_MARKET
            # deben enviarse a POST /fapi/v1/algoOrder.
            # POST /fapi/v1/order devuelve -4120 para estos tipos.
            #
            # Parámetros según doc oficial (developers.binance.com):
            #   algoType     = "CONDITIONAL"   (obligatorio, único valor soportado)
            #   type         = "STOP_MARKET"
            #   triggerPrice = precio de disparo (nombre correcto; stopPrice era /fapi/v1/order)
            #   closePosition= "true"  → cierra toda la posición al dispararse
            #                            no puede convivir con quantity ni reduceOnly
            #   workingType  = "CONTRACT_PRICE" | "MARK_PRICE"
            #   positionSide sólo si Hedge Mode: "LONG" o "SHORT"
            #                con closePosition=true, NO se puede usar BUY en LONG side
            #                ni SELL en SHORT side (doc: "cannot be used with BUY orders
            #                in LONG position side")
            algo_params = {
                "algoType"    : "CONDITIONAL",
                "symbol"      : symbol_clean,
                "side"        : opp_side,
                "type"        : "STOP_MARKET",
                "triggerPrice": str(sl_p),
                "closePosition": "true",
                "workingType" : "CONTRACT_PRICE",
            }
            if getattr(self, "_hedge_mode", False):
                # One-way: positionSide omitido (default BOTH)
                # Hedge:   SL de LONG → side=SELL, positionSide=LONG
                #          SL de SHORT→ side=BUY,  positionSide=SHORT
                algo_params["positionSide"] = "LONG" if side == "buy" else "SHORT"

            self.client._request_futures_api(
                'post', 'algoOrder', True, data=algo_params
            )
            self.log.info(f"🛡️ SL colocado: {sl_p}")

            # ── TP: TAKE_PROFIT_MARKET via /fapi/v1/algoOrder ────────────────────
            # FIX #2: refrescar last_price justo antes del TP.
            #   El SL puede tardar varios segundos en procesarse y el precio puede
            #   haber cruzado el TP calculado sobre fill_price si el mercado se movió
            #   a favor durante la espera del llenado de la orden LIMIT (hasta 30s).
            #   Reutilizar last_price del inicio del método introduce ese race condition.
            # FIX #3: reintentar TP una vez con precio ajustado en caso de -2021
            #   (Order would immediately trigger). Loguear el error completo para
            #   diagnóstico en lugar de silenciarlo con `tp_p = None`.
            tp_p = None
            if tp_precio_input is not None:
                try:
                    # Precio de mercado fresco — evita el race condition con el SL
                    try:
                        ticker_tp  = self.exchange.fetch_ticker(self.symbol)
                        last_price_tp = float(ticker_tp["last"])
                    except Exception:
                        last_price_tp = last_price   # fallback al precio inicial

                    tp_price = float(tp_precio_input)

                    # Sanidad: ajustar si el TP cruzó el precio de mercado actual
                    MIN_TP_DIST_PCT = 0.05   # al menos 0.05% de distancia del precio actual
                    if side == "buy":
                        min_tp = last_price_tp * (1 + MIN_TP_DIST_PCT / 100)
                        if tp_price <= last_price_tp:
                            self.log.warning(
                                f"⚠️ TP ({tp_price:.4f}) ≤ last_price ({last_price_tp:.4f}) "
                                f"— ajustando a {min_tp:.4f}"
                            )
                            tp_price = min_tp
                    else:
                        max_tp = last_price_tp * (1 - MIN_TP_DIST_PCT / 100)
                        if tp_price >= last_price_tp:
                            self.log.warning(
                                f"⚠️ TP ({tp_price:.4f}) ≥ last_price ({last_price_tp:.4f}) "
                                f"— ajustando a {max_tp:.4f}"
                            )
                            tp_price = max_tp

                    tp_p = self.exchange.price_to_precision(self.symbol, tp_price)

                    tp_params = {
                        "algoType"     : "CONDITIONAL",
                        "symbol"       : symbol_clean,
                        "side"         : opp_side,
                        "type"         : "TAKE_PROFIT_MARKET",
                        "triggerPrice" : str(tp_p),
                        "closePosition": "true",
                        "workingType"  : "CONTRACT_PRICE",
                    }
                    if getattr(self, "_hedge_mode", False):
                        tp_params["positionSide"] = "LONG" if side == "buy" else "SHORT"

                    self.client._request_futures_api(
                        'post', 'algoOrder', True, data=tp_params
                    )
                    self.log.info(f"🎯 TP colocado: {tp_p}")

                except Exception as e_tp:
                    err_str = str(e_tp)
                    self.log.error(f"❌ Error colocando TP (params={tp_params if 'tp_params' in dir() else 'N/A'}): {e_tp}")
                    # Reintento único si el error es -2021 (triggerPrice inmediato)
                    # o -4061 (precio inválido): alejar el TP un tick más del precio actual
                    if ("-2021" in err_str or "-4061" in err_str) and 'tp_params' in dir():
                        try:
                            # Ampliar 0.1% adicional respecto al precio actual
                            retry_ticker  = self.exchange.fetch_ticker(self.symbol)
                            retry_last    = float(retry_ticker["last"])
                            if side == "buy":
                                tp_retry = retry_last * 1.001
                            else:
                                tp_retry = retry_last * 0.999
                            tp_params["triggerPrice"] = str(
                                self.exchange.price_to_precision(self.symbol, tp_retry)
                            )
                            self.client._request_futures_api(
                                'post', 'algoOrder', True, data=tp_params
                            )
                            tp_p = tp_params["triggerPrice"]
                            self.log.info(f"🎯 TP colocado (reintento): {tp_p}")
                        except Exception as e_tp2:
                            self.log.error(f"❌ Error colocando TP (reintento): {e_tp2}")
                            tp_p = None
                    else:
                        tp_p = None   # error no recuperable — marcar como no colocado

            self.log.success(
                f"🛡️ Protecciones activas | SL={sl_p}"
                + (f" | TP={tp_p}" if tp_p is not None else " | TP=no colocado")
            )
            return True

        except Exception as e:
            if not self._handle_rate_limit_error(e):
                self.log.error(f"Error protecciones (SL): {e}")
            return False

    # ──────────────────────────────────────────────────────────────
    # CÁLCULO DE TAMAÑO DE POSICIÓN (interés compuesto + cap)
    # ──────────────────────────────────────────────────────────────

    def _calcular_qty(self, price: float) -> float:
        """
        pos_nominal = min(balance × margen × leverage, max_pos_size)
        qty         = pos_nominal / price
        """
        pos_nominal = min(
            self.cache_balance * self.margen_usado * self.leverage,
            self.max_pos_size
        )
        qty = self.exchange.amount_to_precision(self.symbol, pos_nominal / price)
        return float(qty)

    # ──────────────────────────────────────────────────────────────
    # ENTRADA CON ORDEN LIMIT
    # ──────────────────────────────────────────────────────────────

    def execute_entry(self, side: str, price: float,
                      tp_pct: float, sl_pct: float,
                      scenario: str | None = None) -> bool:
        """
        Abre posición con orden LIMIT al bid/ask actual + offset mínimo.
        Si no se llena en LIMIT_TIMEOUT_SEG segundos → cancela y devuelve False.
        Esto elimina el deslizamiento de las órdenes market.
        """
        try:
            # Sincronizar balance (reutilizar sync_balance para no duplicar lógica)
            self.sync_balance(force=True)

            # Precio limit: ligeramente sobre ask para LONG, bajo bid para SHORT
            ob = self.exchange.fetch_order_book(self.symbol, limit=5)
            if side == "buy":
                limit_price = float(ob["asks"][0][0]) * (1 + LIMIT_OFFSET_PCT / 100)
            else:
                limit_price = float(ob["bids"][0][0]) * (1 - LIMIT_OFFSET_PCT / 100)

            limit_price_str = self.exchange.price_to_precision(self.symbol, limit_price)
            qty             = self._calcular_qty(limit_price)

            if qty <= 0:
                self.log.error("❌ Qty = 0. Abortando entrada.")
                return False

            self.log.info(
                f"📤 Orden LIMIT {'LONG' if side=='buy' else 'SHORT'} | "
                f"precio={limit_price_str} | qty={qty} | "
                f"nominal=${qty * limit_price:.2f}"
            )

            order    = self.exchange.create_order(
                self.symbol, "limit", side, qty, float(limit_price_str),
                params={"timeInForce": "GTC"}
            )
            order_id = order["id"]

            # ── Esperar llenado ─────────────────────────────────────────────
            # Bug fix: sleep DESPUÉS del fetch (no antes), para no saltear el
            # llenado que ocurre justo al final del deadline.
            filled_qty  = 0.0
            fill_price  = limit_price   # se sobreescribe con el precio real al llenar
            fetch_error = False
            deadline    = time.time() + LIMIT_TIMEOUT_SEG
            check_every = 2             # segundos entre consultas (era 3, ahora más rápido)

            while True:
                try:
                    o = self.exchange.fetch_order(order_id, self.symbol)
                    status     = o.get("status", "")
                    filled_qty = float(o.get("filled", 0) or 0)

                    if status == "closed" or filled_qty >= float(qty) * 0.95:
                        # Bug fix: usar average price, nunca quedarse con limit_price
                        avg = o.get("average") or o.get("price")
                        fill_price = float(avg) if avg else limit_price
                        break

                    if status in ("canceled", "expired", "rejected"):
                        self.log.warning(f"⚠️ Orden limit {status}. Señal descartada.")
                        return False

                except Exception as _fe:
                    # No romper el loop en el primer error de red — intentar de nuevo
                    self.log.warning(f"fetch_order error (reintentando): {_fe}")
                    fetch_error = True

                if time.time() >= deadline:
                    break
                time.sleep(check_every)

            # Si no se llenó completamente, cancelar
            if filled_qty < float(qty) * 0.95:
                try:
                    self.exchange.cancel_order(order_id, self.symbol)
                except Exception:
                    pass
                self.log.warning(
                    f"⏱️ Timeout limit ({LIMIT_TIMEOUT_SEG}s) — "
                    f"llenado {filled_qty:.4f}/{qty}. Señal descartada."
                )
                return False

            # Calcular SL y TP desde el fill price
            # FIX: usar `is not None and > 0` en vez de `if tp_pct` para no
            # descartar tp_pct válidos que sean float cercano a 0 por redondeo.
            if side == "buy":
                sl_precio = fill_price * (1 - sl_pct / 100)
                tp_precio = fill_price * (1 + tp_pct / 100) if (tp_pct is not None and tp_pct > 0) else None
            else:
                sl_precio = fill_price * (1 + sl_pct / 100)
                tp_precio = fill_price * (1 - tp_pct / 100) if (tp_pct is not None and tp_pct > 0) else None

            # Timestamp de timeout: anclado al cierre de la vela de señal
            # FIX 2 (v5): antes usaba datetime.now() + 150min desde el llenado,
            # generando desfase vs backtest (que cuenta desde cierre de vela).
            from datetime import timezone as _tz
            try:
                df_15m_cache = self._df_cache.get("15m")
                if df_15m_cache is not None and len(df_15m_cache) >= 1:
                    last_open  = df_15m_cache.index[-1]
                    last_close = pd.Timestamp(last_open) + pd.Timedelta(minutes=15)
                    ts_timeout = last_close + pd.Timedelta(minutes=getattr(self, "timeout_velas", 10) * 15)
                    if ts_timeout.tzinfo is None:
                        ts_timeout = ts_timeout.tz_localize(_tz.utc)
                else:
                    ts_timeout = datetime.now(_tz.utc) + pd.Timedelta(minutes=getattr(self, "timeout_velas", 10) * 15)
            except Exception:
                ts_timeout = datetime.now(_tz.utc) + pd.Timedelta(minutes=getattr(self, "timeout_velas", 10) * 15)

            self.posicion_actual = {
                "qty"          : filled_qty,
                "side"         : side,
                "entry_price"  : fill_price,
                "sl_precio"    : sl_precio,
                "sl_pct"       : sl_pct,
                "tp_precio"    : tp_precio,
                "tp_pct"       : tp_pct,
                "scenario"     : scenario,
                "max_alcanzado": fill_price,
                "be_activated" : False,
                "entry_balance": self.cache_balance,
                "ts_timeout"   : ts_timeout,
            }

            ok = self.set_protections(side, filled_qty, sl_precio, tp_precio)
            if not ok:
                self.log.critical("🚨 FALLO SL/TP post-entrada → cerrando posición")
                try:
                    opp = "sell" if side == "buy" else "buy"
                    self.exchange.create_order(self.symbol, "market", opp, filled_qty,
                                               params={"reduceOnly": True})
                except Exception as e2:
                    self.log.error(f"Error cierre emergencia: {e2}")
                self.posicion_actual = None
                self.entry_lock      = False
                return False

            self.lock_time = datetime.now(timezone.utc)
            self.log.success(
                f"✅ LIMIT llenada {'LONG' if side=='buy' else 'SHORT'} | "
                f"fill={fill_price:.4f} | qty={filled_qty} | "
                f"SL={sl_precio:.4f} | TP={tp_precio:.4f} | timeout={ts_timeout.strftime('%H:%M')} UTC | {scenario or ''}"
            )
            self.log_trade_export("ENTRY", side, filled_qty, fill_price, scenario=scenario)
            self.save_internal_state()
            notificar_apertura(side=side, symbol=self.symbol, entry_price=fill_price,
                                tp_pct=tp_pct, sl_pct=sl_pct, leverage=self.leverage,
                                timeout_velas=getattr(self, "timeout_velas", 10),
                                scenario=scenario, log=self.log)
            return True

        except Exception as e:
            self.entry_lock = False
            if not self._handle_rate_limit_error(e):
                self.log.error(f"Error execute_entry: {e}")
            return False

    # ──────────────────────────────────────────────────────────────
    # MANEJO CENTRALIZADO DE CIERRE DE POSICIÓN
    # ──────────────────────────────────────────────────────────────

    def _handle_position_closed(self, source="monitor"):
        """
        Punto único de manejo de cierre de posición.
        Calcula PnL, exporta trade, notifica Instagram, ejecuta circuit breaker
        y limpia el estado. Llamado desde monitor_position y audit_persistence.
        """
        if not self.posicion_actual:
            return

        pos   = self.posicion_actual
        side  = pos["side"]
        entry = float(pos["entry_price"])

        try:
            self._cancel_all_orders(self.get_clean_symbol())
        except Exception:
            pass

        # Forzar sync inmediato del balance real
        self.sync_balance(force=True)

        entry_balance = float(pos.get("entry_balance", 0.0))
        qty = float(pos["qty"])
        costos = entry * qty * 0.0009   # aprox 2 lados

        # Calcular PnL usando el balance real si tenemos entry_balance válido
        if entry_balance > 0:
            pnl_neto = self.cache_balance - entry_balance
            pnl_bruto = pnl_neto + costos

            # Deducir el precio de salida basado en el PnL real
            if side == "buy":
                precio_salida = entry + (pnl_bruto / qty)
            else:
                precio_salida = entry - (pnl_bruto / qty)
        else:
            # Fallback (no debería ocurrir, pero por seguridad)
            precio_salida = entry
            try:
                symbol_clean = self.get_clean_symbol()
                pos_info = self.client.futures_position_information(symbol=symbol_clean)
                mark = float(next((p["markPrice"] for p in pos_info), 0))
                if mark > 0:
                    precio_salida = mark
            except Exception:
                pass

            pnl_bruto = (
                (precio_salida - entry) * qty if side == "buy"
                else (entry - precio_salida) * qty
            )
            pnl_neto = pnl_bruto - costos

        self.daily_pnl += pnl_neto

        if pnl_neto > 0:
            self.loss_streak = 0
        else:
            self.loss_streak += 1

        self.log.success(
            f"📉 Posición cerrada ({source}) | PnL: ${pnl_neto:.4f} | racha SL: {self.loss_streak}"
        )
        self.log_trade_export(
            "EXIT", side, pos["qty"], entry, precio_salida, pnl_neto,
            scenario=pos.get("scenario")
        )
        notificar_cierre(
            side=side, symbol=self.symbol, entry_price=entry,
            exit_price=precio_salida, pnl_usd=pnl_neto,
            scenario=pos.get("scenario"), log=self.log
        )
        self._manage_circuit_breaker()
        self.posicion_actual = None
        self.entry_lock      = False
        self.save_internal_state()

    # ──────────────────────────────────────────────────────────────
    # MONITOR DE POSICIÓN ABIERTA
    # ──────────────────────────────────────────────────────────────

    def monitor_position(self, r15):
        """
        Monitorea la posición usando datos 15m.
        El cierre lo maneja el SL/TP en el exchange.
        Detecta cierre externo y actualiza el estado.
        """
        try:
            if not self.posicion_actual:
                return

            pos   = self.posicion_actual
            side  = pos["side"]
            entry = float(pos["entry_price"])

            # MFE tracking
            if side == "buy":
                if float(r15["current"]["high"]) > pos["max_alcanzado"]:
                    pos["max_alcanzado"] = float(r15["current"]["high"])
            else:
                if float(r15["current"]["low"]) < pos["max_alcanzado"]:
                    pos["max_alcanzado"] = float(r15["current"]["low"])

            # 1. Verificar si la posición sigue abierta en el exchange
            # Implementamos throttle para evitar saturar la API
            now = time.time()
            if now - self.last_position_check < self.position_check_interval:
                return  # No consultamos todavía, retenemos el estado conocido
            
            self.last_position_check = now
            symbol_clean = self.get_clean_symbol()
            pos_info     = self.client.futures_position_information(symbol=symbol_clean)
            active_pos   = next((p for p in pos_info if float(p["positionAmt"]) != 0), None)

            if not active_pos:
                self._handle_position_closed(source="monitor")
                return

            # 2. Cierre por timeout si sigue abierta
            from datetime import timezone as _tz
            ts_timeout = pos.get("ts_timeout")
            if ts_timeout is not None:
                ahora = datetime.now(_tz.utc)
                if isinstance(ts_timeout, str):
                    ts_timeout = pd.Timestamp(ts_timeout).to_pydatetime()
                    if ts_timeout.tzinfo is None:
                        ts_timeout = ts_timeout.replace(tzinfo=_tz.utc)
                if ahora >= ts_timeout:
                    tv = getattr(self, "timeout_velas", 10)
                    self.log.info(f"⏱️ Timeout {tv} velas alcanzado — cerrando posición")
                    try:
                        opp = "sell" if side == "buy" else "buy"
                        self.exchange.create_order(
                            self.symbol, "market", opp, pos["qty"],
                            params={"reduceOnly": True}
                        )
                        try:
                            self._cancel_all_orders(self.get_clean_symbol())
                        except Exception:
                            pass
                    except Exception as e:
                        self.log.error(f"Error cierre timeout: {e}")
                    return

        except Exception as e:
            if not self._handle_rate_limit_error(e):
                self.log.error(f"Error monitor_position: {e}")

    # ──────────────────────────────────────────────────────────────
    # LOOP PRINCIPAL
    # ──────────────────────────────────────────────────────────────

    def run(self):
        self.is_running = True

        self.load_internal_state()

        if not self.load_behavior_data():
            self.log.error(
                f"❌ No se pudo cargar la estrategia v4. "
                f"Verifica que existan JSON en '{self.models_dir}/' "
                f"con prefijo '{self.model_prefix}' y versión v4/v3."
            )
            return

        if not self.resync_session():
            self.log.error("❌ Fallo crítico en sincronización.")
            return

        while self.is_running:
            try:
                # ── Circuit breaker / cooldown ─────────────────────
                if self.is_paused and self.lock_time:
                    ahora = datetime.now(timezone.utc)
                    horas = (ahora - self.lock_time).total_seconds() / 3600
                    if horas < 12:
                        if int(time.time()) % 60 == 0:
                            self.log.warning(f"⏳ Cooldown activo. Faltan {12 - horas:.1f} hs.")
                        time.sleep(10)
                        continue
                    self.log.success("🔓 Cooldown completado. Reanudando.")
                    self.is_paused = False
                    self.lock_time = None
                    self.save_internal_state()
                elif self.is_paused:
                    time.sleep(30)
                    continue

                self.audit_persistence()
                self.sync_balance()      # sincroniza balance real cada 60s

                # ── Sincronizar parámetros desde UI ───────────────
                if self.ui_ref:
                    try:
                        new_lev = int(self.ui_ref.ent_lev.get())
                        mar_val = float(self.ui_ref.ent_mar.get().replace(",", "."))
                        self.margen_usado = mar_val / 100 if mar_val > 1 else mar_val
                        self.max_pos_size = float(self.ui_ref.ent_max.get().replace(",", "."))
                        self.factor_tp    = float(self.ui_ref.ent_tp.get().replace(",", "."))
                        self.factor_sl    = float(self.ui_ref.ent_sl.get().replace(",", "."))
                        self.timeout_velas = int(float(self.ui_ref.ent_timeout.get())) if hasattr(self.ui_ref, "ent_timeout") else getattr(self, "timeout_velas", 10)

                        if new_lev != self.leverage:
                            self.leverage = new_lev
                            try:
                                self.client.futures_change_leverage(
                                    symbol=self.get_clean_symbol(), leverage=self.leverage
                                )
                                self.log.success(f"🔄 Leverage → {self.leverage}x")
                            except Exception as e:
                                if "-4161" not in str(e):
                                    self.log.warning(f"Leverage: {e}")
                    except Exception:
                        pass

                # ── Si IP está baneada, saltar todo polling REST ────────
                if self._is_banned():
                    ban_secs = max(0, (self._api_ban_until_ms / 1000) - time.time())
                    wait = min(ban_secs + 1, 120)
                    time.sleep(wait)
                    continue

                # ── Datos de mercado (Polling optimizado) ───────────────
                try:
                    # Polling ligero para detectar nueva vela y precio actual (peso 1)
                    bars_2 = self.exchange.fetch_ohlcv(self.symbol, timeframe="15m", limit=2)
                    if not bars_2 or len(bars_2) < 2:
                        time.sleep(5)
                        continue
                    # Reset backoff al primer request exitoso
                    self._poll_backoff = 5
                except Exception as e:
                    # Detectar ban -1003 también en ccxt (envuelve los códigos)
                    if self._handle_rate_limit_error(e):
                        ban_secs = max(0, (self._api_ban_until_ms / 1000) - time.time())
                        wait = min(ban_secs + 1, 120)
                        time.sleep(wait)
                        continue
                    err_str = str(e)
                    if "429" in err_str or "Too Many Requests" in err_str:
                        # Backoff exponencial: 30s → 60s → 120s → 120s (cap)
                        backoff = getattr(self, "_poll_backoff", 5)
                        backoff = min(backoff * 2, 120)
                        self._poll_backoff = backoff
                        self.log.error(f"Error fetching 15m ticker: {e}")
                        self.log.warning(f"⏳ Rate limit 429 — esperando {backoff}s antes de reintentar.")
                        time.sleep(backoff)
                    else:
                        self.log.error(f"Error fetching 15m ticker: {e}")
                        time.sleep(5)
                    continue

                last_closed_ts = int(bars_2[-2][0])
                current_price  = float(bars_2[-1][4])
                current_high   = float(bars_2[-1][2])
                current_low    = float(bars_2[-1][3])

                # ── Detección de nueva vela 15m ────────────────────
                nueva_vela_15m = False
                if self.last_5m_timestamp is None:
                    self.last_5m_timestamp = last_closed_ts - 1
                elif last_closed_ts > self.last_5m_timestamp:
                    nueva_vela_15m = True
                    self.last_5m_timestamp = last_closed_ts

                if not hasattr(self, "cached_market_data") or nueva_vela_15m:
                    market_data = self.fetch_market_state() # Peso 7
                    if market_data.get("15m") is None or market_data.get("1h") is None or market_data.get("4h") is None:
                        self.log.warning("⏳ Esperando datos completos (15m/1H/4H)...")
                        # NO resetear last_5m_timestamp: evita forzar fetch_market_state()
                        # (7 requests) en cada iteración del loop cuando hay rate limit.
                        # La próxima vela real disparará el refresh automáticamente.
                        time.sleep(10)
                        continue
                    self.cached_market_data = market_data
                else:
                    market_data = self.cached_market_data

                r15 = market_data["15m"].copy()
                r1h = market_data["1h"]
                r4h = market_data["4h"]

                # Inyectar datos en tiempo real para UI y MFE
                r15["current"] = r15["current"].copy()
                r15["current"]["close"] = current_price
                r15["current"]["high"]  = current_high
                r15["current"]["low"]   = current_low

                # ── Actualización de estado en UI ──────────────────
                current_price = float(r15["current"]["close"])
                adx_v         = float(r15["current"].get("ADX",       0) or 0)
                trend_ok      = bool(r15["current"]["close"] > (r15["current"].get("EMA_55", 0) or 0))
                momentum_ok   = bool((r15["current"].get("SQZ_DIR",   0) or 0) > 0)
                adx_ok        = adx_v > 20

                dist_ema = 0.0
                ema55_v  = float(r15["current"].get("EMA_55", 0) or 0)
                if ema55_v > 0:
                    dist_ema = (current_price - ema55_v) / ema55_v * 100

                self.log.update_state({
                    "price"      : current_price,
                    "balance"    : self.cache_balance,
                    "pnl"        : self.daily_pnl,
                    "trend_ok"   : trend_ok,
                    "adx_ok"     : adx_ok,
                    "adx_val"    : round(adx_v, 2),
                    "momentum_ok": momentum_ok,
                    "dist_ema_1h": round(dist_ema, 2),
                    "dist_ok"    : abs(dist_ema) > 0.3,
                })

                # ── Lógica principal ───────────────────────────────
                if self.posicion_actual:
                    self.monitor_position(r15)

                elif nueva_vela_15m and not self.entry_lock:
                    signal, tp_pct, sl_pct, escenario = self._behavior_signal()

                    if signal:
                        self.entry_lock = True
                        self.log.success(
                            f"📡 Señal {'LONG' if signal=='buy' else 'SHORT'} "
                            f"| {escenario} | iniciando orden limit..."
                        )
                        ok = self.execute_entry(
                            signal,
                            current_price,   # referencia; el método usa order book real
                            tp_pct or 0.8,
                            sl_pct or 0.4,
                            scenario=escenario,
                        )
                        if not ok:
                            self.entry_lock = False

                # El gatillo es 15m — polling cada 5s es suficiente
                # Si el IP está baneado, esperar hasta que el ban expire
                if self._is_banned():
                    ban_secs = max(0, (self._api_ban_until_ms / 1000) - time.time())
                    wait = min(ban_secs + 1, 120)  # cap 120s por seguridad
                    time.sleep(wait)
                else:
                    time.sleep(5)

            except Exception as e:
                if self._handle_rate_limit_error(e):
                    ban_secs = max(0, (self._api_ban_until_ms / 1000) - time.time())
                    wait = min(ban_secs + 1, 120)
                    time.sleep(wait)
                else:
                    self.log.error(f"Error ciclo principal: {e}")
                    time.sleep(10)

    def stop(self):
        self.is_running = False
