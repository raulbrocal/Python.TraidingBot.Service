import MetaTrader5 as mt5
import logging
import os
import time
from models import TradeAction, TradeSignal

logger = logging.getLogger(__name__)

class MT5Executor:
    def __init__(self, account, password, server):
        self.account = int(account)
        self.password = password
        self.server = server
        
        # Identificadores únicos (Magic Numbers) para separar las estrategias
        self.magic_number = 123456         # PrimeGold
        self.logan_magic_number = 202611   # LoganGold
        
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

    # =====================================================================
    # ESTRATEGIA: PRIME GOLD (Estable y funcionando al 100%)
    # =====================================================================
    def execute(self, signal):
        acc = mt5.account_info()
        if not acc: return False

        total_risk_money = acc.balance * (self.risk_percent / 100)
        risk_per_order = total_risk_money / 9 

        p_mid = (signal.entry_min + signal.entry_max) / 2
        entries = [signal.entry_min, p_mid, signal.entry_max]
        
        tps = signal.take_profits[:3]
        if len(tps) < 3: tps = tps * 3

        tick = mt5.symbol_info_tick(signal.symbol)
        if not tick:
            logger.error(f"❌ No se pudo obtener el precio actual de {signal.symbol}")
            return False

        current_ask = tick.ask
        current_bid = tick.bid

        logger.info(f"📊 Ejecutando Matriz 3x3 en {signal.symbol}. Riesgo/Orden: ${risk_per_order:.2f}")

        for entry_p in entries:
            for tp_p in tps:
                lot = self._calculate_lot(signal.symbol, entry_p, signal.stop_loss, risk_per_order)
                
                is_market = False
                action_type = mt5.TRADE_ACTION_PENDING
                exec_price = entry_p
                order_type = mt5.ORDER_TYPE_BUY_LIMIT if signal.action == TradeAction.BUY else mt5.ORDER_TYPE_SELL_LIMIT

                if signal.action == TradeAction.BUY and current_ask <= entry_p:
                    is_market = True
                    action_type = mt5.TRADE_ACTION_DEAL
                    order_type = mt5.ORDER_TYPE_BUY
                    exec_price = current_ask
                elif signal.action == TradeAction.SELL and current_bid >= entry_p:
                    is_market = True
                    action_type = mt5.TRADE_ACTION_DEAL
                    order_type = mt5.ORDER_TYPE_SELL
                    exec_price = current_bid

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

                if not is_market:
                    request["type_time"] = mt5.ORDER_TIME_GTC

                res = mt5.order_send(request)
                if res.retcode != mt5.TRADE_RETCODE_DONE:
                    tipo_fallo = "Mercado" if is_market else "Límite"
                    logger.error(f"❌ Error Orden {tipo_fallo} en {exec_price}: {res.comment}")
                else:
                    if is_market:
                        logger.info(f"⚡ Rescate exitoso: Orden Market ejecutada a {exec_price}")
                        
        return True

    def set_trades_to_breakeven(self):
        positions = mt5.positions_get(magic=self.magic_number)
        
        if not positions:
            logger.info("ℹ️ No hay posiciones abiertas, procediendo a limpiar órdenes pendientes huérfanas.")
            self.cancel_pending_orders()
            return True

        active_symbols = set(pos.symbol for pos in positions)

        for symbol in active_symbols:
            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info: 
                continue

            point = symbol_info.point
            digits = symbol_info.digits
            
            current_spread_points = symbol_info.spread
            safety_offset_points = max(current_spread_points + 5, 20) 
            offset = safety_offset_points * point

            logger.info(f"🔄 Aplicando BE en {symbol} (Colchón: +{safety_offset_points} puntos)")

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
        """Entrada inmediata a mercado para PrimeGold (Market Order)"""
        acc = mt5.account_info()
        if not acc: return False
        
        symbol = signal.symbol
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if not tick: 
            logger.error(f"No se pudo obtener el precio actual de {symbol}")
            return False

        current_price = tick.ask if signal.action == TradeAction.BUY else tick.bid
        
        total_risk_money = acc.balance * (self.risk_percent / 100)
        risk_per_order = total_risk_money / 3 
        
        tps = signal.take_profits[:3]
        
        logger.info(f"⚡ EJECUCIÓN MARKET (ACTIVA) en {symbol} a {current_price}")

        for tp_p in tps:
            lot = self._calculate_lot(symbol, current_price, signal.stop_loss, risk_per_order)
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
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

    # =====================================================================
    # ESTRATEGIA: LOGAN GOLD (Mejorada y robustecida)
    # =====================================================================
    def execute_logan_market_order(self, action, symbol="XAUUSD"):
        """Abre una posición instantánea a mercado para LoganGold (Retorna precio de apertura verificado)"""
        acc = mt5.account_info()
        if not acc:
            logger.error("❌ No se pudo obtener la información de la cuenta para LoganGold.")
            return None, None, None

        # Lote proporcional seguro (0.50 lotes para cuenta de 200k)
        calculated_lot = round(acc.balance * 0.0000025, 2)
        lot = min(calculated_lot, self.max_lot_per_order)
        
        si = mt5.symbol_info(symbol)
        if si:
            lot = max(lot, si.volume_min)
        else:
            lot = max(lot, 0.01)

        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            logger.error(f"❌ Imposible obtener cotización en vivo de {symbol} para LoganGold.")
            return None, None, None

        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if action == "BUY" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": self.logan_magic_number,
            "comment": "LoganGold Immediate",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logger.info(f"[MT5] Lanzando orden instantánea LoganGold: {action} {lot} lotes a {price}")
        res = mt5.order_send(request)
        
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            actual_price = res.price
            
            # --- SOLUCIÓN CRÍTICA: ANTI-PRECIO 0.0 ---
            if actual_price <= 0.0:
                logger.warning(f"⚠️ El broker retornó precio de ejecución 0.0. Recuperando precio real de apertura desde MT5...")
                # Intentamos obtener la posición activa hasta 5 veces (con pequeños retrasos de latencia)
                for _ in range(5):
                    positions = mt5.positions_get(ticket=res.order)
                    if positions:
                        actual_price = positions[0].price_open
                        logger.info(f"🎯 ¡Precio real de apertura recuperado de la posición viva!: {actual_price}")
                        break
                    time.sleep(0.05)
            
            # Si el broker sigue sin cooperar (lo cual es rarísimo tras el bucle), usamos el precio del tick enviado
            if actual_price <= 0.0:
                actual_price = price
                logger.warning(f"⚠️ No se pudo leer la posición viva. Usando precio estimado de cotización: {actual_price}")
                
            return res.order, actual_price, res.volume
        else:
            logger.error(f"❌ Error al abrir orden LoganGold: {res.comment} (Retcode: {res.retcode})")
            return None, None, None

    def modify_position_sl(self, ticket, sl_price):
        """Modifica quirúrgicamente el Stop Loss de una posición activa usando su ticket"""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.error(f"❌ No se localizó la posición #{ticket} para actualizar el SL.")
            return False
        
        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": float(sl_price),
            "tp": pos.tp
        }
        
        res = mt5.order_send(request)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            return True
        else:
            logger.error(f"❌ Error al modificar SL del Ticket #{ticket}: {res.comment}")
            return False

    def partial_close_position(self, ticket, volume_to_close):
        """Ejecuta un cierre parcial abriendo un lote inverso enlazado al ticket original"""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.error(f"❌ No se encontró la posición con Ticket #{ticket} para realizar el parcial.")
            return False
        
        pos = positions[0]
        symbol = pos.symbol
        
        volume_to_close = min(float(volume_to_close), pos.volume)
        volume_to_close = round(volume_to_close, 2)
        if volume_to_close <= 0:
            return False

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            return False

        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume_to_close,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": self.logan_magic_number,
            "comment": "Logan Partial Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        res = mt5.order_send(request)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"✅ Reducción parcial completada: {volume_to_close} lotes cerrados en Ticket #{ticket}")
            return True
        else:
            logger.error(f"❌ Falló la ejecución del parcial en Ticket #{ticket}: {res.comment}")
            return False

    def close_position_completely(self, ticket):
        """Cierra el 100% de los lotes remanentes de una posición activa"""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False
        return self.partial_close_position(ticket, positions[0].volume)