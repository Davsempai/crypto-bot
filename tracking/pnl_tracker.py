"""
tracking/pnl_tracker.py — Seguimiento de P&L diario, semanal y mensual

Funciones:
  - Registrar resultado de cada operación cerrada
  - Calcular P&L diario actualizado
  - Resumen mensual con capital simulado
  - Proyección: "si hubieras operado con $X este mes..."
"""
import aiosqlite
import json
from datetime import datetime, timezone, timedelta, date
from dataclasses import dataclass, field
from typing import Optional
from utils.logger import setup_logger
from config import config

logger = setup_logger("pnl_tracker")
DB_PATH = "data/bot_database.db"


@dataclass
class DailyPnL:
    """P&L de un día específico."""
    date: str
    trades_count: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_r: float = 0.0
    win_rate: float = 0.0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    pairs_traded: list[str] = field(default_factory=list)


@dataclass
class MonthlyReport:
    """Reporte mensual completo."""
    month: str          # YYYY-MM
    month_name: str     # Enero 2025

    # Operaciones
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    tp1_count: int = 0
    tp2_count: int = 0
    sl_count: int = 0

    # Rendimiento en R
    total_r: float = 0.0
    avg_r_per_trade: float = 0.0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0

    # Capital simulado
    starting_capital: float = 0.0
    ending_capital: float = 0.0
    total_profit_usd: float = 0.0
    total_profit_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0

    # Días
    profitable_days: int = 0
    losing_days: int = 0
    best_day_r: float = 0.0
    worst_day_r: float = 0.0

    # Detalle diario
    daily_breakdown: list[DailyPnL] = field(default_factory=list)


MONTH_NAMES_ES = {
    "01": "Enero", "02": "Febrero", "03": "Marzo",
    "04": "Abril", "05": "Mayo", "06": "Junio",
    "07": "Julio", "08": "Agosto", "09": "Septiembre",
    "10": "Octubre", "11": "Noviembre", "12": "Diciembre",
}


