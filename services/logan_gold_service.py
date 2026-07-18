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

    async def process_message(self, message: str):
        self.logger.info("📩 Procesando mensaje de Logan Gold...")
        msg_lower = message.lower().strip()
        
        # 1. FILTROS DE GESTIÓN DE TP Y CIERRES ESPECÍFICOS DE LOGAN

        # --- TP 1: No hacer nada (Estrategia Logan Gold) ---
        if "tp 1" in msg_lower:
            # Si el mensaje exige mover a BE en el mismo texto (ej: "TP 1 MUEVAN SL A BE YA"),
            # dejamos pasar el flujo para que aplique el Breakeven. Si no, salimos sin operar.
            if not any(kw in msg_lower for kw in ["be", "sl a be", "muevan", "asegurar"]):
                self.logger.info("🎯 TP 1 detectado en Telegram. Estrategia: Mantener la posición (Sin acciones en TP1).")
                return

        # --- TP 2: Asegurar el 80% de todas las posiciones vivas ---
        if msg_lower.startswith("tp 2"):
            self.logger.info("✂️ TP 2 alcanzado. Reduciendo el 80% del volumen de las posiciones...")
            self._execute_partial_close_all(percentage=0.80)
            return

        # --- TP 3: Asegurar otro 10% del total (50% de lo que queda vivo) ---
        if msg_lower.startswith("tp 3"):
            self.logger.info("✂️ TP 3 alcanzado. Reduciendo la mitad del volumen restante (10% del inicial)...")
            self._execute_partial_close_all(percentage=0.50)
            return
            
        # --- Cierre total ---
        if any(kw in msg_lower for kw in ["todos los tps", "posiciones cerradas", "cerrar todo"]):
            self.logger.info("🛑 Comando de cierre total detectado. Liquidando cartera Logan Gold...")
            self._execute_complete_close_all()
            return

        # 2. MAPEO DE SEÑAL NORMAL
        signal = self.mapper.map_message(message)
        if not signal:
            return

        # 3. ENRUTADOR DE ACCIONES ACTUALIZADO
        if signal.action in [TradeAction.BUY, TradeAction.SELL]:
            # Si es una entrada inmediata de pánico ("YA"), no tendrá rango de entrada (0.0)
            if signal.entry_min == 0.0 and signal.entry_max == 0.0:
                self._execute_market_order(signal)
            else:
                # Si contiene rango (ej: 3994 - 4000), es el bloque de parámetros estructurados
                self._process_signal_parameters(signal)
            
        elif signal.action == TradeAction.BREAKEVEN:
            self.logger.info("🛡️ Comando de Breakeven en vivo detectado. Protegiendo posiciones...")
            self._set_trades_to_breakeven()

    def _execute_market_order(self, signal: TradeSignal):
        """Abre una posición instantánea a mercado para el gatillo de pánico."""
        balance = self.executor.get_account_balance()
        if balance <= 0:
            self.logger.error("❌ No se pudo obtener el balance de la cuenta para LoganGold.")
            return

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

        # Enviamos la orden al broker con TP 0.0 para gestionar las salidas de forma dinámica
        res = self.executor.send_order(
            symbol=symbol,
            order_type=order_type,
            volume=lot,
            price=price,
            sl=signal.stop_loss,
            tp=0.0,  # Sin TP rígido para evitar cierres prematuros en MT5
            magic=self.magic_number,
            comment="LoganGold Immediate",
            is_market=True
        )

        if res:
            actual_price = res.price
            if actual_price <= 0.0:
                self.logger.warning("⚠️ Broker retornó precio 0.0. Recuperando precio real de la API...")
                for _ in range(5):
                    positions = self.executor.get_positions(ticket=res.order)
                    if positions:
                        actual_price = positions[0].price_open
                        self.logger.info(f"🎯 Precio real recuperado de la posición viva: {actual_price}")
                        break
                    time.sleep(0.05)
            self.logger.info(f"✅ Trade instantáneo LoganGold vivo. Ticket: #{res.order}")
        else:
            self.logger.error("❌ Fallo en la apertura de la orden instantánea de LoganGold.")

    def _process_signal_parameters(self, signal: TradeSignal):
        """
        Procesa el bloque de parámetros estructurados:
        1. Modifica el SL del trade ya abierto en mercado con el "YA".
        2. Deja un límite (Buy Limit o Sell Limit) en la zona extrema para cuando el precio rellene.
        """
        self.logger.info(f"📊 Procesando parámetros estructurados para {signal.symbol}...")
        
        # 1. ACTUALIZAR EL SL DE TODAS LAS OPERACIONES QUE YA ESTÉN ABIERTAS (del "YA")
        active_positions = self.executor.get_positions(magic_number=self.magic_number)
        if active_positions:
            self.logger.info(f"🔄 Seteando Stop Loss a {signal.stop_loss} para {len(active_positions)} posiciones vivas...")
            for pos in active_positions:
                self.executor.modify_position_sl(pos.ticket, signal.stop_loss)
        else:
            self.logger.warning("⚠️ No se encontraron posiciones activas para ajustar el SL.")

        # 2. COLOCAR LA ORDEN LÍMITE EN LA PARTE ALTA/BAJA DE LA ZONA
        symbol = signal.symbol if signal.symbol else "XAUUSD"
        si = self.executor.get_symbol_info(symbol)
        
        # Calculamos el precio de la orden límite
        # Para SELL: Colocamos Sell Limit en el extremo superior de la zona - 1.0 (ej: 4000 - 1.0 = 3999)
        # Para BUY: Colocamos Buy Limit en el extremo inferior de la zona + 1.0 (ej: 3994 + 1.0 = 3995)
        if signal.action == TradeAction.SELL:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT
            limit_price = signal.entry_max - 1.0
        else:
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
            limit_price = signal.entry_min + 1.0

        # Lote proporcional idéntico al de mercado
        balance = self.executor.get_account_balance()
        calculated_lot = round(balance * 0.0000025, 2)
        lot = min(calculated_lot, self.max_lot_per_order)
        lot = max(lot, si.volume_min) if si else max(lot, 0.01)

        self.logger.info(f"⏳ Colocando orden pendiente Limit en {symbol}: {limit_price} (SL: {signal.stop_loss})")
        
        # Enviamos la orden pendiente (TP en 0.0 para que no interfiera con el broker)
        self.executor.send_order(
            symbol=symbol,
            order_type=order_type,
            volume=lot,
            price=limit_price,
            sl=signal.stop_loss,
            tp=0.0,
            magic=self.magic_number,
            comment="LoganGold Limit Zone",
            is_market=False
        )

    def _execute_partial_close_all(self, percentage: float = 0.5):
        """Cierra el porcentaje especificado de volumen en TODAS las posiciones activas de Logan."""
        positions = self.executor.get_positions(magic_number=self.magic_number)
        if not positions:
            self.logger.warning("⚠️ No hay posiciones vivas en cartera de Logan para aplicar cierre parcial.")
            return

        for pos in positions:
            volume_to_close = round(pos.volume * percentage, 2)
            
            si = self.executor.get_symbol_info(pos.symbol)
            min_vol = si.volume_min if si else 0.01
            
            # En caso de que el volumen restante sea minúsculo, cerramos todo el lote residual
            if volume_to_close < min_vol:
                self.logger.warning(f"⚠️ Volumen a cerrar ({volume_to_close}) menor que el mínimo. Forzando cierre completo para el Ticket #{pos.ticket}.")
                volume_to_close = pos.volume

            success = self.executor.close_position(pos.ticket, volume_to_close)
            if success:
                self.logger.info(f"✂️ Ticket #{pos.ticket}: {volume_to_close} lotes cerrados con éxito ({percentage*100}%).")

    def _execute_complete_close_all(self):
        """Cierra el 100% de todas las posiciones activas de Logan y limpia las pendientes."""
        # 1. Liquidar trades vivos
        positions = self.executor.get_positions(magic_number=self.magic_number)
        if positions:
            for pos in positions:
                self.executor.close_position(pos.ticket, pos.volume)
            self.logger.info("🛑 Todas las posiciones de Logan Gold han sido liquidadas de mercado.")
        
        # 2. Eliminar órdenes limit de la zona que no se hayan completado
        pending_orders = self.executor.get_pending_orders(magic_number=self.magic_number)
        if pending_orders:
            for order in pending_orders:
                self.executor.cancel_pending_order(order.ticket)
            self.logger.info("🧹 Órdenes límite pendientes eliminadas de la zona.")

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