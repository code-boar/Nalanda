"""Centralised logging setup using rich for readable console output."""

from __future__ import annotations

import logging
import sys

from rich.logging import RichHandler

_CONFIGURED = False


def setup_logging(level: int | str = logging.INFO) -> None:
    """Configure root logging with a rich handler. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    # Windows consoles default to a legacy code page (e.g. cp1252); force UTF-8 so
    # rich can print non-ASCII titles (accents, etc.) without erroring on emit.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # not a reconfigurable TextIO
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except ValueError:  # e.g. the stream already has buffered data
            pass
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=False)],
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)
