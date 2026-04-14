"""
backtest/yearly.py — Backtest anual con simulación de capital detallada

Analiza un año completo de datos y genera:
  - Reporte mensual desglosado
  - Curva de equity
  - Estadísticas por par y timeframe
  - Simulación con capital personalizado
"""
import asyncio
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backtest.engine import BacktestEngine, BacktestTrade
from analysis.technical import TechnicalAnalyzer
from typing import Any
from config import config
from utils.logger import setup_logger
from tracking.pnl_tracker import MONTH_NAMES_ES

logger = setup_logger("yearly_backtest")
analyzer = TechnicalAnalyzer()


@dataclass
class MonthlyBacktestResult:
    month: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    starting_capital: float = 0.0
    ending_capital: float = 0.0
    monthly_pnl_usd: float = 0.0
    monthly_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0


@dataclass
class YearlyBacktestResult:
    pair: str
    timeframe: str
    year: int
    total_candles: int = 0

    # Métricas anuales
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    tp1_count: int = 0
    tp2_count: int = 0
    sl_count: int = 0
    win_rate: float = 0.0
    total_r: float = 0.0
    avg_r_per_trade: float = 0.0
    profit_factor: float = 0.0
    expected_value: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_r: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0

    # Capital simulado
    initial_capital: float = 1000.0
    final_capital: float = 1000.0
    peak_capital: float = 1000.0
    total_profit_usd: float = 0.0
    total_profit_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    risk_pct_used: float = 1.0

    # Desglose mensual
    monthly_results: list[MonthlyBacktestResult] = field(default_factory=list)

    # Curva de equity
    equity_curve: list[float] = field(default_factory=list)
    all_trades: list[BacktestTrade] = field(default_factory=list)

    # Rating
    rating: str = ""
    rating_emoji: str = ""


