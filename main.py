"""
main.py — Crypto Futures Bot v4 — Bot + Dashboard simultáneos
"""
import asyncio
import os
import sys
import argparse

os.makedirs("data", exist_ok=True)
os.makedirs("dashboard/static", exist_ok=True)

from config import config
from utils.logger import setup_logger
from utils.database import init_db
from tracking.pnl_tracker import pnl_tracker

logger = setup_logger("main")


async def run_bot():
    """Corre solo el bot de Telegram."""
    from bot.telegram_bot import create_app, MarketMonitor, signal_tracker
    app = create_app()
    monitor = MarketMonitor(app)

    async with app:
        await app.initialize()
        await app.start()
        monitor.start()
        tracker_task = asyncio.create_task(signal_tracker.start())
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("🤖 Bot Telegram online")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            signal_tracker.stop()
            tracker_task.cancel()
            monitor.scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


async def run_dashboard(host: str = "0.0.0.0", port: int = 8080):
    """Corre solo el dashboard web."""
    import uvicorn
    from dashboard.api.server import app as web_app

    cfg = uvicorn.Config(web_app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    logger.info(f"🌐 Dashboard disponible en http://localhost:{port}")
    await server.serve()


async def run_all(host: str = "0.0.0.0", port: int = 8080):
    """Corre bot + dashboard en paralelo."""
    from bot.telegram_bot import create_app, MarketMonitor, signal_tracker
    import uvicorn
    from dashboard.api.server import app as web_app

    # Bot
    tg_app = create_app()
    monitor = MarketMonitor(tg_app)

    # Dashboard
    uvi_cfg = uvicorn.Config(web_app, host=host, port=port, log_level="warning")
    uvi_server = uvicorn.Server(uvi_cfg)

    async with tg_app:
        await tg_app.initialize()
        await tg_app.start()
        monitor.start()
        tracker_task = asyncio.create_task(signal_tracker.start())
        await tg_app.updater.start_polling(drop_pending_updates=True)

        logger.info("🚀 Sistema completo online:")
        logger.info(f"   🤖 Bot Telegram: activo")
        logger.info(f"   🌐 Dashboard: http://localhost:{port}")
        logger.info(f"   📡 WebSocket: ws://localhost:{port}/ws/live")

        try:
            await asyncio.gather(
                asyncio.Event().wait(),
                uvi_server.serve(),
            )
        except (KeyboardInterrupt, SystemExit):
            logger.info("⏹ Deteniendo sistema...")
        finally:
            signal_tracker.stop()
            tracker_task.cancel()
            monitor.scheduler.shutdown(wait=False)
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
            logger.info("👋 Sistema detenido")


async def startup():
    """Inicialización común."""
    logger.info("=" * 55)
    logger.info("🤖 CRYPTO FUTURES BOT v4")
    logger.info("=" * 55)

    errors = config.validate()
    for e in errors:
        logger.warning(f"⚠️  {e}")

    await init_db()
    await pnl_tracker.ensure_pnl_table()
    logger.info("✅ Base de datos lista")
    mode = "🧪 PAPER TRADING" if config.PAPER_TRADING else "💰 LIVE MODE"
    logger.info(f"✅ {mode} | Pares: {', '.join(config.TRADING_PAIRS)}")


def main():
    parser = argparse.ArgumentParser(description="Crypto Futures Bot v4")
    parser.add_argument("--mode", choices=["all", "bot", "dashboard"], default="all",
                        help="Modo de ejecución (default: all)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Puerto del dashboard (default: 8080)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host del dashboard (default: 0.0.0.0)")
    args = parser.parse_args()

    if not config.TELEGRAM_BOT_TOKEN and args.mode != "dashboard":
        logger.error("❌ TELEGRAM_BOT_TOKEN requerido para modo bot/all")
        if args.mode == "bot":
            sys.exit(1)

    async def run():
        await startup()
        if args.mode == "all":
            await run_all(args.host, args.port)
        elif args.mode == "bot":
            await run_bot()
        elif args.mode == "dashboard":
            await run_dashboard(args.host, args.port)

    asyncio.run(run())


if __name__ == "__main__":
    main()
