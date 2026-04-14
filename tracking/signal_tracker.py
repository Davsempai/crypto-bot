"""
tracking/signal_tracker.py — Seguimiento en tiempo real de señales abiertas

Para cada señal abierta, monitorea el precio tick a tick y notifica:
  - TP1 alcanzado → cerrar 50%, mover SL a breakeven
  - TP2 alcanzado → cerrar 100%, operación completada
  - SL alcanzado  → pérdida, operación cerrada
  - Precio acercándose a SL (>70% del camino)
  - Trailing stop activado (después de TP1)
"""
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable
from typing import Any
from utils.database import get_open_signals, close_signal, save_signal
from utils.logger import setup_logger

logger = setup_logger("signal_tracker")


@dataclass
class LiveSignal:
    """Estado en tiempo real de una señal abierta."""
    db_id: int
    pair: str
    direction: str
    entry_low: float
    entry_high: float
    entry_price: float          # mid de la zona de entrada
    stop_loss: float
    original_sl: float          # SL original (para mover a BE)
    tp1: float
    tp2: float
    rr_ratio: float
    timeframe: str

    # Estado de la gestión
    tp1_hit: bool = False
    sl_moved_to_be: bool = False   # SL movido a breakeven
    trailing_active: bool = False
    trailing_sl: float = 0.0

    # Tracking
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_price: float = 0.0
    max_favorable: float = 0.0    # Máximo precio favorable visto
    current_pnl_r: float = 0.0    # P&L actual en R
    current_pnl_pct: float = 0.0

    # Alertas ya enviadas (evitar spam)
    warned_sl_close: bool = False
    warned_halfway: bool = False

    @property
    def risk(self) -> float:
        if self.direction == "LONG":
            return self.entry_price - self.original_sl
        return self.original_sl - self.entry_price

    def compute_pnl(self, current_price: float) -> tuple[float, float]:
        """Retorna (pnl_r, pnl_pct)."""
        if self.direction == "LONG":
            pnl_pct = (current_price - self.entry_price) / self.entry_price * 100
        else:
            pnl_pct = (self.entry_price - current_price) / self.entry_price * 100

        pnl_r = pnl_pct / (abs(self.risk) / self.entry_price * 100) if self.risk else 0
        return round(pnl_r, 3), round(pnl_pct, 3)

    def effective_sl(self) -> float:
        """SL activo (original, breakeven o trailing)."""
        if self.trailing_active and self.trailing_sl:
            return self.trailing_sl
        if self.sl_moved_to_be:
            return self.entry_price
        return self.stop_loss


