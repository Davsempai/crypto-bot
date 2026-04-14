"""
bot/telegram_bot.py — Bot v3.1: todos los bugs corregidos
Fixes:
  - update.message es None en callbacks → usar safe_reply() universal
  - Señales duplicadas → hash por dirección+zona redondeada, cooldown global
  - Claude balance agotado → detectar error 400 y desactivar permanentemente
  - Weex interval inválido en backtest anual → interceptar en yearly_bt
  - FCN /api/analyze sin resultados → usar /api/news como fuente principal
"""
import asyncio
from functools import wraps
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import config
from market.exchange import exchange as binance
from market.liquidations import liq_monitor
from analysis.multi_timeframe import get_mtf_engine
from analysis.macro import macro_analyzer, news_monitor
from analysis.signal_filter import signal_filter
from backtest.engine import BacktestEngine, format_backtest_report, format_multi_backtest_report
from backtest.yearly import get_yearly_engine, format_yearly_report
from alerts.alert_manager import formatter
from tracking.signal_tracker import SignalTracker, SignalUpdate
from tracking.pnl_tracker import (
    pnl_tracker, format_monthly_report_telegram,
    format_capital_simulation, format_daily_update,
)
from utils.database import (
    init_db, save_signal, save_market_snapshot,
    save_news_alert, get_stats, get_open_signals,
)
from utils.log_reader import read_last_lines, filter_errors, format_log_for_telegram, get_summary
from utils.logger import setup_logger

logger = setup_logger("telegram_bot")

_recent_signals: dict[str, list[datetime]] = defaultdict(list)
backtest_engine = BacktestEngine(binance)
yearly_bt       = get_yearly_engine(binance)
mtf             = get_mtf_engine(binance)
signal_tracker  = SignalTracker(binance)

# ─── Helper universal de respuesta ────────────────────────────────────────────
# Funciona tanto desde comandos (/start) como desde botones inline (callback)

async def safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     text: str, **kwargs):
    """
    Envía un mensaje sin importar si viene de comando o botón inline.
    - Comando  → update.message existe     → reply_text()
    - Callback → update.message es None    → bot.send_message()
    """
    cid = update.effective_chat.id
    if update.message:
        await update.message.reply_text(text, **kwargs)
    else:
        await context.bot.send_message(cid, text, **kwargs)


# ─── Decoradores ──────────────────────────────────────────────────────────────

def authorized_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if config.AUTHORIZED_USERS and uid not in config.AUTHORIZED_USERS:
            await safe_reply(update, context, "🚫 No autorizado.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def count_recent_signals(pair: str) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
    _recent_signals[pair] = [t for t in _recent_signals[pair] if t > cutoff]
    return len(_recent_signals[pair])


def register_signal(pair: str):
    _recent_signals[pair].append(datetime.now(timezone.utc))


async def _save_and_track(signal_data: dict, market_data: dict,
                           grade: str, score: int, risk: float) -> tuple[int, str]:
    """Guarda la señal y la registra en el tracker."""
    sid = await save_signal(signal_data)
    signal_tracker.register_new_signal(signal_data, sid)
    register_signal(signal_data["pair"])

    from analysis.technical import Signal
    sig_obj = Signal(
        pair=signal_data["pair"], direction=signal_data["direction"],
        timeframe=signal_data["timeframe"],
        entry_low=signal_data["entry_low"], entry_high=signal_data["entry_high"],
        stop_loss=signal_data["stop_loss"], tp1=signal_data["tp1"],
        tp2=signal_data["tp2"], rr_ratio=signal_data["rr_ratio"],
        confidence=score,
        confluences=signal_data.get("reason", {}).get("confluences", []),
    )
    ge   = signal_filter.get_grade_emoji(grade)
    text = formatter.format_signal(sig_obj, market_data, sid)
    text += (f"\n\n{ge} *Grade:* `{grade}` | Score: `{score}/100`\n"
             f"⚖️ *Riesgo ajustado:* `{risk:.2f}%`")
    return sid, text


# ─── Callback del signal tracker ──────────────────────────────────────────────

_app_ref = None

async def _on_signal_update(update: SignalUpdate):
    if not config.SIGNAL_CHANNEL_ID or not _app_ref:
        return
    try:
        await _app_ref.bot.send_message(
            chat_id=config.SIGNAL_CHANNEL_ID,
            text=update.message,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Error enviando update de señal: {e}")


# ─── COMANDOS ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Mercado",        callback_data="market"),
         InlineKeyboardButton("🔬 MTF Analysis",   callback_data="mtf_menu")],
        [InlineKeyboardButton("💥 Liquidaciones",  callback_data="liq_menu"),
         InlineKeyboardButton("📰 Noticias",        callback_data="news")],
        [InlineKeyboardButton("🧪 Backtest 500v",  callback_data="bt_menu"),
         InlineKeyboardButton("📅 Backtest Anual", callback_data="bt_yearly_menu")],
        [InlineKeyboardButton("💰 Simular Capital",callback_data="sim_capital"),
         InlineKeyboardButton("📆 Reporte Mensual",callback_data="monthly_report")],
        [InlineKeyboardButton("🎯 Señales abiertas",callback_data="open_signals"),
         InlineKeyboardButton("📈 Estadísticas",   callback_data="stats")],
    ])
    mode  = "🧪 PAPER" if config.PAPER_TRADING else "💰 LIVE"
    pairs = " ".join(f"`{p}`" for p in config.TRADING_PAIRS)
    await safe_reply(update, context,
        f"🤖 *CRYPTO FUTURES BOT v3.1* ✅\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 `{mode}` | {pairs}\n\n"
        f"*Módulos:* MTF · Señales A+/A/B/C · Tracking · P&L · Dashboard",
        reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
    )


