from __future__ import annotations

import base64
import xml.etree.ElementTree as ET

from playwright.sync_api import sync_playwright

from ..assets import AssetAPIError, sanitize_svg


def _sanitize_chat_svg(svg: bytes) -> bytes:
    try:
        return sanitize_svg(svg)
    except AssetAPIError as exc:
        if exc.code != "unsafe_svg":
            raise
    # Chat drops scripts and external hrefs; the shared validator stays strict.
    root = ET.fromstring(svg)
    for parent in root.iter():
        for child in list(parent):
            if child.tag.rsplit("}", 1)[-1].lower() == "script":
                parent.remove(child)
        for name, value in list(parent.attrib.items()):
            if name.rsplit("}", 1)[-1].lower() == "href" and value and not value.startswith("#"):
                del parent.attrib[name]
    return sanitize_svg(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def rasterize_svg(svg: bytes, width: int, height: int) -> bytes:
    """Render a transparent PNG at up to twice the box resolution, capped at 4096px."""
    data = _sanitize_chat_svg(svg)
    if width <= 0 or height <= 0:
        raise ValueError("SVG raster box dimensions must be positive")
    scale = min(2.0, 4096 / max(width, height))
    viewport = {"width": max(1, round(width * scale)), "height": max(1, round(height * scale))}
    encoded = base64.b64encode(data).decode("ascii")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb"])
        try:
            page = browser.new_page(
                viewport=viewport,
                device_scale_factor=1, java_script_enabled=False, service_workers="block",
            )
            page.route("**/*", lambda route: route.abort())
            page.set_content(
                '<style>html,body{margin:0;background:transparent}img{display:block;width:100vw;height:100vh;object-fit:contain}</style>'
                f'<img src="data:image/svg+xml;base64,{encoded}">',
                wait_until="load",
            )
            return page.screenshot(type="png", omit_background=True, animations="disabled")
        finally:
            browser.close()
