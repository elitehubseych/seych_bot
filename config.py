import os
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    load_dotenv()


class Config:

    TOKEN_GROUP = os.getenv("TOKEN_GROUP", "")
    ID_GROUP = os.getenv("ID_GROUP", "")
    CONFIRMATION_TOKEN = os.getenv("CONFIRMATION_TOKEN", "")
    DEV_ID = os.getenv("DEV_ID", "")
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    PEER_BASE = int(os.getenv("PEER_BASE", "0"))
    PEER_ELITE = int(os.getenv("PEER_ELITE", "0"))


config = Config()

_REQUIRED_KEYS = (
    "TOKEN_GROUP",
    "ID_GROUP",
    "CONFIRMATION_TOKEN",
    "DATABASE_URL",
)

for _key in _REQUIRED_KEYS:
    if not getattr(config, _key):
        raise RuntimeError(f"Не задана переменная окружения {_key} в .env")