class PnLTracker:
    """Calcula y reporta el P&L del bot."""

    async def ensure_pnl_table(self):
        """Asegura que la tabla de P&L diario existe."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS daily_pnl (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    date        TEXT NOT NULL UNIQUE,
                    trades      INTEGER DEFAULT 0,
                    wins        INTEGER DEFAULT 0,
                    losses      INTEGER DEFAULT 0,
                    total_r     REAL DEFAULT 0,
                    details     TEXT       -- JSON con detalle de trades
                )
            """)
            await db.commit()

    async def get_closed_trades(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> list[dict]:
        """Obtiene trades cerrados en un rango de fechas."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM signals WHERE status != 'OPEN'"
            params = []
            if from_date:
                query += " AND timestamp >= ?"
                params.append(from_date)
            if to_date:
                query += " AND timestamp <= ?"
                params.append(to_date)
            query += " ORDER BY timestamp ASC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_monthly_report(
        self,
        year: int,
        month: int,
        capital: float = 1000.0,
        risk_pct: float = None,
    ) -> MonthlyReport:
        """
        Genera el reporte mensual completo.

        Args:
            year: Año (ej: 2025)
            month: Mes (1-12)
            capital: Capital inicial simulado en USD
            risk_pct: % de riesgo por operación (usa config si no se especifica)
        """
        risk_pct = risk_pct or config.MAX_RISK_PER_TRADE
        month_str = f"{year}-{month:02d}"
        from_dt = f"{year}-{month:02d}-01"
        # Último día del mes
        if month == 12:
            to_dt = f"{year+1}-01-01"
        else:
            to_dt = f"{year}-{month+1:02d}-01"

        trades = await self.get_closed_trades(from_dt, to_dt)

        month_name = f"{MONTH_NAMES_ES.get(f'{month:02d}', '')} {year}"
        report = MonthlyReport(
            month=month_str,
            month_name=month_name,
            starting_capital=capital,
        )

        if not trades:
            report.ending_capital = capital
            return report

        # Agrupar por día
        days: dict[str, list[dict]] = {}
        for t in trades:
            day = t["timestamp"][:10]
            days.setdefault(day, []).append(t)

        # Métricas globales
        total_r_list = [t["pnl_r"] for t in trades]
        wins = [r for r in total_r_list if r > 0]
        losses = [abs(r) for r in total_r_list if r < 0]

        report.total_trades = len(trades)
        report.winning_trades = len(wins)
        report.losing_trades = len(losses)
        report.tp1_count = sum(1 for t in trades if t["status"] == "TP1")
        report.tp2_count = sum(1 for t in trades if t["status"] == "TP2")
        report.sl_count = sum(1 for t in trades if t["status"] == "SL")
        report.total_r = round(sum(total_r_list), 3)
        report.avg_r_per_trade = round(report.total_r / report.total_trades, 3)
        report.best_trade_r = round(max(total_r_list), 2) if total_r_list else 0
        report.worst_trade_r = round(min(total_r_list), 2) if total_r_list else 0
        report.win_rate = round(len(wins) / report.total_trades * 100, 1)
        report.profit_factor = round(sum(wins) / sum(losses), 2) if losses else float("inf")

        # Capital simulado: aplicar cada trade secuencialmente
        running_capital = capital
        peak_capital = capital
        max_dd_usd = 0.0

        for t in trades:
            risk_amount = running_capital * (risk_pct / 100)
            trade_pnl = t["pnl_r"] * risk_amount
            running_capital += trade_pnl
            peak_capital = max(peak_capital, running_capital)
            dd = peak_capital - running_capital
            max_dd_usd = max(max_dd_usd, dd)

        report.ending_capital = round(running_capital, 2)
        report.total_profit_usd = round(running_capital - capital, 2)
        report.total_profit_pct = round((running_capital - capital) / capital * 100, 2)
        report.max_drawdown_usd = round(max_dd_usd, 2)
        report.max_drawdown_pct = round(max_dd_usd / peak_capital * 100, 2) if peak_capital else 0

        # Breakdown diario
        daily_list = []
        for day_str, day_trades in sorted(days.items()):
            day_r_list = [t["pnl_r"] for t in day_trades]
            daily = DailyPnL(
                date=day_str,
                trades_count=len(day_trades),
                winning_trades=len([r for r in day_r_list if r > 0]),
                losing_trades=len([r for r in day_r_list if r < 0]),
                total_r=round(sum(day_r_list), 3),
                win_rate=round(len([r for r in day_r_list if r > 0]) / len(day_r_list) * 100, 0),
                best_trade_r=round(max(day_r_list), 2),
                worst_trade_r=round(min(day_r_list), 2),
                pairs_traded=list(set(t["pair"] for t in day_trades)),
            )
            daily_list.append(daily)

        report.daily_breakdown = daily_list
        profitable_days = [d for d in daily_list if d.total_r > 0]
        losing_days = [d for d in daily_list if d.total_r < 0]
        report.profitable_days = len(profitable_days)
        report.losing_days = len(losing_days)
        report.best_day_r = round(max((d.total_r for d in daily_list), default=0), 2)
        report.worst_day_r = round(min((d.total_r for d in daily_list), default=0), 2)

        return report

    async def get_daily_update(self, capital: float = 1000.0) -> dict:
        """P&L del día de hoy para actualización diaria automática."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

        trades = await self.get_closed_trades(today, tomorrow)

        # Acumulado del mes
        now = datetime.now(timezone.utc)
        month_trades = await self.get_closed_trades(
            f"{now.year}-{now.month:02d}-01", tomorrow
        )

        today_r = sum(t["pnl_r"] for t in trades)
        month_r = sum(t["pnl_r"] for t in month_trades)

        # Capital
        month_risk = capital * config.MAX_RISK_PER_TRADE / 100

        # Simular mes completo
        cap = capital
        for t in month_trades:
            cap += t["pnl_r"] * (cap * config.MAX_RISK_PER_TRADE / 100)
        month_profit_usd = round(cap - capital, 2)
        month_profit_pct = round((cap - capital) / capital * 100, 2)

        today_profit_usd = round(today_r * month_risk, 2)

        return {
            "date": today,
            "today_trades": len(trades),
            "today_r": round(today_r, 3),
            "today_profit_usd": today_profit_usd,
            "month_trades": len(month_trades),
            "month_r": round(month_r, 3),
            "month_profit_usd": month_profit_usd,
            "month_profit_pct": month_profit_pct,
            "month_wins": len([t for t in month_trades if t["pnl_r"] > 0]),
            "month_losses": len([t for t in month_trades if t["pnl_r"] < 0]),
            "current_capital": round(cap, 2),
        }

    async def simulate_with_capital(
        self,
        capital: float,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        risk_pct: Optional[float] = None,
    ) -> dict:
        """
        Simula el rendimiento con un capital específico en un rango de fechas.
        Retorna un dict con resultado detallado.
        """
        risk_pct = risk_pct or config.MAX_RISK_PER_TRADE
        trades = await self.get_closed_trades(from_date, to_date)

        if not trades:
            return {
                "capital": capital,
                "risk_pct": risk_pct,
                "total_trades": 0,
                "error": "Sin operaciones en el período seleccionado",
            }

        running = capital
        peak = capital
        max_dd = 0.0
        equity_curve = [capital]

        for t in trades:
            risk_amount = running * (risk_pct / 100)
            pnl = t["pnl_r"] * risk_amount
            running += pnl
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)
            equity_curve.append(round(running, 2))

        wins = [t for t in trades if t["pnl_r"] > 0]
        losses = [t for t in trades if t["pnl_r"] < 0]
        total_r = sum(t["pnl_r"] for t in trades)

        return {
            "capital_inicial": capital,
            "capital_final": round(running, 2),
            "ganancia_usd": round(running - capital, 2),
            "ganancia_pct": round((running - capital) / capital * 100, 2),
            "risk_pct": risk_pct,
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_r": round(total_r, 3),
            "avg_r": round(total_r / len(trades), 3),
            "max_drawdown_usd": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd / peak * 100, 2) if peak else 0,
            "capital_peak": round(peak, 2),
            "equity_curve": equity_curve,
            "from_date": from_date or trades[0]["timestamp"][:10],
            "to_date": to_date or trades[-1]["timestamp"][:10],
        }


