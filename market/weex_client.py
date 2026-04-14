"""
market/weex_client.py — Cliente para Weex Futures API V3 (async)

URL base:  https://api-contract.weex.com
Docs:      https://www.weex.com/api-doc/contract/log/changelog

Diseñado como reemplazo directo de binance_client.py:
→ Misma interfaz pública, mismo formato de salida.
→ El resto del bot (análisis, señales, dashboard) no requiere ningún cambio.

Diferencias clave vs Binance que este cliente maneja internamente:
  • Base URL distinta
  • Auth con 3 claves: APIKey + SecretKey + Passphrase
  • historyKlines: máximo 100 velas por request (se pagina automáticamente)
  • OI history no disponible → se calcula con 2 snapshots consecutivos
"""

import asyncio
import hashlib
import hmac
import time
from typing import Optional

import aiohttp
import pandas as pd

from config import config
from utils.logger import setup_logger

logger = setup_logger("weex_client")


class WeexFuturesClient:
    """Cliente async para Weex USDT-M Futures API V3."""

    BASE_URL = "https://api-contract.weex.com"

    # Intervalos soportados por Weex (distinto nombre que Binance en algunos casos)
    INTERVAL_MAP = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "12h": "12h",
        "1d": "1d", "1w": "1w",
    }

    def __init__(self):
        self.api_key    = config.WEEX_API_KEY
        self.secret_key = config.WEEX_SECRET_KEY
        self.passphrase = config.WEEX_PASSPHRASE
        self._session: Optional[aiohttp.ClientSession] = None
        # Cache de OI para calcular cambio porcentual
        self._oi_cache: dict[str, tuple[float, float]] = {}   # symbol → (oi, timestamp)

    # ─── Sesión y firma ───────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """HMAC-SHA256 sobre timestamp+method+path+body."""
        message = f"{timestamp}{method.upper()}{path}{body}"
        return hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        """Cabeceras de autenticación para endpoints privados."""
        ts = str(int(time.time() * 1000))
        return {
            "X-Weex-ACCESS-KEY":        self.api_key,
            "X-Weex-ACCESS-SIGN":       self._sign(ts, method, path, body),
            "X-Weex-ACCESS-TIMESTAMP":  ts,
            "X-Weex-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":             "application/json",
        }

    async def _get(
        self,
        endpoint: str,
        params: dict = None,
        signed: bool = False,
        _retries: int = 3,
    ) -> dict | list:
        session = await self._get_session()
        params  = params or {}
        qs      = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        full_path = f"{endpoint}?{qs}" if qs else endpoint
        headers   = self._auth_headers("GET", full_path) if signed else {}
        url       = f"{self.BASE_URL}{endpoint}"

        for attempt in range(_retries):
            try:
                async with session.get(
                    url, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict) and data.get("code") not in (None, 0, "0", 200, "200"):
                        logger.error(f"Weex error [{data.get('code')}]: {data.get('msg')} — {endpoint}")
                    return data
            except aiohttp.ClientConnectorError as e:
                if attempt < _retries - 1:
                    wait = 2 ** attempt   # 1s, 2s, 4s
                    logger.warning(f"Red caída ({endpoint}), reintento {attempt+1}/{_retries} en {wait}s")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"Error GET {endpoint}: {e}")
                    return {}
            except Exception as e:
                logger.error(f"Error GET {endpoint}: {e}")
                return {}
        return {}

    # ─── Endpoints de mercado (públicos) ──────────────────────────────────────

    async def get_price(self, symbol: str) -> float:
        """Precio mark price actual."""
        data = await self._get("/capi/v3/market/symbolPrice", {"symbol": symbol, "priceType": "MARK"})
        if isinstance(data, dict):
            return float(data.get("price", 0))
        # Si retorna lista
        if isinstance(data, list) and data:
            return float(data[0].get("price", 0))
        return 0.0

    async def get_ticker_24h(self, symbol: str) -> dict:
        """Stats de las últimas 24h."""
        data = await self._get("/capi/v3/market/ticker/24hr", {"symbol": symbol})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return {}

    async def get_funding_rate(self, symbol: str) -> dict:
        """Funding rate actual."""
        data = await self._get("/capi/v3/market/premiumIndex", {"symbol": symbol})
        item = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        if not item:
            return {}
        # lastFundingRate viene como "0.00025" → convertir a % multiplicando x 100
        raw_rate = float(item.get("lastFundingRate", 0))
        return {
            "symbol":           symbol,
            "funding_rate":     round(raw_rate * 100, 6),   # en %
            "forecast_rate":    round(float(item.get("forecastFundingRate", 0)) * 100, 6),
            "mark_price":       float(item.get("markPrice", 0)),
            "index_price":      float(item.get("indexPrice", 0)),
            "next_funding_time": item.get("nextFundingTime"),
            "collect_cycle_min": item.get("collectCycle", 480),
        }

    async def get_open_interest(self, symbol: str) -> dict:
        """Open Interest actual."""
        data = await self._get("/capi/v3/market/openInterest", {"symbol": symbol})
        if isinstance(data, dict) and "openInterest" in data:
            return {
                "symbol":        symbol,
                "open_interest": float(data.get("openInterest", 0)),
                "timestamp":     data.get("time"),
            }
        return {"symbol": symbol, "open_interest": 0.0, "timestamp": None}

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
    ) -> pd.DataFrame:
        """
        Velas OHLCV como DataFrame — hasta 1000 por request.
        El endpoint /capi/v3/market/klines acepta hasta 1000 velas.
        Para más (backtest anual) usar get_klines_history().
        """
        weex_interval = self.INTERVAL_MAP.get(interval, interval)
        limit = min(limit, 1000)

        data = await self._get("/capi/v3/market/klines", {
            "symbol":   symbol,
            "interval": weex_interval,
            "limit":    limit,
        })

        return self._parse_klines(data)

    async def get_klines_history(
        self,
        symbol: str,
        interval: str,
        total_candles: int = 1500,
    ) -> pd.DataFrame:
        """
        Descarga velas históricas paginando /capi/v3/market/historyKlines.
        Weex limita a 100 velas por request — hacemos múltiples llamadas.

        Para 1 año en 1H → ~8760 velas → ~88 requests (con sleep entre cada uno).
        Para 1 año en 4H → ~2190 velas → ~22 requests.
        """
        weex_interval = self.INTERVAL_MAP.get(interval, interval)
        tf_ms         = self._interval_to_ms(interval)

        all_candles: list = []
        end_time    = int(time.time() * 1000)
        remaining   = total_candles
        max_pages   = 200   # límite de seguridad (200 × 100 = 20,000 velas máx)
        pages       = 0

        logger.info(f"Paginando {total_candles} velas de {symbol} {interval}...")

        while remaining > 0 and pages < max_pages:
            batch      = min(remaining, 100)
            start_time = end_time - (batch * tf_ms)

            data = await self._get("/capi/v3/market/historyKlines", {
                "symbol":    symbol,
                "interval":  weex_interval,
                "startTime": start_time,
                "endTime":   end_time - 1,
                "limit":     batch,
            })

            if not isinstance(data, list) or not data:
                logger.debug(f"Sin más datos históricos en página {pages+1}")
                break

            all_candles = data + all_candles   # prepend → orden cronológico
            end_time    = start_time
            remaining  -= len(data)
            pages      += 1

            if len(data) < batch:
                break   # El exchange no tiene más datos

            await asyncio.sleep(0.12)   # Rate limit suave

        logger.info(f"Total velas descargadas: {len(all_candles)} ({pages} requests)")
        return self._parse_klines(all_candles)

    def _parse_klines(self, data: list) -> pd.DataFrame:
        """Convierte la respuesta de klines al mismo DataFrame que usaba Binance."""
        if not data or not isinstance(data, list):
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)

        # Eliminar timestamps duplicados (ocurren en los bordes de paginación)
        # Conservar la última aparición (más reciente/confiable)
        df = df[~df.index.duplicated(keep="last")]

        return df

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        """Convierte un intervalo string a milisegundos."""
        units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
        for suffix, ms in units.items():
            if interval.endswith(suffix):
                try:
                    return int(interval[:-1]) * ms
                except ValueError:
                    pass
        return 3_600_000  # default 1h

    async def get_oi_change_1h(self, symbol: str) -> float:
        """
        Calcula el cambio % de OI en la última hora usando cache.
        Weex no expone historial de OI, así que comparamos con
        el snapshot anterior almacenado en memoria.
        """
        current = await self.get_open_interest(symbol)
        oi_now = current.get("open_interest", 0)
        ts_now = time.time()

        if oi_now == 0:
            return 0.0

        cached = self._oi_cache.get(symbol)

        if cached:
            oi_old, ts_old = cached
            age_hours = (ts_now - ts_old) / 3600
            # Solo calcular cambio si el snapshot tiene entre 45min y 2h
            if 0.75 <= age_hours <= 2.0 and oi_old > 0:
                pct_change = (oi_now - oi_old) / oi_old * 100
                # Renovar cache solo si ya pasó 1h
                if age_hours >= 1.0:
                    self._oi_cache[symbol] = (oi_now, ts_now)
                return round(pct_change, 2)

        # Primer snapshot — guardar y retornar 0
        self._oi_cache[symbol] = (oi_now, ts_now)
        return 0.0

    async def get_long_short_ratio(self, symbol: str) -> dict:
        """
        Weex V3 no expone endpoint público de L/S ratio.
        Aproximamos desde el funding rate:
          funding > 0  → más longs que shorts → ratio > 1
          funding < 0  → más shorts que longs → ratio < 1
        """
        funding_data = await self.get_funding_rate(symbol)
        fr = funding_data.get("funding_rate", 0)

        # Mapeo heurístico: cada 0.01% de funding ≈ 0.1 de ratio
        ratio = 1.0 + (fr / 0.01) * 0.1
        ratio = max(0.3, min(3.0, ratio))   # clamp entre 0.3 y 3.0

        if ratio > 1.2:
            bias = "LONG"
        elif ratio < 0.8:
            bias = "SHORT"
        else:
            bias = "NEUTRAL"

        return {
            "symbol":           symbol,
            "long_short_ratio": round(ratio, 3),
            "bias":             bias,
            "note":             "Estimado desde Funding Rate (Weex no expone L/S ratio público)",
        }

    # ─── Método principal: todos los datos juntos ─────────────────────────────

    async def get_all_futures_market_data(self, symbol: str) -> dict:
        """
        Agrega todos los datos de futuros en un solo dict.
        Misma interfaz que BinanceFuturesClient.get_all_futures_market_data().
        """
        funding, oi_raw, ticker, lsr = await asyncio.gather(
            self.get_funding_rate(symbol),
            self.get_open_interest(symbol),
            self.get_ticker_24h(symbol),
            self.get_long_short_ratio(symbol),
            return_exceptions=True,
        )

        # Si alguna coroutine falló, usar dict vacío
        if isinstance(funding, Exception): funding = {}
        if isinstance(oi_raw, Exception):  oi_raw  = {}
        if isinstance(ticker, Exception):  ticker  = {}
        if isinstance(lsr, Exception):     lsr     = {}

        oi_change = await self.get_oi_change_1h(symbol)

        return {
            "pair":              symbol,
            "price":             float(ticker.get("lastPrice", 0)),
            "price_change_24h":  float(ticker.get("priceChangePercent", 0)),
            "volume_24h":        float(ticker.get("quoteVolume", 0)),
            "high_24h":          float(ticker.get("highPrice", 0)),
            "low_24h":           float(ticker.get("lowPrice", 0)),
            "funding_rate":      funding.get("funding_rate", 0),
            "forecast_rate":     funding.get("forecast_rate", 0),
            "mark_price":        funding.get("mark_price", 0),
            "index_price":       funding.get("index_price", 0),
            "open_interest":     oi_raw.get("open_interest", 0),
            "oi_change_1h":      oi_change,
            "long_short_ratio":  lsr.get("long_short_ratio", 1.0),
            "market_bias":       lsr.get("bias", "NEUTRAL"),
        }

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ─── Instancia global (reemplaza a `binance` en todo el proyecto) ─────────────

weex = WeexFuturesClient()

# Alias para compatibilidad con el resto del código que importa `binance`
binance = weex
