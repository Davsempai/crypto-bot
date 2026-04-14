"""
market/binance_client.py — Cliente para Binance Futures API (async)
"""
import aiohttp
import hashlib
import hmac
import time
import pandas as pd
from typing import Optional
from config import config
from utils.logger import setup_logger

logger = setup_logger("binance")


class BinanceFuturesClient:
    """Cliente async para Binance USDT-M Futures."""

    BASE_URL = config.BINANCE_BASE_URL

    def __init__(self):
        self.api_key = config.BINANCE_API_KEY
        self.secret = config.BINANCE_API_SECRET
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "X-MBX-APIKEY": self.api_key,
                    "Content-Type": "application/json",
                }
            )
        return self._session

    def _sign(self, params: dict) -> dict:
        """Firma los parámetros con HMAC-SHA256."""
        params["timestamp"] = int(time.time() * 1000)
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(
            self.secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    async def _get(self, endpoint: str, params: dict = None, signed: bool = False) -> dict | list:
        session = await self._get_session()
        params = params or {}
        if signed:
            params = self._sign(params)
        url = f"{self.BASE_URL}{endpoint}"
        try:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                if isinstance(data, dict) and "code" in data and data["code"] != 200:
                    logger.error(f"Binance error {data.get('code')}: {data.get('msg')}")
                return data
        except Exception as e:
            logger.error(f"Error en GET {endpoint}: {e}")
            return {}

    # ─── Datos de mercado ──────────────────────────────────────────────────

    async def get_price(self, symbol: str) -> float:
        """Precio actual del par."""
        data = await self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data.get("price", 0))

    async def get_ticker_24h(self, symbol: str) -> dict:
        """Stats de las últimas 24h."""
        return await self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})

    async def get_funding_rate(self, symbol: str) -> dict:
        """Funding rate actual y próximo."""
        data = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        if not data:
            return {}
        return {
            "symbol": symbol,
            "funding_rate": float(data.get("lastFundingRate", 0)) * 100,  # en %
            "next_funding_time": data.get("nextFundingTime"),
            "mark_price": float(data.get("markPrice", 0)),
            "index_price": float(data.get("indexPrice", 0)),
        }

    async def get_open_interest(self, symbol: str) -> dict:
        """Open Interest actual."""
        data = await self._get("/fapi/v1/openInterest", {"symbol": symbol})
        if not data:
            return {}
        return {
            "symbol": symbol,
            "open_interest": float(data.get("openInterest", 0)),
            "timestamp": data.get("time"),
        }

    async def get_open_interest_history(self, symbol: str, period: str = "1h", limit: int = 24) -> list:
        """Historial de OI para detectar cambios."""
        data = await self._get("/futures/data/openInterestHist", {
            "symbol": symbol,
            "period": period,
            "limit": limit,
        })
        return data if isinstance(data, list) else []

    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        """Velas OHLCV como DataFrame."""
        data = await self._get("/fapi/v1/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        if not data or not isinstance(data, list):
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df

    async def get_liquidations(self, symbol: str) -> dict:
        """Datos de liquidaciones recientes (aproximado via Long/Short ratio)."""
        # Binance no da liquidaciones directas via REST público
        # Usamos top trader long/short ratio como proxy
        ls_data = await self._get("/futures/data/topLongShortPositionRatio", {
            "symbol": symbol,
            "period": "1h",
            "limit": 2,
        })
        lsr = float(ls_data[0].get("longShortRatio", 1.0)) if isinstance(ls_data, list) and ls_data else 1.0
        return {
            "symbol": symbol,
            "long_short_ratio": lsr,
            "bias": "LONG" if lsr > 1.2 else "SHORT" if lsr < 0.8 else "NEUTRAL",
        }

    async def get_all_futures_market_data(self, symbol: str) -> dict:
        """Recopila todos los datos de mercado de futuros para un par."""
        import asyncio
        funding, oi, ticker, liq = await asyncio.gather(
            self.get_funding_rate(symbol),
            self.get_open_interest(symbol),
            self.get_ticker_24h(symbol),
            self.get_liquidations(symbol),
        )

        # Cambio de OI en últimas horas
        oi_hist = await self.get_open_interest_history(symbol, "1h", 2)
        oi_change_1h = 0.0
        if len(oi_hist) >= 2:
            oi_old = float(oi_hist[0].get("sumOpenInterest", 0))
            oi_new = float(oi_hist[-1].get("sumOpenInterest", 0))
            oi_change_1h = ((oi_new - oi_old) / oi_old * 100) if oi_old else 0

        return {
            "pair": symbol,
            "price": float(ticker.get("lastPrice", 0)),
            "price_change_24h": float(ticker.get("priceChangePercent", 0)),
            "volume_24h": float(ticker.get("quoteVolume", 0)),
            "funding_rate": funding.get("funding_rate", 0),
            "mark_price": funding.get("mark_price", 0),
            "open_interest": oi.get("open_interest", 0),
            "oi_change_1h": round(oi_change_1h, 2),
            "long_short_ratio": liq.get("long_short_ratio", 1.0),
            "market_bias": liq.get("bias", "NEUTRAL"),
        }

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# Instancia global
binance = BinanceFuturesClient()
