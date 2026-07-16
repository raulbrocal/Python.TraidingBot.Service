import logging
from abc import ABC, abstractmethod

class BaseService(ABC):
    def __init__(self, channel_id: int, executor):
        self.channel_id = channel_id
        self.executor = executor
        # Instanciamos un logger dinámico que tomará el nombre de la clase hija
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def process_message(self, message: str):
        """
        Punto de entrada principal. 
        Cada servicio hijo DEBE implementar su propia lógica de procesamiento.
        """
        pass