@authorized_only
async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    msg = await context.bot.send_message(cid, "⏳ Cargando mercado...")
    try:
        mdl = [await binance.get_all_futures_market_data(p) for p in config.TRADING_PAIRS]
        summary = await macro_analyzer.generate_market_summary(mdl)
        await msg.edit_text(
            formatter.format_market_summary(mdl, summary),
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Error /market: {e}")
        await msg.edit_text("❌ Error al obtener datos del mercado.")


@authorized_only
async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    capital = float(args[0].replace(",", "")) if args else 1000.0
    cid = update.effective_chat.id
    msg = await context.bot.send_message(cid, "⏳ Calculando P&L...")
    try:
        await pnl_tracker.ensure_pnl_table()
        data = await pnl_tracker.get_daily_update(capital)
        await msg.edit_text(format_daily_update(data), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error /pnl: {e}")
        await msg.edit_text("❌ Error al calcular P&L.")


@authorized_only
async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args    = context.args
    now     = datetime.now(timezone.utc)
    month   = int(args[0]) if len(args) >= 1 else now.month
    year    = int(args[1]) if len(args) >= 2 else now.year
    capital = float(args[2]) if len(args) >= 3 else 1000.0
    cid = update.effective_chat.id
    msg = await context.bot.send_message(cid, f"⏳ Generando reporte {month}/{year}...")
    try:
        report = await pnl_tracker.get_monthly_report(year, month, capital)
        await msg.edit_text(format_monthly_report_telegram(report), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error /monthly: {e}")
        await msg.edit_text("❌ Error generando reporte mensual.")


@authorized_only
async def cmd_simulate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await safe_reply(update, context,
            "💰 *Simulador*\n\nUso: `/simulate [capital] [riesgo%] [desde] [hasta]`\n"
            "Ej: `/simulate 5000 1 2025-01-01 2025-06-30`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    capital   = float(args[0].replace(",", ""))
    risk      = float(args[1]) if len(args) > 1 else config.MAX_RISK_PER_TRADE
    from_date = args[2] if len(args) > 2 else None
    to_date   = args[3] if len(args) > 3 else None
    cid = update.effective_chat.id
    msg = await context.bot.send_message(cid, f"⏳ Simulando con ${capital:,.0f}...")
    try:
        sim = await pnl_tracker.simulate_with_capital(capital, from_date, to_date, risk)
        await msg.edit_text(format_capital_simulation(sim), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error /simulate: {e}")
        await msg.edit_text("❌ Error en simulación.")


@authorized_only
async def cmd_backtest_yearly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        buttons = [
            [InlineKeyboardButton("🌐 TODOS — 4H (recomendado)", callback_data="bt_combined_4h")],
            [InlineKeyboardButton("BTC 4H", callback_data="bty_BTCUSDT_4h"),
             InlineKeyboardButton("ETH 4H", callback_data="bty_ETHUSDT_4h")],
            [InlineKeyboardButton("SOL 4H", callback_data="bty_SOLUSDT_4h"),
             InlineKeyboardButton("BTC 1H", callback_data="bty_BTCUSDT_1h")],
        ]
        await safe_reply(update, context,
            "📅 *Backtest Anual*\n\n"
            "ℹ️ *Timeframes del sistema:*\n"
            "• *En vivo (MTF):* 4H bias → 1H setup → 15M entrada\n"
            "• *Backtest:* simula en 4H (mejor ratio señal/ruido)\n\n"
            "🌐 *TODOS LOS PARES* combina BTC+ETH+SOL+BNB+XRP y "
            "simula cuánto habrías ganado con tu capital.\n\n"
            "_Usa `/yearly ALL 4h 5000 1` para personalizar capital y riesgo_",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    pair    = args[0].upper()
    tf      = args[1] if len(args) > 1 else "4h"
    capital = float(args[2].replace(",", "")) if len(args) > 2 else 1000.0
    risk    = float(args[3]) if len(args) > 3 else config.MAX_RISK_PER_TRADE

    if pair == "ALL":
        await _run_combined_backtest(update, context, capital, risk, tf)
    else:
        await _run_yearly_backtest(update, context, pair, tf, capital, risk)


@authorized_only
async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        buttons = [
            [InlineKeyboardButton("BTC 4H", callback_data="bt_BTCUSDT_4h"),
             InlineKeyboardButton("ETH 4H", callback_data="bt_ETHUSDT_4h")],
            [InlineKeyboardButton("SOL 4H", callback_data="bt_SOLUSDT_4h"),
             InlineKeyboardButton("BTC 1H", callback_data="bt_BTCUSDT_1h")],
            [InlineKeyboardButton("📊 Multi-par 4H", callback_data="bt_MULTI")],
        ]
        await safe_reply(update, context,
            "🧪 *Backtest* (1000 velas)\n"
            "_Recomendado: 4H → cubre ~5 meses de señales_\n"
            "_Para 1 año completo usa /yearly_",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    pair = args[0].upper()
    tf   = args[1] if len(args) > 1 else "4h"
    await _run_backtest(update, context, pair, tf)


@authorized_only
async def cmd_mtf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pair = " ".join(context.args).upper() if context.args else None
    if not pair:
        buttons = [[InlineKeyboardButton(p, callback_data=f"mtf_{p}")] for p in config.TRADING_PAIRS]
        await safe_reply(update, context,
            "🔬 ¿Qué par analizamos? (4H → 1H → 15M)",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    await _run_mtf_analysis(update, context, pair)


@authorized_only
async def cmd_liquidations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pair = " ".join(context.args).upper() if context.args else "BTCUSDT"
    await _show_liquidations(update, context, pair)


@authorized_only
async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    signals = await get_open_signals()
    live    = signal_tracker.get_live_signals()
    live_map = {s.db_id: s for s in live}
    if not signals:
        await safe_reply(update, context,
            "📭 *No hay señales abiertas.*", parse_mode=ParseMode.MARKDOWN)
        return
    text = f"🎯 *SEÑALES ABIERTAS ({len(signals)})*\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
    for s in signals:
        e        = "🟢" if s["direction"] == "LONG" else "🔴"
        live_s   = live_map.get(s["id"])
        pnl_txt  = ""
        if live_s and live_s.last_price:
            pnl_r, pnl_pct = live_s.compute_pnl(live_s.last_price)
            clr = "📈" if pnl_r >= 0 else "📉"
            tp1_done = "✅TP1" if live_s.tp1_hit else ""
            pnl_txt = f"\n  {clr} P&L: `{pnl_r:+.2f}R` (`{pnl_pct:+.2f}%`) {tp1_done}"
        text += (
            f"\n{e} #{s['id']:04d} *{s['pair']}* {s['direction']}\n"
            f"  Zona: `${s['entry_low']:,.4g}—${s['entry_high']:,.4g}` | SL: `${s['stop_loss']:,.4g}`\n"
            f"  TP1: `${s['tp1']:,.4g}` | TP2: `${s['tp2']:,.4g}` | `{s['timeframe']}`"
            f"{pnl_txt}\n"
        )
    await safe_reply(update, context, text, parse_mode=ParseMode.MARKDOWN)


@authorized_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await get_stats()
    await safe_reply(update, context,
        formatter.format_stats(stats), parse_mode=ParseMode.MARKDOWN)


@authorized_only
async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    msg = await context.bot.send_message(cid, "⏳ Buscando noticias...")
    try:
        news_list = await news_monitor.get_important_news()
        if not news_list:
            await msg.edit_text("📭 *Sin noticias de alto impacto.*",
                                parse_mode=ParseMode.MARKDOWN)
            return
        await msg.edit_text(f"📰 {len(news_list)} noticia(s) relevante(s):")
        for news in news_list[:3]:
            await context.bot.send_message(
                cid, formatter.format_news_alert(news),
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            )
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"Error /news: {e}")
        await msg.edit_text("❌ Error al obtener noticias.")


@authorized_only
async def cmd_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    msg = await context.bot.send_message(cid, "⏳ Obteniendo funding rates...")
    lines = ["💰 *FUNDING RATES*\n━━━━━━━━━━━━━━━━━━━━━━━━"]
    for pair in config.TRADING_PAIRS:
        data = await binance.get_funding_rate(pair)
        fr   = data.get("funding_rate", 0)
        e    = "🔥" if abs(fr) >= config.FUNDING_RATE_THRESHOLD else (
               "⚠️" if abs(fr) >= config.FUNDING_RATE_THRESHOLD * 0.5 else "✅")
        note = "  ← EXTREMO" if abs(fr) >= config.FUNDING_RATE_THRESHOLD else ""
        lines.append(f"{e} *{pair}*: `{fr:+.4f}%`{note}")
    lines.append(f"\n_Umbral: ±{config.FUNDING_RATE_THRESHOLD:.4f}%_")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, context,
        "❓ *COMANDOS*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 `/market` — Snapshot de mercado\n"
        "🔬 `/mtf [PAR]` — Análisis Multi-Timeframe\n"
        "💥 `/liquidations [PAR]` — Mapa de liquidaciones\n"
        "📰 `/news` — Noticias de alto impacto\n"
        "💰 `/funding` — Funding rates\n"
        "🎯 `/signals` — Señales abiertas con P&L en vivo\n"
        "📈 `/stats` — Track record completo\n"
        "💵 `/pnl [capital]` — P&L de hoy + mes\n"
        "📅 `/monthly [mes] [año] [capital]` — Reporte mensual\n"
        "🧮 `/simulate [capital] [riesgo%]` — Simulador\n"
        "🧪 `/backtest [PAR] [TF]` — Backtest 1000 velas\n"
        "📅 `/yearly [PAR/ALL] [TF] [capital] [riesgo%]` — Backtest anual\n"
        "🔄 `/scan` — Escanear todos los pares\n\n"
        "*Diagnóstico del bot:*\n"
        "📋 `/log [N]` — Últimas N líneas del log\n"
        "❌ `/logerrors` — Solo errores y warnings\n"
        "📊 `/logsum` — Resumen rápido del log",
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized_only
async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía las últimas N líneas del log por Telegram."""
    args = context.args
    n    = int(args[0]) if args and args[0].isdigit() else 50
    n    = min(n, 150)
    lines  = read_last_lines(n)
    chunks = format_log_for_telegram(lines, f"LOG (ultimas {n} lineas)")
    for chunk in chunks:
        await safe_reply(update, context, chunk, parse_mode=ParseMode.MARKDOWN)


@authorized_only
async def cmd_logerrors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía solo errores y warnings del log."""
    lines  = read_last_lines(300)
    errors = filter_errors(lines)
    if not errors:
        await safe_reply(update, context, "✅ Sin errores en las ultimas 300 lineas del log.")
        return
    chunks = format_log_for_telegram(errors[-60:], f"ERRORES/WARNINGS ({len(errors)} total)")
    for chunk in chunks:
        await safe_reply(update, context, chunk, parse_mode=ParseMode.MARKDOWN)


@authorized_only
async def cmd_logsum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resumen rápido del estado del log."""
    await safe_reply(update, context, get_summary(), parse_mode=ParseMode.MARKDOWN)


# ─── Callbacks ────────────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # query.answer() puede fallar por timeout de red — no dejar que rompa todo
    try:
        await query.answer()
    except Exception:
        pass  # El botón ya procesará igual aunque el ACK falle
    d   = query.data
    cid = update.effective_chat.id

    # Tabla de dispatch simple — todos usan safe_reply internamente
    simple = {
        "market":      cmd_market,
        "news":        cmd_news,
        "stats":       cmd_stats,
        "open_signals": cmd_signals,
    }
    if d in simple:
        await simple[d](update, context)

    elif d == "monthly_report":
        now = datetime.now(timezone.utc)
        context.args = [str(now.month), str(now.year), "1000"]
        await cmd_monthly(update, context)

    elif d == "sim_capital":
        await query.message.reply_text(
            "💰 *Simulador*\nUsa: `/simulate [capital] [riesgo%]`\n"
            "Ej: `/simulate 5000 1`",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif d == "mtf_menu":
        buttons = [[InlineKeyboardButton(p, callback_data=f"mtf_{p}")]
                   for p in config.TRADING_PAIRS]
        await query.message.reply_text(
            "🔬 ¿Qué par analizamos?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    elif d.startswith("mtf_"):
        await _run_mtf_analysis(update, context, d[4:])

    elif d == "liq_menu":
        buttons = [[InlineKeyboardButton(p, callback_data=f"liq_{p}")]
                   for p in config.TRADING_PAIRS]
        await query.message.reply_text(
            "💥 ¿Liquidaciones de qué par?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    elif d.startswith("liq_"):
        await _show_liquidations(update, context, d[4:])

    elif d == "bt_menu":
        await cmd_backtest(update, context)
    elif d == "bt_MULTI":
        await _run_multi_backtest(update, context)
    # ⚠️  bt_yearly_menu y bty_ DEBEN ir ANTES de startswith("bt_")
    elif d == "bt_yearly_menu":
        await cmd_backtest_yearly(update, context)
    elif d.startswith("bty_"):
        parts = d.split("_")
        if len(parts) >= 3:
            await _run_yearly_backtest(update, context, parts[1], parts[2],
                                       1000.0, config.MAX_RISK_PER_TRADE)
    elif d == "bt_combined":
        await _run_combined_backtest(update, context, 1000.0, config.MAX_RISK_PER_TRADE, "1h")
    elif d == "bt_combined_4h":
        await _run_combined_backtest(update, context, 1000.0, config.MAX_RISK_PER_TRADE, "4h")
    elif d.startswith("bt_"):
        parts = d.split("_")
        if len(parts) >= 3:
            await _run_backtest(update, context, parts[1], parts[2])


# ─── Funciones de análisis ────────────────────────────────────────────────────

async def _run_mtf_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, pair: str):
    cid = update.effective_chat.id
    msg = await context.bot.send_message(
        cid, f"🔬 Analizando *{pair}* (4H→1H→15M)...", parse_mode=ParseMode.MARKDOWN)
    try:
        md = await binance.get_all_futures_market_data(pair)
        if not md.get("price"):
            await msg.edit_text(f"❌ `{pair}` no encontrado.")
            return
        mtf_result = await mtf.full_analysis(pair, md)
        if mtf_result.signal and mtf_result.entry_15m == "CONFIRMED":
            fr = signal_filter.evaluate(mtf_result.signal, md, count_recent_signals(pair))
            if fr.approved:
                sid, text = await _save_and_track(
                    {"pair": pair, "direction": mtf_result.signal.direction,
                     "entry_low": mtf_result.signal.entry_low,
                     "entry_high": mtf_result.signal.entry_high,
                     "stop_loss": mtf_result.signal.stop_loss,
                     "tp1": mtf_result.signal.tp1, "tp2": mtf_result.signal.tp2,
                     "rr_ratio": mtf_result.signal.rr_ratio,
                     "timeframe": mtf_result.signal.timeframe,
                     "reason": {"confluences": mtf_result.confluences}},
                    md, fr.grade, fr.score, fr.adjusted_risk,
                )
                await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            else:
                reasons = "\n".join(f"  {r}" for r in fr.reasons_rejected[:3])
                await msg.edit_text(
                    f"🔬 *{pair}* — Setup detectado pero rechazado\n"
                    f"Score: `{fr.score}/100`\n\n{reasons}",
                    parse_mode=ParseMode.MARKDOWN,
                )
        else:
            be  = {"BULLISH": "🟢", "BEARISH": "🔴", "RANGING": "🟡"}.get(mtf_result.bias_4h, "⚪")
            etx = {"CONFIRMED": "✅", "PENDING": "⏳ Retest", "NONE": "❌"}.get(mtf_result.entry_15m, "❓")
            conf = "\n".join(f"  {c}" for c in mtf_result.confluences) or "  Sin confluencias"
            warns = ("\n⚠️ *Advertencias:*\n" + "\n".join(f"  {w}" for w in mtf_result.warnings)
                     ) if mtf_result.warnings else ""
            await msg.edit_text(
                f"🔬 *MTF — {pair}*\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"4H: {be} `{mtf_result.bias_4h}` RSI `{mtf_result.rsi_4h}` EMA `{mtf_result.ema_trend_4h}`\n"
                f"1H: `{mtf_result.setup_1h}` RSI `{mtf_result.rsi_1h}`\n"
                f"15M: `{etx}` RSI `{mtf_result.rsi_15m}`\n\n"
                f"*Confluencias:*\n{conf}{warns}\n\n"
                f"_{'Alineados — sin trigger aún' if mtf_result.aligned else 'TFs no alineados'}_",
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.error(f"Error MTF {pair}: {e}", exc_info=True)
        await msg.edit_text(f"❌ Error analizando {pair}.")


async def _run_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        pair: str, tf: str):
    cid = update.effective_chat.id
    msg = await context.bot.send_message(
        cid, f"🧪 Backtest *{pair} {tf}* (1000 velas)...", parse_mode=ParseMode.MARKDOWN)
    try:
        result = await backtest_engine.run(pair, tf, periods=1000)
        await msg.edit_text(format_backtest_report(result), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error backtest {pair}: {e}", exc_info=True)
        await msg.edit_text(f"❌ Error en backtest de {pair}.")


async def _run_multi_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    msg = await context.bot.send_message(cid, "🧪 Backtest multi-par (1000 velas)...")
    try:
        results = await backtest_engine.run_multi_pair(timeframe="1h", periods=1000)
        await msg.edit_text(format_multi_backtest_report(results), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error backtest multi: {e}", exc_info=True)
        await msg.edit_text("❌ Error en backtest multi-par.")


async def _run_combined_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  capital: float, risk: float, tf: str = "1h"):
    """
    Backtest combinado: descarga datos de TODOS los pares, junta todas las
    señales en orden cronológico y simula el capital como si hubieras
    operado cada señal con el mismo % de riesgo.
    """
    cid  = update.effective_chat.id
    pairs = config.TRADING_PAIRS
    msg  = await context.bot.send_message(
        cid,
        f"📅 *Backtest Combinado — {len(pairs)} pares*\n"
        f"💵 Capital: `${capital:,.0f}` | ⚖️ Riesgo: `{risk}%`/trade | TF: `{tf}`\n"
        f"⏳ Analizando {' + '.join(pairs)}...\n"
        f"_(puede tardar 2-3 min)_",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        from backtest.engine import BacktestTrade
        import pandas as pd

        # ── 1. Correr backtest en cada par ──────────────────────────────────
        all_trades: list[BacktestTrade] = []
        pair_summaries = []

        for pair in pairs:
            try:
                # Usar yearly engine para tener datos de un año completo
                result = await yearly_bt.run_yearly(pair, tf, capital=capital, risk_pct=risk)
                closed = [t for t in result.all_trades if t.result != "OPEN"]
                all_trades.extend(closed)
                pair_summaries.append({
                    "pair":    pair,
                    "trades":  result.total_trades,
                    "wr":      result.win_rate,
                    "total_r": result.total_r,
                })
                logger.info(f"Backtest {pair}: {result.total_trades} trades, {result.total_r:+.2f}R")
            except Exception as e:
                logger.error(f"Error backtest {pair}: {e}")
                pair_summaries.append({"pair": pair, "trades": 0, "wr": 0, "total_r": 0})

        if not all_trades:
            await msg.edit_text("❌ Sin trades encontrados en ningún par.")
            return

        # ── 2. Ordenar todas las señales cronológicamente ───────────────────
        all_trades.sort(key=lambda t: t.entry_time)

        # ── 3. Simular capital secuencialmente ──────────────────────────────
        running       = capital
        peak          = capital
        max_dd_usd    = 0.0
        equity        = [capital]
        wins          = 0
        losses        = 0
        total_r_sum   = 0.0
        monthly: dict[str, float] = {}   # "YYYY-MM" → capital al final del mes

        for t in all_trades:
            risk_usd   = running * (risk / 100)
            pnl_usd    = t.pnl_r * risk_usd
            running   += pnl_usd
            peak       = max(peak, running)
            dd         = peak - running
            max_dd_usd = max(max_dd_usd, dd)
            equity.append(round(running, 2))
            total_r_sum += t.pnl_r
            if t.pnl_r > 0: wins  += 1
            else:            losses += 1

            mk = t.entry_time.strftime("%Y-%m")
            monthly[mk] = running

        total_trades = len(all_trades)
        win_rate     = round(wins / total_trades * 100, 1) if total_trades else 0
        total_profit = running - capital
        profit_pct   = round(total_profit / capital * 100, 2)
        avg_r        = round(total_r_sum / total_trades, 3) if total_trades else 0
        max_dd_pct   = round(max_dd_usd / peak * 100, 2) if peak else 0

        result_emoji = "📈" if total_profit >= 0 else "📉"
        result_word  = "HABRÍAS GANADO" if total_profit >= 0 else "HABRÍAS PERDIDO"
        gain_color   = "✅" if total_profit >= 0 else "❌"

        # ── 4. Formatear mensaje ────────────────────────────────────────────
        text  = f"📅 *BACKTEST COMBINADO — TODOS LOS PARES*\n"
        text += f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        text += f"📊 Pares: `{' · '.join(pairs)}`\n"
        text += f"⏱ Timeframe: `{tf}` | ⚖️ Riesgo: `{risk}%`/trade\n\n"

        text += f"*🎯 Resultado total:*\n"
        text += f"  Trades: `{total_trades}` | ✅ Wins: `{wins}` | ❌ Losses: `{losses}`\n"
        text += f"  Win Rate: `{win_rate}%`\n"
        text += f"  Total R: `{total_r_sum:+.2f}R` | Avg/trade: `{avg_r:+.3f}R`\n\n"

        text += f"*💰 Simulación con ${capital:,.0f}:*\n"
        text += f"  {result_emoji} *{result_word}: `${abs(total_profit):,.2f}` (`{profit_pct:+.2f}%`)*\n"
        text += f"  Capital final: `${running:,.2f}`\n"
        text += f"  Capital pico: `${peak:,.2f}`\n"
        text += f"  📉 Max Drawdown: `${max_dd_usd:,.2f}` (`{max_dd_pct:.1f}%`)\n\n"

        # Desglose por par
        text += f"*📊 Por par:*\n```\n"
        text += f"{'Par':<10} {'Trades':>6} {'WR%':>6} {'R':>7}\n"
        text += f"{'─'*33}\n"
        for s in pair_summaries:
            text += f"{s['pair']:<10} {s['trades']:>6} {s['wr']:>5.1f}% {s['total_r']:>+7.2f}\n"
        text += f"```\n"

        # Desglose mensual
        if monthly:
            text += f"\n*📆 Capital por mes:*\n```\n"
            prev = capital
            for mk in sorted(monthly.keys()):
                cap      = monthly[mk]
                month_g  = cap - prev
                year, m  = mk.split("-")
                from tracking.pnl_tracker import MONTH_NAMES_ES
                mname    = f"{MONTH_NAMES_ES.get(m, m)[:3]} {year[2:]}"
                sign     = "▲" if month_g >= 0 else "▼"
                text    += f"{mname:<10} ${cap:>10,.2f}  {sign}${abs(month_g):>8,.2f}\n"
                prev     = cap
            text += "```"

        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error backtest combinado: {e}", exc_info=True)
        await msg.edit_text(
            f"❌ Error en backtest combinado.\n_Error: {str(e)[:120]}_",
            parse_mode=ParseMode.MARKDOWN,
        )


async def _run_yearly_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                pair: str, tf: str, capital: float, risk: float):
    cid = update.effective_chat.id
    msg = await context.bot.send_message(
        cid,
        f"📅 *Backtest Anual* — {pair} {tf}\n"
        f"💵 `${capital:,.0f}` | ⚖️ `{risk}%` riesgo\n"
        f"⏳ Descargando datos... (~1-2 min)",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        result = await yearly_bt.run_yearly(pair, tf, capital=capital, risk_pct=risk)
        await msg.edit_text(format_yearly_report(result), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error yearly {pair}: {e}", exc_info=True)
        await msg.edit_text(
            f"❌ Error en backtest anual de {pair}.\n"
            f"_El error fue: {str(e)[:100]}_",
            parse_mode=ParseMode.MARKDOWN,
        )


async def _show_liquidations(update: Update, context: ContextTypes.DEFAULT_TYPE, pair: str):
    cid = update.effective_chat.id
    msg = await context.bot.send_message(
        cid, f"💥 Liquidaciones *{pair}*...", parse_mode=ParseMode.MARKDOWN)
    try:
        price = await binance.get_price(pair)
        liq_data = await liq_monitor.get_liquidation_levels(pair, price)
        large    = await liq_monitor.get_recent_large_liquidations(pair)
        text     = liq_monitor.format_liquidation_summary(liq_data)
        if large:
            text += "\n\n💥 *Grandes recientes:*\n```"
            for liq in large[:5]:
                label = "LONG liq." if liq["side"] == "SELL" else "SHORT liq."
                text += f"\n{label}  ${liq['value_usd']:,.0f}  @  ${liq['price']:,.2f}"
            text += "```"
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error liquidaciones {pair}: {e}", exc_info=True)
        await msg.edit_text("❌ Error al obtener liquidaciones.")


# ─── Monitor automático ───────────────────────────────────────────────────────

class MarketMonitor:
    def __init__(self, app: Application):
        self.app = app
        self.scheduler  = AsyncIOScheduler()
        self._last_funding: dict = {}
        self._last_oi: dict = {}

        # ── Anti-duplicados mejorado ───────────────────────────────────────
        # Clave: "{pair}_{direction}" → último timestamp de envío
        # Hash incluye solo dirección (no precio exacto) para que cambios de
        # decimales no generen señales "nuevas"
        self._last_signal_sent: dict[str, float] = {}
        self._min_signal_cooldown = 3600  # 1 hora mínima entre señales del mismo par+dirección

    def start(self):
        self.scheduler.add_job(self.monitor_market, "interval",
                               seconds=config.CHECK_INTERVAL, id="monitor")
        self.scheduler.add_job(self.monitor_news, "interval", minutes=5, id="news")
        self.scheduler.add_job(self.send_daily_pnl, "cron",
                               hour=23, minute=55, id="daily_pnl")
        self.scheduler.add_job(self.send_market_summary, "interval", hours=4, id="summary")
        self.scheduler.start()
        logger.info("✅ Monitor v3.1 iniciado")

    async def _send(self, text: str):
        if not config.SIGNAL_CHANNEL_ID:
            return
        try:
            await self.app.bot.send_message(
                config.SIGNAL_CHANNEL_ID, text,
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Error send canal: {e}")

    def _is_duplicate(self, pair: str, direction: str) -> bool:
        """
        Devuelve True si ya se envió una señal del mismo par+dirección
        hace menos de min_signal_cooldown segundos.
        """
        key     = f"{pair}_{direction}"
        now_ts  = datetime.now(timezone.utc).timestamp()
        last_ts = self._last_signal_sent.get(key, 0)
        elapsed = now_ts - last_ts
        if elapsed < self._min_signal_cooldown:
            logger.debug(
                f"🔄 Duplicado bloqueado: {key} "
                f"(hace {int(elapsed/60)}min, cooldown={self._min_signal_cooldown//60}min)"
            )
            return True
        return False

    def _register_sent(self, pair: str, direction: str):
        key = f"{pair}_{direction}"
        self._last_signal_sent[key] = datetime.now(timezone.utc).timestamp()

    async def monitor_market(self):
        for pair in config.TRADING_PAIRS:
            try:
                md    = await binance.get_all_futures_market_data(pair)
                await save_market_snapshot(md)
                fr    = md.get("funding_rate", 0)
                oi    = md.get("oi_change_1h", 0)
                price = md.get("price", 0)

                # Alertas funding
                if abs(fr) >= config.FUNDING_RATE_THRESHOLD:
                    if abs(fr) > abs(self._last_funding.get(pair, 0)) * 1.1:
                        self._last_funding[pair] = fr
                        await self._send(
                            formatter.format_funding_alert(pair, fr, config.FUNDING_RATE_THRESHOLD))

                # Alertas OI
                if abs(oi) >= config.OI_CHANGE_THRESHOLD:
                    if abs(oi) > abs(self._last_oi.get(pair, 0)) * 1.1:
                        self._last_oi[pair] = oi
                        await self._send(formatter.format_oi_alert(pair, oi, price))

                # MTF + señal
                mtf_result = await mtf.full_analysis(pair, md)
                if (mtf_result.signal and mtf_result.aligned
                        and mtf_result.entry_15m == "CONFIRMED"):

                    direction = mtf_result.signal.direction

                    # ── Anti-duplicados: bloquear mismo par+dirección < 1h ──
                    if self._is_duplicate(pair, direction):
                        await asyncio.sleep(2)
                        continue

                    filter_r = signal_filter.evaluate(
                        mtf_result.signal, md, count_recent_signals(pair))

                    if filter_r.approved and filter_r.grade in ("A+", "A", "B"):
                        self._register_sent(pair, direction)
                        sid, text = await _save_and_track(
                            {"pair": pair, "direction": direction,
                             "entry_low":  mtf_result.signal.entry_low,
                             "entry_high": mtf_result.signal.entry_high,
                             "stop_loss":  mtf_result.signal.stop_loss,
                             "tp1":        mtf_result.signal.tp1,
                             "tp2":        mtf_result.signal.tp2,
                             "rr_ratio":   mtf_result.signal.rr_ratio,
                             "timeframe":  mtf_result.signal.timeframe,
                             "reason":     {"confluences": mtf_result.confluences}},
                            md, filter_r.grade, filter_r.score, filter_r.adjusted_risk,
                        )
                        await self._send(text)
                        logger.info(f"🎯 Señal {filter_r.grade} enviada: {pair} {direction}")

                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Error monitoreando {pair}: {e}")

    async def send_daily_pnl(self):
        try:
            await pnl_tracker.ensure_pnl_table()
            data = await pnl_tracker.get_daily_update(1000.0)
            if data.get("today_trades", 0) > 0:
                await self._send(format_daily_update(data))
        except Exception as e:
            logger.error(f"Error daily pnl: {e}")

    async def monitor_news(self):
        try:
            for news in await news_monitor.get_important_news():
                await save_news_alert(news)
                if news.get("impact") == "HIGH":
                    await self._send(formatter.format_news_alert(news))
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error monitor_news: {e}")

    async def send_market_summary(self):
        try:
            dl = [await binance.get_all_futures_market_data(p) for p in config.TRADING_PAIRS]
            summary = await macro_analyzer.generate_market_summary(dl)
            await self._send(formatter.format_market_summary(dl, summary))
        except Exception as e:
            logger.error(f"Error summary: {e}")


# ─── App ──────────────────────────────────────────────────────────────────────

def create_app() -> Application:
    global _app_ref
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .connect_timeout(30)       # segundos para establecer conexión
        .read_timeout(30)          # segundos para leer respuesta
        .write_timeout(30)         # segundos para enviar
        .pool_timeout(30)
        .build()
    )
    _app_ref = app
    signal_tracker.add_callback(_on_signal_update)

    # ── Error handler global — evita logs feos por timeouts de red ──
    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        err_str = str(err)
        # Timeouts de red → solo loguear en DEBUG, no son bugs
        if any(x in err_str for x in ("TimedOut", "NetworkError", "ConnectTimeout", "ReadTimeout")):
            logger.debug(f"Timeout de red (normal): {err_str[:80]}")
        else:
            logger.error(f"Error en update: {err}", exc_info=context.error)

    app.add_error_handler(_error_handler)

    cmds = [
        ("start", cmd_start), ("market", cmd_market), ("mtf", cmd_mtf),
        ("backtest", cmd_backtest), ("yearly", cmd_backtest_yearly),
        ("liquidations", cmd_liquidations), ("signals", cmd_signals),
        ("stats", cmd_stats), ("pnl", cmd_pnl), ("monthly", cmd_monthly),
        ("simulate", cmd_simulate), ("news", cmd_news),
        ("funding", cmd_funding), ("help", cmd_help),
        ("log", cmd_log), ("logerrors", cmd_logerrors), ("logsum", cmd_logsum),
        ("scan", lambda u, c: _scan_all(u, c)),
    ]
    for name, handler in cmds:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(button_callback))
    return app


async def _scan_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, context,
        f"🔍 Escaneando {len(config.TRADING_PAIRS)} pares...")
    for pair in config.TRADING_PAIRS:
        await _run_mtf_analysis(update, context, pair)
        await asyncio.sleep(3)
