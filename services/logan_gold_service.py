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
        self.magic_number = 202611
        self.max_lot_per_order = 0.50 

    async def process_message(self, message: str, is_edit: bool = False):
        self.logger.info(f"📩 Procesando {'EDICIÓN' if is_edit else 'MENSAJE'} de Logan Gold...")
        msg_lower = message.lower().strip()
        
        # 1. FILTROS RÁPIDOS DE EVENTOS (Solo para mensajes nuevos)
        if not is_edit:
            # --- TP 1: No hacer nada ---
            if "tp 1" in msg_lower and not any(kw in msg_lower for kw in ["be", "sl a be", "asegurar"]):
                self.logger.info("🎯 TP 1 detectado. Estrategia: Mantener (órdenes sin cambios).")
                return

            # --- TP 2: Auto-Breakeven ---
            if "tp 2" in msg_lower:
                self.logger.info("✂️ TP 2 detectado. Protegiendo el resto a Breakeven...")
                self._set_trades_to_breakeven()
                return

            # --- TP 3 / Cierres totales ---
            if "tp 3" in msg_lower:
                self.logger.info("🎯 TP 3 detectado. Dejando correr el runner (Orden C).")
                return
                
            if any(kw in msg_lower for kw in ["todos los tps", "posiciones cerradas", "cerrar todo"]):
                self.logger.info("🛑 Comando de cierre total detectado. Liquidando runners de Logan Gold...")
                self._execute_complete_close_all()
                return

        # 2. MAPEO DE SEÑAL
        signal = self.mapper.map_message(message)
        if not signal:
            return

        # 3. ENRUTADOR DE ACCIONES
        if signal.action in [TradeAction.BUY, TradeAction.SELL]:
            # Si es un mensaje editado, JAMÁS abrimos operaciones. Solo actualizamos.
            if is_edit:
                self._update_existing_positions(signal)
            else:
                # Si es un mensaje nuevo, distinguimos entre Gatillo Inmediato y Parámetros
                if signal.entry_min == 0.0 and signal.entry_max == 0.0 and signal.stop_loss == 0.0:
                    self._execute_split_market_orders(signal)
                else:
                    self._process_signal_parameters(signal)
                
        elif signal.action == TradeAction.BREAKEVEN:
            self.logger.info("🛡️ Comando de Breakeven explícito detectado. Protegiendo posiciones...")
            self._set_trades_to_breakeven()
            
        elif signal.action == TradeAction.MOVE_SL:
            self.logger.info(f"🔄 Comando MOVE_SL detectado. Moviendo a: {signal.stop_loss}")
            self._update_dynamic_sl(signal.stop_loss)


    # ---------------------------------------------------------
    # GESTIÓN DE ACTUALIZACIONES RÁPIDAS
    # ---------------------------------------------------------

    def _update_existing_positions(self, signal: TradeSignal):
        """Busca posiciones activas y actualiza el SL según el mensaje editado."""
        if signal.stop_loss <= 0.0:
            return
            
        self.logger.info("✍️ Mensaje editado con SL detectado. Evaluando actualizar posiciones activas...")
        
        active_positions = self.executor.get_positions(magic_number=self.magic_number)
        if not active_positions:
            self.logger.warning("⚠️ Logan editó un mensaje, pero no hay posiciones activas para este canal.")
            return

        for pos in active_positions:
            if pos.sl != signal.stop_loss:
                self.logger.info(f"🛠️ Ajustando SL de Ticket #{pos.ticket} a {signal.stop_loss} por edición en Telegram.")
                self.executor.modify_position_sl(pos.ticket, signal.stop_loss)


    # ---------------------------------------------------------
    # LÓGICA DE EJECUCIÓN 80/10/10
    # ---------------------------------------------------------

    def _calculate_lot_distribution(self, total_volume: float, min_volume: float):
        vol_a = round(total_volume * 0.80, 2)
        vol_b = round(total_volume * 0.10, 2)
        vol_c = round(total_volume * 0.10, 2)

        if vol_b < min_volume or vol_c < min_volume:
            if total_volume >= min_volume * 2:
                vol_b = min_volume
                vol_a = round(total_volume - vol_b, 2)
                vol_c = 0.0
            else:
                vol_a = total_volume
                vol_b, vol_c = 0.0, 0.0
                
        return max(vol_a, 0.0), max(vol_b, 0.0), max(vol_c, 0.0)

    def _execute_split_market_orders(self, signal: TradeSignal):
        balance = self.executor.get_account_balance()
        if balance <= 0: return

        symbol = signal.symbol if signal.symbol else "XAUUSD"
        si = self.executor.get_symbol_info(symbol)
        min_vol = si.volume_min if si else 0.01

        total_lot = min(round(balance * 0.0000025, 2), self.max_lot_per_order)
        total_lot = max(total_lot, min_vol)
        
        vol_a, vol_b, vol_c = self._calculate_lot_distribution(total_lot, min_vol)

        tick = self.executor.get_tick(symbol)
        if not tick: return
        
        order_type = mt5.ORDER_TYPE_BUY if signal.action == TradeAction.BUY else mt5.ORDER_TYPE_SELL
        price = tick.ask if signal.action == TradeAction.BUY else tick.bid

        self.logger.info(f"⚡ Lanzando Gatillo Multi-Orden LoganGold: {signal.action.name} (Total: {total_lot} lotes a {price})")

        orders = [
            (vol_a, "Logan A 80%"),
            (vol_b, "Logan B 10%"),
            (vol_c, "Logan C 10%")
        ]

        for vol, comment in orders:
            if vol > 0:
                self.executor.send_order(
                    symbol=symbol, order_type=order_type, volume=vol, price=price,
                    sl=0.0, tp=0.0, magic=self.magic_number, comment=comment, is_market=True
                )
                time.sleep(0.1)


    # ---------------------------------------------------------
    # INYECCIÓN DE PARÁMETROS Y ESCUDO ANTI-SLIPPAGE
    # ---------------------------------------------------------

    def _process_signal_parameters(self, signal: TradeSignal):
        symbol = signal.symbol if signal.symbol else "XAUUSD"
        active_positions = self.executor.get_positions(magic_number=self.magic_number)
        
        if not active_positions:
            self.logger.warning("⚠️ Llegaron parámetros pero no hay órdenes 'YA' activas.")
            return

        tick = self.executor.get_tick(symbol)
        if not tick: return
        
        pos_type = active_positions[0].type
        current_price = tick.bid if pos_type == mt5.POSITION_TYPE_SELL else tick.ask
        
        # 1. ESCUDO ANTI-SLIPPAGE
        if signal.stop_loss > 0.0:
            breached = False
            if pos_type == mt5.POSITION_TYPE_SELL and current_price >= signal.stop_loss:
                breached = True
            elif pos_type == mt5.POSITION_TYPE_BUY and current_price <= signal.stop_loss:
                breached = True
                
            if breached:
                self.logger.error(f"🚨 SLIPPAGE CRÍTICO: El precio actual ({current_price}) rebasó el SL ({signal.stop_loss}). ¡Cerrando todo!")
                self._execute_complete_close_all()
                return

        # 2. INYECTAR PARÁMETROS A LAS ÓRDENES VIVAS
        self.logger.info(f"🔄 Inyectando SL ({signal.stop_loss}) y TPs a las órdenes vivas...")
        
        # Lógica dinámica de asignación de TPs
        tps = signal.take_profits
        tp_a = 0.0
        tp_b = 0.0
        
        if len(tps) >= 3:
            tp_a = tps[1]  # Orden A (80%) -> TP2
            tp_b = tps[2]  # Orden B (10%) -> TP3
        elif len(tps) == 2:
            tp_a = tps[0]  # Orden A (80%) -> TP1
            tp_b = tps[1]  # Orden B (10%) -> TP2
        elif len(tps) == 1:
            tp_a = tps[0]  # Ambas al único TP
            tp_b = tps[0]

        for pos in active_positions:
            target_tp = 0.0
            
            if "Logan A" in pos.comment:
                target_tp = tp_a
            elif "Logan B" in pos.comment:
                target_tp = tp_b
            elif "Logan C" in pos.comment:
                # TP Lejano (+30.0 puntos = 300 pips aprox en XAUUSD)
                offset = 30.0
                target_tp = pos.price_open + offset if pos.type == mt5.POSITION_TYPE_BUY else pos.price_open - offset
                
            self.executor.modify_position(pos.ticket, sl=signal.stop_loss, tp=target_tp)

        # 3. DEJAR ORDEN LÍMITE
        self._place_limit_order(signal, symbol, pos_type)

    def _place_limit_order(self, signal, symbol, pos_type):
        si = self.executor.get_symbol_info(symbol)
        if signal.entry_min == 0.0 or signal.entry_max == 0.0: return

        limit_price = signal.entry_max - 1.0 if pos_type == mt5.POSITION_TYPE_SELL else signal.entry_min + 1.0
        order_type = mt5.ORDER_TYPE_SELL_LIMIT if pos_type == mt5.POSITION_TYPE_SELL else mt5.ORDER_TYPE_BUY_LIMIT
        
        balance = self.executor.get_account_balance()
        vol = max(round(balance * 0.0000025, 2), si.volume_min if si else 0.01)

        self.logger.info(f"⏳ Colocando orden Limit en {limit_price}")
        self.executor.send_order(
            symbol=symbol, order_type=order_type, volume=vol, price=limit_price,
            sl=signal.stop_loss, tp=0.0, magic=self.magic_number, comment="Logan Limit", is_market=False
        )


    # ---------------------------------------------------------
    # GESTIÓN DE SL DINÁMICO Y CIERRES
    # ---------------------------------------------------------

    def _update_dynamic_sl(self, raw_sl: float):
        positions = self.executor.get_positions(magic_number=self.magic_number)
        if not positions: return

        reference_price = positions[0].price_open
        final_sl = raw_sl

        # Inteligencia para precios abreviados
        if raw_sl < 1000:
            str_ref = str(int(reference_price))
            str_raw = str(int(raw_sl))
            prefix = str_ref[:-len(str_raw)]
            final_sl = float(prefix + str(raw_sl))

        self.logger.info(f"🛡️ Modificando SL de todas las posiciones a: {final_sl}")
        for pos in positions:
            self.executor.modify_position_sl(pos.ticket, final_sl)

    def _set_trades_to_breakeven(self):
        positions = self.executor.get_positions(magic_number=self.magic_number)
        for pos in positions:
            si = self.executor.get_symbol_info(pos.symbol)
            if not si: continue

            offset = max(si.spread + 5, 20) * si.point
            new_sl = round(pos.price_open + offset, si.digits) if pos.type == mt5.POSITION_TYPE_BUY else round(pos.price_open - offset, si.digits)
            
            if (pos.type == mt5.POSITION_TYPE_BUY and pos.sl < new_sl) or (pos.type == mt5.POSITION_TYPE_SELL and (pos.sl > new_sl or pos.sl == 0.0)):
                self.executor.modify_position_sl(pos.ticket, new_sl)
                self.logger.info(f"✅ SL de Ticket #{pos.ticket} blindado en {new_sl}")

    def _execute_complete_close_all(self):
        positions = self.executor.get_positions(magic_number=self.magic_number)
        for pos in (positions or []):
            self.executor.close_position(pos.ticket, pos.volume)
            
        pending = self.executor.get_pending_orders(magic_number=self.magic_number)
        for order in (pending or []):
            self.executor.cancel_pending_order(order.ticket)