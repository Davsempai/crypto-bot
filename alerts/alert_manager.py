"""
alerts/alert_manager.py — Formatea y gestiona todas las alertas del bot
"""
from datetime import datetime
from typing import Optional
from analysis.technical import Signal
from config import config
from utils.logger import setup_logger

logger = setup_logger("alerts")


class AlertFormatter:
    """Formatea mensajes de alerta para Telegram con Markdown."""

    @staticmethod
    def format_signal(signal: Signal, market_data: dict, signal_id: int = 0) -> str:
        """Formatea una señal de trading completa."""
        emoji = "🟢" if signal.direction == "LONG" else "🔴"
        arrow = "▲" if signal.direction == "LONG" else "▼"

        funding = market_data.get("funding_rate", 0)
        oi_change = market_data.get("oi_change_1h", 0)
        oi_emoji = "📈" if oi_change > 0 else "📉"
        funding_str = f"{funding:+.4f}%"
        vol_24h = market_data.get("volume_24h", 0)

        # Precisión de decimales según precio del activo
        price = market_data.get("price", signal.entry_high)
        if price >= 10_000: d = 1
        elif price >= 1_000: d = 2
        elif price >= 100:   d = 3
        elif price >= 1:     d = 4
        else:                d = 5

        def fp(v): return f"${v:,.{d}f}"

        # Calcular % de SL y TPs
        entry_mid = (signal.entry_low + signal.entry_high) / 2
        sl_pct   = (signal.stop_loss - entry_mid) / entry_mid * 100
        tp1_pct  = (signal.tp1 - entry_mid) / entry_mid * 100
        tp2_pct  = (signal.tp2 - entry_mid) / entry_mid * 100

        confluences_text = "\n".join(f"  {c}" for c in signal.confluences)
        warnings_text = ("\n\n⚠️ *Advertencias:*\n" + "\n".join(f"  {w}" for w in signal.warnings)) if signal.warnings else ""

        id_text = f"  `#{signal_id:04d}`" if signal_id else ""

        msg = f"""{emoji} *{signal.direction} — {signal.pair}* {arrow}
━━━━━━━━━━━━━━━━━━━━━━━━
📍 *Zona Entrada:* `{fp(signal.entry_low)} — {fp(signal.entry_high)}`
🛑 *Stop Loss:*    `{fp(signal.stop_loss)}` `({sl_pct:+.2f}%)`
🎯 *TP1* (50%):    `{fp(signal.tp1)}` `({tp1_pct:+.2f}%)`
🏆 *TP2* (50%):    `{fp(signal.tp2)}` `({tp2_pct:+.2f}%)`

⚙️ *Futuros:*
  • Funding: `{funding_str}` {"🔥" if abs(funding) > 0.05 else "✅"}
  • OI 1h: `{oi_change:+.2f}%` {oi_emoji}

📐 *Confluencias:*
{confluences_text}{warnings_text}

📊 *R:R*: `1:{signal.rr_ratio}` | ⏱ `{signal.timeframe}` | 🎯 `{signal.confidence}%`
⚖️ *Riesgo:* `{config.MAX_RISK_PER_TRADE}%`{id_text}
{"🧪 _PAPER TRADING_" if config.PAPER_TRADING else ""}
⏰ `{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC`"""

        return msg

    @staticmethod
    def format_funding_alert(symbol: str, funding_rate: float, threshold: float) -> str:
        """Alerta de funding rate extremo."""
        direction = "LONGS sobre-pagando" if funding_rate > 0 else "SHORTS sobre-pagando"
        signal_hint = "⚠️ Posible corrección bajista" if funding_rate > 0 else "⚠️ Posible squeeze alcista"
        emoji = "🔥" if abs(funding_rate) > threshold * 2 else "⚡"

        return f"""{emoji} *ALERTA FUNDING RATE — {symbol}*
━━━━━━━━━━━━━━━━━━━━━━━━
💰 Funding Rate: `{funding_rate:+.4f}%`
📊 Umbral configurado: `±{threshold:.4f}%`
🎯 Situación: _{direction}_

{signal_hint}

_El funding rate indica desequilibrio en posiciones. Evitar abrir en la dirección dominante._

⏰ `{datetime.utcnow().strftime('%H:%M')} UTC`"""

    @staticmethod
    def format_oi_alert(symbol: str, oi_change: float, price: float) -> str:
        """Alerta de cambio significativo en Open Interest."""
        direction = "creciendo" if oi_change > 0 else "cayendo"
        interpretation = (
            "Nuevas posiciones abriendo — confirma movimiento" if oi_change > 0
            else "Posiciones cerrando — posible reversión"
        )
        emoji = "📈" if oi_change > 0 else "📉"

        return f"""{emoji} *ALERTA OPEN INTEREST — {symbol}*
━━━━━━━━━━━━━━━━━━━━━━━━
💹 OI Change (1h): `{oi_change:+.2f}%`
💰 Precio actual: `${price:,.2f}`
📊 OI está {direction}

💡 _{interpretation}_

⏰ `{datetime.utcnow().strftime('%H:%M')} UTC`"""

    @staticmethod
    def format_news_alert(news: dict) -> str:
        """Formatea alerta de noticia importante."""
        sentiment = news.get("sentiment", "NEUTRAL")
        impact = news.get("impact", "LOW")
        advice = news.get("trading_advice", "NORMAL")

        sentiment_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(sentiment, "⚪")
        impact_emoji = {"HIGH": "🚨", "MEDIUM": "⚠️", "LOW": "ℹ️"}.get(impact, "ℹ️")

        advice_map = {
            "WAIT": "⏸ Espera confirmación antes de entrar",
            "AVOID_NEW_LONGS": "🚫 Evita nuevas posiciones LONG",
            "AVOID_NEW_SHORTS": "🚫 Evita nuevas posiciones SHORT",
            "OPPORTUNITY_LONG": "💡 Posible oportunidad LONG en el dip",
            "OPPORTUNITY_SHORT": "💡 Posible oportunidad SHORT en el rally",
            "NORMAL": "✅ Operar con normalidad",
        }
        advice_text = advice_map.get(advice, "✅ Operar con normalidad")

        assets = news.get("affected_assets", [])
        assets_str = " • ".join(f"`{a}`" for a in assets) if assets else "General"
        url = news.get("url", "")
        url_text = f"\n🔗 [Ver noticia]({url})" if url else ""

        return f"""{impact_emoji} *NOTICIA {impact} IMPACTO* — {sentiment_emoji} {sentiment}
━━━━━━━━━━━━━━━━━━━━━━━━
📰 *{news.get('title', '')}*

📊 *Análisis IA:*
_{news.get('reasoning', '')}_

🎯 *Activos:* {assets_str}
💼 *Recomendación:* {advice_text}{url_text}

⏰ `{datetime.utcnow().strftime('%H:%M')} UTC`"""

    @staticmethod
    def format_market_summary(market_data_list: list[dict], ai_summary: str) -> str:
        """Resumen de mercado completo."""
        pairs_text = ""
        for d in market_data_list:
            change_emoji = "📈" if d["price_change_24h"] > 0 else "📉"
            funding_emoji = "🔥" if abs(d["funding_rate"]) > 0.05 else "✅"
            pairs_text += (
                f"\n*{d['pair']}* {change_emoji}\n"
                f"  Precio: `${d['price']:,.2f}` ({d['price_change_24h']:+.1f}%)\n"
                f"  Funding: `{d['funding_rate']:+.4f}%` {funding_emoji} | OI: `{d['oi_change_1h']:+.2f}%`\n"
            )

        return f"""📊 *RESUMEN DE MERCADO*
━━━━━━━━━━━━━━━━━━━━━━━━
{pairs_text}
🤖 *Análisis IA:*
_{ai_summary}_

⏰ `{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC`"""

    @staticmethod
    def format_stats(stats: dict) -> str:
        """Estadísticas del bot."""
        win_rate = stats.get("win_rate", 0)
        total_r = stats.get("total_r", 0)
        wr_emoji = "🟢" if win_rate >= 55 else "🟡" if win_rate >= 45 else "🔴"
        r_emoji = "💰" if total_r > 0 else "📉"

        recent = stats.get("recent_signals", [])
        recent_text = ""
        for s in recent:
            status_emoji = {"TP1": "✅", "TP2": "🏆", "SL": "❌", "OPEN": "⏳", "CANCELLED": "🚫"}.get(s["status"], "⏳")
            recent_text += f"\n  {status_emoji} {s['pair']} {s['direction']} — {s['status']}"
            if s["pnl_r"] != 0:
                recent_text += f" ({s['pnl_r']:+.1f}R)"

        return f"""📈 *ESTADÍSTICAS DEL BOT*
━━━━━━━━━━━━━━━━━━━━━━━━
📊 Total señales: `{stats.get('total_signals', 0)}`
✅ TP1 hit: `{stats.get('tp1_hit', 0)}`
🏆 TP2 hit: `{stats.get('tp2_hit', 0)}`
❌ SL hit: `{stats.get('sl_hit', 0)}`

{wr_emoji} *Win Rate:* `{win_rate:.1f}%`
{r_emoji} *Total R:* `{total_r:+.2f}R`

📋 *Últimas señales:*{recent_text if recent_text else " Sin señales aún"}

⏰ `{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC`"""


formatter = AlertFormatter()
