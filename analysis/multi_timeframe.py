"""
analysis/multi_timeframe.py — Motor de confluencia multi-timeframe (MTF)

Lógica de cascada:
  4H  → Define el BIAS macro (tendencia principal)
  1H  → Confirma el SETUP (estructura + liquidez)
  15M → Busca la ENTRADA precisa (FVG / OB de corto plazo)
  5M  → Refinamiento opcional del entry trigger

Un trade solo se abre si los 3 primeros timeframes están alineados.
"""
import asyncio
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from analysis.technical import TechnicalAnalyzer, Signal
from typing import Any
from config import config
from utils.logger import setup_logger

logger = setup_logger("mtf")

analyzer = TechnicalAnalyzer()


@dataclass
class MTFAnalysis:
    """Resultado completo de análisis multi-timeframe."""
    pair: str
    bias_4h: str            # BULLISH / BEARISH / RANGING
    setup_1h: str           # VALID_LONG / VALID_SHORT / NONE
    entry_15m: str          # CONFIRMED / PENDING / NONE
    aligned: bool           # True si los 3 TF están alineados
    final_direction: str    # LONG / SHORT / NONE
    confluences: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Datos de cada TF
    rsi_4h: float = 0.0
    rsi_1h: float = 0.0
    rsi_15m: float = 0.0
    ema_trend_4h: str = ""   # ABOVE / BELOW (precio vs EMA200)
    structure_4h: dict = field(default_factory=dict)
    structure_1h: dict = field(default_factory=dict)
    fvg_15m: Optional[dict] = None
    ob_15m: Optional[dict] = None

    # Señal final
    signal: Optional[Signal] = None
    confidence_score: int = 0


