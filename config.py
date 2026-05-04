import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


def _parse_bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
MONGO_URI: str = os.getenv("MONGO_URI", "")

DB_NAME: str = os.getenv("DB_NAME", "trackapps_bot")
APPS_COLLECTION: str = os.getenv("APPS_COLLECTION", "apps")
CHATS_COLLECTION: str = os.getenv("CHATS_COLLECTION", "chats")

CHECK_INTERVAL: timedelta = timedelta(minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "20")))
CONCURRENCY_LIMIT: int = int(os.getenv("CONCURRENCY_LIMIT", "6"))
HTTP_TIMEOUT_SECONDS: float = float(os.getenv("HTTP_TIMEOUT_SECONDS", "60"))
# Детальні таймінги перевірок Play (логи [check] у checker/main)
CHECK_VERBOSE_LOGS: bool = _parse_bool_env("CHECK_VERBOSE_LOGS", False)


def _parse_app_method() -> int:
    """APP_ID для CRM/заголовків: 0=немає, 1=Trello, 2=trello_app_id from db, 3=Trello пріоритет, 4=ручний пріоритет."""
    try:
        v = int(os.getenv("PARSE_APP_METHOD", "1"))
        if 0 <= v <= 4:
            return v
    except ValueError:
        pass
    return 1


PARSE_APP_METHOD: int = _parse_app_method()

TRELLO_API_KEY: str = os.getenv("TRELLO_API_KEY", "")
TRELLO_API_SECRET: str = os.getenv("TRELLO_API_SECRET", "")
TRELLO_TOKEN: str = os.getenv("TRELLO_TOKEN", "")
TRELLO_ORG_ID: str = os.getenv("TRELLO_ORG_ID", "")
TRELLO_PROCESSING_BOARD_ID: str = os.getenv("TRELLO_PROCESSING_BOARD_ID", "")
TRELLO_IN_MARKET_LIST_ID: str = os.getenv("TRELLO_IN_MARKET_LIST_ID", "")
TRELLO_BANNED_LIST_ID: str = os.getenv("TRELLO_BANNED_LIST_ID", "")

# CRM
CRM_DB_MONGO_URI: str = os.getenv("CRM_DB_MONGO_URI", "")
CRM_DB_NAME: str = os.getenv("CRM_DB_NAME", "")
CRM_ACCOUNTS_COLLECTION: str = os.getenv("CRM_ACCOUNTS_COLLECTION", "accounts")
CRM_APPS_COLLECTION: str = os.getenv("CRM_APPS_COLLECTION", "apps")
CRM_TYPE_APPS: str = os.getenv("CRM_TYPE_APPS", "gray")