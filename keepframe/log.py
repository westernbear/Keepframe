from __future__ import annotations

import logging
import os
import re
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


_ABS_PATH = re.compile(r"(?<![\w.~:/\\])(?:[A-Za-z]:)?(?:[\\/][^\s'\"\\/:<>|,;()\[\]]+)+[\\/]?")


def scrub_paths(text: object) -> str:
    """`text` with every absolute path cut to its last part (`…/name`): workspace, temp and install paths stay out of
    anything a client may see and out of the reasons the server logs."""
    return _ABS_PATH.sub(lambda m: "…/" + re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1], str(text))
