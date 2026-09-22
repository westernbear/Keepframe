from __future__ import annotations

from pathlib import Path


def looks_live_action(path: Path) -> str | None:
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(path))
    ok, bgr = cap.read()
    cap.release()
    if not ok:
        return None
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
    ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    skin = cv2.inRange(ycrcb, (0, 133, 77), (255, 173, 127))
    frac = float(np.mean(skin > 0))
    if sharp > 120 and frac > 0.08:
        return "실사 푸티지. 평면 2D MG·UI 녹화만 받음."
    return None
