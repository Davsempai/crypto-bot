"""
backtest/engine.py — Motor de backtesting para validar la estrategia

Simula la estrategia sobre datos históricos y calcula métricas reales:
- Win rate, Factor de beneficio, Sharpe Ratio
- Max Drawdown, Recovery Factor
- Curva de equity, distribución de resultados
"""
import asyncio
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from analysis.technical import TechnicalAnalyzer
from typing import Any
from config import config
from utils.logger import setup_logger

logger = setup_logger("backtest")

analyzer = TechnicalAnalyzer()


@dataclass
class BacktestTrade:
    """Representa una operación simulada en el backtest."""
    id: int
    pair: str
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    rr_ratio: float
    entry_time: datetime
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    result: str = "OPEN"     # TP2 / TP1_BE / SL / OPEN
    pnl_r: float = 0.0
    pnl_pct: float = 0.0
    confluences: list[str] = field(default_factory=list)
    # Gestión parcial: True cuando TP1 fue tocado y SL movido a breakeven
    tp1_hit: bool = False


@dataclass
class BacktestResult:
    """Resultado completo del backtest."""
    pair: str
    timeframe: str
    start_date: str
    end_date: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    tp1_count: int = 0
    tp2_count: int = 0
    sl_count: int = 0

    # Métricas de rendimiento
    win_rate: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    profit_factor: float = 0.0
    expected_value: float = 0.0    # EV por operación en R
    total_r: float = 0.0
    max_drawdown_r: float = 0.0
    max_consecutive_losses: int = 0
    sharpe_ratio: float = 0.0
    recovery_factor: float = 0.0

    # Capital simulado (partiendo de 1000 USDT, 1% riesgo)
    initial_capital: float = 1000.0
    final_capital: float = 1000.0
    peak_capital: float = 1000.0

    # Series para gráficos
    equity_curve: list[float] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)

    def compute_metrics(self):
        """Calcula todas las métricas desde la lista de trades."""
        closed = [t for t in self.trades if t.result != "OPEN"]
        self.total_trades = len(closed)

        if self.total_trades == 0:
            return

        self.tp1_count = sum(1 for t in closed if t.result == "TP1_BE")
        self.tp2_count = sum(1 for t in closed if t.result == "TP2")
        self.sl_count  = sum(1 for t in closed if t.result == "SL")
        self.winning_trades = sum(1 for t in closed if t.pnl_r > 0)
        self.losing_trades  = sum(1 for t in closed if t.pnl_r <= 0)
        self.win_rate = round(self.winning_trades / self.total_trades * 100, 1)

        wins_r = [t.pnl_r for t in closed if t.pnl_r > 0]
        losses_r = [abs(t.pnl_r) for t in closed if t.pnl_r < 0]

        self.avg_win_r = round(np.mean(wins_r), 2) if wins_r else 0
        self.avg_loss_r = round(np.mean(losses_r), 2) if losses_r else 0
        self.total_r = round(sum(t.pnl_r for t in closed), 2)

        total_profit = sum(wins_r)
        total_loss = sum(losses_r)
        self.profit_factor = round(total_profit / total_loss, 2) if total_loss else float("inf")

        self.expected_value = round(
            (self.win_rate / 100 * self.avg_win_r) - ((1 - self.win_rate / 100) * self.avg_loss_r), 3
        )

        # Equity curve (simulando 1% de riesgo por operación)
        capital = self.initial_capital
        equity = [capital]
        peak = capital
        max_dd = 0
        consec_losses = 0
        max_consec = 0

        for t in closed:
            risk_amount = capital * (config.MAX_RISK_PER_TRADE / 100)
            capital += t.pnl_r * risk_amount
            equity.append(round(capital, 2))
            peak = max(peak, capital)
            dd = (peak - capital) / peak * 100
            max_dd = max(max_dd, dd)

            if t.pnl_r < 0:
                consec_losses += 1
                max_consec = max(max_consec, consec_losses)
            else:
                consec_losses = 0

        self.equity_curve = equity
        self.final_capital = round(capital, 2)
        self.peak_capital = round(peak, 2)
        self.max_drawdown_r = round(max_dd, 2)
        self.max_consecutive_losses = max_consec

        # Sharpe Ratio (simplificado, sin tasa libre de riesgo)
        r_series = pd.Series([t.pnl_r for t in closed])
        self.sharpe_ratio = round(
            r_series.mean() / r_series.std() * np.sqrt(252) if r_series.std() > 0 else 0, 2
        )

        # Recovery Factor
        gross_profit = sum(wins_r) * self.initial_capital * config.MAX_RISK_PER_TRADE / 100
        self.recovery_factor = round(
            gross_profit / (self.max_drawdown_r / 100 * self.peak_capital), 2
        ) if self.max_drawdown_r > 0 else float("inf")


