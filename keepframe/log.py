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


_SEP = r"(?:[\\/]|%2[Ff]|%5[Cc])++"   # a separator run: /, \, \\ (repr), \/ (JSON), //, %2F, %5C
_CH = r"(?:(?!%2[Ff]|%5[Cc])[^\s'\"`\\/<>|*?])"   # a path character (no space, quote or separator)
_DIRS = rf"(?:{_CH}++(?: {_CH}++)*+{_SEP})*+"   # directories; single spaces inside one, a separator must follow
_NOT_AFTER = r"(?<![\w.~/\\%:…-])"
_PATH = re.compile(
    rf"{_NOT_AFTER}(?:[A-Za-z]:)?{_SEP}(?={_CH}){_DIRS}{_CH}*"        # /abs, C:\abs, \\unc, \/json, %2Fenc
    rf"|(?:(?<=:)/(?![/\\])|(?<=://)/)(?={_CH}){_DIRS}{_CH}*"        # error:/abs, PATH=…:/abs, file:///abs
    rf"|{_NOT_AFTER}(?:~[\w.-]*|\.\.?)(?:{_SEP}\.\.?)*{_SEP}(?={_CH}){_DIRS}{_CH}*"   # ~/x, ~user/x, ../x, ./x
    rf"|{_NOT_AFTER}[^\W\d][\w-]*{_SEP}(?:{_CH}++{_SEP})++{_CH}*")   # relative a/b/c: 2+ separators, no spaces
_LAST = re.compile(r"(?:[\\/]|%2[Ff]|%5[Cc])+")


def scrub_paths(text: object) -> str:
    """`text` with every path cut to its last part (`…/name`): workspace, temp, home and install paths stay out of the
    reasons the server logs (and out of anything a client may see — client text should carry codes, not exception
    text). Absolute POSIX/Windows/UNC paths, Python-repr and JSON-escaped separators, URL-encoded ones, `~` and `../`
    paths, `file://` URLs, colon-prefixed paths (`error:/x`, `PATH=/a:/b`), directories with spaces, and relative paths
    with two or more separators. Kept: http(s) URLs, host/path without a scheme, `AC/DC`, `8/16`, `2026/10/09`, and
    text already scrubbed (idempotent)."""
    return _PATH.sub(lambda m: "…/" + _LAST.split(_LAST.sub("/", m.group(0)).rstrip("/"))[-1], str(text))


def describe(e: BaseException, *, trace: bool = False) -> str:
    """`Type: message` of `e` (its whole traceback when `trace`) with paths cut: for the server log only — client text
    carries a code instead."""
    if trace:
        import traceback
        return scrub_paths("".join(traceback.format_exception(e)).rstrip())
    return scrub_paths(f"{type(e).__name__}: {e}")
