import os
import logging
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events

# Importar Ejecutor y Mapeador existentes
from executor import MT5Executor
from mapper import SignalMapper

# Importar los nuevos Servicios
from services.prime_gold_service import PrimeGoldService
from services.logan_gold_service import LoganGoldService

logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# Configuración Telegram
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE_NUMBER")

# Configuración MT5
MT5_ACCOUNT = os.getenv("MT5_ACCOUNT")
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER = os.getenv("MT5_SERVER")

# 1. Instanciar componentes compartidos
executor = MT5Executor(MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER)
prime_mapper = SignalMapper() # El mapeador actual pertenece a PrimeGold

# 2. Recuperar IDs de canales
PRIME_GOLD_ID = int(os.getenv("PRIME_GOLD_CHANNEL_ID"))
LOGAN_GOLD_ID = int(os.getenv("LOGAN_GOLD_CHANNEL_ID"))

# 3. Inicializar los servicios con sus dependencias
services_registry = {
    PRIME_GOLD_ID: PrimeGoldService(PRIME_GOLD_ID, prime_mapper, executor),
    LOGAN_GOLD_ID: LoganGoldService(LOGAN_GOLD_ID, executor)
}

# Lista de canales que el cliente de Telegram debe escuchar
LISTEN_CHANNELS = list(services_registry.keys())

client = TelegramClient('my_bot', API_ID, API_HASH)

# Escucha activa en múltiples canales a la vez
@client.on(events.NewMessage(chats=LISTEN_CHANNELS))
async def router_handler(event):
    channel_id = event.chat_id
    message_text = event.raw_text
    
    # Buscamos qué servicio debe procesar este canal
    service = services_registry.get(channel_id)
    
    if service:
        # Ejecutamos de forma asíncrona el procesamiento de ese servicio particular
        await service.process_message(message_text)

async def main():
    # Conexión global a MetaTrader 5
    if not executor.connect():
        logger.critical("No se pudo iniciar/conectar a MetaTrader 5. Saliendo...")
        return
    
    logger.info("Iniciando cliente de Telegram Multiservicio...")
    await client.start(phone=PHONE)
    
    logger.info(f"📡 Escuchando de forma continua {len(LISTEN_CHANNELS)} canales de trading...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Servicio multiservicio detenido por el usuario.")