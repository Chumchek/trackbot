
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_FILE = os.getenv("LOG_FILE", "bot.log")
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
# 5 MB per file, keep last 3 files → max 15 MB on disk
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3


def setup_logging() -> None:
    formatter = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    root.addHandler(file_handler)
