import os
import time
import logging
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient, events
from executor import MT5Executor
from mapper import PrimeGoldMapper, LoganGoldMapper
from services.prime_gold_service import PrimeGoldService
from services.logan_gold_service import LoganGoldService, MILESTONE

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

# A dónde se mandan los avisos de ERROR/CRITICAL por Telegram. "me" = tus
# Mensajes Guardados (no requiere configurar nada más). Si prefieres un chat o
# canal privado dedicado a avisos, pon aquí su chat_id en el .env.
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID", "me")

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


# --- AVISOS POR TELEGRAM PARA ERRORES CRÍTICOS ---

class TelegramAlertHandler(logging.Handler):
    """
    Handler de logging adicional: cualquier log de nivel ERROR o superior
    (en CUALQUIER logger del proyecto, no solo MainOrchestrator -- incluye
    LoganGoldService, PrimeGoldService, etc.) se manda también por Telegram,
    además de quedar en bot.log como siempre. Así te enteras al momento desde
    el móvil de un "SLIPPAGE CRÍTICO", un "SL SOSPECHOSO" o un fallo de
    conexión, sin tener que ir a mirar el log a mano.

    Si todavía no hay un loop de asyncio corriendo (p.ej. un error muy al
    principio, antes de conectar a Telegram) el aviso se omite en silencio
    -- nunca deja de escribirse en bot.log. Si el ENVÍO falla (p.ej. sin
    internet en ese instante, o Telegram no reconoce aún el chat_id de
    destino), queda registrado como WARNING en el log normal -- nunca por
    Telegram, para no entrar en bucle avisando de que no se pudo avisar.
    """
    _LOGGER_NAME = "TelegramAlertHandler"

    def __init__(self, client: TelegramClient, target, level=logging.ERROR):
        super().__init__(level=level)
        self.client = client
        self.target = target
        self._diag = logging.getLogger(self._LOGGER_NAME)

    def emit(self, record: logging.LogRecord):
        if record.name == self._LOGGER_NAME:
            return  # evita bucle infinito si el aviso de un fallo de envío vuelve a fallar
        try:
            msg = self.format(record)
        except Exception:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._send_safe(msg))

    async def _send_safe(self, text: str):
        try:
            await self.client.send_message(self.target, text[:4000])
        except Exception as e:
            self._diag.warning(f"No se pudo enviar el aviso por Telegram a '{self.target}': {e}")


logging.getLogger().addHandler(TelegramAlertHandler(client, ALERT_CHAT_ID, level=MILESTONE))


# --- ROUTER DE MENSAJES (EVENT HANDLER) ---

async def delegate_to_service(event, is_edit: bool):
    """Función centralizada para enrutar mensajes nuevos y editados a su servicio."""
    chat_id = event.chat_id
    message_text = event.raw_text

    service = services_registry.get(chat_id)
    if not service:
        # Solo advertimos de canales no registrados si es un mensaje nuevo, para no spamear
        if not is_edit:
            logger.warning(f"⚠️ Recibido mensaje de un canal no registrado: {chat_id}")
        return

    logger.info(f"📬 {'✏️ Edición' if is_edit else 'Nuevo mensaje'} en canal #{chat_id}. Delegando a {service.__class__.__name__}...")
    
    try:
        # Inyectamos is_edit para que el servicio sepa cómo actuar
        await service.process_message(message_text, is_edit=is_edit)
    except Exception as e:
        logger.error(f"❌ Error grave procesando {'edición' if is_edit else 'señal'} en {service.__class__.__name__}: {e}", exc_info=True)


@client.on(events.NewMessage(chats=list(services_registry.keys())))
async def new_message_handler(event):
    await delegate_to_service(event, is_edit=False)


@client.on(events.MessageEdited(chats=list(services_registry.keys())))
async def edited_message_handler(event):
    await delegate_to_service(event, is_edit=True)


# --- CONEXIÓN CON REINTENTOS ---
async def connect_mt5_with_retry(delay_seconds: int = 15):
    attempt = 0
    while True:
        attempt += 1
        logger.info(f"🔌 Conectando a MetaTrader 5 (Servidor: {MT5_SERVER}, intento {attempt})...")
        try:
            if executor.connect():
                logger.info("✅ Conexión establecida con éxito con MetaTrader 5.")
                return
        except Exception as e:
            logger.warning(f"⚠️ Excepción conectando a MT5 (intento {attempt}): {e}")
        logger.warning(f"⚠️ Fallo al conectar con MT5 (intento {attempt}). Reintentando en {delay_seconds}s...")
        await asyncio.sleep(delay_seconds)


async def start_telegram_with_retry(delay_seconds: int = 15):
    attempt = 0
    while True:
        attempt += 1
        try:
            logger.info(f"📲 Conectando a la API de Telegram y autenticando sesión (intento {attempt})...")
            await client.start()
            logger.info("✅ Telegram conectado con éxito. Escuchando canales activos...")
            return
        except Exception as e:
            logger.warning(f"⚠️ Fallo al conectar con Telegram (intento {attempt}): {e}. Reintentando en {delay_seconds}s...")
            await asyncio.sleep(delay_seconds)


# --- FLUJO PRINCIPAL DE INICIO ---

async def main():
    logger.info("🚀 Iniciando Sistema de Trading Algorítmico...")

    await connect_mt5_with_retry()
    await start_telegram_with_retry()

    # Aviso de arranque -- si esto llega a tus Mensajes Guardados, sabes que
    # el bot se levantó bien tras el corte de luz (o el reinicio que sea).
    try:
        await client.send_message(ALERT_CHAT_ID, "✅ Bot de trading iniciado y escuchando canales.")
    except Exception:
        pass

    # Mantener el loop de Telethon vivo de forma asíncrona
    await client.run_until_disconnected()


if __name__ == "__main__":
    RESTART_DELAY_SECONDS = 30
    while True:
        try:
            asyncio.run(main())
            logger.warning(f"⚠️ El bucle principal terminó de forma inesperada. Reiniciando en {RESTART_DELAY_SECONDS}s...")
        except KeyboardInterrupt:
            logger.info("🛑 Ejecución del bot detenida manualmente por el usuario.")
            break
        except Exception as e:
            logger.critical(f"❌ Error fatal imprevisto en el bucle principal: {e}", exc_info=True)
            logger.warning(f"⚠️ Reiniciando en {RESTART_DELAY_SECONDS}s...")
        time.sleep(RESTART_DELAY_SECONDS)