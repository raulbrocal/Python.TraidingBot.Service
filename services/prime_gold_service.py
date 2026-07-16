import logging
import MetaTrader5 as mt5
from models import TradeSignal, TradeAction
from services.base_service import BaseService

logger = logging.getLogger(__name__)

class PrimeGoldService(BaseService):
    def __init__(self, channel_id: int, mapper, executor):
        # Llama al constructor del BaseService (asigna self.channel_id, self.executor y self.logger)
        super().__init__(channel_id, executor)
        self.mapper = mapper
        
        # Configuración de riesgo y magic number exclusiva de Prime Gold
        self.magic_number = 123456
        self.risk_percent = 0.5  # 0.5% de riesgo total por señal
        self.max_lot_per_order = 0.5 

    async def process_message(self, message: str):
        self.logger.info("📩 Procesando mensaje del canal...")
        
        msg_lower = message.lower().strip()
        
        # 1. FILTRO DE BREAKEVEN ESPECÍFICO DE PRIME GOLD
        # Hacemos este check primero para cazar sus expresiones exactas
        be_keywords = [
            "sl a be", "sl en be", "break even", "breakeven", 
            "muevan a be", "asegurar", "aseguren",
            "sl al precio de entrada",
        ]
        if any(keyword in msg_lower for keyword in be_keywords):
            self.logger.info("🛡️ Comando de Breakeven explícito detectado. Asegurando la matriz...")
            self._set_trades_to_breakeven()
            return

        # 2. MAPEO NORMAL DE LA SEÑAL (Matriz, Market, Cancelación)
        signal = self.mapper.map_message(message)
        if not signal:
            return

        # 3. ENRUTADOR DE ACCIONES
        if signal.action in [TradeAction.BUY, TradeAction.SELL]:
            self._execute_matrix(signal)
            
        elif signal.action == TradeAction.ACTIVATE:
            self._execute_market(signal)
            
        elif signal.action == TradeAction.BREAKEVEN:
            self._set_trades_to_breakeven()
            
        elif signal.action == TradeAction.CANCEL:
            self.logger.info("🗑️ Comando de cancelación detectado. Limpiando órdenes...")
            self._cancel_pending_orders()

    def _calculate_lot(self, symbol: str, entry: float, sl: float, risk_money: float) -> float:
        """Cálculo de lotaje proporcional al riesgo por orden de la matriz."""
        si = self.executor.get_symbol_info(symbol)
        dist = abs(entry - sl)
        
        if dist <= 0 or not si: 
            return 0.01
        
        risk_per_lot = (si.trade_tick_value / si.trade_tick_size) * dist
        if risk_per_lot == 0:
            return 0.01

        lot = round(risk_money / risk_per_lot, 2)
        lot = min(lot, self.max_lot_per_order)
        return max(lot, si.volume_min)

    def _execute_matrix(self, signal: TradeSignal):
        """Lógica central: Distribuye el riesgo en 9 órdenes (3 Entradas x 3 TPs)."""
        balance = self.executor.get_account_balance()
        if balance <= 0:
            self.logger.error("❌ No se pudo obtener el balance de la cuenta para calcular el riesgo.")
            return

        # Dividimos el riesgo total de la señal entre las 9 balas de la matriz
        total_risk_money = balance * (self.risk_percent / 100)
        risk_per_order = total_risk_money / 9 

        p_mid = (signal.entry_min + signal.entry_max) / 2
        entries = [signal.entry_min, p_mid, signal.entry_max]
        
        tps = signal.take_profits[:3]
        if len(tps) < 3: 
            tps = tps * 3

        tick = self.executor.get_tick(signal.symbol)
        if not tick:
            self.logger.error(f"❌ No se pudo obtener el precio actual de {signal.symbol}")
            return

        current_ask = tick.ask
        current_bid = tick.bid

        self.logger.info(f"📊 Ejecutando Matriz 3x3 en {signal.symbol}. Riesgo/Orden: ${risk_per_order:.2f}")

        for entry_p in entries:
            for tp_p in tps:
                lot = self._calculate_lot(signal.symbol, entry_p, signal.stop_loss, risk_per_order)
                
                is_market = False
                exec_price = entry_p
                
                if signal.action == TradeAction.BUY:
                    order_type = mt5.ORDER_TYPE_BUY_LIMIT
                    # Rescate de limit a market si el precio ya cruzó el punto de entrada
                    if current_ask <= entry_p:
                        is_market = True
                        order_type = mt5.ORDER_TYPE_BUY
                        exec_price = current_ask
                else:
                    order_type = mt5.ORDER_TYPE_SELL_LIMIT
                    if current_bid >= entry_p:
                        is_market = True
                        order_type = mt5.ORDER_TYPE_SELL
                        exec_price = current_bid

                comment = f"M3x3 {'MKT' if is_market else 'LMT'} {entry_p}"
                
                res = self.executor.send_order(
                    symbol=signal.symbol,
                    order_type=order_type,
                    volume=lot,
                    price=exec_price,
                    sl=signal.stop_loss,
                    tp=tp_p,
                    magic=self.magic_number,
                    comment=comment,
                    is_market=is_market
                )
                
                if res:
                    if is_market:
                        self.logger.info(f"⚡ Rescate exitoso: Orden Market ejecutada a {exec_price}")
                else:
                    tipo = "Mercado" if is_market else "Límite"
                    self.logger.error(f"❌ Falló el envío de orden {tipo} en nivel {exec_price}")

    def _execute_market(self, signal: TradeSignal):
        """Entrada de pánico a mercado (Comando 'Activa' en Prime Gold)."""
        balance = self.executor.get_account_balance()
        if balance <= 0: return

        # Si el mapper no extrajo el símbolo, usamos Gold por defecto
        symbol = signal.symbol if signal.symbol else "XAUUSD"
        
        tick = self.executor.get_tick(symbol)
        if not tick: return

        current_price = tick.ask if signal.action == TradeAction.BUY else tick.bid
        
        # En una entrada activa, dividimos el riesgo solo en 3 (los 3 TPs)
        total_risk_money = balance * (self.risk_percent / 100)
        risk_per_order = total_risk_money / 3 
        
        tps = signal.take_profits[:3]
        if not tps:
            self.logger.warning("⚠️ Señal ACTIVA recibida sin TPs definidos, abortando ejecución.")
            return

        self.logger.info(f"⚡ EJECUCIÓN MARKET (ACTIVA) en {symbol} a {current_price}")

        order_type = mt5.ORDER_TYPE_BUY if signal.action == TradeAction.BUY else mt5.ORDER_TYPE_SELL

        for tp_p in tps:
            lot = self._calculate_lot(symbol, current_price, signal.stop_loss, risk_per_order)
            self.executor.send_order(
                symbol=symbol,
                order_type=order_type,
                volume=lot,
                price=current_price,
                sl=signal.stop_loss,
                tp=tp_p,
                magic=self.magic_number,
                comment="Market (Activa)",
                is_market=True
            )

    def _set_trades_to_breakeven(self):
        """Mueve el Stop Loss al precio de entrada más un colchón por spread y limpia las pendientes."""
        positions = self.executor.get_positions(magic_number=self.magic_number)
        
        if not positions:
            self.logger.info("ℹ️ No hay posiciones abiertas. Limpiando posibles órdenes límite huérfanas.")
            self._cancel_pending_orders()
            return

        active_symbols = set(pos.symbol for pos in positions)

        for symbol in active_symbols:
            symbol_info = self.executor.get_symbol_info(symbol)
            if not symbol_info: continue

            point = symbol_info.point
            digits = symbol_info.digits
            
            current_spread_points = symbol_info.spread
            safety_offset_points = max(current_spread_points + 5, 20) 
            offset = safety_offset_points * point

            self.logger.info(f"🔄 Aplicando BE en matriz {symbol} (Colchón Spread: +{safety_offset_points} ptos)")

            symbol_positions = [p for p in positions if p.symbol == symbol]
            for pos in symbol_positions:
                open_price = pos.price_open
                
                # Cálculo de BE según dirección
                if pos.type == mt5.POSITION_TYPE_BUY:
                    new_sl = round(open_price + offset, digits)
                    # Evitar modificar si el SL actual ya es mejor
                    if pos.sl >= new_sl and pos.sl != 0.0: continue
                else:
                    new_sl = round(open_price - offset, digits)
                    if pos.sl <= new_sl and pos.sl != 0.0: continue

                success = self.executor.modify_position_sl(pos.ticket, new_sl)
                if success:
                    self.logger.info(f"✅ SL Ticket #{pos.ticket} blindado en {new_sl}")

        self.logger.info("🧹 Ejecutando limpieza de órdenes límite no activadas...")
        self._cancel_pending_orders()

    def _cancel_pending_orders(self):
        """Elimina todas las órdenes límite de la matriz 3x3 que nunca entraron al mercado."""
        orders = self.executor.get_pending_orders(magic_number=self.magic_number)
        if not orders: return
        
        for order in orders:
            success = self.executor.cancel_pending_order(order.ticket)
            if success:
                self.logger.info(f"🗑️ Orden pendiente #{order.ticket} eliminada.")