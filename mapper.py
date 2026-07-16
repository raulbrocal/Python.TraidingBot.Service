import re
from abc import ABC, abstractmethod
from models import TradeSignal, TradeAction

class BaseMapper(ABC):
    @abstractmethod
    def map_message(self, message: str) -> TradeSignal | None:
        """Contrato obligatorio para parsear mensajes de canales específicos."""
        pass


class PrimeGoldMapper(BaseMapper):
    def map_message(self, message: str) -> TradeSignal | None:
        if not message:
            return None

        msg_lower = message.lower().strip()
        
        # 1. Entrada de pánico (Activa)
        if msg_lower == "activa" or msg_lower.startswith("activa"):
            return TradeSignal(action=TradeAction.ACTIVATE, symbol="XAUUSD")
        
        # 2. Cancelación
        cancel_keywords = ["se fue", "anulamos", "buscamos otra"]
        if any(kw in msg_lower for kw in cancel_keywords):
            return TradeSignal(action=TradeAction.CANCEL, symbol="XAUUSD")

        # 3. Señal estándar (BUY/SELL LIMIT)
        action = None
        if "buy" in msg_lower: action = TradeAction.BUY
        elif "sell" in msg_lower: action = TradeAction.SELL
        
        if not action: 
            return None

        try:
            symbol_match = re.search(r'(XAUUSD|GOLD)', msg_lower, re.I)
            symbol = "XAUUSD"  # Por defecto siempre operamos Oro en estos canales
            
            # Extraer rango (ej: 2315 - 2311)
            entries = re.findall(r'(\d+(?:\.\d+)?)', msg_lower)
            if len(entries) < 2:
                return None
            e1, e2 = float(entries[0]), float(entries[1])
            
            # Stop Loss
            sl_match = re.search(r'sl\s*[:\s]*(\d+(?:\.\d+)?)', msg_lower, re.I)
            sl = float(sl_match.group(1)) if sl_match else 0.0
            
            # Take Profits
            tps = [float(tp) for tp in re.findall(r'tp\s*[:\s]*(\d+(?:\.\d+)?)', msg_lower, re.I)]
            
            return TradeSignal(
                action=action,
                symbol=symbol,
                entry_min=min(e1, e2),
                entry_max=max(e1, e2),
                stop_loss=sl,
                take_profits=tps,
                raw_message=message
            )
        except Exception:
            return None


class LoganGoldMapper(BaseMapper):
    def map_message(self, message: str) -> TradeSignal | None:
        if not message:
            return None

        msg_lower = message.lower().strip()

        # 1. Gatillo de Entrada Inmediata a Mercado ("GOLD SELL YA", "GOLD BUY YA")
        if "ya" in msg_lower and not "be" in msg_lower:
            if "buy" in msg_lower:
                return TradeSignal(action=TradeAction.BUY, symbol="XAUUSD", raw_message=message)
            elif "sell" in msg_lower:
                return TradeSignal(action=TradeAction.SELL, symbol="XAUUSD", raw_message=message)

        # 2. Gestión de Breakeven ("BE", "MUEVAN SL A BE YA")
        be_keywords = ["be", "muevan sl a be", "sl a be"]
        if any(keyword == msg_lower or keyword in msg_lower for keyword in be_keywords):
            return TradeSignal(action=TradeAction.BREAKEVEN, symbol="XAUUSD", raw_message=message)

        # 3. Procesar Señal Estándar de Logan (con su rango y SL)
        action = None
        if "buy" in msg_lower: action = TradeAction.BUY
        elif "sell" in msg_lower: action = TradeAction.SELL

        if not action:
            return None

        try:
            symbol = "XAUUSD"

            # Buscar rango con guion (ej: 4030 - 4034)
            range_match = re.search(r'(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)', msg_lower)
            if range_match:
                e1, e2 = float(range_match.group(1)), float(range_match.group(2))
                entry_min = min(e1, e2)
                entry_max = max(e1, e2)
            else:
                entry_min = entry_max = 0.0

            # Stop Loss (SL: 4038)
            sl_match = re.search(r'sl\s*[:\s]*(\d+(?:\.\d+)?)', msg_lower, re.I)
            sl = float(sl_match.group(1)) if sl_match else 0.0

            # Take Profits (Ignoramos strings como "ABIERTO" buscando solo los numéricos)
            tps = [float(tp) for tp in re.findall(r'tp\s*[:\s]*(\d+(?:\.\d+)?)', msg_lower, re.I)]

            return TradeSignal(
                action=action,
                symbol=symbol,
                entry_min=entry_min,
                entry_max=entry_max,
                stop_loss=sl,
                take_profits=tps,
                raw_message=message
            )
        except Exception:
            return None