class YearlyBacktestEngine:
    """Motor de backtesting anual con simulación de capital detallada."""

    MAX_CANDLES_PER_REQUEST = 1000   # Weex limita a 1000 por request
    VALID_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "2h", "4h", "12h", "1d", "1w"}

    def __init__(self, binance_client: Any):
        self.binance      = binance_client
        self.base_engine  = BacktestEngine(binance_client)

    async def _fetch_year_data(self, pair: str, timeframe: str, year: int) -> pd.DataFrame:
        """
        Descarga datos de un año completo paginando en múltiples requests.

        1H  → 8760 velas/año → ~9 requests de 1000
        4H  → 2190 velas/año → ~3 requests de 1000
        1D  →  365 velas/año → 1 request

        Para Weex usamos get_klines_history que pagina automáticamente.
        Para Binance usamos get_klines con el máximo disponible.
        """
        tf_minutes = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "2h": 120, "4h": 240, "12h": 720, "1d": 1440
        }
        minutes           = tf_minutes.get(timeframe, 60)
        candles_per_year  = int(365 * 24 * 60 / minutes)

        logger.info(
            f"Descargando ~{candles_per_year} velas de {pair} {timeframe} "
            f"(1 año ≈ {candles_per_year} velas de {timeframe})..."
        )

        # Weex: usar historyKlines con paginación automática (múltiples requests)
        if hasattr(self.binance, "get_klines_history"):
            df = await self.binance.get_klines_history(pair, timeframe, candles_per_year)
        else:
            # Binance: pedir el máximo permitido
            df = await self.binance.get_klines(pair, timeframe, min(candles_per_year, 1500))

        if df.empty:
            logger.error(f"Sin datos para {pair} {timeframe}")
            return pd.DataFrame()

        logger.info(f"Datos descargados: {len(df)} velas ({df.index[0]} → {df.index[-1]})")
        df = analyzer.add_indicators(df)
        return df

    async def run_yearly(
        self,
        pair: str,
        timeframe: str = "1h",
        year: Optional[int] = None,
        capital: float = 1000.0,
        risk_pct: Optional[float] = None,
    ) -> YearlyBacktestResult:
        """
        Ejecuta backtest de un año completo con simulación de capital.

        Args:
            pair: Par de trading
            timeframe: Timeframe a testear
            year: Año (None = usa datos disponibles)
            capital: Capital inicial en USD
            risk_pct: % de riesgo por trade
        """
        risk_pct = risk_pct or config.MAX_RISK_PER_TRADE
        year     = year or datetime.now().year - 1

        # Sanear timeframe — si viene algo raro usar 1h por defecto
        if timeframe not in self.VALID_TIMEFRAMES:
            logger.warning(f"Timeframe inválido '{timeframe}' → usando 1h")
            timeframe = "1h"

        result = YearlyBacktestResult(
            pair=pair,
            timeframe=timeframe,
            year=year,
            initial_capital=capital,
            final_capital=capital,
            risk_pct_used=risk_pct,
        )

        # ── Descargar datos ──
        df = await self._fetch_year_data(pair, timeframe, year)
        if df.empty or len(df) < 200:
            logger.error(f"Datos insuficientes para backtest anual de {pair}")
            return result

        result.total_candles = len(df)

        # ── Simular trades (en executor para no bloquear el event loop) ──
        import asyncio, functools
        loop = asyncio.get_event_loop()
        trades = await loop.run_in_executor(
            None,
            functools.partial(self._simulate_trades_sync, df, pair, timeframe)
        )
        result.all_trades = trades

        if not trades:
            logger.warning(f"Sin trades encontrados en backtest anual de {pair}")
            return result

        # ── Calcular métricas ──
        self._compute_metrics(result, trades, capital, risk_pct)

        # ── Desglose mensual ──
        result.monthly_results = self._compute_monthly_breakdown(trades, capital, risk_pct)

        # ── Rating ──
        result.rating, result.rating_emoji = self._rate_strategy(result)

        logger.info(
            f"✅ Backtest anual {pair}: {len(trades)} trades | "
            f"WR: {result.win_rate}% | R: {result.total_r:+.2f} | "
            f"Capital: ${result.final_capital:,.2f} | Rating: {result.rating}"
        )
        return result

    def _simulate_trades_sync(
        self,
        df: pd.DataFrame,
        pair: str,
        timeframe: str,
        min_confluences: int = 2,
    ) -> list[BacktestTrade]:
        """Simula la estrategia (sync — corre en thread executor)."""
        trades = []
        trade_id = 0
        active_trade = None
        candles_since_close = 99

        for i in range(100, len(df) - 1):
            current_df = df.iloc[:i + 1]
            next_bar   = df.iloc[i + 1]

            if active_trade:
                active_trade = self.base_engine._manage_active_trade(active_trade, next_bar)
                if active_trade.result != "OPEN":
                    trades.append(active_trade)
                    active_trade = None
                    candles_since_close = 0
                continue

            candles_since_close += 1
            if candles_since_close < 3:
                continue

            signal = analyzer.generate_signal(
                current_df,
                {"pair": pair, "price": current_df["close"].iloc[-1],
                 "funding_rate": 0, "oi_change_1h": 0},
                timeframe,
            )

            if signal and len(signal.confluences) >= min_confluences:
                trade_id += 1
                entry_mid = (signal.entry_low + signal.entry_high) / 2
                active_trade = BacktestTrade(
                    id=trade_id,
                    pair=pair,
                    direction=signal.direction,
                    entry_price=entry_mid,
                    stop_loss=signal.stop_loss,
                    tp1=signal.tp1,
                    tp2=signal.tp2,
                    rr_ratio=signal.rr_ratio,
                    entry_time=current_df.index[-1],
                    confluences=signal.confluences,
                )

        if active_trade:
            active_trade.result = "OPEN"
            trades.append(active_trade)

        return trades

    def _compute_metrics(
        self,
        result: YearlyBacktestResult,
        trades: list[BacktestTrade],
        capital: float,
        risk_pct: float,
    ):
        """Calcula todas las métricas del backtest anual."""
        closed = [t for t in trades if t.result != "OPEN"]
        if not closed:
            return

        result.total_trades = len(closed)
        result.tp1_count = sum(1 for t in closed if t.result in ("TP1_BE",))
        result.tp2_count = sum(1 for t in closed if t.result == "TP2")
        result.sl_count  = sum(1 for t in closed if t.result == "SL")
        # Win = cualquier trade con pnl_r > 0 (TP2 o TP1_BE)
        result.winning_trades = sum(1 for t in closed if t.pnl_r > 0)
        result.losing_trades  = sum(1 for t in closed if t.pnl_r <= 0)
        result.win_rate = round(result.winning_trades / result.total_trades * 100, 1)

        r_values = [t.pnl_r for t in closed]
        wins_r = [r for r in r_values if r > 0]
        losses_r = [abs(r) for r in r_values if r < 0]

        result.total_r = round(sum(r_values), 2)
        result.avg_r_per_trade = round(result.total_r / result.total_trades, 3)
        result.profit_factor = round(sum(wins_r) / sum(losses_r), 2) if losses_r else float("inf")
        result.expected_value = round(
            (result.win_rate / 100 * (sum(wins_r) / len(wins_r) if wins_r else 0)) -
            ((1 - result.win_rate / 100) * (sum(losses_r) / len(losses_r) if losses_r else 0)),
            3
        )

        # Sharpe
        r_series = pd.Series(r_values)
        result.sharpe_ratio = round(
            r_series.mean() / r_series.std() * np.sqrt(252) if r_series.std() > 0 else 0, 2
        )

        # Rachas
        max_consec_l = max_consec_w = cur_l = cur_w = 0
        for r in r_values:
            if r < 0:
                cur_l += 1; cur_w = 0
                max_consec_l = max(max_consec_l, cur_l)
            else:
                cur_w += 1; cur_l = 0
                max_consec_w = max(max_consec_w, cur_w)

        result.max_consecutive_losses = max_consec_l
        result.max_consecutive_wins = max_consec_w

        # Capital simulado
        running = capital
        peak = capital
        max_dd_usd = 0.0
        equity = [capital]

        for t in closed:
            risk_amount = running * (risk_pct / 100)
            running += t.pnl_r * risk_amount
            peak = max(peak, running)
            max_dd_usd = max(max_dd_usd, peak - running)
            equity.append(round(running, 2))

        result.equity_curve = equity
        result.final_capital = round(running, 2)
        result.peak_capital = round(peak, 2)
        result.total_profit_usd = round(running - capital, 2)
        result.total_profit_pct = round((running - capital) / capital * 100, 2)
        result.max_drawdown_usd = round(max_dd_usd, 2)
        result.max_drawdown_pct = round(max_dd_usd / peak * 100, 2) if peak else 0

        dd_r_series = []
        peak_r = 0
        for r in r_values:
            peak_r = max(peak_r, sum(r_values[:r_values.index(r) + 1]))
            dd_r_series.append(peak_r - sum(r_values[:r_values.index(r) + 1]))
        result.max_drawdown_r = round(max(dd_r_series) if dd_r_series else 0, 2)

    def _compute_monthly_breakdown(
        self,
        trades: list[BacktestTrade],
        capital: float,
        risk_pct: float,
    ) -> list[MonthlyBacktestResult]:
        """Agrupa trades por mes y calcula métricas mensuales."""
        monthly: dict[str, list[BacktestTrade]] = {}
        for t in trades:
            if t.result == "OPEN":
                continue
            month_key = t.entry_time.strftime("%Y-%m")
            monthly.setdefault(month_key, []).append(t)

        results = []
        running_capital = capital

        for month_key in sorted(monthly.keys()):
            month_trades = monthly[month_key]
            r_values = [t.pnl_r for t in month_trades]
            wins = [r for r in r_values if r > 0]
            losses = [abs(r) for r in r_values if r < 0]

            month_start_cap = running_capital
            peak = running_capital
            max_dd = 0.0

            for r in r_values:
                risk_amount = running_capital * (risk_pct / 100)
                running_capital += r * risk_amount
                peak = max(peak, running_capital)
                max_dd = max(max_dd, peak - running_capital)

            month_pnl = running_capital - month_start_cap
            year, month = month_key.split("-")
            month_name = f"{MONTH_NAMES_ES.get(month, month)} {year}"

            mr = MonthlyBacktestResult(
                month=month_name,
                trades=len(month_trades),
                wins=len(wins),
                losses=len(losses),
                total_r=round(sum(r_values), 2),
                win_rate=round(len(wins) / len(month_trades) * 100, 1) if month_trades else 0,
                profit_factor=round(sum(wins) / sum(losses), 2) if losses else float("inf"),
                starting_capital=round(month_start_cap, 2),
                ending_capital=round(running_capital, 2),
                monthly_pnl_usd=round(month_pnl, 2),
                monthly_pnl_pct=round(month_pnl / month_start_cap * 100, 2) if month_start_cap else 0,
                max_drawdown_pct=round(max_dd / peak * 100, 2) if peak else 0,
            )
            results.append(mr)

        return results

    def _rate_strategy(self, result: YearlyBacktestResult) -> tuple[str, str]:
        """Califica la estrategia basándose en múltiples métricas."""
        score = 0
        if result.win_rate >= 55: score += 2
        elif result.win_rate >= 45: score += 1
        if result.profit_factor >= 2.0: score += 3
        elif result.profit_factor >= 1.5: score += 2
        elif result.profit_factor >= 1.2: score += 1
        if result.expected_value > 0.1: score += 2
        elif result.expected_value > 0: score += 1
        if result.max_drawdown_pct < 10: score += 2
        elif result.max_drawdown_pct < 20: score += 1
        if result.sharpe_ratio > 1.5: score += 2
        elif result.sharpe_ratio > 0.8: score += 1
        if result.total_profit_pct > 50: score += 2
        elif result.total_profit_pct > 20: score += 1

        if score >= 10:
            return "EXCELENTE 🏆", "🏆"
        elif score >= 7:
            return "BUENA ⭐", "⭐"
        elif score >= 4:
            return "ACEPTABLE 🟡", "🟡"
        else:
            return "NECESITA MEJORA 🔴", "🔴"


