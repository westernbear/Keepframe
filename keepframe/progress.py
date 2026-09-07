from __future__ import annotations

import contextvars
from collections.abc import Callable

from keepframe.log import get

STAGES = (
    "frames",
    "background",
    "text",
    "regions",
    "tracking",
    "sprites",
    "keyframes",
    "semantics",
    "constraints",
    "report",
)

StageFn = Callable[[str, str | None], None]
_on_stage: contextvars.ContextVar[StageFn | None] = contextvars.ContextVar("keepframe_stage", default=None)
log = get("keepframe.analyze")


def bind_stage(fn: StageFn | None):
    return _on_stage.set(fn)


def reset_stage(token) -> None:
    _on_stage.reset(token)


def remaining_eta(stage: str, total: int | None) -> int | None:
    if not total:
        return total
    if stage not in STAGES:
        return total
    left = len(STAGES) - STAGES.index(stage)
    return max(1, int(round(total * left / len(STAGES))))


def report_stage(name: str, detail: str | None = None) -> None:
    extra = f" {detail}" if detail else ""
    log.info("stage=%s%s", name, extra)
    fn = _on_stage.get()
    if fn:
        fn(name, detail)
