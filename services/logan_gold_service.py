import logging
import asyncio
import re
import MetaTrader5 as mt5  # Usado para el monitoreo de precio en tiempo real
from models import TradeAction

logger = logging.getLogger(__name__)

class LoganGoldService:
    def __init__(self, channel_id, executor):
        self.channel_id = channel_id
        self.executor = executor
        
        # Estado de la posición activa
        self.active_ticket = None
        self.action = None          # 'BUY' o 'SELL'
        self.entry_price = None
        self.initial_volume = None
        
        # Niveles de precio de la señal
        self.sl = None
        self.tp1 = None
        self.tp2 = None
        self.tp3 = None
        
        # Flags de control de gestión
        self.tp2_closed = False
        self.tp3_closed = False
        
        # Tarea en segundo plano para monitorear el precio
        self.monitor_task = None

    async def process_message(self, message_text):
        text_upper = message_text.upper()

        # 1. IGNORAR MENSAJE DE PREPARACIÓN
        if "READY" in text_upper:
            logger.info("[LoganGold] 🔔 Mensaje 'Ready' recibido. Ignorado, esperando gatillo...")
            return

        # 2. GATILLO DE ENTRADA INMEDIATA A MERCADO (GOLD BUY YA / GOLD SELL YA)
        if "GOLD BUY YA" in text_upper or "GOLD SELL YA" in text_upper:
            if self.active_ticket:
                logger.warning("[LoganGold] ⚠️ Intento de abrir orden 'YA' pero ya hay una posición activa en ejecución.")
                return
            
            self.action = "BUY" if "GOLD BUY YA" in text_upper else "SELL"
            logger.info(f"[LoganGold] 🚀 ¡GATILLO DETECTADO! Ejecutando {self.action} a mercado inmediato...")
            
            # Calculamos lote dinámico y ejecutamos a mercado inmediato sin SL/TP aún
            ticket, price, volume = await asyncio.to_thread(self._execute_immediate_market, self.action)
            
            if ticket:
                self.active_ticket = ticket
                self.entry_price = price
                self.initial_volume = volume
                self.tp2_closed = False
                self.tp3_closed = False
                logger.info(f"[LoganGold] ✅ Posición abierta con Ticket #{ticket} a precio {price} (Lotes: {volume})")
                
                # Iniciar el bucle de monitorización en tiempo real del precio de XAUUSD
                if self.monitor_task:
                    self.monitor_task.cancel()
                self.monitor_task = asyncio.create_task(self._track_live_price())
            else:
                logger.error("[LoganGold] ❌ Error crítico: No se pudo abrir la posición a mercado.")
            return

        # 3. SEGUNDO MENSAJE: EXTRACCIÓN DE NIVELES (SL Y TPs)
        if "SL:" in text_upper or "SL " in text_upper:
            if not self.active_ticket:
                logger.warning("[LoganGold] ⚠️ Se recibieron niveles de SL/TP pero no hay ninguna posición activa en el bot.")
                return
            
            logger.info("[LoganGold] 📝 Procesando mensaje de niveles de Stop Loss y Take Profits...")
            self._parse_levels(message_text)
            
            if self.sl:
                # Modificamos la orden en MT5 para asignarle el Stop Loss inicial oficial
                success = await asyncio.to_thread(self.executor.modify_position_sl, self.active_ticket, self.sl)
                if success:
                    logger.info(f"[LoganGold] 🛡️ Stop Loss oficial fijado en {self.sl} para Ticket #{self.active_ticket}")
                else:
                    logger.error(f"[LoganGold] ❌ No se pudo actualizar el SL en MetaTrader 5.")

    def _parse_levels(self, text):
        """Extrae el SL y la lista de TPs de forma secuencial usando expresiones regulares"""
        # Limpiar texto para evitar fallos de formato
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        tps_encontrados = []
        for line in lines:
            line_upper = line.upper()
            # Extraer SL
            if "SL:" in line_upper or line_upper.startswith("SL "):
                match = re.search(r'(?:SL:?\s*)([0-9.]+)', line_upper)
                if match: self.sl = float(match.group(1))
            # Extraer TPs correlativos
            elif "TP:" in line_upper or line_upper.startswith("TP "):
                match = re.search(r'(?:TP:?\s*)([0-9.]+)', line_upper)
                if match: tps_encontrados.append(float(match.group(1)))
        
        # Asignar los TPs por orden de aparición en la imagen
        if len(tps_encontrados) >= 1: self.tp1 = tps_encontrados[0]
        if len(tps_encontrados) >= 2: self.tp2 = tps_encontrados[1]
        if len(tps_encontrados) >= 3: self.tp3 = tps_encontrados[2]
        
        logger.info(f"[LoganGold] Niveles Cargados -> SL: {self.sl} | TP1: {self.tp1} | TP2: {self.tp2} | TP3: {self.tp3}")

    async def _track_live_price(self):
        """Bucle asíncrono que vigila el precio de XAUUSD en MT5 para ejecutar las salidas parciales"""
        logger.info(f"[LoganGold] 🔍 Monitoreo en vivo iniciado para Ticket #{self.active_ticket}...")
        
        while self.active_ticket:
            await asyncio.sleep(1) # Revisar el precio cada segundo
            
            tick = mt5.symbol_info_tick("XAUUSD")
            if not tick:
                continue
                
            current_price = tick.bid if self.action == "BUY" else tick.ask
            
            # Validar si la posición sigue viva en MT5 (por si tocó el SL real en el servidor)
            if not self._is_position_still_open():
                logger.info(f"[LoganGold] ℹ️ La posición #{self.active_ticket} se cerró externamente (SL o manual).")
                self._reset_state()
                break

            # --- GESTIÓN DE TAKE PROFITS ---
            
            # CASO TP2: Cierre del 80%
            if self.tp2 and not self.tp2_closed:
                hit = (self.action == "BUY" and current_price >= self.tp2) or (self.action == "SELL" and current_price <= self.tp2)
                if hit:
                    volume_to_close = round(self.initial_volume * 0.80, 2)
                    if volume_to_close > 0:
                        logger.info(f"[LoganGold] 🎯 ¡TP2 Alcanzado ({self.tp2})! Cerrando 80% parcial ({volume_to_close} lotes)...")
                        await asyncio.to_thread(self.executor.partial_close_position, self.active_ticket, volume_to_close)
                    self.tp2_closed = True

            # CASO TP3: Cierre de un 15% adicional (dejando 5% correr) + Mover SL a TP1
            if self.tp3 and not self.tp3_closed:
                hit = (self.action == "BUY" and current_price >= self.tp3) or (self.action == "SELL" and current_price <= self.tp3)
                if hit:
                    volume_to_close = round(self.initial_volume * 0.15, 2)
                    logger.info(f"[LoganGold] 🎯 ¡TP3 Alcanzado ({self.tp3})! Cerrando 15% adicional ({volume_to_close} lotes)...")
                    if volume_to_close > 0:
                        await asyncio.to_thread(self.executor.partial_close_position, self.active_ticket, volume_to_close)
                    
                    self.tp3_closed = True
                    
                    if self.tp1:
                        logger.info(f"[LoganGold] 🛡️ Aplicando BE a TP1: Moviendo SL a {self.tp1}")
                        await asyncio.to_thread(self.executor.modify_position_sl, self.active_ticket, self.tp1)

            # CASO MAX 100 PIPS: Cierre definitivo (10.0 puntos de movimiento en Oro de forma estándar)
            pips_distance = abs(current_price - self.entry_price)
            if pips_distance >= 10.0:  # 10.0 USD de cambio en XAUUSD equivale a 100 pips estándares
                logger.info(f"[LoganGold] 🏁 Límite de 100 pips alcanzado desde la entrada ({self.entry_price} -> {current_price}). Cerrando remanente.")
                await asyncio.to_thread(self.executor.close_position_completely, self.active_ticket)
                self._reset_state()
                break

    def _execute_immediate_market(self, action):
        """Llama al executor de MT5 para lanzar la orden instantánea adaptada al tamaño de la cuenta"""
        # Aquí reutilizaremos el balance o reglas de gestión monetaria que tengas en tu executor
        # Este método debe devolver: (ticket, precio_entrada, lotaje_abierto)
        return self.executor.execute_logan_market_order(action, symbol="XAUUSD")

    def _is_position_still_open(self):
        """Verifica si el ticket sigue figurando en las posiciones abiertas de MT5"""
        positions = mt5.positions_get(ticket=self.active_ticket)
        return len(positions) > 0

    def _reset_state(self):
        """Limpia las variables para quedar listo para la próxima operación"""
        self.active_ticket = None
        self.action = None
        self.entry_price = None
        self.initial_volume = None
        self.sl = None
        self.tp1 = None
        self.tp2 = None
        self.tp3 = None
        self.tp2_closed = False
        self.tp3_closed = False
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None