def format_yearly_report(result: YearlyBacktestResult, show_monthly: bool = True) -> str:
    """Formatea el reporte anual para Telegram."""
    if result.total_trades == 0:
        return f"❌ Backtest anual {result.pair} — Sin trades en el período"

    gain_emoji = "📈" if result.total_profit_usd >= 0 else "📉"
    sign = "+" if result.total_profit_usd >= 0 else ""

    text = f"""📊 *BACKTEST ANUAL — {result.pair} {result.timeframe}*
{result.rating_emoji} Calificación: *{result.rating}*
━━━━━━━━━━━━━━━━━━━━━━━━
📅 Datos: `{result.total_candles}` velas analizadas

*🎯 Operaciones:*
  Total: `{result.total_trades}` | ✅TP1: `{result.tp1_count}` | 🏆TP2: `{result.tp2_count}` | ❌SL: `{result.sl_count}`
  Win Rate: `{result.win_rate}%` | Profit Factor: `{result.profit_factor}`
  EV por trade: `{result.expected_value:+.3f}R`

*📐 Rendimiento:*
  Total R: `{result.total_r:+.2f}R`
  Promedio/trade: `{result.avg_r_per_trade:+.3f}R`
  Sharpe Ratio: `{result.sharpe_ratio}`
  Max DD (R): `{result.max_drawdown_r}R`
  Rachas: 🏆`{result.max_consecutive_wins}` wins / 💔`{result.max_consecutive_losses}` losses

*💰 Capital simulado* (`{result.risk_pct_used}% riesgo/trade`):
  Inicio: `${result.initial_capital:,.2f}`
  {gain_emoji} Final: `${result.final_capital:,.2f}`
  🏔 Pico: `${result.peak_capital:,.2f}`
  P&L: `{sign}${abs(result.total_profit_usd):,.2f}` (`{result.total_profit_pct:+.2f}%`)
  📉 Max Drawdown: `${result.max_drawdown_usd:,.2f}` (`{result.max_drawdown_pct:.1f}%`)"""

    if show_monthly and result.monthly_results:
        text += "\n\n*📆 Desglose mensual:*\n```"
        text += f"\n{'Mes':<18} {'Trades':>6} {'R':>7} {'P&L%':>7}"
        text += f"\n{'─'*42}"
        for m in result.monthly_results:
            emoji = "+" if m.total_r >= 0 else "-"
            text += f"\n{m.month:<18} {m.trades:>6} {m.total_r:>+7.2f} {m.monthly_pnl_pct:>+6.1f}%"
        text += "\n```"

    return text


