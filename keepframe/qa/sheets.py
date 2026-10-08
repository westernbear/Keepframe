from __future__ import annotations
from pathlib import Path
from typing import Sequence
import cv2
import numpy as np

GUTTER, STRIP = 4, 26


def _cell(img, cell_w: int, cell_h: int) -> np.ndarray:
    if img is None:   # a check that did not run
        cell = np.full((cell_h, cell_w, 3), 48, np.uint8)
        cv2.line(cell, (0, 0), (cell_w - 1, cell_h - 1), (90, 90, 90), 2)
        cv2.line(cell, (cell_w - 1, 0), (0, cell_h - 1), (90, 90, 90), 2)
        return cell
    img = np.asarray(img)
    img = img.round().clip(0, 255).astype(np.uint8) if img.dtype != np.uint8 else img
    return cv2.resize(img, (cell_w, cell_h), interpolation=cv2.INTER_AREA)


def _label(canvas: np.ndarray, text: str, x: int, y: int) -> None:
    cv2.putText(canvas, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1, cv2.LINE_AA)


def comparison_sheet(rows: Sequence[Sequence], labels: Sequence[str], out: Path, cell_w: int = 480,
                     row_labels: Sequence[str] | None = None) -> Path:
    """One row per frame, one column per label (RGB images; None draws a crossed-out cell), labels on top."""
    first = next((np.asarray(c) for r in rows for c in r if c is not None), None)
    cell_h = round(cell_w * first.shape[0] / first.shape[1]) if first is not None else cell_w * 9 // 16
    n = max(len(labels), max((len(r) for r in rows), default=0))
    width = n * cell_w + (n + 1) * GUTTER
    sheet = np.full((STRIP + len(rows) * (cell_h + GUTTER) + GUTTER, width, 3), 24, np.uint8)
    for i, text in enumerate(labels):
        _label(sheet, text, GUTTER + i * (cell_w + GUTTER) + 6, STRIP - 8)
    for r, row in enumerate(rows):
        y = STRIP + GUTTER + r * (cell_h + GUTTER)
        for i in range(n):
            x = GUTTER + i * (cell_w + GUTTER)
            sheet[y:y + cell_h, x:x + cell_w] = _cell(row[i] if i < len(row) else None, cell_w, cell_h)
        if row_labels:
            cv2.rectangle(sheet, (GUTTER, y), (GUTTER + 9 * len(row_labels[r]) + 10, y + 22), (24, 24, 24), -1)
            _label(sheet, row_labels[r], GUTTER + 5, y + 16)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR))
    return out