@dataclass
class SignalUpdate:
    """Evento de actualización de una señal."""
    signal: LiveSignal
    event_type: str     # TP1_HIT / TP2_HIT / SL_HIT / SL_MOVED_BE / TRAILING_UPDATE / WARNING_SL / UPDATE_PNL
    current_price: float
    message: str
    pnl_r: float = 0.0
    pnl_pct: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SignalTracker:
    """
    Monitorea todas las señales abiertas en tiempo real.
    Notifica via callback cuando hay eventos importantes.
    """

    def __init__(self, binance_client: Any):
        self.binance = binance_client
        self._live_signals: dict[int, LiveSignal] = {}   # db_id → LiveSignal
        self._callbacks: list[Callable] = []
        self._running = False

    def add_callback(self, callback: Callable):
        """Registra una función para recibir actualizaciones."""
        self._callbacks.append(callback)

    async def _notify(self, update: SignalUpdate):
        """Notifica a todos los callbacks registrados."""
        for cb in self._callbacks:
            try:
                await cb(update)
            except Exception as e:
                logger.error(f"Error en callback: {e}")

    async def load_open_signals(self):
        """Carga señales abiertas desde la base de datos."""
        db_signals = await get_open_signals()
        for s in db_signals:
            if s["id"] not in self._live_signals:
                entry_price = (s["entry_low"] + s["entry_high"]) / 2
                live = LiveSignal(
                    db_id=s["id"],
                    pair=s["pair"],
                    direction=s["direction"],
                    entry_low=s["entry_low"],
                    entry_high=s["entry_high"],
                    entry_price=entry_price,
                    stop_loss=s["stop_loss"],
                    original_sl=s["stop_loss"],
                    tp1=s["tp1"],
                    tp2=s["tp2"],
                    rr_ratio=s["rr_ratio"],
                    timeframe=s["timeframe"],
                )
                self._live_signals[s["id"]] = live
                logger.info(f"📡 Trackeando señal #{s['id']}: {s['pair']} {s['direction']}")

    def register_new_signal(self, signal_data: dict, db_id: int):
        """Registra una señal nueva inmediatamente al crearla."""
        entry_price = (signal_data["entry_low"] + signal_data["entry_high"]) / 2
        live = LiveSignal(
            db_id=db_id,
            pair=signal_data["pair"],
            direction=signal_data["direction"],
            entry_low=signal_data["entry_low"],
            entry_high=signal_data["entry_high"],
            entry_price=entry_price,
            stop_loss=signal_data["stop_loss"],
            original_sl=signal_data["stop_loss"],
            tp1=signal_data["tp1"],
            tp2=signal_data["tp2"],
            rr_ratio=signal_data["rr_ratio"],
            timeframe=signal_data.get("timeframe", "1h"),
        )
        self._live_signals[db_id] = live
        logger.info(f"📡 Nueva señal registrada para tracking: #{db_id} {signal_data['pair']}")

    async def check_signal(self, sig: LiveSignal) -> list[SignalUpdate]:
        """Evalúa una señal con el precio actual y retorna eventos."""
        updates = []
        try:
            price = await self.binance.get_price(sig.pair)
            if not price:
                return []

            sig.last_price = price
            pnl_r, pnl_pct = sig.compute_pnl(price)
            sig.current_pnl_r = pnl_r
            sig.current_pnl_pct = pnl_pct

            # Precio favorable máximo
            if sig.direction == "LONG":
                sig.max_favorable = max(sig.max_favorable, price)
                going_up = price > sig.entry_price
            else:
                sig.max_favorable = max(sig.max_favorable, sig.entry_price * 2 - price)
                going_up = price < sig.entry_price

            effective_sl = sig.effective_sl()

            # ──────────────────────────────────────────────────
            # TP1 HIT
            # ──────────────────────────────────────────────────
            if not sig.tp1_hit:
                tp1_hit = (
                    (sig.direction == "LONG" and price >= sig.tp1) or
                    (sig.direction == "SHORT" and price <= sig.tp1)
                )
                if tp1_hit:
                    sig.tp1_hit = True
                    sig.sl_moved_to_be = True   # Mover SL a breakeven
                    sig.trailing_active = True

                    # Trailing SL inicial = breakeven
                    sig.trailing_sl = sig.entry_price

                    tp1_pnl_r = 0.75  # 50% de la posición cerrada a 1.5R
                    updates.append(SignalUpdate(
                        signal=sig,
                        event_type="TP1_HIT",
                        current_price=price,
                        pnl_r=tp1_pnl_r,
                        pnl_pct=pnl_pct,
                        message=self._format_tp1_message(sig, price),
                    ))
                    logger.info(f"🎯 TP1 hit #{sig.db_id} {sig.pair} @ ${price:,.2f}")

            # ──────────────────────────────────────────────────
            # TP2 HIT
            # ──────────────────────────────────────────────────
            elif sig.tp1_hit:
                tp2_hit = (
                    (sig.direction == "LONG" and price >= sig.tp2) or
                    (sig.direction == "SHORT" and price <= sig.tp2)
                )
                if tp2_hit:
                    tp2_r = round(0.5 * 1.5 + 0.5 * sig.rr_ratio, 2)
                    updates.append(SignalUpdate(
                        signal=sig,
                        event_type="TP2_HIT",
                        current_price=price,
                        pnl_r=tp2_r,
                        pnl_pct=pnl_pct,
                        message=self._format_tp2_message(sig, price, tp2_r),
                    ))
                    await close_signal(sig.db_id, "TP2", tp2_r)
                    del self._live_signals[sig.db_id]
                    logger.info(f"🏆 TP2 hit #{sig.db_id} {sig.pair} @ ${price:,.2f} | +{tp2_r}R")
                    return updates

                # Actualizar trailing stop
                if sig.trailing_active:
                    new_trail = self._compute_trailing_sl(sig, price)
                    if new_trail != sig.trailing_sl:
                        old_trail = sig.trailing_sl
                        sig.trailing_sl = new_trail
                        updates.append(SignalUpdate(
                            signal=sig,
                            event_type="TRAILING_UPDATE",
                            current_price=price,
                            pnl_r=pnl_r,
                            pnl_pct=pnl_pct,
                            message=self._format_trailing_message(sig, price, old_trail, new_trail),
                        ))

            # ──────────────────────────────────────────────────
            # SL HIT (original o trailing)
            # ──────────────────────────────────────────────────
            sl_hit = (
                (sig.direction == "LONG" and price <= effective_sl) or
                (sig.direction == "SHORT" and price >= effective_sl)
            )
            if sl_hit:
                if sig.tp1_hit:
                    # SL golpeó después de TP1 → al menos ganamos en TP1
                    final_r = 0.75  # Solo el TP1 (50% cerrado a 1.5R)
                    result_label = "BREAKEVEN/WIN"
                else:
                    final_r = -1.0
                    result_label = "SL"

                updates.append(SignalUpdate(
                    signal=sig,
                    event_type="SL_HIT",
                    current_price=effective_sl,
                    pnl_r=final_r,
                    pnl_pct=pnl_pct,
                    message=self._format_sl_message(sig, effective_sl, final_r, result_label),
                ))
                await close_signal(sig.db_id, result_label if sig.tp1_hit else "SL", final_r)
                del self._live_signals[sig.db_id]
                logger.info(f"❌ SL hit #{sig.db_id} {sig.pair} @ ${effective_sl:,.2f}")
                return updates

            # ──────────────────────────────────────────────────
            # ADVERTENCIA: precio cerca del SL
            # ──────────────────────────────────────────────────
            if not sig.tp1_hit and not sig.warned_sl_close:
                risk = abs(sig.entry_price - sig.stop_loss)
                dist_to_sl = abs(price - sig.stop_loss)
                if risk and dist_to_sl / risk < 0.25:  # A menos del 25% del SL
                    sig.warned_sl_close = True
                    updates.append(SignalUpdate(
                        signal=sig,
                        event_type="WARNING_SL",
                        current_price=price,
                        pnl_r=pnl_r,
                        pnl_pct=pnl_pct,
                        message=self._format_warning_message(sig, price),
                    ))

        except Exception as e:
            logger.error(f"Error checking signal #{sig.db_id}: {e}")

        return updates

    def _compute_trailing_sl(self, sig: LiveSignal, price: float) -> float:
        """
        Trailing stop que sigue el precio a una distancia del 50% del rango TP1-TP2.
        Se actualiza solo si es más favorable que el anterior.
        """
        trail_distance = abs(sig.tp2 - sig.tp1) * 0.5

        if sig.direction == "LONG":
            new_trail = round(price - trail_distance, 2)
            return max(new_trail, sig.trailing_sl)  # Solo sube
        else:
            new_trail = round(price + trail_distance, 2)
            return min(new_trail, sig.trailing_sl)  # Solo baja

    # ─── Formateo de mensajes ──────────────────────────────────────────────

    def _format_tp1_message(self, sig: LiveSignal, price: float) -> str:
        be_emoji = "🟢" if sig.direction == "LONG" else "🔴"
        return f"""🎯 *TP1 ALCANZADO — #{sig.db_id:04d}*
━━━━━━━━━━━━━━━━━━━━━━━━
{be_emoji} *{sig.pair}* {sig.direction}
💰 Precio actual: `${price:,.2f}`
✅ TP1: `${sig.tp1:,.2f}` → *CERRAR 50% ahora*

📋 *Qué hacer:*
1️⃣ Cierra el 50% de tu posición en `${sig.tp1:,.2f}`
2️⃣ Mueve el SL al breakeven → `${sig.entry_price:,.2f}`
3️⃣ Deja correr el 50% restante hacia TP2

🎯 TP2 objetivo: `${sig.tp2:,.2f}`
🛡 Nuevo SL (BE): `${sig.entry_price:,.2f}` ← *Ya estás en ganancia segura*
📊 P&L parcial: `+0.75R` (50% cerrado a 1.5R)

⏰ `{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC`"""

    def _format_tp2_message(self, sig: LiveSignal, price: float, total_r: float) -> str:
        be_emoji = "🟢" if sig.direction == "LONG" else "🔴"
        capital_gain = total_r  # Se calcula externamente con el capital real
        return f"""🏆 *TP2 ALCANZADO — #{sig.db_id:04d}*
━━━━━━━━━━━━━━━━━━━━━━━━
{be_emoji} *{sig.pair}* {sig.direction} ← *OPERACIÓN COMPLETADA*
💰 Precio: `${price:,.2f}` | TP2: `${sig.tp2:,.2f}`

✅ *Cierra el 50% restante de tu posición*

📊 *Resultado final:*
  • TP1 (50%) → `+1.5R`
  • TP2 (50%) → `+{sig.rr_ratio}R`
  • *Total ponderado: `+{total_r}R`*

🎉 ¡Operación exitosa! Registrada en el track record.
⏰ `{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC`"""

    def _format_sl_message(self, sig: LiveSignal, sl_price: float, final_r: float, result: str) -> str:
        if result != "SL":
            return f"""🛡 *SL ACTIVADO (BREAKEVEN) — #{sig.db_id:04d}*
━━━━━━━━━━━━━━━━━━━━━━━━
*{sig.pair}* {sig.direction}
🔒 SL en breakeven activado: `${sl_price:,.2f}`

📊 *Resultado: operación protegida*
  • TP1 (50%) cerrado previamente → `+1.5R`
  • 50% restante cerrado en BE → `0R`
  • *Total: `+0.75R`* — ¡Sin pérdida!

⏰ `{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC`"""

        return f"""❌ *STOP LOSS ACTIVADO — #{sig.db_id:04d}*
━━━━━━━━━━━━━━━━━━━━━━━━
*{sig.pair}* {sig.direction}
💔 SL tocado: `${sl_price:,.2f}`
📊 Resultado: `-1R`

💡 *Recuerda:* Las pérdidas son parte del proceso.
Con gestión correcta (1% de riesgo), esto es un pequeño retroceso.

📈 El sistema necesita: >50% win rate o buen R:R para ser rentable.
⏰ `{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC`"""

    def _format_trailing_message(self, sig: LiveSignal, price: float, old_sl: float, new_sl: float) -> str:
        return f"""🔄 *TRAILING STOP ACTUALIZADO — #{sig.db_id:04d}*
━━━━━━━━━━━━━━━━━━━━━━━━
*{sig.pair}* {sig.direction}
💰 Precio: `${price:,.2f}`
📍 SL anterior: `${old_sl:,.2f}`
🔒 *SL nuevo: `${new_sl:,.2f}`* ← actualizado

🎯 TP2 objetivo: `${sig.tp2:,.2f}`
📊 P&L actual: `{sig.current_pnl_r:+.2f}R` (`{sig.current_pnl_pct:+.2f}%`)
⏰ `{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC`"""

    def _format_warning_message(self, sig: LiveSignal, price: float) -> str:
        dist = abs(price - sig.stop_loss)
        be_emoji = "⚠️"
        return f"""{be_emoji} *PRECIO CERCA DEL SL — #{sig.db_id:04d}*
━━━━━━━━━━━━━━━━━━━━━━━━
*{sig.pair}* {sig.direction}
💰 Precio actual: `${price:,.2f}`
🛑 Stop Loss: `${sig.stop_loss:,.2f}` (a `${dist:,.2f}` de distancia)
📊 P&L actual: `{sig.current_pnl_r:+.2f}R` (`{sig.current_pnl_pct:+.2f}%`)

⚠️ _El precio está en el 25% final antes del SL_
_No muevas el SL — deja que el sistema gestione._
⏰ `{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC`"""

    # ─── Loop principal ────────────────────────────────────────────────────

    async def start(self):
        """Inicia el loop de tracking en background."""
        self._running = True
        await self.load_open_signals()
        logger.info(f"🔍 Signal tracker iniciado — {len(self._live_signals)} señales activas")

        while self._running:
            await self.load_open_signals()  # Chequear nuevas señales cada ciclo

            for sig_id in list(self._live_signals.keys()):
                if sig_id not in self._live_signals:
                    continue
                sig = self._live_signals[sig_id]
                updates = await self.check_signal(sig)
                for update in updates:
                    await self._notify(update)

            await asyncio.sleep(15)  # Chequear cada 15 segundos

    def stop(self):
        self._running = False

    def get_active_count(self) -> int:
        return len(self._live_signals)

    def get_live_signals(self) -> list[LiveSignal]:
        return list(self._live_signals.values())
