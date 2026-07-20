from dataclasses import dataclass, field
from typing import List
from enum import Enum

class TradeAction(Enum):
    BUY = "BUY"
    SELL = "SELL"
    BREAKEVEN = "BREAKEVEN"
    CANCEL = "CANCEL"
    ACTIVATE = "ACTIVATE"
    MOVE_SL = "MOVE_SL"

@dataclass
class TradeSignal:
    action: TradeAction
    symbol: str = ""
    entry_min: float = 0.0
    entry_max: float = 0.0
    stop_loss: float = 0.0
    take_profits: List[float] = field(default_factory=list)
    raw_message: str = ""