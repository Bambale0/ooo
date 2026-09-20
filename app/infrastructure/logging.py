import logging

from pythonjsonlogger.json import JsonFormatter

from app.infrastructure.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s %(trace_id)s"))
    root.addHandler(handler)