class BacktestEngine:
    """Motor de backtesting vectorizado."""

    def __init__(self, binance_client: Any):
        self.binance = binance_client

    async def run(
        self,
        pair: str,
        timeframe: str = "4h",
        periods: int = 1000,
        min_confluences: int = 2,   # V2 filtra en origen, aquí relajamos el umbral
    ) -> BacktestResult:
        """
        Ejecuta el backtest completo.

        Args:
            pair: Par de trading (ej: BTCUSDT)
            timeframe: Timeframe a testear
            periods: Número de velas históricas
            min_confluences: Confluencias mínimas para entrada
        """
        logger.info(f"🔄 Backtest iniciado: {pair} {timeframe} ({periods} velas)")

        # Descargar datos históricos
        df = await self.binance.get_klines(pair, timeframe, min(periods + 100, 1000))

        if df.empty or len(df) < 100:
            logger.error(f"Datos insuficientes para backtest de {pair}")
            return BacktestResult(pair=pair, timeframe=timeframe,
                                  start_date="", end_date="")

        df = analyzer.add_indicators(df)

        result = BacktestResult(
            pair=pair,
            timeframe=timeframe,
            start_date=str(df.index[50]),
            end_date=str(df.index[-1]),
        )

        # Simulación en executor para no bloquear el event loop del bot
        import functools
        loop = asyncio.get_event_loop()
        trades = await loop.run_in_executor(
            None, functools.partial(self._run_simulation_sync, df, pair, timeframe, min_confluences)
        )

        result.trades = trades
        result.compute_metrics()

        logger.info(
            f"✅ Backtest completado: {len(trades)} trades | "
            f"WR: {result.win_rate}% | Total R: {result.total_r:+.2f}R | "
            f"PF: {result.profit_factor}"
        )
        return result

    def _run_simulation_sync(
        self, df: pd.DataFrame, pair: str, timeframe: str, min_confluences: int
    ) -> list:
        """Loop de simulación sincrónico — corre en thread executor."""
        trades = []
        trade_id = 0
        active_trade: Optional[BacktestTrade] = None
        candles_since_close = 99

        for i in range(100, len(df) - 1):
            current_df  = df.iloc[:i + 1]
            current_bar = df.iloc[i]
            next_bar    = df.iloc[i + 1]
            current_price = float(current_bar["close"])

            if active_trade:
                active_trade = self._manage_active_trade(active_trade, next_bar)
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
                {"pair": pair, "price": current_price, "funding_rate": 0, "oi_change_1h": 0},
                timeframe,
            )

            if signal and len(signal.confluences) >= min_confluences:
                trade_id += 1
                entry_mid = (signal.entry_low + signal.entry_high) / 2
                active_trade = BacktestTrade(
                    id=trade_id, pair=pair, direction=signal.direction,
                    entry_price=entry_mid, stop_loss=signal.stop_loss,
                    tp1=signal.tp1, tp2=signal.tp2, rr_ratio=signal.rr_ratio,
                    entry_time=current_bar.name, confluences=signal.confluences,
                )

        if active_trade:
            last_bar = df.iloc[-1]
            active_trade.exit_time  = last_bar.name
            active_trade.exit_price = float(last_bar["close"])
            active_trade.result     = "OPEN"
            trades.append(active_trade)

        return trades

    def _manage_active_trade(self, trade: BacktestTrade, bar: pd.Series) -> BacktestTrade:
        """
        Simula la gestión correcta del trade:
          - TP1 (2R) alcanzado → cierra 50%, mueve SL a breakeven, sigue con 50%
          - TP2 (4R) alcanzado → cierra el 50% restante → pnl total = 3.0R
          - SL antes de TP1   → pérdida completa → pnl = -1.0R
          - BE después de TP1 → segunda mitad en breakeven → pnl = +1.0R

        R:R efectivo ponderado:
          Si TP2: 0.5 × 2R + 0.5 × 4R = 3.0R
          Si BE:  0.5 × 2R + 0.5 × 0R = 1.0R
          Si SL:  -1.0R
        """
        high = bar["high"]
        low  = bar["low"]

        # SL efectivo — se mueve a breakeven después de TP1
        eff_sl = trade.entry_price if trade.tp1_hit else trade.stop_loss

        if trade.direction == "LONG":

            # Primero checar SL (tiene prioridad si la vela toca ambos lados)
            if low <= eff_sl:
                trade.result    = "TP1_BE" if trade.tp1_hit else "SL"
                trade.exit_price = eff_sl
                trade.exit_time  = bar.name
                trade.pnl_r     = 1.0 if trade.tp1_hit else -1.0
                trade.pnl_pct   = round(
                    (eff_sl - trade.entry_price) / trade.entry_price * 100, 3
                )

            # TP2 completo
            elif high >= trade.tp2:
                trade.result    = "TP2"
                trade.exit_price = trade.tp2
                trade.exit_time  = bar.name
                trade.pnl_r     = 3.0   # 0.5×2R + 0.5×4R
                trade.pnl_pct   = round(
                    (trade.tp2 - trade.entry_price) / trade.entry_price * 100, 3
                )

            # TP1 alcanzado por primera vez → parcial, mover SL a BE
            elif not trade.tp1_hit and high >= trade.tp1:
                trade.tp1_hit = True
                # Trade sigue abierto, SL movido a entry_price (breakeven)

        else:  # SHORT

            if high >= eff_sl:
                trade.result    = "TP1_BE" if trade.tp1_hit else "SL"
                trade.exit_price = eff_sl
                trade.exit_time  = bar.name
                trade.pnl_r     = 1.0 if trade.tp1_hit else -1.0
                trade.pnl_pct   = round(
                    (trade.entry_price - eff_sl) / trade.entry_price * 100, 3
                )

            elif low <= trade.tp2:
                trade.result    = "TP2"
                trade.exit_price = trade.tp2
                trade.exit_time  = bar.name
                trade.pnl_r     = 3.0
                trade.pnl_pct   = round(
                    (trade.entry_price - trade.tp2) / trade.entry_price * 100, 3
                )

            elif not trade.tp1_hit and low <= trade.tp1:
                trade.tp1_hit = True

        return trade

    async def run_multi_pair(
        self,
        pairs: list[str] = None,
        timeframe: str = "4h",
        periods: int = 1000,
    ) -> dict[str, BacktestResult]:
        """Corre backtest en múltiples pares y retorna comparativa."""
        pairs = pairs or config.TRADING_PAIRS
        logger.info(f"🔄 Backtest multi-par: {pairs}")

        tasks = [self.run(pair, timeframe, periods) for pair in pairs]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        results = {}
        for pair, result in zip(pairs, results_list):
            if isinstance(result, Exception):
                logger.error(f"Error en backtest de {pair}: {result}")
            else:
                results[pair] = result

        return results


