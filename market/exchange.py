"""
market/exchange.py — Selector de exchange

Importa este módulo en lugar de binance_client o weex_client directamente.
El cliente correcto se elige automáticamente según EXCHANGE en el .env.

Uso en cualquier parte del código:
    from market.exchange import exchange
    price = await exchange.get_price("BTCUSDT")
"""
from config import config
from utils.logger import setup_logger

logger = setup_logger("exchange")


def get_exchange_client():
    if config.EXCHANGE == "weex":
        from market.weex_client import WeexFuturesClient
        logger.info("🔌 Exchange: Weex Futures API V3")
        return WeexFuturesClient()
    else:
        from market.binance_client import BinanceFuturesClient
        logger.info("🔌 Exchange: Binance Futures API")
        return BinanceFuturesClient()


# Instancia global — usar esta en todo el proyecto
exchange = get_exchange_client()

# Alias para compatibilidad con código existente que importa `binance`
binance = exchange
