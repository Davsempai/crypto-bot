"""
config.py — Configuración central del bot
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    AUTHORIZED_USERS: list[int] = [
        int(uid.strip())
        for uid in os.getenv("AUTHORIZED_USERS", "").split(",")
        if uid.strip().isdigit()
    ]
    SIGNAL_CHANNEL_ID: int = int(os.getenv("SIGNAL_CHANNEL_ID", "0") or "0")

    # ── Exchange ───────────────────────────────────────────────────────────────
    # Cambia aquí el exchange: "weex" o "binance"
    EXCHANGE: str = os.getenv("EXCHANGE", "weex").lower()

    # Weex (exchange principal)
    WEEX_API_KEY:    str = os.getenv("WEEX_API_KEY", "")
    WEEX_SECRET_KEY: str = os.getenv("WEEX_SECRET_KEY", "")
    WEEX_PASSPHRASE: str = os.getenv("WEEX_PASSPHRASE", "")

    # Binance (alternativo)
    BINANCE_API_KEY:    str = os.getenv("BINANCE_API_KEY", "")
    BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")
    USE_TESTNET: bool = os.getenv("USE_TESTNET", "false").lower() == "true"
    BINANCE_BASE_URL: str = (
        "https://testnet.binancefuture.com"
        if os.getenv("USE_TESTNET", "false").lower() == "true"
        else "https://fapi.binance.com"
    )

    # ── Anthropic ─────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # ── CryptoPanic ───────────────────────────────────────────────────────────

    # ── Trading ───────────────────────────────────────────────────────────────
    TRADING_PAIRS: list[str] = [
        p.strip().upper()
        for p in os.getenv("TRADING_PAIRS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
        if p.strip()
    ]
    CHECK_INTERVAL: int   = int(os.getenv("CHECK_INTERVAL", "60"))
    FUNDING_RATE_THRESHOLD: float = float(os.getenv("FUNDING_RATE_THRESHOLD", "0.05"))
    OI_CHANGE_THRESHOLD: float    = float(os.getenv("OI_CHANGE_THRESHOLD", "5.0"))
    MAX_RISK_PER_TRADE: float     = float(os.getenv("MAX_RISK_PER_TRADE", "1.0"))
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"

    # ── Sistema ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DB_PATH: str   = "data/bot_database.db"

    # ── Análisis técnico ──────────────────────────────────────────────────────
    TIMEFRAMES: list[str]  = ["5m", "15m", "1h", "4h"]
    RSI_PERIOD: int        = 14
    RSI_OVERBOUGHT: float  = 70.0
    RSI_OVERSOLD: float    = 30.0
    CANDLES_LIMIT: int     = 200

    def validate(self) -> list[str]:
        errors = []
        if not self.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN no configurado")

        if self.EXCHANGE == "weex":
            if not self.WEEX_API_KEY:
                errors.append("WEEX_API_KEY no configurado")
            if not self.WEEX_SECRET_KEY:
                errors.append("WEEX_SECRET_KEY no configurado")
            if not self.WEEX_PASSPHRASE:
                errors.append("WEEX_PASSPHRASE no configurado")
        else:
            if not self.BINANCE_API_KEY:
                errors.append("BINANCE_API_KEY no configurado")

        if not self.ANTHROPIC_API_KEY:
            errors.append("ANTHROPIC_API_KEY no configurado (análisis macro desactivado)")
        return errors

    @property
    def exchange_name(self) -> str:
        return "Weex" if self.EXCHANGE == "weex" else "Binance"


config = Config()
