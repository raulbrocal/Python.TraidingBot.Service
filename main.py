import os
import logging
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient, events
from executor import MT5Executor
from mapper import PrimeGoldMapper, LoganGoldMapper
from services.prime_gold_service import PrimeGoldService
from services.logan_gold_service import LoganGoldService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("MainOrchestrator")

# Cargar variables del archivo .env
load_dotenv()

# --- VALIDACIÓN DE VARIABLES DE ENTORNO ---
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")

MT5_ACCOUNT = os.getenv("MT5_ACCOUNT")
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER = os.getenv("MT5_SERVER")

PRIME_GOLD_ID = os.getenv("PRIME_GOLD_CHANNEL_ID")
LOGAN_GOLD_ID = os.getenv("LOGAN_GOLD_CHANNEL_ID")

if not all([API_ID, API_HASH, MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER, PRIME_GOLD_ID, LOGAN_GOLD_ID]):
    logger.critical("❌ Faltan configurar variables críticas en el archivo .env. Abortando inicio.")
    exit(1)

# Conversión de tipos para IDs
API_ID = int(API_ID)
PRIME_GOLD_ID = int(PRIME_GOLD_ID)
LOGAN_GOLD_ID = int(LOGAN_GOLD_ID)


# --- INICIALIZACIÓN DE COMPONENTES ---

# 1. Instanciamos el executor genérico y los mappers
executor = MT5Executor(MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER)
prime_mapper = PrimeGoldMapper()
logan_mapper = LoganGoldMapper()

# 2. Registramos los servicios emparejando el ID del canal con su Servicio correspondiente
services_registry = {
    PRIME_GOLD_ID: PrimeGoldService(PRIME_GOLD_ID, prime_mapper, executor),
    LOGAN_GOLD_ID: LoganGoldService(LOGAN_GOLD_ID, logan_mapper, executor)
}

# 3. Inicializamos el cliente de Telegram
client = TelegramClient('trading_bot_session', API_ID, API_HASH)


# --- ROUTER DE MENSAJES (EVENT HANDLER) ---

@client.on(events.NewMessage(chats=list(services_registry.keys())))
async def router_handler(event):
    chat_id = event.chat_id
    message_text = event.raw_text

    # Recuperamos el servicio específico para este chat ID
    service = services_registry.get(chat_id)
    if not service:
        logger.warning(f"⚠️ Recibido mensaje de un canal no registrado: {chat_id}")
        return

    logger.info(f"📬 Nuevo mensaje en canal #{chat_id}. Delegando a {service.__class__.__name__}...")
    
    try:
        # Cada servicio procesa el mensaje bajo su propio contrato heredado de BaseService
        await service.process_message(message_text)
    except Exception as e:
        logger.error(f"❌ Error grave procesando señal en {service.__class__.__name__}: {e}", exc_info=True)


# --- FLUJO PRINCIPAL DE INICIO ---

async def main():
    logger.info("🚀 Iniciando Sistema de Trading Algorítmico...")

    # 1. Conexión y Login en MetaTrader 5
    logger.info(f"🔌 Conectando a MetaTrader 5 (Servidor: {MT5_SERVER})...")
    if not executor.connect():
        logger.critical("❌ ERROR CRÍTICO: Imposible conectar o loguearse en MetaTrader 5. Abortando.")
        return
    logger.info("✅ Conexión establecida con éxito con MetaTrader 5.")

    # 2. Arranque del listener de Telegram
    logger.info("📲 Conectando a la API de Telegram y autenticando sesión...")
    await client.start()
    logger.info("✅ Telegram conectado con éxito. Escuchando canales activos...")

    # Mantener el loop de Telethon vivo de forma asíncrona
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Ejecución del bot detenida manualmente por el usuario.")
    except Exception as e:
        logger.critical(f"❌ Error fatal imprevisto en el bucle principal: {e}", exc_info=True)