def format_monthly_report_telegram(report: MonthlyReport, show_daily: bool = True) -> str:
    """Formatea el reporte mensual para Telegram."""
    pnl_emoji = "📈" if report.total_profit_usd >= 0 else "📉"
    result_emoji = "🟢" if report.total_profit_usd >= 0 else "🔴"

    # Header
    text = f"""📅 *REPORTE MENSUAL — {report.month_name.upper()}*
━━━━━━━━━━━━━━━━━━━━━━━━
*📊 Operaciones:*
  Total: `{report.total_trades}` | ✅ TP1: `{report.tp1_count}` | 🏆 TP2: `{report.tp2_count}` | ❌ SL: `{report.sl_count}`
  Win Rate: `{report.win_rate}%` | Profit Factor: `{report.profit_factor}`

*📐 Rendimiento en R:*
  Total R: `{report.total_r:+.2f}R`
  Promedio/trade: `{report.avg_r_per_trade:+.3f}R`
  Mejor trade: `{report.best_trade_r:+.2f}R` | Peor: `{report.worst_trade_r:+.2f}R`

*💰 Capital simulado* (`{config.MAX_RISK_PER_TRADE}% riesgo/trade`):
  Inicio: `${report.starting_capital:,.2f}`
  {result_emoji} Final: `${report.ending_capital:,.2f}`
  {pnl_emoji} P&L: `${report.total_profit_usd:+,.2f}` (`{report.total_profit_pct:+.2f}%`)
  📉 Max Drawdown: `${report.max_drawdown_usd:,.2f}` (`{report.max_drawdown_pct:.1f}%`)

*📆 Días:*
  🟢 Rentables: `{report.profitable_days}` | 🔴 En pérdida: `{report.losing_days}`
  Mejor día: `{report.best_day_r:+.2f}R` | Peor día: `{report.worst_day_r:+.2f}R`"""

    if show_daily and report.daily_breakdown:
        text += "\n\n*📋 Detalle diario:*\n```"
        text += f"\n{'Día':<12} {'Trades':>6} {'R':>7} {'W%':>5}"
        text += f"\n{'-'*34}"
        for d in report.daily_breakdown:
            emoji = "+" if d.total_r >= 0 else "-"
            text += f"\n{d.date[5:]:<12} {d.trades_count:>6} {d.total_r:>+7.2f} {d.win_rate:>4.0f}%"
        text += "\n```"

    return text


def format_capital_simulation(sim: dict) -> str:
    """Formatea la simulación de capital para Telegram."""
    if sim.get("error"):
        return f"❌ {sim['error']}"

    gain = sim["ganancia_usd"]
    pct = sim["ganancia_pct"]
    result_emoji = "📈" if gain >= 0 else "📉"
    result_word = "GANANCIA" if gain >= 0 else "PÉRDIDA"

    return f"""💰 *SIMULACIÓN DE CAPITAL*
━━━━━━━━━━━━━━━━━━━━━━━━
📅 Período: `{sim['from_date']}` → `{sim['to_date']}`
⚖️ Riesgo por trade: `{sim['risk_pct']}%`

*Capital:*
  💵 Inicial: `${sim['capital_inicial']:,.2f}`
  💰 Final: `${sim['capital_final']:,.2f}`
  🏔 Pico: `${sim['capital_peak']:,.2f}`

{result_emoji} *{result_word}: `${abs(gain):,.2f}` (`{pct:+.2f}%`)*

*Operaciones:*
  Total: `{sim['total_trades']}` | ✅ Wins: `{sim['wins']}` | ❌ Losses: `{sim['losses']}`
  Win Rate: `{sim['win_rate']}%` | Total R: `{sim['total_r']:+.2f}R`
  Promedio R/trade: `{sim['avg_r']:+.3f}R`

*Riesgo:*
  📉 Max Drawdown: `${sim['max_drawdown_usd']:,.2f}` (`{sim['max_drawdown_pct']:.1f}%`)

_Simulación basada en el historial real del bot con {sim['risk_pct']}% de riesgo compuesto._"""


def format_daily_update(data: dict) -> str:
    """Formato del resumen diario automático."""
    today_emoji = "📈" if data["today_r"] >= 0 else "📉"
    month_emoji = "📈" if data["month_r"] >= 0 else "📉"

    return f"""🔔 *UPDATE DIARIO — {data['date']}*
━━━━━━━━━━━━━━━━━━━━━━━━
{today_emoji} *Hoy:*
  Trades: `{data['today_trades']}` | R: `{data['today_r']:+.2f}R`
  P&L: `${data['today_profit_usd']:+,.2f}`

{month_emoji} *Mes acumulado:*
  Trades: `{data['month_trades']}` (✅`{data['month_wins']}` / ❌`{data['month_losses']}`)
  Total R: `{data['month_r']:+.2f}R`
  P&L: `${data['month_profit_usd']:+,.2f}` (`{data['month_profit_pct']:+.2f}%`)
  Capital actual: `${data['current_capital']:,.2f}`

⏰ `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC`"""


pnl_tracker = PnLTracker()
