import MetaTrader5 as mt5
import logging
import os
from models import TradeAction, TradeSignal

logger = logging.getLogger(__name__)

class MT5Executor:
    def __init__(self, account, password, server):
        self.account = int(account)
        self.password = password
        self.server = server
        self.magic_number = 123456
        # Riesgo del 0.5% para FTMO
        self.risk_percent = float(os.getenv("RISK_PERCENT_PER_SIGNAL", 0.5))
        self.max_lot_per_order = 0.5 

    def connect(self):
        if not mt5.initialize(): return False
        return mt5.login(self.account, password=self.password, server=self.server)

    def _calculate_lot(self, symbol, entry, sl, money):
        si = mt5.symbol_info(symbol)
        dist = abs(entry - sl)
        if dist <= 0 or not si: return 0.01
        
        # Fórmula: Lote = Riesgo / (Distancia * (TickValue/TickSize))
        risk_per_lot = (si.trade_tick_value / si.trade_tick_size) * dist
        lot = round(money / risk_per_lot, 2)
        
        # Cap de seguridad: No más de 0.5 por orden
        lot = min(lot, self.max_lot_per_order)
        return max(lot, si.volume_min)

    def execute(self, signal):
        acc = mt5.account_info()
        if not acc: return False

        # Riesgo total ($1,000 en cuenta de $200k)
        total_risk_money = acc.balance * (self.risk_percent / 100)
        risk_per_order = total_risk_money / 9 

        # Crear 3 puntos de entrada en la zona
        p_mid = (signal.entry_min + signal.entry_max) / 2
        entries = [signal.entry_min, p_mid, signal.entry_max]
        
        # Usar los 3 primeros TPs
        tps = signal.take_profits[:3]
        if len(tps) < 3: tps = tps * 3 # Backup

        # 1. Obtener precio actual en vivo (Ask y Bid)
        tick = mt5.symbol_info_tick(signal.symbol)
        if not tick:
            logger.error(f"❌ No se pudo obtener el precio actual de {signal.symbol}")
            return False

        current_ask = tick.ask
        current_bid = tick.bid

        logger.info(f"📊 Ejecutando Matriz 3x3 en {signal.symbol}. Riesgo/Orden: ${risk_per_order:.2f}")

        for entry_p in entries:
            for tp_p in tps:
                # Calculamos el lote basado en el precio planificado (como solicitaste, mismo peso)
                lot = self._calculate_lot(signal.symbol, entry_p, signal.stop_loss, risk_per_order)
                
                # Por defecto, configuramos la orden como PENDIENTE (Limit)
                is_market = False
                action_type = mt5.TRADE_ACTION_PENDING
                exec_price = entry_p
                order_type = mt5.ORDER_TYPE_BUY_LIMIT if signal.action == TradeAction.BUY else mt5.ORDER_TYPE_SELL_LIMIT

                # 2. LÓGICA DE RESCATE: Verificar si el precio ya superó el nivel de entrada
                if signal.action == TradeAction.BUY and current_ask <= entry_p:
                    # El precio ya está por debajo o igual a la entrada. Buy Limit daría error. Entramos a mercado.
                    is_market = True
                    action_type = mt5.TRADE_ACTION_DEAL
                    order_type = mt5.ORDER_TYPE_BUY
                    exec_price = current_ask
                elif signal.action == TradeAction.SELL and current_bid >= entry_p:
                    # El precio ya está por encima o igual a la entrada. Sell Limit daría error. Entramos a mercado.
                    is_market = True
                    action_type = mt5.TRADE_ACTION_DEAL
                    order_type = mt5.ORDER_TYPE_SELL
                    exec_price = current_bid

                # Construir la petición a MT5
                request = {
                    "action": action_type,
                    "symbol": signal.symbol,
                    "volume": lot,
                    "type": order_type,
                    "price": exec_price,
                    "sl": signal.stop_loss,
                    "tp": tp_p,
                    "magic": self.magic_number,
                    "comment": f"M3x3 {'MKT' if is_market else 'LMT'} {entry_p}"
                }

                # La caducidad GTC solo se aplica a órdenes pendientes, si es mercado da error
                if not is_market:
                    request["type_time"] = mt5.ORDER_TIME_GTC

                res = mt5.order_send(request)
                if res.retcode != mt5.TRADE_RETCODE_DONE:
                    tipo_fallo = "Mercado" if is_market else "Límite"
                    logger.error(f"❌ Error Orden {tipo_fallo} en {exec_price}: {res.comment}")
                else:
                    # Mensaje opcional para ver en consola qué hizo el bot
                    if is_market:
                        logger.info(f"⚡ Rescate exitoso: Orden Market ejecutada a {exec_price}")
                    
        return True

    def set_trades_to_breakeven(self):
        # 1. Obtener todas las posiciones activas de este bot
        positions = mt5.positions_get(magic=self.magic_number)
        
        if not positions:
            logger.info("ℹ️ No hay posiciones abiertas, procediendo a limpiar órdenes pendientes huérfanas.")
            # USAMOS TU FUNCIÓN AQUÍ: Si mandan BE pero ya se había cerrado por TP/SL, limpiamos la matriz restante
            self.cancel_pending_orders()
            return True

        # 2. Extraer los símbolos únicos en los que estamos operando ahora mismo
        active_symbols = set(pos.symbol for pos in positions)

        for symbol in active_symbols:
            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info: 
                continue

            point = symbol_info.point
            digits = symbol_info.digits
            
            # --- CÁLCULO DEL COLCHÓN DE SEGURIDAD ---
            current_spread_points = symbol_info.spread
            safety_offset_points = max(current_spread_points + 5, 20) 
            offset = safety_offset_points * point

            logger.info(f"🔄 Aplicando BE en {symbol} (Colchón: +{safety_offset_points} puntos)")

            # --- PUNTO A: MODIFICAR POSICIONES ABIERTAS ---
            symbol_positions = [p for p in positions if p.symbol == symbol]
            for pos in symbol_positions:
                ticket = pos.ticket
                open_price = pos.price_open
                
                if pos.type == mt5.POSITION_TYPE_BUY:
                    new_sl = round(open_price + offset, digits)
                elif pos.type == mt5.POSITION_TYPE_SELL:
                    new_sl = round(open_price - offset, digits)
                else:
                    continue

                if (pos.type == mt5.POSITION_TYPE_BUY and pos.sl >= new_sl) or \
                   (pos.type == mt5.POSITION_TYPE_SELL and (pos.sl <= new_sl and pos.sl != 0.0)):
                    continue

                request_sl = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "sl": new_sl,
                    "tp": pos.tp
                }
                mt5.order_send(request_sl)

        # --- PUNTO B: LIMPIEZA DE ÓRDENES PENDIENTES ---
        # USAMOS TU FUNCIÓN AQUÍ: Una vez protegidas las operaciones vivas, borramos el resto de la matriz
        logger.info("🧹 Ejecutando limpieza de órdenes pendientes restantes...")
        self.cancel_pending_orders()

        return True

    def cancel_pending_orders(self):
        orders = mt5.orders_get(magic=self.magic_number)
        if not orders: return True
        for o in orders:
            mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
        return True
    
    def execute_market(self, signal: TradeSignal):
        """Entrada inmediata a mercado (Market Order)"""
        acc = mt5.account_info()
        if not acc: return False
        
        # Obtener el precio actual (Ask para BUY, Bid para SELL)
        symbol = signal.symbol
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if not tick: 
            logger.error(f"No se pudo obtener el precio actual de {symbol}")
            return False

        current_price = tick.ask if signal.action == TradeAction.BUY else tick.bid
        
        # Riesgo dividido en 3 (un trade por cada TP)
        total_risk_money = acc.balance * (self.risk_percent / 100)
        risk_per_order = total_risk_money / 3 
        
        tps = signal.take_profits[:3]
        
        logger.info(f"⚡ EJECUCIÓN MARKET (ACTIVA) en {symbol} a {current_price}")

        for tp_p in tps:
            # Calculamos el lote basado en la distancia del precio actual al SL
            lot = self._calculate_lot(symbol, current_price, signal.stop_loss, risk_per_order)
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL, # DEAL = Entrada inmediata
                "symbol": symbol,
                "volume": lot,
                "type": mt5.ORDER_TYPE_BUY if signal.action == TradeAction.BUY else mt5.ORDER_TYPE_SELL,
                "price": current_price,
                "sl": signal.stop_loss,
                "tp": tp_p,
                "magic": self.magic_number,
                "comment": "Market (Activa)",
                "type_time": mt5.ORDER_TIME_GTC,
            }
            
            res = mt5.order_send(request)
            if res.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(f"❌ Error Market Order: {res.comment}")
        
        return True