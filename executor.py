import MetaTrader5 as mt5
import logging
import time

logger = logging.getLogger(__name__)

class MT5Executor:
    def __init__(self, account, password, server):
        self.account = int(account)
        self.password = password
        self.server = server

    def connect(self):
        if not mt5.initialize(): 
            return False
        return mt5.login(self.account, password=self.password, server=self.server)

    def get_tick(self, symbol: str):
        """Retorna el tick actual (bid, ask) del símbolo."""
        mt5.symbol_select(symbol, True)
        return mt5.symbol_info_tick(symbol)

    def get_symbol_info(self, symbol: str):
        """Retorna la información completa del símbolo (digits, point, spread, etc)."""
        return mt5.symbol_info(symbol)

    def get_account_balance(self) -> float:
        """Retorna el balance actual de la cuenta."""
        acc = mt5.account_info()
        return acc.balance if acc else 0.0

    def get_positions(self, magic_number: int = None, ticket: int = None):
        """Retorna posiciones activas, filtrables por magic o ticket."""
        if ticket:
            return mt5.positions_get(ticket=ticket)
        if magic_number:
            return mt5.positions_get(magic=magic_number)
        return mt5.positions_get()

    def get_pending_orders(self, magic_number: int = None):
        """Retorna órdenes pendientes (limits/stops), filtrables por magic."""
        if magic_number:
            return mt5.orders_get(magic=magic_number)
        return mt5.orders_get()

    def send_order(self, symbol: str, order_type: int, volume: float, price: float, 
                   sl: float, tp: float, magic: int, comment: str, is_market: bool = False):
        """Envía una orden pura a MT5 (Market o Limit)."""
        
        action_type = mt5.TRADE_ACTION_DEAL if is_market else mt5.TRADE_ACTION_PENDING
        
        request = {
            "action": action_type,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(price),
            "sl": float(sl),
            "tp": float(tp),
            "magic": int(magic),
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC
        }

        # Para órdenes a mercado (Market Execution), usamos IOC o FOK dependiendo del broker
        if is_market:
            request["type_filling"] = mt5.ORDER_FILLING_IOC
            request["deviation"] = 20

        res = mt5.order_send(request)
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"❌ Error al enviar orden {comment}: {res.comment} (Retcode: {res.retcode})")
            return None
        return res

    def modify_position_sl(self, ticket: int, new_sl: float):
        """Modifica el Stop Loss de una posición viva."""
        positions = self.get_positions(ticket=ticket)
        if not positions:
            logger.error(f"❌ No se encontró la posición #{ticket} para modificar SL.")
            return False
            
        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": float(new_sl),
            "tp": pos.tp
        }
        
        res = mt5.order_send(request)
        return res.retcode == mt5.TRADE_RETCODE_DONE

    def modify_position(self, ticket: int, sl: float, tp: float):
        """Modifica el Stop Loss y el Take Profit de una posición viva simultáneamente."""
        positions = self.get_positions(ticket=ticket)
        if not positions:
            logger.error(f"❌ No se encontró la posición #{ticket} para modificar SL/TP.")
            return False
            
        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": float(sl),
            "tp": float(tp)
        }
        
        res = mt5.order_send(request)
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"❌ Error al modificar posición {ticket}: {res.comment} (Retcode: {res.retcode})")
            return False
        return True

    def close_position(self, ticket: int, volume_to_close: float):
        """Cierra total o parcialmente una posición."""
        positions = self.get_positions(ticket=ticket)
        if not positions:
            return False
            
        pos = positions[0]
        tick = self.get_tick(pos.symbol)
        if not tick: return False

        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": float(volume_to_close),
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        res = mt5.order_send(request)
        return res.retcode == mt5.TRADE_RETCODE_DONE

    def cancel_pending_order(self, ticket: int):
        """Elimina una orden pendiente (Limit/Stop)."""
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket
        }
        res = mt5.order_send(request)
        return res.retcode == mt5.TRADE_RETCODE_DONE