def format_yearly_capital_simulation(
    result: YearlyBacktestResult,
    custom_capital: float,
    custom_risk: float,
) -> str:
    """Simula con capital y riesgo personalizado sobre resultado anual."""
    if result.total_trades == 0:
        return "❌ Sin datos para simular"

    closed = [t for t in result.all_trades if t.result != "OPEN"]
    running = custom_capital
    peak = custom_capital
    max_dd = 0.0

    monthly_sim: dict[str, float] = {}
    for t in closed:
        month_key = t.entry_time.strftime("%Y-%m")
        month_start = running
        risk_amount = running * (custom_risk / 100)
        running += t.pnl_r * risk_amount
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

        if month_key not in monthly_sim:
            monthly_sim[month_key] = month_start
        monthly_sim[month_key] = running

    gain = running - custom_capital
    gain_pct = (gain / custom_capital) * 100
    gain_emoji = "📈" if gain >= 0 else "📉"
    result_word = "HABRÍAS GANADO" if gain >= 0 else "HABRÍAS PERDIDO"

    text = f"""💰 *SIMULACIÓN CON TU CAPITAL*
━━━━━━━━━━━━━━━━━━━━━━━━
📊 Estrategia: `{result.pair}` {result.timeframe} ({result.total_trades} trades)
💵 Capital inicial: `${custom_capital:,.2f}`
⚖️ Riesgo por trade: `{custom_risk}%`

{gain_emoji} *{result_word}: `${abs(gain):,.2f}` (`{gain_pct:+.2f}%`)*

Capital final: `${running:,.2f}`
Capital pico: `${peak:,.2f}`
Max Drawdown: `${max_dd:,.2f}` (`{max_dd/peak*100:.1f}%`)"""

    if monthly_sim:
        text += "\n\n*📆 Capital por mes:*\n```"
        prev_month_end = custom_capital
        for month_key in sorted(monthly_sim.keys()):
            cap = monthly_sim[month_key]
            month_gain = cap - prev_month_end
            year, month = month_key.split("-")
            m_name = f"{MONTH_NAMES_ES.get(month, month)[:3]} {year[2:]}"
            sign_e = "▲" if month_gain >= 0 else "▼"
            text += f"\n{m_name:<10} ${cap:>10,.2f}  {sign_e}{abs(month_gain):>8,.2f}"
            prev_month_end = cap
        text += "\n```"

    return text


yearly_engine: Optional[YearlyBacktestEngine] = None

def get_yearly_engine(binance_client: Any) -> YearlyBacktestEngine:
    global yearly_engine
    if yearly_engine is None:
        yearly_engine = YearlyBacktestEngine(binance_client)
    return yearly_engine
