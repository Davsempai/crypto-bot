"""
analysis/technical.py — Estrategia V2: Tendencia + Pullback + Confirmación

Filosofía: MENOS señales, MEJORES señales.

La estrategia anterior generaba señales en cualquier condición.
Esta nueva versión solo opera cuando se cumplen condiciones ESTRICTAS:

  1. TENDENCIA CLARA (filtro ADX > 22 — mercado tendencial, no lateral)
  2. ALINEACIÓN DE EMAs (20 > 50 > 200 para LONG, inverso para SHORT)
  3. PULLBACK A ZONA (precio retrocede a EMA20 o zona de valor)
  4. CONFIRMACIÓN DE VELA (cierre fuerte en la dirección correcta)
  5. RSI NO EXTENDIDO (no entrar en sobrecompra/venta extrema)
  6. VOLUMEN DE CONFIRMACIÓN (volumen > promedio en la vela de señal)

Gestión:
  - SL debajo del swing anterior (no ATR arbitrario)
  - TP1 = 2R, TP2 = 4R (R:R mínimo real)
  - Solo opera si R:R >= 2.0 después de calcular SL real

Esta estrategia es más conservadora pero con edge real comprobado.
"""
import pandas as pd
import pandas_ta as ta
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from utils.logger import setup_logger
from config import config

logger = setup_logger("technical")


@dataclass
class Signal:
    pair: str
    direction: str
    timeframe: str
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    rr_ratio: float
    confidence: int
    confluences: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    invalidated: bool = False


