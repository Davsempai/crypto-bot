"""
utils/database.py — Base de datos SQLite para track record y estado
"""
import aiosqlite
import json
from datetime import datetime
from utils.logger import setup_logger

logger = setup_logger("database")
DB_PATH = "data/bot_database.db"


async def init_db():
    """Inicializa las tablas de la base de datos."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                pair        TEXT NOT NULL,
                direction   TEXT NOT NULL,       -- LONG / SHORT
                entry_low   REAL NOT NULL,
                entry_high  REAL NOT NULL,
                stop_loss   REAL NOT NULL,
                tp1         REAL NOT NULL,
                tp2         REAL NOT NULL,
                rr_ratio    REAL NOT NULL,
                timeframe   TEXT NOT NULL,
                reason      TEXT,                -- JSON con confluencias
                status      TEXT DEFAULT 'OPEN', -- OPEN/TP1/TP2/SL/CANCELLED
                pnl_r       REAL DEFAULT 0,      -- resultado en R
                closed_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS market_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                pair            TEXT NOT NULL,
                price           REAL,
                funding_rate    REAL,
                open_interest   REAL,
                oi_change_1h    REAL,
                volume_24h      REAL,
                long_liq_1h     REAL,
                short_liq_1h    REAL
            );

            CREATE TABLE IF NOT EXISTS news_alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                title       TEXT NOT NULL,
                url         TEXT,
                sentiment   TEXT,   -- BULLISH / BEARISH / NEUTRAL
                impact      TEXT,   -- HIGH / MEDIUM / LOW
                ai_analysis TEXT,
                sent_to_tg  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bot_stats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                total_signals   INTEGER DEFAULT 0,
                tp1_hit         INTEGER DEFAULT 0,
                tp2_hit         INTEGER DEFAULT 0,
                sl_hit          INTEGER DEFAULT 0,
                total_r         REAL DEFAULT 0,
                updated_at      TEXT
            );
        """)
        # Inicializar stats si no existen
        await db.execute(
            "INSERT OR IGNORE INTO bot_stats (id, updated_at) VALUES (1, ?)",
            (datetime.utcnow().isoformat(),)
        )
        await db.commit()
    logger.info("Base de datos inicializada correctamente")


async def save_signal(signal: dict) -> int:
    """Guarda una señal y retorna su ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO signals 
            (timestamp, pair, direction, entry_low, entry_high, stop_loss, tp1, tp2, rr_ratio, timeframe, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            signal["pair"],
            signal["direction"],
            signal["entry_low"],
            signal["entry_high"],
            signal["stop_loss"],
            signal["tp1"],
            signal["tp2"],
            signal["rr_ratio"],
            signal["timeframe"],
            json.dumps(signal.get("reason", {})),
        ))
        await db.commit()
        # Actualizar stats
        await db.execute(
            "UPDATE bot_stats SET total_signals = total_signals + 1, updated_at = ? WHERE id = 1",
            (datetime.utcnow().isoformat(),)
        )
        await db.commit()
        logger.info(f"Señal guardada ID={cursor.lastrowid} — {signal['pair']} {signal['direction']}")
        return cursor.lastrowid


async def save_market_snapshot(data: dict):
    """Guarda snapshot del mercado."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO market_snapshots 
            (timestamp, pair, price, funding_rate, open_interest, oi_change_1h, volume_24h, long_liq_1h, short_liq_1h)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            data.get("pair"),
            data.get("price"),
            data.get("funding_rate"),
            data.get("open_interest"),
            data.get("oi_change_1h"),
            data.get("volume_24h"),
            data.get("long_liq_1h", 0),
            data.get("short_liq_1h", 0),
        ))
        await db.commit()


async def save_news_alert(news: dict) -> int:
    """Guarda una noticia analizada."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO news_alerts (timestamp, title, url, sentiment, impact, ai_analysis)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            news.get("title", ""),
            news.get("url", ""),
            news.get("sentiment", "NEUTRAL"),
            news.get("impact", "LOW"),
            news.get("ai_analysis", ""),
        ))
        await db.commit()
        return cursor.lastrowid


async def get_stats() -> dict:
    """Obtiene estadísticas generales del bot."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM bot_stats WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            if not row:
                return {}
            stats = dict(row)

        # Win rate
        total = stats["tp1_hit"] + stats["tp2_hit"] + stats["sl_hit"]
        wins = stats["tp1_hit"] + stats["tp2_hit"]
        stats["win_rate"] = round((wins / total * 100), 1) if total > 0 else 0
        stats["total_closed"] = total

        # Últimas 5 señales
        async with db.execute(
            "SELECT pair, direction, status, pnl_r FROM signals ORDER BY id DESC LIMIT 5"
        ) as cursor:
            rows = await cursor.fetchall()
            stats["recent_signals"] = [dict(r) for r in rows]

        return stats


async def get_open_signals() -> list:
    """Retorna señales abiertas."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM signals WHERE status = 'OPEN' ORDER BY timestamp DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def close_signal(signal_id: int, status: str, pnl_r: float):
    """Cierra una señal con su resultado."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE signals SET status = ?, pnl_r = ?, closed_at = ? WHERE id = ?",
            (status, pnl_r, datetime.utcnow().isoformat(), signal_id)
        )
        # Actualizar stats
        col_map = {"TP1": "tp1_hit", "TP2": "tp2_hit", "SL": "sl_hit"}
        if status in col_map:
            await db.execute(
                f"UPDATE bot_stats SET {col_map[status]} = {col_map[status]} + 1, "
                f"total_r = total_r + ?, updated_at = ? WHERE id = 1",
                (pnl_r, datetime.utcnow().isoformat())
            )
        await db.commit()
        logger.info(f"Señal {signal_id} cerrada: {status} | PnL: {pnl_r:+.2f}R")