class MultiTimeframeEngine:
    """
    Motor MTF que analiza 4H → 1H → 15M en cascada
    y genera señales de alta calidad.
    """

    # Pesos de cada timeframe en el score de confianza
    TF_WEIGHTS = {"4h": 0.40, "1h": 0.35, "15m": 0.25}

    def __init__(self, binance_client: Any):
        self.binance = binance_client

    async def full_analysis(self, pair: str, market_data: dict) -> MTFAnalysis:
        """
        Ejecuta el análisis MTF completo en cascada.
        Descarga los 3 timeframes en paralelo para eficiencia.
        """
        logger.info(f"🔍 MTF Analysis: {pair}")

        # Descargar datos en paralelo
        df_4h, df_1h, df_15m = await asyncio.gather(
            self.binance.get_klines(pair, "4h", 200),
            self.binance.get_klines(pair, "1h", 200),
            self.binance.get_klines(pair, "15m", 200),
        )

        # Agregar indicadores
        df_4h = analyzer.add_indicators(df_4h)
        df_1h = analyzer.add_indicators(df_1h)
        df_15m = analyzer.add_indicators(df_15m)

        result = MTFAnalysis(
            pair=pair,
            bias_4h="RANGING",
            setup_1h="NONE",
            entry_15m="NONE",
            aligned=False,
            final_direction="NONE",
        )

        # ──────────────────────────────────────────────
        # PASO 1: Analizar 4H → BIAS macro
        # ──────────────────────────────────────────────
        result = self._analyze_4h(result, df_4h)

        if result.bias_4h == "RANGING":
            logger.debug(f"{pair} — 4H ranging, sin bias claro")
            result.warnings.append("⚠️ 4H en rango — mayor riesgo de fakeout")

        # ──────────────────────────────────────────────
        # PASO 2: Analizar 1H → SETUP
        # ──────────────────────────────────────────────
        result = self._analyze_1h(result, df_1h)

        # ──────────────────────────────────────────────
        # PASO 3: Verificar alineación 4H + 1H
        # ──────────────────────────────────────────────
        result = self._check_alignment(result)

        if not result.aligned and result.bias_4h != "RANGING":
            logger.debug(f"{pair} — TF no alineados: 4H={result.bias_4h} 1H={result.setup_1h}")
            return result

        # ──────────────────────────────────────────────
        # PASO 4: Analizar 15M → ENTRY TRIGGER
        # ──────────────────────────────────────────────
        result = self._analyze_15m(result, df_15m)

        # ──────────────────────────────────────────────
        # PASO 5: Generar señal final si todo está alineado
        # ──────────────────────────────────────────────
        if result.aligned and result.entry_15m == "CONFIRMED":
            signal = self._build_final_signal(result, df_15m, market_data)
            if signal:
                result.signal = signal
                result.confidence_score = self._compute_confidence(result, market_data)
                logger.info(
                    f"✅ Señal MTF: {pair} {result.final_direction} | "
                    f"Confianza: {result.confidence_score}%"
                )

        return result

    # ─── Análisis por timeframe ────────────────────────────────────────────

    def _analyze_4h(self, result: MTFAnalysis, df: pd.DataFrame) -> MTFAnalysis:
        """Determina el bias macro en 4H."""
        if df.empty or len(df) < 50:
            return result

        structure = analyzer.detect_market_structure(df)
        result.structure_4h = structure
        result.bias_4h = structure["bias"]

        # Lectura segura contra NaN
        def safe(col, default):
            try:
                v = df[col].iloc[-1]
                return float(v) if (v is not None and v == v) else default
            except Exception:
                return default

        price  = float(df["close"].iloc[-1])
        rsi    = safe("rsi", 50.0)
        ema200 = safe("ema_200", price)

        result.rsi_4h       = round(rsi, 1)
        result.ema_trend_4h = "ABOVE" if price > ema200 else "BELOW"

        # Confluencias del 4H
        if result.bias_4h == "BULLISH":
            result.confluences.append("✅ [4H] Estructura alcista — HH/HL activo")
        elif result.bias_4h == "BEARISH":
            result.confluences.append("✅ [4H] Estructura bajista — LH/LL activo")

        if result.ema_trend_4h == "ABOVE" and result.bias_4h == "BULLISH":
            result.confluences.append(f"✅ [4H] Precio sobre EMA200 — tendencia alcista confirmada")
        elif result.ema_trend_4h == "BELOW" and result.bias_4h == "BEARISH":
            result.confluences.append(f"✅ [4H] Precio bajo EMA200 — tendencia bajista confirmada")

        if structure.get("choch"):
            direction = structure.get("bos_direction", "")
            result.confluences.append(f"✅ [4H] CHoCH {direction} — posible cambio de tendencia")

        return result

    def _analyze_1h(self, result: MTFAnalysis, df: pd.DataFrame) -> MTFAnalysis:
        """Busca el setup en 1H (estructura + liquidez)."""
        if df.empty or len(df) < 50:
            return result

        structure = analyzer.detect_market_structure(df)
        result.structure_1h = structure

        def safe(col, default):
            try:
                v = df[col].iloc[-1]
                return float(v) if (v is not None and v == v) else default
            except Exception:
                return default

        rsi = safe("rsi", 50.0)
        result.rsi_1h = round(rsi, 1)

        liq_sweep = analyzer.detect_liquidity_sweep(df)

        # Setup LONG válido en 1H
        long_valid = (
            structure["bias"] in ["BULLISH", "RANGING"]
            and (
                liq_sweep.get("type") == "BEARISH_SWEEP"
                or structure.get("choch") and structure.get("bos_direction") == "BULLISH"
                or (rsi < 45 and structure["bias"] == "BULLISH")
            )
        )

        # Setup SHORT válido en 1H
        short_valid = (
            structure["bias"] in ["BEARISH", "RANGING"]
            and (
                liq_sweep.get("type") == "BULLISH_SWEEP"
                or structure.get("choch") and structure.get("bos_direction") == "BEARISH"
                or (rsi > 55 and structure["bias"] == "BEARISH")
            )
        )

        if long_valid:
            result.setup_1h = "VALID_LONG"
            result.confluences.append(f"✅ [1H] Setup LONG — estructura + {'barrido' if liq_sweep.get('swept') else 'RSI'} confirmado")
        elif short_valid:
            result.setup_1h = "VALID_SHORT"
            result.confluences.append(f"✅ [1H] Setup SHORT — estructura + {'barrido' if liq_sweep.get('swept') else 'RSI'} confirmado")

        if liq_sweep.get("swept"):
            result.confluences.append(f"✅ [1H] {liq_sweep['description']}")

        return result

    def _check_alignment(self, result: MTFAnalysis) -> MTFAnalysis:
        """Verifica si 4H y 1H están alineados."""
        long_aligned = (
            result.bias_4h in ["BULLISH", "RANGING"]
            and result.setup_1h == "VALID_LONG"
        )
        short_aligned = (
            result.bias_4h in ["BEARISH", "RANGING"]
            and result.setup_1h == "VALID_SHORT"
        )

        if long_aligned:
            result.aligned = True
            result.final_direction = "LONG"
            result.confluences.append("✅ [MTF] 4H + 1H alineados — LONG")
        elif short_aligned:
            result.aligned = True
            result.final_direction = "SHORT"
            result.confluences.append("✅ [MTF] 4H + 1H alineados — SHORT")

        return result

    def _analyze_15m(self, result: MTFAnalysis, df: pd.DataFrame) -> MTFAnalysis:
        """Busca el trigger de entrada en 15M."""
        if df.empty or len(df) < 30 or not result.aligned:
            return result

        def safe(col, default):
            try:
                v = df[col].iloc[-1]
                return float(v) if (v is not None and v == v) else default
            except Exception:
                return default

        price = float(df["close"].iloc[-1])
        rsi   = safe("rsi",  50.0)
        atr   = safe("atr",  price * 0.003)
        result.rsi_15m = round(rsi, 1)

        # Calcular FVGs y OBs AQUÍ (se eliminaron en refactor anterior)
        fvgs = analyzer.find_fair_value_gaps(df, min_gap_pct=0.05)
        obs  = analyzer.find_order_blocks(df)

        direction = result.final_direction
        fvg_type = "BULLISH" if direction == "LONG" else "BEARISH"
        ob_type = "BULLISH" if direction == "LONG" else "BEARISH"

        # Buscar FVG cercano
        relevant_fvg = None
        for fvg in reversed(fvgs):
            if fvg["type"] == fvg_type:
                dist = abs(fvg["midpoint"] - price) / price * 100
                if dist < 1.5:
                    relevant_fvg = fvg
                    break

        # Buscar OB cercano
        relevant_ob = None
        for ob in reversed(obs):
            if ob["type"] == ob_type:
                dist = abs(ob["midpoint"] - price) / price * 100
                if dist < 2.0:
                    relevant_ob = ob
                    break

        result.fvg_15m = relevant_fvg
        result.ob_15m = relevant_ob

        # ¿Hay trigger de entrada?
        if relevant_fvg:
            result.entry_15m = "CONFIRMED"
            result.confluences.append(
                f"✅ [15M] FVG {fvg_type} encontrado "
                f"(${relevant_fvg['bottom']:,.2f} — ${relevant_fvg['top']:,.2f})"
            )
        elif relevant_ob:
            result.entry_15m = "CONFIRMED"
            result.confluences.append(
                f"✅ [15M] Order Block {ob_type} en zona "
                f"(${relevant_ob['bottom']:,.2f} — ${relevant_ob['top']:,.2f})"
            )
        else:
            result.entry_15m = "PENDING"
            result.warnings.append("⏳ [15M] Sin FVG/OB de entrada — esperar retest")

        # RSI en zona correcta en 15M
        if direction == "LONG" and rsi < 50:
            result.confluences.append(f"✅ [15M] RSI bajo ({rsi:.1f}) — momentum disponible")
        elif direction == "SHORT" and rsi > 50:
            result.confluences.append(f"✅ [15M] RSI alto ({rsi:.1f}) — momentum agotado")

        return result

    def _build_final_signal(
        self,
        result: MTFAnalysis,
        df_15m: pd.DataFrame,
        market_data: dict,
    ) -> Optional[Signal]:
        """Construye la señal final usando el entry del 15M."""
        price = float(df_15m["close"].iloc[-1])

        def safe(col, default):
            try:
                v = df_15m[col].iloc[-1]
                return float(v) if (v is not None and v == v) else default
            except Exception:
                return default

        atr       = safe("atr", price * 0.005)
        direction = result.final_direction

        # Zona de entrada desde FVG o OB del 15M
        entry_zone = result.fvg_15m or result.ob_15m
        if entry_zone:
            entry_low  = entry_zone["bottom"]
            entry_high = entry_zone["top"]
        else:
            margin     = atr * 0.5
            entry_low  = price - margin
            entry_high = price + margin

        entry_mid = (entry_low + entry_high) / 2

        # ── Precisión decimal según precio del activo ──────────────────────
        if price >= 10_000:
            dec = 1
        elif price >= 1_000:
            dec = 2
        elif price >= 100:
            dec = 3
        elif price >= 1:
            dec = 4
        else:
            dec = 5

        def r(v): return round(v, dec)

        # ── SL siempre fuera de la zona, mínimo 1.5×ATR desde el extremo ──
        zone_width = entry_high - entry_low
        min_sl_distance = max(atr * 1.5, zone_width * 0.5 + atr * 0.5)

        # También intentamos anclar el SL a la estructura del 1H si es mejor
        structure_1h = result.structure_1h
        if direction == "LONG":
            struct_sl = structure_1h.get("last_low", 0)
            candidate_sl = r(entry_low - min_sl_distance)
            # Usar estructura si queda más abajo (más conservador)
            stop_loss = r(min(candidate_sl, struct_sl - atr * 0.3)) if struct_sl and struct_sl < entry_low else candidate_sl
            risk = entry_mid - stop_loss
        else:
            struct_sl = structure_1h.get("last_high", 0)
            candidate_sl = r(entry_high + min_sl_distance)
            stop_loss = r(max(candidate_sl, struct_sl + atr * 0.3)) if struct_sl and struct_sl > entry_high else candidate_sl
            risk = stop_loss - entry_mid

        # Validar risk mínimo
        min_risk = price * 0.001
        if risk <= min_risk or risk > price * 0.08:
            return None

        # ── TPs con R:R garantizado ─────────────────────────────────────────
        # MTF usa R:R más generoso (1.8R y 4.0R) por mayor calidad de señal
        if direction == "LONG":
            tp1 = r(entry_mid + risk * 1.8)
            tp2 = r(entry_mid + risk * 4.0)
        else:
            tp1 = r(entry_mid - risk * 1.8)
            tp2 = r(entry_mid - risk * 4.0)

        # Validar dirección de TPs
        if direction == "LONG" and (tp1 <= entry_high or tp2 <= tp1):
            return None
        if direction == "SHORT" and (tp1 >= entry_low or tp2 >= tp1):
            return None

        rr = round(abs(tp2 - entry_mid) / risk, 2)
        if rr < 2.5:
            return None

        return Signal(
            pair=result.pair,
            direction=direction,
            timeframe="MTF-15M",
            entry_low=r(entry_low),
            entry_high=r(entry_high),
            stop_loss=r(stop_loss),
            tp1=tp1,
            tp2=tp2,
            rr_ratio=rr,
            confidence=0,
            confluences=result.confluences,
            warnings=result.warnings,
        )

    def _compute_confidence(self, result: MTFAnalysis, market_data: dict) -> int:
        """Calcula score de confianza 0-100 basado en todas las confluencias."""
        score = 0

        # Alineación de TF (base)
        if result.bias_4h != "RANGING":
            score += 25
        elif result.aligned:
            score += 10

        # Setup 1H
        if result.setup_1h != "NONE":
            score += 20

        # Entry 15M
        if result.entry_15m == "CONFIRMED":
            score += 20
            if result.fvg_15m:
                score += 5  # FVG es más preciso que OB

        # RSI alineados
        direction = result.final_direction
        if direction == "LONG":
            if result.rsi_4h < 60:
                score += 5
            if result.rsi_1h < 55:
                score += 5
            if result.rsi_15m < 50:
                score += 5
        else:
            if result.rsi_4h > 40:
                score += 5
            if result.rsi_1h > 45:
                score += 5
            if result.rsi_15m > 50:
                score += 5

        # Datos de futuros
        funding = market_data.get("funding_rate", 0)
        oi_change = market_data.get("oi_change_1h", 0)

        if direction == "LONG" and funding < -0.02:
            score += 5
        elif direction == "SHORT" and funding > 0.02:
            score += 5

        if direction == "LONG" and oi_change > 2:
            score += 5
        elif direction == "SHORT" and oi_change < -2:
            score += 5

        # Penalizaciones
        score -= len(result.warnings) * 5

        return max(0, min(100, score))


mtf_engine: Optional[MultiTimeframeEngine] = None


def get_mtf_engine(binance_client: Any) -> MultiTimeframeEngine:
    global mtf_engine
    if mtf_engine is None:
        mtf_engine = MultiTimeframeEngine(binance_client)
    return mtf_engine
