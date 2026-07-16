import logging
import time
import MetaTrader5 as mt5
from models import TradeSignal, TradeAction
from services.base_service import BaseService

logger = logging.getLogger(__name__)

class LoganGoldService(BaseService):
    def __init__(self, channel_id: int, mapper, executor):
        super().__init__(channel_id, executor)
        self.mapper = mapper
        
        # Magic number y configuraciones exclusivas de Logan
        self.magic_number = 202611
        self.max_lot_per_order = 0.50 
        
        # Guardamos el último ticket para poder aplicarle cierres parciales
        self.last_ticket = None

    async def process_message(self, message: str):
        self.logger.info("📩 Procesando mensaje de Logan Gold...")
        msg_lower = message.lower().strip()
        
        # 1. FILTROS DE GESTIÓN DE TP Y CIERRES ESPECÍFICOS DE LOGAN
        
        # --- TP 2: Asegurar el 80% de la posición inicial ---
        if msg_lower.startswith("tp 2"):
            if self.last_ticket:
                self.logger.info("✂️ TP 2 alcanzado. Cerrando el 80% del volumen inicial...")
                self._execute_partial_close(self.last_ticket, percentage=0.80)
            else:
                self.logger.warning("⚠️ TP 2 solicitado, pero no hay ticket activo en memoria.")
            return

        # --- TP 3: Asegurar otro 10% de la posición inicial ---
        if msg_lower.startswith("tp 3"):
            if self.last_ticket:
                self.logger.info("✂️ TP 3 alcanzado. Cerrando la mitad del volumen restante (10% del total)...")
                # Pasamos 0.50 porque queremos la mitad del 20% que quedaba vivo
                self._execute_partial_close(self.last_ticket, percentage=0.50)
            else:
                self.logger.warning("⚠️ TP 3 solicitado, pero no hay ticket activo en memoria.")
            return
            
        # --- Cierre total ---
        if any(kw in msg_lower for kw in ["todos los tps", "posiciones cerradas"]):
            if self.last_ticket:
                self.logger.info("🛑 Comando de cierre total detectado. Cerrando el resto de la posición.")
                self._execute_complete_close(self.last_ticket)
            return
        
        # 2. MAPEO DE SEÑAL NORMAL
        signal = self.mapper.map_message(message)
        if not signal:
            return

        # 3. ENRUTADOR DE ACCIONES
        if signal.action in [TradeAction.BUY, TradeAction.SELL]:
            self._execute_market_order(signal)
            
        elif signal.action == TradeAction.BREAKEVEN:
            self.logger.info("🛡️ Comando de Breakeven en vivo detectado para Logan. Protegiendo posición...")
            self._set_trades_to_breakeven()

    def _execute_market_order(self, signal: TradeSignal):
        """Abre una posición instantánea a mercado y verifica el precio de apertura."""
        balance = self.executor.get_account_balance()
        if balance <= 0:
            self.logger.error("❌ No se pudo obtener el balance para LoganGold.")
            return

        # Lote proporcional específico de Logan (0.50 lotes para cuenta de 200k)
        calculated_lot = round(balance * 0.0000025, 2)
        lot = min(calculated_lot, self.max_lot_per_order)
        
        symbol = signal.symbol if signal.symbol else "XAUUSD"
        si = self.executor.get_symbol_info(symbol)
        lot = max(lot, si.volume_min) if si else max(lot, 0.01)

        tick = self.executor.get_tick(symbol)
        if not tick:
            self.logger.error(f"❌ Imposible obtener cotización en vivo de {symbol}.")
            return

        order_type = mt5.ORDER_TYPE_BUY if signal.action == TradeAction.BUY else mt5.ORDER_TYPE_SELL
        price = tick.ask if signal.action == TradeAction.BUY else tick.bid

        self.logger.info(f"⚡ Lanzando orden instantánea LoganGold: {signal.action.name} {lot} lotes a {price}")

        # Delegamos el envío puro de la orden al executor genérico
        res = self.executor.send_order(
            symbol=symbol,
            order_type=order_type,
            volume=lot,
            price=price,
            sl=signal.stop_loss,
            tp=signal.take_profits[0] if signal.take_profits else 0.0,
            magic=self.magic_number,
            comment="LoganGold Immediate",
            is_market=True
        )

        if res:
            actual_price = res.price
            
            # --- SOLUCIÓN CRÍTICA: ANTI-PRECIO 0.0 ---
            if actual_price <= 0.0:
                self.logger.warning("⚠️ Broker retornó precio 0.0. Recuperando desde MT5...")
                for _ in range(5):
                    positions = self.executor.get_positions(ticket=res.order)
                    if positions:
                        actual_price = positions[0].price_open
                        self.logger.info(f"🎯 Precio real recuperado de la posición viva: {actual_price}")
                        break
                    time.sleep(0.05)
            
            # Guardamos el ticket para futuros cierres parciales
            self.last_ticket = res.order
            self.logger.info(f"✅ Trade LoganGold vivo. Ticket: #{res.order}")
        else:
            self.logger.error("❌ Fallo en la apertura de orden LoganGold.")

    def _execute_partial_close(self, ticket: int, percentage: float = 0.5):
        """Calcula el volumen correspondiente al porcentaje y solicita el cierre al ejecutor."""
        positions = self.executor.get_positions(ticket=ticket)
        if not positions:
            self.logger.error(f"❌ No se localizó el Ticket #{ticket} para cierre parcial.")
            return False
            
        pos = positions[0]
        volume_to_close = round(pos.volume * percentage, 2)
        
        # Respetar el lote mínimo del broker
        si = self.executor.get_symbol_info(pos.symbol)
        min_vol = si.volume_min if si else 0.01
        if volume_to_close < min_vol:
            self.logger.warning(f"⚠️ El volumen a cerrar ({volume_to_close}) es menor al mínimo permitido.")
            return False

        success = self.executor.close_position(ticket, volume_to_close)
        if success:
            self.logger.info(f"✂️✅ Reducción del {percentage*100}% completada: {volume_to_close} lotes cerrados.")
        return success

    def _execute_complete_close(self, ticket: int):
        """Cierra el 100% de la posición actual."""
        positions = self.executor.get_positions(ticket=ticket)
        if not positions:
            return False
            
        success = self.executor.close_position(ticket, positions[0].volume)
        if success:
            self.logger.info(f"🛑✅ Posición #{ticket} cerrada por completo.")
            self.last_ticket = None  # Limpiamos el tracker
        return success
    
    def _set_trades_to_breakeven(self):
        """Mueve el Stop Loss de las posiciones activas de Logan al precio de entrada más colchón."""
        positions = self.executor.get_positions(magic_number=self.magic_number)
        if not positions:
            self.logger.info("ℹ️ No hay posiciones abiertas de LoganGold para aplicar BE.")
            return

        for pos in positions:
            symbol_info = self.executor.get_symbol_info(pos.symbol)
            if not symbol_info: continue

            point = symbol_info.point
            digits = symbol_info.digits
            
            current_spread_points = symbol_info.spread
            safety_offset_points = max(current_spread_points + 5, 20) 
            offset = safety_offset_points * point

            open_price = pos.price_open
            
            if pos.type == mt5.POSITION_TYPE_BUY:
                new_sl = round(open_price + offset, digits)
                if pos.sl >= new_sl and pos.sl != 0.0: continue
            else:
                new_sl = round(open_price - offset, digits)
                if pos.sl <= new_sl and pos.sl != 0.0: continue

            success = self.executor.modify_position_sl(pos.ticket, new_sl)
            if success:
                self.logger.info(f"✅ SL de Logan Ticket #{pos.ticket} blindado en {new_sl}")