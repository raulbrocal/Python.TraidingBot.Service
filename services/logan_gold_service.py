import logging
import asyncio
import re
from datetime import datetime
import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

class LoganGoldService:
    def __init__(self, channel_id, executor):
        self.channel_id = channel_id
        self.executor = executor
        
        # Estado de gatillo controlado por marca de tiempo (Anti condiciones de carrera)
        self.last_ready_time = None  
        
        # Tracking preciso de la posición activa de Logan
        self.active_ticket = None
        self.entry_price = None
        self.direction = None  # "BUY" o "SELL"
        self.volume = 0.0
        
        # Tarea de monitoreo en segundo plano
        self.monitor_task = None

    async def process_message(self, text: str):
        text_lower = text.lower().strip()
        logger.info(f"[LoganGold] 📩 Procesando mensaje del canal...")

        # 1. DETECCIÓN DEL "READY"
        if "ready" in text_lower:
            self.last_ready_time = datetime.now()
            logger.info("[LoganGold] 🔔 Mensaje 'Ready' registrado con marca de tiempo actual.")
            return

        # 2. DETECCIÓN DEL GATILLO "YA"
        if "sell ya" in text_lower or "buy ya" in text_lower:
            now = datetime.now()
            # Margen máximo de 5 minutos (300 segundos) para validar el Ready previo
            if self.last_ready_time and (now - self.last_ready_time).total_seconds() < 300:
                logger.info("🚀 ¡GATILLO VALIDADO POR TIEMPO! Ejecutando a mercado de inmediato...")
                self.last_ready_time = None  # Consumimos el Ready para evitar dobles entradas
                
                action = "SELL" if "sell ya" in text_lower else "BUY"
                
                # Lanzar orden en MT5 (con la corrección anti-0.0 implementada en executor.py)
                ticket, price, volume = self.executor.execute_logan_market_order(action)
                
                if ticket and price and price > 0:
                    self.active_ticket = ticket
                    self.entry_price = float(price)
                    self.direction = action
                    self.volume = float(volume)
                    
                    logger.info(f"[LoganGold] ✅ Posición abierta con Ticket #{ticket} a precio {price} (Lotes: {volume})")
                    
                    # Cancelamos monitoreo previo si existía (por seguridad)
                    if self.monitor_task and not self.monitor_task.done():
                        self.monitor_task.cancel()
                        
                    # Iniciamos el bucle de monitoreo en vivo de esta posición
                    self.monitor_task = asyncio.create_task(self._monitor_position(ticket))
                else:
                    logger.error("[LoganGold] ❌ El ejecutor no pudo abrir la posición o devolvió valores inválidos.")
            else:
                logger.warning("[LoganGold] ⚠️ Gatillo 'YA' ignorado: No hubo un 'Ready' reciente o pasaron más de 5 minutos.")
            return

        # 3. ACTUALIZACIÓN DE SL/TP INICIAL (Ej: SL: 4081)
        sl_match = re.search(r"sl:\s*(\d+(?:\.\d+)?)", text_lower)
        if sl_match:
            sl_val = float(sl_match.group(1))
            if self.active_ticket:
                logger.info(f"[LoganGold] 🎯 Nivel de SL detectado: {sl_val}. Aplicando al Ticket #{self.active_ticket}...")
                success = self.executor.modify_position_sl(self.active_ticket, sl_val)
                if success:
                    logger.info(f"[LoganGold] ✅ SL modificado con éxito a {sl_val}")
                else:
                    logger.error(f"[LoganGold] ❌ No se pudo modificar el SL en MT5.")
            else:
                logger.warning("[LoganGold] ⚠️ Se recibieron niveles de SL/TP pero no hay ninguna posición activa en el bot.")
            return

        # 4. MODIFICACIONES DE SL EN VIVO (Mover a BE o nuevos niveles de SL)
        # Caso A: Breakeven (Ej: "recordad el SL en BE" o "aseguren ganancias")
        if "sl en be" in text_lower or "sl a be" in text_lower or "aseguren" in text_lower:
            if self.active_ticket and self.entry_price:
                # Aplicamos un pequeño colchón de spread (0.2 USD en Oro = 2 pips) para cubrir comisiones
                offset = 0.20 if self.direction == "BUY" else -0.20
                be_price = round(self.entry_price + offset, 2)
                
                logger.info(f"[LoganGold] 🛡️ Ajustando SL de Ticket #{self.active_ticket} a Breakeven ({be_price})...")
                self.executor.modify_position_sl(self.active_ticket, be_price)
            return

        # Caso B: Mover SL a nivel específico (Ej: "MOVEMOS SL A 4070" o "SL A 4070")
        move_sl_match = re.search(r"(?:movemos|move|sl)\s+sl\s+(?:a|to)\s*(\d+(?:\.\d+)?)", text_lower) or \
                        re.search(r"sl\s+(?:a|to)\s*(\d+(?:\.\d+)?)", text_lower)
        if move_sl_match:
            target_sl = float(move_sl_match.group(1))
            if self.active_ticket:
                logger.info(f"[LoganGold] 🔄 Ajustando SL dinámicamente a {target_sl} para Ticket #{self.active_ticket}...")
                self.executor.modify_position_sl(self.active_ticket, target_sl)
            return

        # 5. CIERRES TOTALES O ABANDONO (Ej: "estoy fuera", "cerrar todo", "close now")
        if "estoy fuera" in text_lower or "close" in text_lower or "cerrar" in text_lower:
            if self.active_ticket:
                logger.info(f"[LoganGold] 🛑 Solicitud de cierre total detectada. Cerrando Ticket #{self.active_ticket}...")
                self.executor.close_position_completely(self.active_ticket)
                self._reset_state()
            return

    async def _monitor_position(self, ticket):
        """Bucle asíncrono para verificar que la posición sigue viva y aplicar medidas de seguridad"""
        logger.info(f"[LoganGold] 🔍 Monitoreo en vivo iniciado para Ticket #{ticket}...")
        
        # Pequeña pausa inicial para dejar que MT5 asiente la posición en el servidor
        await asyncio.sleep(1) 
        
        while self.active_ticket == ticket:
            try:
                # 1. Verificar si la posición sigue existiendo (por si tocó SL/TP nativo en MT5)
                positions = mt5.positions_get(ticket=ticket)
                if not positions:
                    logger.info(f"[LoganGold] ℹ️ La posición #{ticket} ya se ha cerrado en MT5. Finalizando monitoreo.")
                    self._reset_state()
                    break

                pos = positions[0]
                current_price = pos.price_current
                entry_price = self.entry_price or pos.price_open
                
                # 2. LÍMITE DE SEGURIDAD (ANTI-FALLOS): 100 pips (10.0 USD de distancia en Oro)
                # Al estar solucionado el bug del precio de entrada (ya no es 0.0), esta regla solo
                # se activará si el precio sufre una catástrofe de 10 USD en contra (o a favor) sin actualizarse.
                if entry_price > 0:
                    price_diff_usd = abs(current_price - entry_price)
                    if price_diff_usd >= 10.0: 
                        logger.warning(f"[LoganGold] 🏁 Límite de seguridad de 100 pips alcanzado ({entry_price} -> {current_price}). Cerrando posición.")
                        self.executor.close_position_completely(ticket)
                        self._reset_state()
                        break

            except Exception as e:
                logger.error(f"[LoganGold] Error en bucle de monitoreo: {e}")
            
            await asyncio.sleep(1)  # Barrido de control cada segundo

    def _reset_state(self):
        """Limpia las variables de tracking al terminar la operación actual"""
        self.active_ticket = None
        self.entry_price = None
        self.direction = None
        self.volume = 0.0
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
        logger.info("[LoganGold] 🧹 Estado de LoganGold reseteado con éxito. Listo para la siguiente señal.")