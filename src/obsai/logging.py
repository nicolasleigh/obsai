"""CLI logging setup; import has no side effects."""

import logging

from rich.logging import RichHandler


def configure_logging(level: int = logging.WARNING) -> None:
    logging.basicConfig(level=level, handlers=[RichHandler(show_path=False)], format="%(message)s")
