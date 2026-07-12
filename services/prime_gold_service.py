import logging
import asyncio
from models import TradeAction

logger = logging.getLogger(__name__)

class PrimeGoldService:
    def __init__(self, channel_id, mapper, executor):
        self.channel_id = channel_id
        self.mapper = mapper
        self.executor = executor
        self.last_valid_signal = None  # Estado aislado para este servicio

    async def process_message(self, message_text):
        signal = self.mapper.map_message(message_text)
        if not signal: 
            return

        # CASO: ACTIVA
        if signal.action == TradeAction.ACTIVATE:
            if self.last_valid_signal:
                logger.info(f"[PrimeGold] ⚡ 'Activa' detectado. Entrando a mercado...")
                success = await asyncio.to_thread(self.executor.execute_market, self.last_valid_signal)
                if success: logger.info("[PrimeGold] ✅ Ejecución market completada.")
                await asyncio.to_thread(self.executor.cancel_pending_orders)
            else:
                logger.warning("[PrimeGold] ⚠️ Se recibió 'Activa' sin señal previa.")

        # CASO: BREAKEVEN
        elif signal.action == TradeAction.BREAKEVEN:
            logger.info("[PrimeGold] 🛡️ Mensaje de BREAKEVEN recibido...")
            await asyncio.to_thread(self.executor.set_trades_to_breakeven)

        # CASO: SEÑAL NUEVA
        elif signal.action in [TradeAction.BUY, TradeAction.SELL]:
            self.last_valid_signal = signal
            logger.info(f"[PrimeGold] 🎯 Nueva señal guardada: {signal.action.value} {signal.symbol}")
            await asyncio.to_thread(self.executor.execute, signal)

        # CASO: CANCELACIÓN
        elif signal.action == TradeAction.CANCEL:
            logger.info("[PrimeGold] 🛑 Cancelando órdenes...")
            await asyncio.to_thread(self.executor.cancel_pending_orders)