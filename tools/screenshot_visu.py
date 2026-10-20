"""Screenshot einer Kachelvisu-Seite des Docker-Testsystems (Playwright mit dem installierten Chrome).

    python tools/screenshot_visu.py [url] [ausgabe.png] [breite] [hoehe] [@x,y ...]

Nur Loopback (Testsystem). Standard: http://127.0.0.1:3777/preview/ -> visu.png im aktuellen Ordner.
Voraussetzung: ``pip install -r tools/requirements-tools.txt`` und ein installierter Google Chrome.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3777/preview/"
out = Path(sys.argv[2] if len(sys.argv) > 2 else "visu.png")
width = int(sys.argv[3]) if len(sys.argv) > 3 else 1400
height = int(sys.argv[4]) if len(sys.argv) > 4 else 900

if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
    sys.exit("Abbruch: nur Loopback (Docker-Testsystem) erlaubt.")

out.parent.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": width, "height": height})
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)  # Kacheln werden nach dem Laden per WebSocket gefüllt
    for step in sys.argv[5:]:  # weitere Argumente: Klicks der Reihe nach, "@x,y" (die Visu ist eine Flutter-Canvas-App)
        x, y = (float(v) for v in step.lstrip("@").split(","))
        page.mouse.click(x, y)
        page.wait_for_timeout(2000)
    page.screenshot(path=str(out))
    print(f"Titel: {page.title()!r}  URL: {page.url}\nGespeichert: {out}")
    browser.close()
