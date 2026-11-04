"""Configuração lida do .env. Só o MongoDB: a análise é somente leitura."""
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


@dataclass(frozen=True)
class Config:
    mongo_uri: str
    mongo_db: str
    mongo_colecao: str


def carregar() -> Config:
    return Config(
        mongo_uri=os.environ["MONGO_URI"],
        mongo_db=os.environ.get("MONGO_DB", "iot"),
        mongo_colecao=os.environ.get("MONGO_COLECAO", "sensores"),
    )
