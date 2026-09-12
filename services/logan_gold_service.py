import logging
import time
import MetaTrader5 as mt5
from models import TradeSignal, TradeAction
from services.base_service import BaseService

logger = logging.getLogger(__name__)

# Nivel intermedio entre INFO (20) y WARNING (30): marca "hitos importantes"
# (YA, parámetros aplicados, BE, cierres) que sí quieres ver por Telegram,
# sin llegar al ruido de cada línea INFO normal (mensajes recibidos, texto
# crudo, confirmaciones por posición individual...). Los ERROR/CRITICAL de
# siempre (slippage, SL sospechoso, corrección de bloque) ya quedan por
# encima de este nivel, así que se siguen mandando igual sin tocar nada.
MILESTONE = 25
logging.addLevelName(MILESTONE, "MILESTONE")

class LoganGoldService(BaseService):
    def __init__(self, channel_id: int, mapper, executor):
        super().__init__(channel_id, executor)
        self.mapper = mapper
        self.magic_number = 202611
        self.max_lot_per_order = 0.50
        # Diferencia maxima (en puntos de precio) que se tolera entre el SL que
        # ya tenian las posiciones y un SL nuevo llegado en un mensaje NUEVO
        # (no edicion) antes de considerarlo sospechoso. Ajustalo si ves falsos
        # positivos o si quieres ser mas estricto.
        self.max_sl_jump_points = 20.0
        # A partir de cuantos puntos de distancia entre el precio real de
        # ejecucion y el rango de entrada declarado se considera que hay un
        # error sistematico en TODO el bloque de precios (rango+SL+TPs), no
        # solo una diferencia normal. Ajustalo si ves falsos positivos.
        self.price_block_sanity_threshold = 40.0

    async def process_message(self, message: str, is_edit: bool = False):
        self.logger.info(f"📩 Procesando {'EDICIÓN' if is_edit else 'MENSAJE'} de Logan Gold...")
        # Sin este log, un incidente como el de hoy es indiagnosticable: el log
        # solo decía "Procesando MENSAJE", nunca QUÉ decía el mensaje. Con esto,
        # la próxima vez se puede ver el texto exacto que disparó cada acción.
        self.logger.info(f"📝 Texto: {message!r}")
        msg_lower = message.lower().strip()
        
        # 1. FILTROS RÁPIDOS DE EVENTOS (Solo para mensajes nuevos)
        if not is_edit:
            # --- TP 1: No hacer nada ---
            if "tp 1" in msg_lower and not any(kw in msg_lower for kw in ["be", "sl a be", "asegurar"]):
                self.logger.info("🎯 TP 1 detectado. Estrategia: Mantener (órdenes sin cambios).")
                return

            # --- TP 2: Auto-Breakeven ---
            if "tp 2" in msg_lower:
                self.logger.log(MILESTONE, "✂️ TP 2 detectado. Protegiendo el resto a Breakeven...")
                self._set_trades_to_breakeven()
                return

            # --- TP 3 / Cierres totales ---
            if "tp 3" in msg_lower:
                self.logger.info("🎯 TP 3 detectado. Dejando correr el runner (Orden C).")
                return
                
            if any(kw in msg_lower for kw in ["todos los tps", "posiciones cerradas", "cerrar todo"]):
                self.logger.log(MILESTONE, "🛑 Comando de cierre total detectado. Liquidando runners de Logan Gold...")
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
            self.logger.log(MILESTONE, "🛡️ Comando de Breakeven explícito detectado. Protegiendo posiciones...")
            self._set_trades_to_breakeven()
            
        elif signal.action == TradeAction.MOVE_SL:
            self.logger.info(f"🔄 Comando MOVE_SL detectado. Moviendo a: {signal.stop_loss}")
            self._update_dynamic_sl(signal.stop_loss)


    # ---------------------------------------------------------
    # GESTIÓN DE ACTUALIZACIONES RÁPIDAS
    # ---------------------------------------------------------

    def _update_existing_positions(self, signal: TradeSignal):
        """Busca posiciones activas y actualiza el SL según el mensaje editado."""
        active_positions = self.executor.get_positions(magic_number=self.magic_number)
        if not active_positions:
            if signal.stop_loss > 0.0:
                self.logger.warning("⚠️ Logan editó un mensaje, pero no hay posiciones activas para este canal.")
            return

        # Una edición también puede traer el mismo error sistemático de
        # bloque completo (rango+SL+TPs desplazados) que un mensaje nuevo.
        self._reconcile_signal_with_market(signal, active_positions[0].price_open)

        if signal.stop_loss <= 0.0:
            return

        self.logger.log(MILESTONE, "✍️ Mensaje editado con SL detectado. Evaluando actualizar posiciones activas...")

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

        self.logger.log(MILESTONE, f"⚡ Lanzando Gatillo Multi-Orden LoganGold: {signal.action.name} (Total: {total_lot} lotes a {price})")

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

    def _sanitize_tp(self, tp: float, ref_price: float, pos_type: int) -> float:
        """Valida y corrige TPs invertidos o con erratas del analista (Ej: TP 4885 en SELL a 4490)."""
        if tp <= 0.0 or ref_price <= 0.0:
            return 0.0
        
        # Comprobar si el TP va en contra de la lógica de la posición
        is_invalid = (pos_type == mt5.POSITION_TYPE_SELL and tp >= ref_price) or \
                     (pos_type == mt5.POSITION_TYPE_BUY and tp <= ref_price)
        
        if is_invalid:
            str_tp = str(int(tp))
            str_ref = str(int(ref_price))
            
            # Si tienen la misma longitud (ej. 4885 y 4490), intentamos corregir el prefijo
            if len(str_tp) == len(str_ref):
                corrected_tp = float(str_ref[:2] + str_tp[2:] + ('.' + str(tp).split('.')[1] if '.' in str(tp) else ''))
                
                # Volvemos a validar si el TP corregido tiene sentido
                still_invalid = (pos_type == mt5.POSITION_TYPE_SELL and corrected_tp >= ref_price) or \
                                (pos_type == mt5.POSITION_TYPE_BUY and corrected_tp <= ref_price)
                                
                if not still_invalid:
                    self.logger.warning(f"⚠️ Errata detectada en TP ({tp}). Corregido automáticamente a {corrected_tp}")
                    return corrected_tp
            
            self.logger.error(f"❌ TP Inválido ({tp}) descartado para {'SELL' if pos_type == mt5.POSITION_TYPE_SELL else 'BUY'} en precio {ref_price}")
            return 0.0 # Si no se puede corregir, se devuelve 0.0 para que MetaTrader no rechace la orden entera
            
        return tp

    def _reconcile_signal_with_market(self, signal: TradeSignal, reference_price: float):
        """
        A veces el analista escribe TODO el bloque de precios (rango, SL, TPs)
        con el mismo error sistemático en las centenas -- p.ej. "4493-4487"
        cuando el precio real ronda 4393: sobran 100 puntos en TODOS los
        números del mensaje, no solo en uno. _sanitize_tp corrige TPs sueltos
        comparándolos contra el precio; esto es lo mismo pero a nivel de
        bloque entero, usando como referencia un precio que SÍ sabemos que es
        correcto: el precio real de apertura de las posiciones ya abiertas
        por el "ya".

        Deliberadamente NO se dispara por "el rango es ancho" (eso ya lo
        probamos hace unos mensajes y daba falsos positivos con rangos anchos
        legítimos), sino por "el precio real queda FUERA del rango declarado,
        y no por poco". Si el precio real cae dentro del rango (por ancho que
        sea), no se toca nada.
        """
        if signal.entry_min <= 0.0 or signal.entry_max <= 0.0 or reference_price <= 0.0:
            return

        if reference_price < signal.entry_min:
            raw_offset = signal.entry_min - reference_price
        elif reference_price > signal.entry_max:
            raw_offset = signal.entry_max - reference_price
        else:
            return  # el precio real cae DENTRO del rango declarado -> nada que corregir

        if abs(raw_offset) < self.price_block_sanity_threshold:
            return  # se sale del rango pero por poco, dentro de lo normal

        offset = round(raw_offset / 50.0) * 50.0
        if offset == 0.0:
            return

        self.logger.error(
            f"⚠️ CORRECCIÓN AUTOMÁTICA DE BLOQUE DE PRECIOS: el rango declarado "
            f"({signal.entry_min}-{signal.entry_max}) está a {abs(raw_offset):.1f} puntos del "
            f"precio real de ejecución ({reference_price}). Aplicando {-offset:+.0f} puntos "
            f"a rango, SL y TPs por igual."
        )
        self.logger.info(
            f"   Antes:   rango=({signal.entry_min}, {signal.entry_max}) sl={signal.stop_loss} tps={signal.take_profits}"
        )

        signal.entry_min = round(signal.entry_min - offset, 2)
        signal.entry_max = round(signal.entry_max - offset, 2)
        if signal.stop_loss > 0.0:
            signal.stop_loss = round(signal.stop_loss - offset, 2)
        signal.take_profits = [round(tp - offset, 2) for tp in signal.take_profits]

        self.logger.info(
            f"   Después: rango=({signal.entry_min}, {signal.entry_max}) sl={signal.stop_loss} tps={signal.take_profits}"
        )

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

        # -1. RECONCILIACIÓN DEL BLOQUE DE PRECIOS CONTRA EL PRECIO REAL
        # Usa el precio real de apertura (dato mas fiable que tenemos) como
        # referencia para detectar y corregir un error sistematico en TODO el
        # mensaje, no solo en el SL. Va antes que el resto de escudos a
        # proposito: si esto corrige el bloque, los escudos de abajo evaluan
        # ya los numeros buenos.
        self._reconcile_signal_with_market(signal, active_positions[0].price_open)

        # 0. GUARDIÁN DE PLAUSIBILIDAD DEL SL
        # Si las posiciones YA tenían un SL real puesto (esto no es la primera
        # vez que llegan parámetros para esta señal -- esa primera vez siempre
        # tiene sl=0 recién abiertas por el "ya"), y el SL que acaba de llegar
        # se desvía una barbaridad del que ya tenían, no nos fiamos ciegamente:
        # ni para sobreescribirlo ni, sobre todo, para dejar que el escudo
        # anti-slippage de abajo cierre todo basándose en un número que puede
        # ser un mensaje suelto, una errata o una edición mal clasificada como
        # mensaje nuevo. Esto es justo lo que pasó hoy: llegó un "Nuevo
        # mensaje" (no una edición) con SL=4308 cuando las posiciones ya
        # tenían SL=4408 puesto -- una diferencia de 100 puntos totalmente
        # implausible para un ajuste real, y bastó para que el precio actual
        # (4394.77) "rebasara" ese SL falso y se cerrara todo sin que hubiera
        # pasado nada realmente crítico en el mercado.
        existing_sls = {pos.sl for pos in active_positions if pos.sl and pos.sl > 0.0}
        if signal.stop_loss > 0.0 and existing_sls:
            max_deviation = max(abs(signal.stop_loss - sl) for sl in existing_sls)
            if max_deviation > self.max_sl_jump_points:
                self.logger.error(
                    f"🚨 SL SOSPECHOSO: llega SL={signal.stop_loss} pero las posiciones ya "
                    f"tenían SL≈{sorted(existing_sls)} ({max_deviation:.1f} puntos de diferencia, "
                    f"máximo permitido {self.max_sl_jump_points}). Este mensaje debería haber sido "
                    f"una EDICIÓN, no uno nuevo -- ignorando por seguridad, no toco ni cierro nada."
                )
                return

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

        # 2. EXTRACCIÓN DIRECTA DE TAKE PROFITS (Sin fallbacks dinámicos ni modificaciones de distancia)
        tps = signal.take_profits
        if not tps:
            self.logger.error("❌ No se encontraron Take Profits en la señal enviada.")
            return

        # TP2 (Índice 1 de la lista: 4483)
        tp_a = tps[1] if len(tps) >= 2 else tps[0]
        
        # TP3 (Índice 2 de la lista: 4481)
        tp_b = tps[2] if len(tps) >= 3 else tp_a
        
        # TP4 (Índice 3 de la lista: 4479)
        tp_c = tps[3] if len(tps) >= 4 else tp_b

        # 3. ASIGNACIÓN POR VOLUMEN (0.40 -> TP2, 0.05 -> TP3, 0.05 -> TP4)
        # Orden DESC por volumen (la de 0.40 siempre primero) y, en empate de
        # volumen entre B y C (ambas 0.05), ASC por ticket -> la que se abrio
        # primero (B) entra antes que la que se abrio despues (C), igual que
        # el orden cronologico con el que se crearon en _execute_split_market_orders.
        sorted_positions = sorted(active_positions, key=lambda p: (-p.volume, p.ticket))
        targets = [tp_a, tp_b, tp_c]
        
        self.logger.log(MILESTONE, f"🔄 Inyectando SL exacto ({signal.stop_loss}) y TPs: 0.40={tp_a}, 0.05={tp_b}, 0.05={tp_c}")

        for i, pos in enumerate(sorted_positions):
            target_tp = targets[i] if i < len(targets) else tp_a

            # Misma sanitización que usan las Limit: si el TP viene invertido/con
            # errata de tecleo, intenta corregirlo antes de descartarlo a 0.0.
            target_tp = self._sanitize_tp(target_tp, pos.price_open, pos_type)

            self.executor.modify_position(pos.ticket, sl=signal.stop_loss, tp=target_tp)

        # 4. DEJAR ORDEN LÍMITE
        self._place_limit_order(signal, symbol, pos_type)

    def _place_limit_order(self, signal, symbol, pos_type):
        si = self.executor.get_symbol_info(symbol)
        if signal.entry_min == 0.0 or signal.entry_max == 0.0: return

        limit_price = signal.entry_max - 1.0 if pos_type == mt5.POSITION_TYPE_SELL else signal.entry_min + 1.0

        # ESCUDO DE RE-ENTRADA INVERTIDA: un SELL_LIMIT solo es valido si el
        # precio de la limit esta POR ENCIMA del precio actual (esperas una
        # subida para vender mejor); un BUY_LIMIT solo si esta POR DEBAJO. Si
        # el precio ya rebaso ese nivel en la direccion contraria (subio tanto
        # que ya esta por encima de una SELL limit, o bajo tanto que ya esta
        # por debajo de una BUY limit) antes de que diera tiempo a colocarla,
        # MT5 la rechaza por invertida -- y como send_order() no comprueba el
        # retcode, eso pasaba desapercibido: no saltaba SL (el escudo
        # anti-slippage de _process_signal_parameters no se activa, porque el
        # SL sigue intacto) pero tampoco existia ninguna orden real esperando
        # si el precio volvia a bajar. En ese caso, en vez de la Limit,
        # ejecutamos las 3 posiciones A/B/C A MERCADO ya mismo, con SL y TP
        # puestos desde el principio (a diferencia del "ya" original, aqui SI
        # tenemos ya esos datos), igual que si hubiera llegado una segunda
        # señal "ya" para esta re-entrada.
        tick = self.executor.get_tick(symbol)
        current_price = (tick.bid if pos_type == mt5.POSITION_TYPE_SELL else tick.ask) if tick else None

        execute_as_market = False
        exec_price = limit_price
        if current_price is not None:
            if pos_type == mt5.POSITION_TYPE_SELL and current_price > limit_price:
                execute_as_market = True
                exec_price = current_price
            elif pos_type == mt5.POSITION_TYPE_BUY and current_price < limit_price:
                execute_as_market = True
                exec_price = current_price

        if execute_as_market:
            order_type = mt5.ORDER_TYPE_SELL if pos_type == mt5.POSITION_TYPE_SELL else mt5.ORDER_TYPE_BUY
        else:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT if pos_type == mt5.POSITION_TYPE_SELL else mt5.ORDER_TYPE_BUY_LIMIT

        balance = self.executor.get_account_balance()
        min_vol = si.volume_min if si else 0.01
        
        total_lot = min(round(balance * 0.0000025, 2), self.max_lot_per_order)
        total_lot = max(total_lot, min_vol)
        
        # 1. Fraccionar el lote (80 / 10 / 10)
        vol_a, vol_b, vol_c = self._calculate_lot_distribution(total_lot, min_vol)

        # 2. LÓGICA ESTRUCTURAL DE TAKE PROFITS PARA LIMITS (o para el fallback a mercado)
        tps = signal.take_profits
        tp_a, tp_b, tp_c = 0.0, 0.0, 0.0
        
        if len(tps) >= 2: tp_a = tps[1]
        elif len(tps) == 1: tp_a = tps[0]
            
        if len(tps) >= 3: tp_b = tps[2]
        else: tp_b = tp_a

        if len(tps) >= 4: tp_c = tps[3]
        else:
            offset = 30.0
            tp_c = exec_price + offset if pos_type == mt5.POSITION_TYPE_BUY else exec_price - offset

        # Saneamiento de erratas en los TPs, contra el precio de ejecucion real
        tp_a = self._sanitize_tp(tp_a, exec_price, pos_type)
        tp_b = self._sanitize_tp(tp_b, exec_price, pos_type)
        tp_c = self._sanitize_tp(tp_c, exec_price, pos_type)

        # 3. Preparar la estructura de ejecución
        label = "ReEntry" if execute_as_market else "Limit"
        orders = [
            (vol_a, f"Logan {label} A 80%", tp_a),
            (vol_b, f"Logan {label} B 10%", tp_b),
            (vol_c, f"Logan {label} C 10%", tp_c)
        ]

        if execute_as_market:
            self.logger.warning(
                f"⚠️ El precio ({current_price}) ya rebasó el nivel de re-entrada ({limit_price}) "
                f"antes de poder colocar la Limit. Ejecutando las 3 órdenes A MERCADO en su lugar, "
                f"con SL={signal.stop_loss} y TP puestos desde ya."
            )
        else:
            self.logger.log(MILESTONE, f"⏳ Colocando {sum(1 for v, _, _ in orders if v > 0)} órdenes Limit estructurales en {limit_price}")
        
        # 4. Lanzar las órdenes
        for vol, comment, tp in orders:
            if vol > 0:
                self.executor.send_order(
                    symbol=symbol, order_type=order_type, volume=vol, price=exec_price,
                    sl=signal.stop_loss, tp=tp, magic=self.magic_number, comment=comment,
                    is_market=execute_as_market
                )
                time.sleep(0.1)


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

        self.logger.log(MILESTONE, f"🛡️ Modificando SL de todas las posiciones a: {final_sl}")
        for pos in positions:
            self.executor.modify_position_sl(pos.ticket, final_sl)

    def _set_trades_to_breakeven(self):
        # 1. Proteger las posiciones activas pasándolas a Breakeven
        positions = self.executor.get_positions(magic_number=self.magic_number)
        for pos in positions:
            si = self.executor.get_symbol_info(pos.symbol)
            if not si: continue

            offset = max(si.spread + 5, 20) * si.point
            new_sl = round(pos.price_open + offset, si.digits) if pos.type == mt5.POSITION_TYPE_BUY else round(pos.price_open - offset, si.digits)
            
            if (pos.type == mt5.POSITION_TYPE_BUY and pos.sl < new_sl) or (pos.type == mt5.POSITION_TYPE_SELL and (pos.sl > new_sl or pos.sl == 0.0)):
                self.executor.modify_position_sl(pos.ticket, new_sl)
                self.logger.info(f"✅ SL de Ticket #{pos.ticket} blindado en {new_sl}")

        # 2. Cancelar cualquier orden Limit pendiente (ya no son válidas)
        pending = self.executor.get_pending_orders(magic_number=self.magic_number)
        if pending:
            self.logger.info(f"🗑️ Operación asegurada en BE. Cancelando {len(pending)} órdenes Limit pendientes...")
            for order in pending:
                self.executor.cancel_pending_order(order.ticket)
                time.sleep(0.1) # Pausa de seguridad para no saturar MetaTrader

    def _execute_complete_close_all(self):
        positions = self.executor.get_positions(magic_number=self.magic_number)
        for pos in (positions or []):
            self.executor.close_position(pos.ticket, pos.volume)
            
        pending = self.executor.get_pending_orders(magic_number=self.magic_number)
        for order in (pending or []):
            self.executor.cancel_pending_order(order.ticket)