def format_backtest_report(result: BacktestResult) -> str:
    """Formatea el resultado del backtest para Telegram."""
    if result.total_trades == 0:
        return f"❌ *Backtest {result.pair}* — Sin trades encontrados en el período"

    # Evaluar la estrategia
    if result.profit_factor >= 1.5 and result.win_rate >= 50:
        rating = "🟢 BUENA"
    elif result.profit_factor >= 1.2 and result.win_rate >= 45:
        rating = "🟡 ACEPTABLE"
    else:
        rating = "🔴 MEJORAR"

    pnl_capital = result.final_capital - result.initial_capital
    pnl_pct = (pnl_capital / result.initial_capital) * 100

    return f"""📊 *BACKTEST — {result.pair} ({result.timeframe})*
━━━━━━━━━━━━━━━━━━━━━━━━
📅 Período: `{result.start_date[:10]}` → `{result.end_date[:10]}`
📋 Total trades: `{result.total_trades}`

*Resultados:*
🏆 TP2 completo: `{result.tp2_count}` | ✅ TP1+BE: `{result.tp1_count}` | ❌ SL: `{result.sl_count}`
🎯 Win Rate: `{result.win_rate}%`
📈 Total R: `{result.total_r:+.2f}R`
⚖️ Profit Factor: `{result.profit_factor}`
💡 EV/trade: `{result.expected_value:+.3f}R`

*Riesgo:*
📉 Max Drawdown: `{result.max_drawdown_r:.1f}%`
🔴 Max pérdidas consec.: `{result.max_consecutive_losses}`
📐 Sharpe: `{result.sharpe_ratio}`

*Capital simulado* (1% riesgo/trade):
💵 Inicial: `$1,000`
💰 Final: `${result.final_capital:,.2f}` (`{pnl_pct:+.1f}%`)

*Calificación:* {rating}"""


def format_multi_backtest_report(results: dict[str, BacktestResult]) -> str:
    """Comparativa de múltiples pares."""
    if not results:
        return "❌ Sin resultados de backtest"

    lines = ["📊 *BACKTEST MULTI-PAR — COMPARATIVA*\n━━━━━━━━━━━━━━━━━━━━━━━━"]
    sorted_results = sorted(
        results.items(),
        key=lambda x: x[1].total_r,
        reverse=True
    )

    for pair, r in sorted_results:
        if r.total_trades == 0:
            continue
        emoji = "🟢" if r.total_r > 5 else "🟡" if r.total_r > 0 else "🔴"
        lines.append(
            f"\n{emoji} *{pair}*\n"
            f"  WR: `{r.win_rate}%` | R: `{r.total_r:+.1f}` | "
            f"PF: `{r.profit_factor}` | Trades: `{r.total_trades}`\n"
            f"  DD: `{r.max_drawdown_r:.1f}%` | EV: `{r.expected_value:+.3f}R`"
        )

    best = sorted_results[0] if sorted_results else None
    if best:
        lines.append(f"\n🏆 *Mejor par:* `{best[0]}` con `{best[1].total_r:+.2f}R`")

    return "\n".join(lines)
