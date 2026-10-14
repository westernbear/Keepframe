"""Render a kf-script HTML page frame by frame and compare it with the source video.
usage: gold_compare.py page.html video.mp4 out_prefix first last step [w h]"""
import sys, asyncio, numpy as np, cv2
from playwright.async_api import async_playwright
page_path, video, out, a, b, step = sys.argv[1:7]; a, b, step = int(a), int(b), int(step)
W, H = (int(sys.argv[7]), int(sys.argv[8])) if len(sys.argv) > 8 else (1920, 1080)
frames = list(range(a, b + 1, step))
src = {}; cap = cv2.VideoCapture(video); n = -1
while True:
    ok, im = cap.read(); n += 1
    if not ok or n > b: break
    if n in frames: src[n] = im
async def render():
    out = {}
    async with async_playwright() as p:
        br = await p.chromium.launch(); pg = await br.new_page(viewport={"width": W, "height": H})
        await pg.goto("file://" + page_path); await pg.wait_for_function("window.__ready === true")
        for f in frames:
            await pg.evaluate(f"window.__seek({f})")
            buf = await pg.screenshot(); out[f] = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
        await br.close()
    return out
ren = asyncio.run(render())
l1 = {f: float(np.abs(src[f].astype(int) - ren[f].astype(int)).mean() / 255) for f in frames}
print("mean L1 %.4f  worst %.4f @f%d" % (np.mean(list(l1.values())), max(l1.values()), max(l1, key=l1.get)))
print(" ".join(f"f{f}:{v:.3f}" for f, v in l1.items()))
pick = frames[:: max(1, len(frames) // 12)][:12]
rows = []
for f in pick:
    s, r = src[f], ren[f]
    d = cv2.applyColorMap(np.clip(np.abs(s.astype(int) - r.astype(int)).sum(2) / 3 * 3, 0, 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    row = np.hstack([cv2.resize(x, (480, int(480 * H / W))) for x in (s, r, d)])
    cv2.putText(row, f"f{f} L1 {l1[f]:.3f}", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    rows.append(row)
cv2.imwrite(out + "_sheet.png", np.vstack(rows))
