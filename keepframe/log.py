from __future__ import annotations

import logging
import os
import sys

_configured = False


def configure(level: str | None = None, *, force: bool = False) -> None:
    """Send INFO and ERROR to stdout so `docker logs` shows both."""
    global _configured
    if _configured and not force:
        return
    name = (level or os.environ.get("KEEPFRAME_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    _configured = True


def get(name: str = "keepframe") -> logging.Logger:
    if not _configured:
        configure()
    return logging.getLogger(name)