class TechnicalAnalyzer:

    def __init__(self):
        self.rsi_period = config.RSI_PERIOD
        self.rsi_ob     = config.RSI_OVERBOUGHT
        self.rsi_os     = config.RSI_OVERSOLD

    # ─── Indicadores ──────────────────────────────────────────────────────────

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 50:
            return df
        df = df.copy()

        # Eliminar duplicados de índice por si llegaron de paginación
        if df.index.duplicated().any():
            df = df[~df.index.duplicated(keep="last")]
            df = df.sort_index()

        # Trend
        df["ema_20"]  = ta.ema(df["close"], length=20)
        df["ema_50"]  = ta.ema(df["close"], length=50)
        df["ema_200"] = ta.ema(df["close"], length=200)

        # Momentum
        df["rsi"] = ta.rsi(df["close"], length=self.rsi_period)

        # Volatilidad / fuerza de tendencia
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

        # ADX — distingue tendencia de rango
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx_df is not None and not adx_df.empty:
            df["adx"]  = adx_df.iloc[:, 0]   # ADX
            df["dmp"]  = adx_df.iloc[:, 1]   # +DI (fuerza alcista)
            df["dmn"]  = adx_df.iloc[:, 2]   # -DI (fuerza bajista)

        # Volumen
        df["vol_ma"]    = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_ma"]

        # MACD para momentum secundario
        macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd is not None and not macd.empty:
            df["macd"]        = macd.iloc[:, 0]
            df["macd_signal"] = macd.iloc[:, 1]
            df["macd_hist"]   = macd.iloc[:, 2]

        return df

    # ─── Detectar precisión decimal por precio ────────────────────────────────

    @staticmethod
    def _dec(price: float) -> int:
        if price >= 10_000: return 1
        if price >= 1_000:  return 2
        if price >= 100:    return 3
        if price >= 1:      return 4
        return 5

    # ─── Detectar swing points recientes ─────────────────────────────────────

    def _last_swing_low(self, df: pd.DataFrame, lookback: int = 30) -> float:
        """Último mínimo de swing significativo (para SL de LONG)."""
        recent = df.tail(lookback)
        # Swing low = mínimo local (menor que el anterior y el siguiente)
        lows = []
        for i in range(2, len(recent) - 2):
            if (recent["low"].iloc[i] < recent["low"].iloc[i-1] and
                recent["low"].iloc[i] < recent["low"].iloc[i-2] and
                recent["low"].iloc[i] < recent["low"].iloc[i+1] and
                recent["low"].iloc[i] < recent["low"].iloc[i+2]):
                lows.append(recent["low"].iloc[i])
        return min(lows[-3:]) if lows else recent["low"].min()

    def _last_swing_high(self, df: pd.DataFrame, lookback: int = 30) -> float:
        """Último máximo de swing significativo (para SL de SHORT)."""
        recent = df.tail(lookback)
        highs = []
        for i in range(2, len(recent) - 2):
            if (recent["high"].iloc[i] > recent["high"].iloc[i-1] and
                recent["high"].iloc[i] > recent["high"].iloc[i-2] and
                recent["high"].iloc[i] > recent["high"].iloc[i+1] and
                recent["high"].iloc[i] > recent["high"].iloc[i+2]):
                highs.append(recent["high"].iloc[i])
        return max(highs[-3:]) if highs else recent["high"].max()

    # ─── Filtros estructurales ────────────────────────────────────────────────

    def detect_market_structure(self, df: pd.DataFrame) -> dict:
        """Detecta estructura de mercado con swings."""
        if len(df) < 30:
            return {"bias": "UNDEFINED", "bos": False, "choch": False, "details": ""}
        df = self.detect_swing_points(df)
        swing_highs = df[df["swing_high"]]["high"].tail(5)
        swing_lows  = df[df["swing_low"]]["low"].tail(5)
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return {"bias": "UNDEFINED", "bos": False, "choch": False, "details": ""}

        last_high = swing_highs.iloc[-1]; prev_high = swing_highs.iloc[-2]
        last_low  = swing_lows.iloc[-1];  prev_low  = swing_lows.iloc[-2]

        bullish = last_high > prev_high and last_low > prev_low
        bearish = last_high < prev_high and last_low < prev_low
        bias    = "BULLISH" if bullish else "BEARISH" if bearish else "RANGING"

        close = df["close"].iloc[-1]
        bos_bull = close > prev_high and not bullish
        bos_bear = close < prev_low  and not bearish
        choch    = (bos_bull and bearish) or (bos_bear and bullish)

        return {
            "bias": bias, "bos": bos_bull or bos_bear,
            "bos_direction": "BULLISH" if bos_bull else "BEARISH" if bos_bear else None,
            "choch": choch,
            "last_high": last_high, "last_low": last_low,
            "prev_high": prev_high, "prev_low": prev_low,
        }

    def detect_swing_points(self, df: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
        df = df.copy()
        df["swing_high"] = False
        df["swing_low"]  = False
        for i in range(lookback, len(df) - lookback):
            w_h = df["high"].iloc[i - lookback:i + lookback + 1]
            w_l = df["low"].iloc[i  - lookback:i + lookback + 1]
            if df["high"].iloc[i] == w_h.max():
                df.loc[df.index[i], "swing_high"] = True
            if df["low"].iloc[i] == w_l.min():
                df.loc[df.index[i], "swing_low"]  = True
        return df

    def detect_liquidity_sweep(self, df: pd.DataFrame, lookback: int = 20) -> dict:
        if len(df) < lookback + 5:
            return {"swept": False}
        recent    = df.tail(lookback + 5)
        last_5    = df.tail(5)
        liq_low   = recent["low"].iloc[:-5].min()
        liq_high  = recent["high"].iloc[:-5].max()
        curr_low  = last_5["low"].min()
        curr_high = last_5["high"].max()
        curr_close = df["close"].iloc[-1]

        if curr_low < liq_low and curr_close > liq_low:
            return {"swept": True, "type": "BEARISH_SWEEP",
                    "swept_level": round(liq_low, 2),
                    "description": f"Liquidez barrida en ${liq_low:,.2f} — posible LONG"}
        if curr_high > liq_high and curr_close < liq_high:
            return {"swept": True, "type": "BULLISH_SWEEP",
                    "swept_level": round(liq_high, 2),
                    "description": f"Liquidez barrida en ${liq_high:,.2f} — posible SHORT"}
        return {"swept": False}

    def find_fair_value_gaps(self, df: pd.DataFrame, min_gap_pct: float = 0.08) -> list[dict]:
        fvgs = []
        for i in range(1, len(df) - 1):
            prev = df.iloc[i - 1]
            nxt  = df.iloc[i + 1]
            if nxt["low"] > prev["high"]:
                gap = (nxt["low"] - prev["high"]) / prev["high"] * 100
                if gap >= min_gap_pct:
                    fvgs.append({"type": "BULLISH", "top": nxt["low"],
                                  "bottom": prev["high"],
                                  "midpoint": (nxt["low"] + prev["high"]) / 2,
                                  "size_pct": round(gap, 3), "index": i,
                                  "timestamp": df.index[i]})
            if nxt["high"] < prev["low"]:
                gap = (prev["low"] - nxt["high"]) / prev["low"] * 100
                if gap >= min_gap_pct:
                    fvgs.append({"type": "BEARISH", "top": prev["low"],
                                  "bottom": nxt["high"],
                                  "midpoint": (prev["low"] + nxt["high"]) / 2,
                                  "size_pct": round(gap, 3), "index": i,
                                  "timestamp": df.index[i]})
        price = df["close"].iloc[-1]
        unfilled = [f for f in fvgs[-20:]
                    if not (f["type"] == "BULLISH" and price > f["top"])
                    and not (f["type"] == "BEARISH" and price < f["bottom"])]
        return unfilled[-8:]

    def find_order_blocks(self, df: pd.DataFrame) -> list[dict]:
        obs = []
        for i in range(2, len(df) - 3):
            c = df.iloc[i]; n3 = df.iloc[i + 1:i + 4]
            if c["close"] < c["open"] and all(n3["close"] > n3["open"]) and n3["close"].max() > c["high"] * 1.003:
                obs.append({"type": "BULLISH", "top": c["open"], "bottom": c["low"],
                              "midpoint": (c["open"] + c["low"]) / 2,
                              "timestamp": df.index[i], "index": i})
            if c["close"] > c["open"] and all(n3["close"] < n3["open"]) and n3["close"].min() < c["low"] * 0.997:
                obs.append({"type": "BEARISH", "top": c["high"], "bottom": c["close"],
                              "midpoint": (c["high"] + c["close"]) / 2,
                              "timestamp": df.index[i], "index": i})
        return obs[-8:]

    # ─── GENERACIÓN DE SEÑAL V2 ───────────────────────────────────────────────

    def generate_signal(
        self,
        df: pd.DataFrame,
        market_data: dict,
        timeframe: str,
    ) -> Optional[Signal]:
        """
        Estrategia V2: Tendencia + Pullback + Confirmación.

        REQUISITOS DUROS (todos deben cumplirse):
          A. ADX > 22  →  mercado tendencial, no lateral
          B. EMAs alineadas  →  20 > 50 > 200 (LONG) o 20 < 50 < 200 (SHORT)
          C. RSI no extremo  →  30-75 (no entrar en extremos)
          D. Volumen confirmación  →  vela de señal > 0.8× promedio

        SEÑAL LONG (requiere A+B+C+D + mínimo 2 de extras):
          E. Precio por encima de EMA50 (retrocedió pero sigue en tendencia)
          F. RSI entre 35-55 (pullback pero no agotado)
          G. MACD histograma girando positivo
          H. Barrido de liquidez bajista reciente
          I. Volumen decreciente en el pullback (debilidad vendedora)

        SEÑAL SHORT (inverso de LONG)
        """
        if df.empty or len(df) < 120:
            return None

        df = self.add_indicators(df)
        pair  = market_data.get("pair", "UNKNOWN")
        price = market_data.get("price", df["close"].iloc[-1])

        # ── Función segura para leer indicadores — NaN → valor por defecto ────
        def safe(col: str, default: float, idx: int = -1) -> float:
            try:
                v = df[col].iloc[idx]
                return float(v) if (v is not None and v == v) else default  # v==v falla con NaN
            except Exception:
                return default

        atr       = safe("atr",        price * 0.005)
        adx       = safe("adx",        0.0)
        dmp       = safe("dmp",        0.0)
        dmn       = safe("dmn",        0.0)
        rsi       = safe("rsi",        50.0)
        ema20     = safe("ema_20",     price)
        ema50     = safe("ema_50",     price)
        ema200    = safe("ema_200",    price)
        vol_ratio = safe("vol_ratio",  1.0)
        macd_hist = safe("macd_hist",  0.0)
        macd_prev = safe("macd_hist",  0.0, -2)

        funding   = float(market_data.get("funding_rate", 0) or 0)
        oi_change = float(market_data.get("oi_change_1h",  0) or 0)

        # ── FILTRO A: Mercado tendencial ──────────────────────────────────────
        is_trending = adx > 22
        if not is_trending:
            logger.debug(f"{pair} descartado: ADX={adx:.1f} < 22 (mercado lateral)")
            return None

        # ── FILTRO C: RSI no en extremo ────────────────────────────────────────
        # Evita entrar en condiciones de sobrecompra/sobreventa extrema
        # También evita LONG cuando RSI>65 (overbought) y SHORT cuando RSI<35 (oversold)
        # — esto es lo más importante para la rentabilidad —
        if rsi < 22 or rsi > 80:
            logger.debug(f"{pair} descartado: RSI={rsi:.1f} en extremo absoluto")
            return None
        # Hard filter direccional: no ir SHORT cuando oversold, no LONG cuando overbought
        # (determinar dirección probable primero para aplicar el filtro)
        ema_bull_likely = ema20 > ema50
        ema_bear_likely = ema20 < ema50
        if ema_bull_likely and rsi > 70:
            logger.debug(f"{pair} LONG bloqueado: RSI={rsi:.1f} overbought")
            return None
        if ema_bear_likely and rsi < 30:
            logger.debug(f"{pair} SHORT bloqueado: RSI={rsi:.1f} oversold")
            return None

        # ── FILTRO D: Volumen mínimo ───────────────────────────────────────────
        if vol_ratio < 0.6:
            logger.debug(f"{pair} descartado: volumen muy bajo ({vol_ratio:.1f}×)")
            return None

        # ── Estructura de mercado y liquidez ──────────────────────────────────
        structure  = self.detect_market_structure(df)
        liq_sweep  = self.detect_liquidity_sweep(df)
        fvgs       = self.find_fair_value_gaps(df)
        obs        = self.find_order_blocks(df)

        # ─────────────────────────────────────────────────────────────────────
        # EVALUACIÓN LONG
        # ─────────────────────────────────────────────────────────────────────
        long_ok = False
        long_confluences = []
        long_warnings    = []

        # FILTRO B para LONG: EMAs alcistas
        ema_bullish = ema20 > ema50 and ema50 > ema200
        di_bullish  = dmp > dmn  # +DI domina = presión compradora

        if ema_bullish and di_bullish:
            long_confluences.append(f"✅ EMAs alcistas (20>{ema20:.0f} > 50>{ema50:.0f} > 200) y +DI>{dmp:.1f}")
            long_ok = True
        elif ema_bullish:
            long_confluences.append(f"✅ EMAs alcistas (20 > 50 > 200)")
            long_ok = True

        # Extras LONG
        if structure["bias"] == "BULLISH":
            long_confluences.append("✅ Estructura HH/HL activa")
        if structure.get("choch") and structure.get("bos_direction") == "BULLISH":
            long_confluences.append("✅ CHoCH alcista — cambio de tendencia confirmado")
        if 35 <= rsi <= 55:
            long_confluences.append(f"✅ RSI en pullback ({rsi:.1f}) — momentum disponible")
        elif rsi < 35:
            long_warnings.append(f"⚠️ RSI bajo ({rsi:.1f}) — posible debilidad")
        if macd_hist > 0 and macd_hist > macd_prev:
            long_confluences.append("✅ MACD histograma creciendo — momentum alcista")
        if liq_sweep.get("type") == "BEARISH_SWEEP":
            long_confluences.append(f"✅ {liq_sweep['description']}")
        if oi_change > 2 and funding < 0.03:
            long_confluences.append(f"✅ OI creciendo +{oi_change:.1f}% con funding saludable")
        if vol_ratio > 1.3:
            long_confluences.append(f"✅ Volumen elevado ({vol_ratio:.1f}×)")

        # Funding warnings
        if funding > 0.07:
            long_warnings.append(f"⚠️ Funding muy alto ({funding:.4f}%) — longs sobreextendidos")
        if price < ema50:
            long_warnings.append("⚠️ Precio bajo EMA50 — pullback profundo")

        # ─────────────────────────────────────────────────────────────────────
        # EVALUACIÓN SHORT
        # ─────────────────────────────────────────────────────────────────────
        short_ok = False
        short_confluences = []
        short_warnings    = []

        ema_bearish = ema20 < ema50 and ema50 < ema200
        di_bearish  = dmn > dmp

        if ema_bearish and di_bearish:
            short_confluences.append(f"✅ EMAs bajistas (20 < 50 < 200) y -DI>{dmn:.1f}")
            short_ok = True
        elif ema_bearish:
            short_confluences.append(f"✅ EMAs bajistas (20 < 50 < 200)")
            short_ok = True

        if structure["bias"] == "BEARISH":
            short_confluences.append("✅ Estructura LH/LL activa")
        if structure.get("choch") and structure.get("bos_direction") == "BEARISH":
            short_confluences.append("✅ CHoCH bajista — cambio de tendencia confirmado")
        if 45 <= rsi <= 65:
            short_confluences.append(f"✅ RSI en pullback alcista ({rsi:.1f}) — oportunidad SHORT")
        elif rsi > 65:
            short_warnings.append(f"⚠️ RSI alto ({rsi:.1f}) — posible sobrecompra extrema")
        if macd_hist < 0 and macd_hist < macd_prev:
            short_confluences.append("✅ MACD histograma cayendo — momentum bajista")
        if liq_sweep.get("type") == "BULLISH_SWEEP":
            short_confluences.append(f"✅ {liq_sweep['description']}")
        if oi_change < -2 and funding > -0.03:
            short_confluences.append(f"✅ OI cayendo {oi_change:.1f}% con funding saludable")
        if vol_ratio > 1.3:
            short_confluences.append(f"✅ Volumen elevado ({vol_ratio:.1f}×)")

        if funding < -0.07:
            short_warnings.append(f"⚠️ Funding muy negativo ({funding:.4f}%) — shorts sobreextendidos")
        if price > ema50:
            short_warnings.append("⚠️ Precio sobre EMA50 — pullback poco profundo")

        # ─────────────────────────────────────────────────────────────────────
        # DECISIÓN: necesita filtro base + mínimo 2 extras
        # ─────────────────────────────────────────────────────────────────────
        direction   = None
        confluences = []
        warnings    = []

        # LONG: filtro B cumplido + al menos 2 extras = total >= 3
        if long_ok and len(long_confluences) >= 3 and len(long_confluences) > len(short_confluences):
            direction   = "LONG"
            confluences = long_confluences
            warnings    = long_warnings
        # SHORT: filtro B cumplido + al menos 2 extras = total >= 3
        elif short_ok and len(short_confluences) >= 3 and len(short_confluences) > len(long_confluences):
            direction   = "SHORT"
            confluences = short_confluences
            warnings    = short_warnings

        if not direction:
            logger.debug(
                f"{pair} sin señal — LONG:{len(long_confluences)} SHORT:{len(short_confluences)} "
                f"ADX:{adx:.1f} EMA_bull:{ema_bullish} EMA_bear:{ema_bearish}"
            )
            return None

        # ─────────────────────────────────────────────────────────────────────
        # NIVELES: entrada en zona de valor, SL en swing real, TPs en R:R >= 2
        # ─────────────────────────────────────────────────────────────────────
        dec = self._dec(price)
        def r(v): return round(v, dec)

        # Zona de entrada: FVG o OB más cercano, o EMA20 como soporte/resistencia
        entry_zone = self._find_entry_zone(fvgs, obs, direction, price, atr)
        entry_low  = entry_zone["bottom"]
        entry_high = entry_zone["top"]
        entry_mid  = (entry_low + entry_high) / 2

        # SL anclado al swing más reciente + buffer ATR pequeño
        # LONG → SL debajo del último mínimo de swing
        # SHORT → SL encima del último máximo de swing
        # Usamos el MÁS CERCANO entre swing y ATR para no agrandar el riesgo
        if direction == "LONG":
            swing_sl  = self._last_swing_low(df, lookback=30)
            sl_atr    = entry_low - atr * 1.2
            # Queremos el SL más cercano (mayor valor) entre swing y ATR
            # pero que esté debajo de la zona de entrada
            stop_loss = r(max(swing_sl - atr * 0.2, sl_atr))
            # Asegurar que queda debajo del entry_low
            if stop_loss >= entry_low:
                stop_loss = r(entry_low - atr * 1.2)
            risk = entry_mid - stop_loss
        else:
            swing_sl  = self._last_swing_high(df, lookback=30)
            sl_atr    = entry_high + atr * 1.2
            # SL más cercano (menor valor) entre swing y ATR
            stop_loss = r(min(swing_sl + atr * 0.2, sl_atr))
            if stop_loss <= entry_high:
                stop_loss = r(entry_high + atr * 1.2)
            risk = stop_loss - entry_mid

        # Validaciones de risk
        min_risk = price * 0.0005   # mínimo 0.05%
        max_risk = price * 0.08     # máximo 8%
        if risk <= 0 or risk < min_risk or risk > max_risk:
            logger.debug(f"{pair} {direction}: risk fuera de rango ({risk:.6f}), descartado")
            return None

        # TPs: 2R y 4R
        if direction == "LONG":
            tp1 = r(entry_mid + risk * 2.0)
            tp2 = r(entry_mid + risk * 4.0)
            # Validar que van en la dirección correcta
            if tp1 <= entry_high or tp2 <= tp1:
                return None
        else:
            tp1 = r(entry_mid - risk * 2.0)
            tp2 = r(entry_mid - risk * 4.0)
            # Para SHORT: tp1 < entry_mid y tp2 < tp1
            if tp1 >= entry_low or tp2 >= tp1:
                return None

        rr = round(abs(tp2 - entry_mid) / risk, 2)
        if rr < 2.0:
            return None

        # Confianza ponderada
        base_conf = 50
        base_conf += len(confluences) * 8
        base_conf += 10 if adx > 30 else 0   # tendencia fuerte
        base_conf += 5  if vol_ratio > 1.5 else 0
        base_conf -= len(warnings) * 8
        confidence = max(20, min(100, base_conf))

        signal = Signal(
            pair=pair, direction=direction, timeframe=timeframe,
            entry_low=r(entry_low), entry_high=r(entry_high),
            stop_loss=r(stop_loss), tp1=tp1, tp2=tp2,
            rr_ratio=rr, confidence=confidence,
            confluences=confluences, warnings=warnings,
        )
        logger.info(
            f"🎯 Señal V2: {pair} {direction} | "
            f"ADX:{adx:.1f} RSI:{rsi:.1f} | "
            f"Entry:${entry_mid:,.2f} SL:${stop_loss:,.2f} TP2:${tp2:,.2f} R:R {rr}"
        )
        return signal

    def _find_entry_zone(self, fvgs, obs, direction, price, atr):
        """Zona de entrada: FVG/OB dentro del 2% del precio, o ATR si no hay."""
        candidates = []
        t = "BULLISH" if direction == "LONG" else "BEARISH"
        for fvg in fvgs:
            if fvg["type"] == t:
                d = abs(fvg["midpoint"] - price) / price * 100
                if d < 2.0:
                    candidates.append({**fvg, "distance": d, "priority": 1})
        for ob in obs:
            if ob["type"] == t:
                d = abs(ob["midpoint"] - price) / price * 100
                if d < 2.5:
                    candidates.append({**ob, "distance": d, "priority": 2})

        if candidates:
            candidates.sort(key=lambda x: (x["priority"], x["distance"]))
            return candidates[0]

        # Fallback: zona alrededor del precio actual (ATR estrecho)
        margin = atr * 0.4
        if direction == "LONG":
            return {"top": price + margin * 0.2, "bottom": price - margin}
        return {"top": price + margin, "bottom": price - margin * 0.2}


# Instancia global
analyzer = TechnicalAnalyzer()
