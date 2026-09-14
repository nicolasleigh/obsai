"""CLI logging setup; import has no side effects."""

import logging
import os

from rich.logging import RichHandler


def configure_logging(level: int = logging.WARNING) -> None:
    configured = os.environ.get("OBSAI_LOG_LEVEL", "").upper()
    if configured in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        level = getattr(logging, configured)
    logging.basicConfig(level=level, handlers=[RichHandler(show_path=False)], format="%(message)s")
