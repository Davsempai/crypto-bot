"""
analysis/signal_filter.py — Filtro de señales V2

Alineado con la estrategia V2 (Tendencia + Pullback + Confirmación).
La V2 ya garantiza R:R >= 2.0 y ADX > 22, así que el filtro
añade contexto de futuros (funding, OI) y horario macro.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from analysis.technical import Signal
from utils.logger import setup_logger
from config import config

logger = setup_logger("signal_filter")


@dataclass
class FilterResult:
    approved: bool
    score: int
    grade: str              # A+ / A / B / C / REJECTED
    reasons_approved: list[str]
    reasons_rejected: list[str]
    adjusted_risk: float


class SignalFilter:

    MIN_SCORE = 50   # Bajado de 55 porque la V2 ya filtra más en origen

    # Horas de mayor riesgo macro (UTC) — reducción de riesgo, no bloqueo
    HIGH_RISK_HOURS_UTC = [(13, 14), (14, 15)]   # Solo datos US (más impacto en crypto)

    def evaluate(
        self,
        signal: Signal,
        market_data: dict,
        recent_signals_count: int = 0,
    ) -> FilterResult:

        approved_reasons = []
        rejected_reasons = []
        score = 45  # Base más baja — la V2 ya es selectiva en origen

        funding   = float(market_data.get("funding_rate",  0) or 0)
        oi_change = float(market_data.get("oi_change_1h",  0) or 0)

        # ── FILTROS HARD ───────────────────────────────────────────────────────

        # R:R mínimo 2.0 (la V2 ya garantiza esto, pero por si acaso)
        if signal.rr_ratio < 2.0:
            return FilterResult(False, 0, "REJECTED", [],
                [f"🚫 R:R insuficiente ({signal.rr_ratio} < 2.0)"], 0)

        # Funding extremo en CONTRA de la señal
        if signal.direction == "LONG" and funding > config.FUNDING_RATE_THRESHOLD * 3:
            return FilterResult(False, 10, "REJECTED", [],
                [f"🚫 Funding extremadamente alto ({funding:+.4f}%) — muy caro ir LONG"], 0)
        if signal.direction == "SHORT" and funding < -config.FUNDING_RATE_THRESHOLD * 3:
            return FilterResult(False, 10, "REJECTED", [],
                [f"🚫 Funding extremadamente negativo ({funding:+.4f}%) — muy caro ir SHORT"], 0)

        # Límite anti-sobretrading: máx 2 señales por par en 4h (bajado de 3)
        if recent_signals_count >= 2:
            return FilterResult(False, 20, "REJECTED", [],
                [f"🚫 Límite anti-sobretrading ({recent_signals_count} señales en 4h)"], 0)

        # ── SCORING ────────────────────────────────────────────────────────────

        # R:R — la V2 da mínimo 2.0, máximo ~5+
        if signal.rr_ratio >= 4.0:
            score += 20
            approved_reasons.append(f"✅ R:R excepcional ({signal.rr_ratio}:1)")
        elif signal.rr_ratio >= 3.0:
            score += 14
            approved_reasons.append(f"✅ R:R excelente ({signal.rr_ratio}:1)")
        elif signal.rr_ratio >= 2.5:
            score += 8
            approved_reasons.append(f"✅ R:R bueno ({signal.rr_ratio}:1)")
        else:
            score += 3
            approved_reasons.append(f"✅ R:R mínimo ({signal.rr_ratio}:1)")

        # Confluencias — la V2 requiere mínimo 3 para generar señal
        n_conf = len(signal.confluences)
        if n_conf >= 7:
            score += 18
            approved_reasons.append(f"✅ Confluencias máximas ({n_conf})")
        elif n_conf >= 5:
            score += 12
            approved_reasons.append(f"✅ Confluencias altas ({n_conf})")
        elif n_conf >= 3:
            score += 6
            approved_reasons.append(f"✅ Confluencias suficientes ({n_conf})")

        # Confianza del motor técnico (calculada por V2 con ADX y volumen)
        if signal.confidence >= 80:
            score += 12
            approved_reasons.append(f"✅ Confianza técnica alta ({signal.confidence}%)")
        elif signal.confidence >= 65:
            score += 7
            approved_reasons.append(f"✅ Confianza técnica media ({signal.confidence}%)")
        elif signal.confidence >= 50:
            score += 3

        # Funding alineado con señal (favorece el trade)
        if signal.direction == "LONG":
            if -0.05 < funding <= 0:
                score += 8
                approved_reasons.append(f"✅ Funding negativo — LONG favorable ({funding:+.4f}%)")
            elif 0 < funding <= config.FUNDING_RATE_THRESHOLD:
                score += 2  # Neutral-positivo para long
            elif funding > config.FUNDING_RATE_THRESHOLD:
                score -= 8
                rejected_reasons.append(f"⚠️ Funding alto — longs pagan ({funding:+.4f}%)")
        else:  # SHORT
            if 0 < funding <= 0.05:
                score += 8
                approved_reasons.append(f"✅ Funding positivo — SHORT favorable ({funding:+.4f}%)")
            elif -config.FUNDING_RATE_THRESHOLD <= funding < 0:
                score += 2
            elif funding < -config.FUNDING_RATE_THRESHOLD:
                score -= 8
                rejected_reasons.append(f"⚠️ Funding negativo — shorts pagan ({funding:+.4f}%)")

        # OI alineado con señal
        if signal.direction == "LONG" and oi_change > 3:
            score += 8
            approved_reasons.append(f"✅ OI creciendo confirma LONG (+{oi_change:.1f}%)")
        elif signal.direction == "SHORT" and oi_change < -3:
            score += 8
            approved_reasons.append(f"✅ OI cayendo confirma SHORT ({oi_change:.1f}%)")
        elif signal.direction == "LONG" and oi_change < -5:
            score -= 5
            rejected_reasons.append(f"⚠️ OI cayendo en LONG — posible trampa ({oi_change:.1f}%)")
        elif signal.direction == "SHORT" and oi_change > 5:
            score -= 5
            rejected_reasons.append(f"⚠️ OI creciendo en SHORT — posible trampa (+{oi_change:.1f}%)")

        # Horario macro (solo penalizar en horas de alto riesgo, no bloquear)
        current_hour = datetime.now(timezone.utc).hour
        in_high_risk = any(s <= current_hour < e for s, e in self.HIGH_RISK_HOURS_UTC)
        if in_high_risk:
            score -= 5
            rejected_reasons.append(f"⚠️ Hora de riesgo macro ({current_hour}:00 UTC)")
        else:
            score += 3
            approved_reasons.append("✅ Horario de bajo riesgo macro")

        # Timeframe MTF — mayor calidad por triple confirmación
        if "MTF" in signal.timeframe:
            score += 12
            approved_reasons.append("✅ Señal MTF (4H+1H+15M alineados)")
        elif signal.timeframe in ("4h", "4H"):
            score += 6
            approved_reasons.append("✅ Timeframe 4H — mayor confiabilidad")

        # Warnings del motor técnico
        for w in signal.warnings:
            score -= 6
            rejected_reasons.append(w)

        # ── DECISIÓN ───────────────────────────────────────────────────────────
        score    = max(0, min(100, score))
        approved = score >= self.MIN_SCORE

        if score >= 82:   grade = "A+"
        elif score >= 70: grade = "A"
        elif score >= 60: grade = "B"
        elif score >= self.MIN_SCORE: grade = "C"
        else:             grade = "REJECTED"

        # Riesgo ajustado — A+ puede usar hasta 1.5× el riesgo base
        risk_multiplier = {"A+": 1.5, "A": 1.0, "B": 0.75, "C": 0.5}.get(grade, 0)
        adjusted_risk   = round(
            min(config.MAX_RISK_PER_TRADE * risk_multiplier, config.MAX_RISK_PER_TRADE * 1.5), 2
        )

        if approved:
            logger.info(
                f"✅ Señal APROBADA: {signal.pair} {signal.direction} | "
                f"Grade: {grade} | Score: {score}/100 | Riesgo: {adjusted_risk}%"
            )
        else:
            reason = rejected_reasons[0] if rejected_reasons else "Score bajo"
            logger.info(
                f"❌ Señal RECHAZADA: {signal.pair} {signal.direction} | "
                f"Score: {score}/100 | {reason}"
            )

        return FilterResult(
            approved=approved, score=score, grade=grade,
            reasons_approved=approved_reasons,
            reasons_rejected=rejected_reasons,
            adjusted_risk=adjusted_risk,
        )

    def get_grade_emoji(self, grade: str) -> str:
        return {"A+": "🌟", "A": "⭐", "B": "🔵", "C": "🟡", "REJECTED": "❌"}.get(grade, "⚪")


signal_filter = SignalFilter()
