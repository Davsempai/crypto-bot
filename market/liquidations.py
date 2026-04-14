"""
market/liquidations.py — Mapa de liquidaciones para Weex y Binance

Weex no expone endpoints públicos de liquidaciones ni L/S ratio histórico,
así que estimamos las zonas a partir de:
  - Precio actual + leverage típicos del mercado retail
  - Funding Rate (proxy del sesgo L/S)
  - Open Interest del exchange

Para Binance se usan sus endpoints propios cuando está disponible.
"""
import aiohttp
from datetime import datetime, timezone
from config import config
from utils.logger import setup_logger

logger = setup_logger("liquidations")


class LiquidationMonitor:

    async def get_liquidation_levels(self, pair: str, current_price: float) -> dict:
        """
        Calcula zonas estimadas de liquidación por nivel de apalancamiento.
        Funciona con cualquier exchange porque usa solo el precio actual.
        """
        from market.exchange import exchange

        # Obtener funding rate del exchange configurado
        try:
            funding_data = await exchange.get_funding_rate(pair)
            fr = funding_data.get("funding_rate", 0)
        except Exception:
            fr = 0.0

        # Obtener ratio L/S (Weex lo estima desde FR; Binance tiene endpoint propio)
        try:
            if hasattr(exchange, "get_long_short_ratio"):
                lsr_data = await exchange.get_long_short_ratio(pair)
                recent_ratio = lsr_data.get("long_short_ratio", 1.0)
            else:
                recent_ratio = 1.0
        except Exception:
            recent_ratio = 1.0

        # Zonas de liquidación estimadas por leverage
        leverage_levels = [5, 10, 20, 50, 100]
        liquidation_zones = []
        for lev in leverage_levels:
            long_liq_pct  = 1 / lev * 0.8
            short_liq_pct = 1 / lev * 0.8
            liquidation_zones.append({
                "leverage":          lev,
                "long_liquidation":  round(current_price * (1 - long_liq_pct),  2),
                "short_liquidation": round(current_price * (1 + short_liq_pct), 2),
                "distance_long_pct":  round(-long_liq_pct  * 100, 1),
                "distance_short_pct": round( short_liq_pct * 100, 1),
            })

        # Sesgo de mercado
        if recent_ratio > 1.3:
            bias         = "LONG_HEAVY"
            risk_note    = "⚠️ Mercado sobre-largo — riesgo de long squeeze"
            hunt_direction = "BEARISH"
        elif recent_ratio < 0.7:
            bias         = "SHORT_HEAVY"
            risk_note    = "⚠️ Mercado sobre-corto — riesgo de short squeeze"
            hunt_direction = "BULLISH"
        else:
            bias         = "BALANCED"
            risk_note    = "✅ Ratio balanceado"
            hunt_direction = "NEUTRAL"

        return {
            "pair":              pair,
            "current_price":     current_price,
            "long_short_ratio":  round(recent_ratio, 3),
            "funding_rate":      fr,
            "market_bias":       bias,
            "hunt_direction":    hunt_direction,
            "risk_note":         risk_note,
            "liquidation_zones": liquidation_zones,
            "exchange":          config.exchange_name,
            "timestamp":         datetime.now(timezone.utc).isoformat(),
        }

    async def get_recent_large_liquidations(self, pair: str) -> list[dict]:
        """
        Intenta obtener liquidaciones grandes recientes.
        Solo disponible si el exchange es Binance.
        Weex no expone este endpoint públicamente.
        """
        if config.EXCHANGE != "binance":
            return []   # Weex no tiene este endpoint
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://fapi.binance.com/fapi/v1/allForceOrders",
                    params={"symbol": pair, "limit": 100},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    data = await resp.json()
            if not isinstance(data, list):
                return []
            large = []
            for liq in data:
                qty   = float(liq.get("origQty", 0))
                price = float(liq.get("averagePrice", 0))
                value = qty * price
                if value >= 50_000:
                    large.append({
                        "side":      liq.get("side", ""),
                        "price":     price,
                        "quantity":  qty,
                        "value_usd": round(value, 0),
                        "time": datetime.fromtimestamp(
                            liq.get("time", 0) / 1000
                        ).isoformat(),
                    })
            large.sort(key=lambda x: x["value_usd"], reverse=True)
            return large[:10]
        except Exception as e:
            logger.debug(f"Error obteniendo liquidaciones: {e}")
            return []

    def format_liquidation_summary(self, data: dict) -> str:
        zones  = data.get("liquidation_zones", [])
        pair   = data["pair"]
        price  = data["current_price"]
        lsr    = data["long_short_ratio"]
        bias   = data["market_bias"]
        note   = data["risk_note"]
        hunt   = data["hunt_direction"]
        exch   = data.get("exchange", "")
        fr     = data.get("funding_rate", 0)

        hunt_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(hunt, "⚪")

        lines = [
            f"💥 *MAPA DE LIQUIDACIONES — {pair}*",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 Precio: `${price:,.2f}` | Exchange: `{exch}`",
            f"📊 L/S Ratio: `{lsr}` — {bias}",
            f"💸 Funding Rate: `{fr:+.4f}%`",
            f"{note}",
            f"{hunt_emoji} Dirección de caza estimada: `{hunt}`",
            "",
            "*Zonas de liquidación estimadas:*",
            "```",
            f"{'Lev':>6} | {'Liq LONGS':>14} | {'Liq SHORTS':>14}",
            "-" * 44,
        ]
        for z in zones:
            lines.append(
                f"{z['leverage']:>5}x | "
                f"${z['long_liquidation']:>12,.2f} ({z['distance_long_pct']:>+.1f}%) | "
                f"${z['short_liquidation']:>12,.2f} ({z['distance_short_pct']:>+.1f}%)"
            )
        lines.append("```")
        lines.append(f"\n⏰ `{datetime.now(timezone.utc).strftime('%H:%M')} UTC`")
        return "\n".join(lines)


liq_monitor = LiquidationMonitor()
