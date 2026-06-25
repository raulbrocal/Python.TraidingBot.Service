import re
from models import TradeSignal, TradeAction

class SignalMapper:
    def map_message(self, message: str) -> TradeSignal | None:
        if not message:
            return None

        msg_lower = message.lower()
        
        # 1. Detección de entrada a mercado inmediata
        if msg_lower.strip() == "activa" or msg_lower.startswith("activa"):
            return TradeSignal(action=TradeAction.ACTIVATE, symbol="", entry_min=0, entry_max=0, stop_loss=0, take_profits=[])
        
        # 2. Detección de Breakeven
        be_keywords = ["breakeven", "SL al precio de entrada", "modicamos el sl", "asegurar"]
        if any(keyword in msg_lower for keyword in be_keywords):
            return TradeSignal(action=TradeAction.BREAKEVEN, symbol="", entry_min=0, entry_max=0, stop_loss=0, take_profits=[])

        # 3. Detección de Cancelación
        cancel_keywords = ["se fue", "anulamos", "buscamos otra"]
        if any(keyword in msg_lower for keyword in cancel_keywords):
            return TradeSignal(action=TradeAction.CANCEL, symbol="", entry_min=0, entry_max=0, stop_loss=0, take_profits=[])

        # 4. Lógica de mapeo de señal normal (BUY/SELL LIMIT)
        action = None
        if "buy" in msg_lower: action = TradeAction.BUY
        elif "sell" in msg_lower: action = TradeAction.SELL
        
        if not action: return None

        try:
            # Extraer Símbolo
            symbol_match = re.search(r'(XAUUSD|GOLD)', msg_lower, re.I)
            symbol = symbol_match.group(1).upper() if symbol_match else "XAUUSD"
            
            # Extraer Rango de Entrada (Ej: 4711-4707)
            entries = re.findall(r'(\d{4}(?:\.\d+)?)', msg_lower)
            e1, e2 = float(entries[0]), float(entries[1])
            
            # Extraer Stop Loss
            sl = float(re.search(r'SL\s*(\d+(?:\.\d+)?)', msg_lower, re.I).group(1))
            
            # Extraer Take Profits
            tps = [float(tp) for tp in re.findall(r'TP\s*(\d+(?:\.\d+)?)', msg_lower, re.I)]
            
            return TradeSignal(
                action=action,
                symbol=symbol,
                entry_min=min(e1, e2),
                entry_max=max(e1, e2),
                stop_loss=sl,
                take_profits=tps
            )
        except Exception as e:
            return None