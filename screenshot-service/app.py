"""SPOHF screenshot service.

Logs into the SPOHF digital twin dashboard (OAuth login form), screenshots one
chart every INTERVAL_MINUTES and serves it so SenseCraft HMI can pull it:

  GET /           -> minimal HTML page showing the chart (use this in the Web Function)
  GET /chart.png  -> the latest screenshot
  GET /health     -> JSON status (last success / last error)
"""

import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spohf")

# --- config (all via environment variables, see .env.example) ---------------
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")
DEMO_MODE = not DASHBOARD_URL  # no dashboard configured -> render a random demo chart
LOGIN_USERNAME = os.environ.get("LOGIN_USERNAME", "")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "")

USERNAME_SELECTOR = os.environ.get(
    "USERNAME_SELECTOR",
    'input[type="email"], input[name="username"], input[name="loginfmt"], #username',
)
PASSWORD_SELECTOR = os.environ.get("PASSWORD_SELECTOR", 'input[type="password"]')
SUBMIT_SELECTOR = os.environ.get(
    "SUBMIT_SELECTOR", 'button[type="submit"], input[type="submit"]'
)
# Optional extra click after login, e.g. Microsoft's "Stay signed in? -> Yes" (#idSIButton9)
POST_LOGIN_CLICK_SELECTOR = os.environ.get("POST_LOGIN_CLICK_SELECTOR", "")

CHART_SELECTOR = os.environ.get("CHART_SELECTOR", "body")
VIEWPORT_WIDTH = int(os.environ.get("VIEWPORT_WIDTH", "1600"))
VIEWPORT_HEIGHT = int(os.environ.get("VIEWPORT_HEIGHT", "1200"))
SETTLE_MS = int(os.environ.get("SETTLE_MS", "3000"))  # let chart animations finish
INTERVAL_MINUTES = float(os.environ.get("INTERVAL_MINUTES", "30"))

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/tmp/spohf"))
CHART_FILE = OUTPUT_DIR / "chart.png"
STATE_FILE = OUTPUT_DIR / "storage_state.json"  # reused session cookies

status = {"last_attempt": None, "last_success": None, "last_error": None}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- screenshot --------------------------------------------------------------
async def login(page) -> None:
    log.info("Login form detected, logging in")
    await page.locator(USERNAME_SELECTOR).first.fill(LOGIN_USERNAME)

    password = page.locator(PASSWORD_SELECTOR).first
    if not await password.is_visible():
        # Two-step login (username -> next -> password), e.g. Microsoft / Google
        await page.locator(SUBMIT_SELECTOR).first.click()
        await password.wait_for(state="visible", timeout=15_000)

    await password.fill(LOGIN_PASSWORD)
    await page.locator(SUBMIT_SELECTOR).first.click()

    if POST_LOGIN_CLICK_SELECTOR:
        try:
            await page.locator(POST_LOGIN_CLICK_SELECTOR).first.click(timeout=10_000)
        except Exception:
            log.info("Post-login click target not shown, continuing")

    await page.wait_for_load_state("networkidle")


def demo_html() -> str:
    """Bar chart with random values, using only the Spectra 6 panel colors."""
    colors = ["#000000", "#ff0000", "#0000ff", "#00a000", "#ffd700", "#000000"]
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    bars = "".join(
        f'<div class="col"><div class="val">{v}</div>'
        f'<div style="height:{v * 9}px;background:{c}"></div></div>'
        for c, v in zip(colors, (random.randint(10, 100) for _ in labels))
    )
    names = "".join(f'<div class="col">{l}</div>' for l in labels)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html><html><head><style>
  body {{ margin: 0; background: #fff; font-family: sans-serif; }}
  #chart {{ width: 1200px; padding: 48px; box-sizing: border-box; }}
  h1 {{ font-size: 64px; margin: 0 0 8px; }}
  .sub {{ font-size: 32px; margin-bottom: 48px; }}
  .bars, .names {{ display: flex; align-items: flex-end; gap: 40px; }}
  .bars {{ height: 1000px; border-bottom: 6px solid #000; }}
  .names {{ font-size: 40px; padding-top: 16px; }}
  .col {{ flex: 1; text-align: center; }}
  .val {{ font-size: 40px; font-weight: bold; margin-bottom: 8px; }}
</style></head><body><div id="chart">
  <h1>SPOHF demo</h1><div class="sub">Generated {stamp}</div>
  <div class="bars">{bars}</div><div class="names">{names}</div>
</div></body></html>"""


async def capture() -> None:
    status["last_attempt"] = now_iso()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            context = await browser.new_context(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                storage_state=str(STATE_FILE) if STATE_FILE.exists() else None,
            )
            page = await context.new_page()

            if DEMO_MODE:
                await page.set_content(demo_html())
                chart = page.locator("#chart")
            else:
                await page.goto(DASHBOARD_URL, wait_until="networkidle")

                if await page.locator(USERNAME_SELECTOR).first.is_visible():
                    await login(page)
                    if not page.url.startswith(DASHBOARD_URL):
                        await page.goto(DASHBOARD_URL, wait_until="networkidle")
                    await context.storage_state(path=str(STATE_FILE))

                chart = page.locator(CHART_SELECTOR).first
                await chart.wait_for(state="visible", timeout=30_000)
                await page.wait_for_timeout(SETTLE_MS)

            tmp = CHART_FILE.with_suffix(".tmp.png")
            await chart.screenshot(path=str(tmp))
            tmp.replace(CHART_FILE)  # atomic swap, never serve a half-written file
        finally:
            await browser.close()

    status["last_success"] = now_iso()
    status["last_error"] = None
    log.info("Screenshot saved to %s", CHART_FILE)


async def capture_loop() -> None:
    while True:
        try:
            await capture()
        except Exception as e:  # keep serving the last good image
            status["last_error"] = f"{now_iso()} {type(e).__name__}: {e}"
            log.exception("Capture failed")
            STATE_FILE.unlink(missing_ok=True)  # force fresh login next time
        await asyncio.sleep(INTERVAL_MINUTES * 60)


# --- web server --------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    if DEMO_MODE:
        log.warning("DASHBOARD_URL is not set, running in DEMO mode (random bar chart)")
    task = asyncio.create_task(capture_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


@app.get("/", response_class=HTMLResponse)
def index():
    version = int(CHART_FILE.stat().st_mtime) if CHART_FILE.exists() else 0
    updated = status["last_success"] or "never"
    return HTMLResponse(
        f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body {{ margin: 0; height: 100%; background: #fff; color: #000;
               font-family: sans-serif; }}
  body {{ display: flex; flex-direction: column; }}
  img {{ flex: 1; min-height: 0; width: 100%; object-fit: contain; }}
  p {{ margin: 8px 16px; font-size: 20px; }}
</style></head>
<body>
  <img src="/chart.png?v={version}" alt="SPOHF chart">
  <p>Updated: {updated}</p>
</body></html>""",
        headers=NO_CACHE,
    )


@app.get("/chart.png")
def chart_png():
    if not CHART_FILE.exists():
        return JSONResponse({"error": "no screenshot yet"}, status_code=503)
    return FileResponse(CHART_FILE, media_type="image/png", headers=NO_CACHE)


@app.get("/health")
def health():
    return status
