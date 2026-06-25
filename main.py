import os
import logging
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events

from mapper import SignalMapper
from executor import MT5Executor
from models import TradeAction

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Cargar variables de entorno
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE_NUMBER")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID"))

MT5_ACCOUNT = os.getenv("MT5_ACCOUNT")
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER = os.getenv("MT5_SERVER")

# Instanciar clases
mapper = SignalMapper()
executor = MT5Executor(MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER)

# Inicializar Telethon (creará un archivo my_bot.session en tu carpeta)
client = TelegramClient('my_bot', API_ID, API_HASH)

last_valid_signal = None

@client.on(events.NewMessage(chats=TARGET_CHANNEL_ID))
async def handler(event):
    global last_valid_signal
    message_text = event.raw_text
    signal = mapper.map_message(message_text)
    
    if not signal: return

    # CASO: ACTIVA (Entrada a mercado)
    if signal.action == TradeAction.ACTIVATE:
        if last_valid_signal:
            logger.info(f"⚡ 'Activa' detectado. Entrando a mercado para {last_valid_signal.symbol}...")
            success = await asyncio.to_thread(executor.execute_market, last_valid_signal)
            if success: logger.info("✅ Ejecución market completada.")
            # Opcional: cancelar órdenes límite anteriores para no duplicar riesgo
            await asyncio.to_thread(executor.cancel_pending_orders)
        else:
            logger.warning("⚠️ Se recibió 'Activa' pero no hay ninguna señal previa guardada.")

    # CASO: BREAKEVEN
    elif signal.action == TradeAction.BREAKEVEN:
        logger.info("🛡️ Mensaje de BREAKEVEN recibido...")
        await asyncio.to_thread(executor.set_trades_to_breakeven)

    # CASO: SEÑAL NUEVA (Guardamos la señal en memoria)
    elif signal.action in [TradeAction.BUY, TradeAction.SELL]:
        last_valid_signal = signal # Guardamos en memoria para el futuro "Activa"
        logger.info(f"🎯 Nueva señal guardada y enviando límites: {signal.action.value} {signal.symbol}")
        await asyncio.to_thread(executor.execute, signal)

    # CASO: CANCELACIÓN
    elif signal.action == TradeAction.CANCEL:
        logger.info("🛑 Cancelando órdenes...")
        await asyncio.to_thread(executor.cancel_pending_orders)

async def main():
    # 1. Conectar a MetaTrader 5
    if not executor.connect():
        logger.critical("No se pudo iniciar/conectar a MetaTrader 5. Saliendo...")
        return
    
    # 2. Conectar a Telegram
    logger.info("Iniciando cliente de Telegram...")
    await client.start(phone=PHONE)
    
    logger.info("📡 Escuchando mensajes de Telegram de forma continua...")
    
    # Mantener el script corriendo
    await client.run_until_disconnected()

if __name__ == '__main__':
    # Punto de entrada de la aplicación
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Servicio detenido por el usuario.")