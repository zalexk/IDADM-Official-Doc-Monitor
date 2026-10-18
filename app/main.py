"""Application entrypoint for ``uvicorn app.main:app``."""

from __future__ import annotations

import logging

from .api import create_app
from .config import Settings

settings = Settings.from_env()
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

app = create_app(settings)
