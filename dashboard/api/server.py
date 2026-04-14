"""
dashboard/api/server.py — FastAPI backend del dashboard

Endpoints REST:
  GET  /api/stats          → Estadísticas generales
  GET  /api/signals        → Señales abiertas y cerradas
  GET  /api/equity         → Curva de equity histórica
  GET  /api/market         → Datos de mercado en tiempo real
  GET  /api/pnl/daily      → P&L diario (últimos 30 días)
  GET  /api/pnl/monthly    → P&L mensual (últimos 12 meses)
  POST /api/simulate       → Simulación con capital personalizado
  GET  /api/backtest/{pair}/{tf} → Correr backtest
  WS   /ws/live            → WebSocket para datos en tiempo real
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

# Asegurar que el root del proyecto esté en el path
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import aiosqlite

from config import config
from market.exchange import exchange as binance
from analysis.multi_timeframe import get_mtf_engine
from analysis.signal_filter import signal_filter
from backtest.engine import BacktestEngine
from backtest.yearly import get_yearly_engine
from tracking.pnl_tracker import pnl_tracker
from utils.logger import setup_logger

logger = setup_logger("dashboard")

DB_PATH = "data/bot_database.db"

app = FastAPI(title="Crypto Futures Bot Dashboard", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

backtest_engine = BacktestEngine(binance)
yearly_engine = get_yearly_engine(binance)
mtf = get_mtf_engine(binance)

# ─── WebSocket manager ────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info(f"WebSocket conectado. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ─── Modelos ──────────────────────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    capital: float = 1000.0
    risk_pct: float = 1.0
    from_date: Optional[str] = None
    to_date: Optional[str] = None


class BacktestRequest(BaseModel):
    pair: str = "BTCUSDT"
    timeframe: str = "1h"
    capital: float = 1000.0
    risk_pct: float = 1.0
    yearly: bool = False


# ─── HTML Principal ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ─── API REST ─────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Stats generales
        async with db.execute("SELECT * FROM bot_stats WHERE id=1") as cur:
            row = await cur.fetchone()
            stats = dict(row) if row else {}

        total = (stats.get("tp1_hit", 0) + stats.get("tp2_hit", 0) + stats.get("sl_hit", 0))
        wins = stats.get("tp1_hit", 0) + stats.get("tp2_hit", 0)
        stats["win_rate"] = round(wins / total * 100, 1) if total else 0
        stats["total_closed"] = total

        # Últimas 10 señales
        async with db.execute(
            "SELECT id, pair, direction, status, pnl_r, timeframe, timestamp FROM signals ORDER BY id DESC LIMIT 10"
        ) as cur:
            rows = await cur.fetchall()
            stats["recent_signals"] = [dict(r) for r in rows]

        # Señales abiertas
        async with db.execute("SELECT COUNT(*) as cnt FROM signals WHERE status='OPEN'") as cur:
            row = await cur.fetchone()
            stats["open_signals"] = row["cnt"] if row else 0

        # Total R acumulado
        async with db.execute("SELECT SUM(pnl_r) as total FROM signals WHERE status != 'OPEN'") as cur:
            row = await cur.fetchone()
            stats["total_r"] = round(row["total"] or 0, 2)

    return stats


@app.get("/api/signals")
async def get_signals(limit: int = 50, status: str = "ALL"):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status == "ALL":
            query = "SELECT * FROM signals ORDER BY id DESC LIMIT ?"
            params = (limit,)
        else:
            query = "SELECT * FROM signals WHERE status=? ORDER BY id DESC LIMIT ?"
            params = (status, limit)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            signals = []
            for r in rows:
                s = dict(r)
                try:
                    s["reason"] = json.loads(s.get("reason") or "{}")
                except Exception:
                    s["reason"] = {}
                signals.append(s)
    return {"signals": signals, "total": len(signals)}


@app.get("/api/equity")
async def get_equity(capital: float = 1000.0, risk_pct: float = 1.0):
    """Curva de equity del historial real del bot."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pnl_r, timestamp, pair, direction, status FROM signals WHERE status != 'OPEN' ORDER BY timestamp ASC"
        ) as cur:
            trades = [dict(r) for r in await cur.fetchall()]

    equity = [capital]
    labels = ["Inicio"]
    running = capital
    peak = capital
    max_dd = 0.0
    drawdown_series = [0.0]

    for t in trades:
        risk_amount = running * (risk_pct / 100)
        running += t["pnl_r"] * risk_amount
        peak = max(peak, running)
        dd = (peak - running) / peak * 100 if peak else 0
        max_dd = max(max_dd, dd)
        equity.append(round(running, 2))
        drawdown_series.append(round(-dd, 2))
        labels.append(t["timestamp"][:10])

    return {
        "equity": equity,
        "drawdown": drawdown_series,
        "labels": labels,
        "initial_capital": capital,
        "final_capital": round(running, 2),
        "peak_capital": round(peak, 2),
        "total_profit_usd": round(running - capital, 2),
        "total_profit_pct": round((running - capital) / capital * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_trades": len(trades),
    }


@app.get("/api/market")
async def get_market():
    """Snapshot de mercado en tiempo real."""
    results = []
    for pair in config.TRADING_PAIRS:
        try:
            data = await binance.get_all_futures_market_data(pair)
            results.append(data)
        except Exception as e:
            logger.error(f"Error fetching {pair}: {e}")
    return {"pairs": results, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/pnl/daily")
async def get_daily_pnl(days: int = 30, capital: float = 1000.0):
    """P&L de los últimos N días."""
    from_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    trades = await pnl_tracker.get_closed_trades(from_date)

    # Agrupar por día
    days_map: dict[str, list] = {}
    for t in trades:
        day = t["timestamp"][:10]
        days_map.setdefault(day, []).append(t)

    # Rellenar días sin trades
    result = []
    running_capital = capital

    # Calcular capital hasta el inicio del período
    all_trades = await pnl_tracker.get_closed_trades(None, from_date)
    for t in all_trades:
        running_capital += t["pnl_r"] * running_capital * config.MAX_RISK_PER_TRADE / 100

    current = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.now(timezone.utc)
    while current <= end:
        day_str = current.strftime("%Y-%m-%d")
        day_trades = days_map.get(day_str, [])
        day_r = sum(t["pnl_r"] for t in day_trades)
        day_pnl_usd = day_r * running_capital * config.MAX_RISK_PER_TRADE / 100

        for t in day_trades:
            running_capital += t["pnl_r"] * running_capital * config.MAX_RISK_PER_TRADE / 100

        result.append({
            "date": day_str,
            "trades": len(day_trades),
            "total_r": round(day_r, 3),
            "pnl_usd": round(day_pnl_usd, 2),
            "wins": len([t for t in day_trades if t["pnl_r"] > 0]),
            "losses": len([t for t in day_trades if t["pnl_r"] < 0]),
            "capital_end": round(running_capital, 2),
        })
        current += timedelta(days=1)

    return {"days": result, "capital_initial": capital, "capital_final": round(running_capital, 2)}


@app.get("/api/pnl/monthly")
async def get_monthly_pnl(months: int = 12, capital: float = 1000.0):
    """P&L de los últimos N meses."""
    results = []
    now = datetime.now(timezone.utc)
    running = capital

    for i in range(months - 1, -1, -1):
        target = now - timedelta(days=i * 30)
        year, month = target.year, target.month
        report = await pnl_tracker.get_monthly_report(year, month, running)
        results.append({
            "month": report.month,
            "month_name": report.month_name,
            "trades": report.total_trades,
            "win_rate": report.win_rate,
            "total_r": report.total_r,
            "pnl_usd": report.total_profit_usd,
            "pnl_pct": report.total_profit_pct,
            "starting_capital": report.starting_capital,
            "ending_capital": report.ending_capital,
            "max_drawdown_pct": report.max_drawdown_pct,
        })
        if report.ending_capital > 0:
            running = report.ending_capital

    return {"months": results}


@app.post("/api/simulate")
async def simulate_capital(req: SimulateRequest):
    """Simulación de capital con los trades reales del bot."""
    result = await pnl_tracker.simulate_with_capital(
        req.capital, req.from_date, req.to_date, req.risk_pct
    )
    return result


@app.post("/api/backtest")
async def run_backtest(req: BacktestRequest):
    """Ejecuta backtest y retorna resultados completos."""
    try:
        if req.yearly:
            result = await yearly_engine.run_yearly(
                req.pair, req.timeframe,
                capital=req.capital, risk_pct=req.risk_pct,
            )
            return {
                "pair": result.pair,
                "timeframe": result.timeframe,
                "total_trades": result.total_trades,
                "win_rate": result.win_rate,
                "total_r": result.total_r,
                "profit_factor": result.profit_factor,
                "expected_value": result.expected_value,
                "sharpe_ratio": result.sharpe_ratio,
                "max_drawdown_pct": result.max_drawdown_pct,
                "initial_capital": result.initial_capital,
                "final_capital": result.final_capital,
                "total_profit_usd": result.total_profit_usd,
                "total_profit_pct": result.total_profit_pct,
                "equity_curve": result.equity_curve,
                "monthly_results": [
                    {
                        "month": m.month,
                        "trades": m.trades,
                        "total_r": m.total_r,
                        "win_rate": m.win_rate,
                        "pnl_usd": m.monthly_pnl_usd,
                        "pnl_pct": m.monthly_pnl_pct,
                    }
                    for m in result.monthly_results
                ],
                "rating": result.rating,
            }
        else:
            result = await backtest_engine.run(req.pair, req.timeframe, periods=500)
            return {
                "pair": result.pair,
                "timeframe": result.timeframe,
                "total_trades": result.total_trades,
                "win_rate": result.win_rate,
                "total_r": result.total_r,
                "profit_factor": result.profit_factor,
                "expected_value": result.expected_value,
                "sharpe_ratio": result.sharpe_ratio,
                "max_drawdown_pct": result.max_drawdown_r,
                "initial_capital": req.capital,
                "final_capital": result.final_capital,
                "total_profit_usd": result.final_capital - req.capital,
                "total_profit_pct": (result.final_capital - req.capital) / req.capital * 100,
                "equity_curve": result.equity_curve,
                "monthly_results": [],
                "rating": "Ver reporte completo",
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    """Endpoint de salud — usado por UptimeRobot para mantener el servicio activo."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/news")
async def get_news(limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM news_alerts ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return {"news": [dict(r) for r in rows]}


@app.get("/api/log")
async def get_log(lines: int = 100, level: str = "ALL"):
    """
    Retorna las últimas N líneas del log.
    level: ALL | ERROR | WARNING | INFO
    """
    from utils.log_reader import read_last_lines, filter_errors, get_summary
    raw = read_last_lines(min(lines, 500))
    if level == "ERROR":
        raw = [l for l in raw if "[ERROR]" in l or "[WARNING]" in l]
    elif level == "WARNING":
        raw = [l for l in raw if "[WARNING]" in l]
    elif level == "INFO":
        raw = [l for l in raw if "[INFO]" in l]
    return {
        "lines": raw,
        "total": len(raw),
        "level": level,
        "summary": get_summary(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/strategy")
async def get_strategy_info():
    """Información sobre la estrategia activa."""
    return {
        "version": "V2 — Tendencia + Pullback + Confirmación",
        "filters": [
            {"name": "ADX > 22", "description": "Solo mercados en tendencia, no laterales"},
            {"name": "EMAs alineadas", "description": "20>50>200 LONG / 20<50<200 SHORT"},
            {"name": "RSI 25-78", "description": "No perseguir extremos"},
            {"name": "Volumen > 0.6x", "description": "Mínimo de participación"},
        ],
        "entry": "FVG o Order Block dentro del 2% del precio actual",
        "stop_loss": "Último swing mínimo/máximo real (no ATR fijo)",
        "tp1": "2R — cierra 50% de la posición",
        "tp2": "4R — cierra el 50% restante",
        "min_rr": 2.0,
        "timeframes": config.TIMEFRAMES,
        "pairs": config.TRADING_PAIRS,
        "risk_per_trade": config.MAX_RISK_PER_TRADE,
        "paper_trading": config.PAPER_TRADING,
    }


# ─── WebSocket live feed ──────────────────────────────────────────────────────

@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    """Envía datos de mercado en tiempo real al dashboard."""
    await manager.connect(ws)
    try:
        while True:
            market_data = []
            for pair in config.TRADING_PAIRS[:4]:  # Máximo 4 para no saturar
                try:
                    data = await binance.get_all_futures_market_data(pair)
                    market_data.append(data)
                except Exception:
                    pass

            # Señales abiertas con P&L actual
            open_signals = []
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY id DESC") as cur:
                    rows = await cur.fetchall()
                    for r in rows:
                        s = dict(r)
                        # Calcular P&L en vivo
                        pair_price = next(
                            (d["price"] for d in market_data if d["pair"] == s["pair"]), 0
                        )
                        if pair_price:
                            entry = (s["entry_low"] + s["entry_high"]) / 2
                            sl = s["stop_loss"]
                            if s["direction"] == "LONG":
                                pnl_pct = (pair_price - entry) / entry * 100
                                risk = entry - sl
                            else:
                                pnl_pct = (entry - pair_price) / entry * 100
                                risk = sl - entry
                            pnl_r = pnl_pct / (risk / entry * 100) if risk else 0
                            s["live_price"] = pair_price
                            s["live_pnl_r"] = round(pnl_r, 3)
                            s["live_pnl_pct"] = round(pnl_pct, 3)
                        open_signals.append(s)

            await ws.send_json({
                "type": "market_update",
                "market": market_data,
                "open_signals": open_signals,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await asyncio.sleep(10)

    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(ws)


async def broadcast_signal_update(update_type: str, data: dict):
    """Broadcastea un update de señal a todos los clientes del dashboard."""
    await manager.broadcast({
        "type": "signal_update",
        "event": update_type,
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
