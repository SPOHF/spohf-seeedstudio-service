"""SPOHF screenshot service.

Logs into the SPOHF digital twin dashboard (OAuth login form), screenshots one
or more figures every INTERVAL_MINUTES and serves them so SenseCraft HMI can pull them:

  GET /             -> HTML page with all figures stacked (use in Web Content)
  GET /fig/{n}      -> HTML page with figure n only (one Web Content widget per figure)
  GET /fig/{n}.png  -> latest screenshot of figure n (also .jpg)
  GET /chart.png    -> figure 1 (also .jpg)
  GET /health       -> JSON status (last success / last error)
"""

import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
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

# Figures to capture: FIG1_SELECTOR, FIG2_SELECTOR, ... (falls back to CHART_SELECTOR)
CHART_SELECTOR = os.environ.get("CHART_SELECTOR", "body")
FIG_SELECTORS = []
while sel := os.environ.get(f"FIG{len(FIG_SELECTORS) + 1}_SELECTOR"):
    FIG_SELECTORS.append(sel)
FIG_SELECTORS = FIG_SELECTORS or [CHART_SELECTOR]
VIEWPORT_WIDTH = int(os.environ.get("VIEWPORT_WIDTH", "1600"))
VIEWPORT_HEIGHT = int(os.environ.get("VIEWPORT_HEIGHT", "1200"))
SETTLE_MS = int(os.environ.get("SETTLE_MS", "3000"))  # let chart animations finish
INTERVAL_MINUTES = float(os.environ.get("INTERVAL_MINUTES", "30"))
IMAGE_MAX_WIDTH = int(os.environ.get("IMAGE_MAX_WIDTH", "0"))  # 0 = keep original size
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "60"))

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/tmp/spohf"))
STATE_FILE = OUTPUT_DIR / "storage_state.json"  # reused session cookies

status = {"last_attempt": None, "last_success": None, "last_error": None}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fig_file(n: int, ext: str) -> Path:
    return OUTPUT_DIR / f"fig{n}.{ext}"


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
                selectors = ["#chart"]
            else:
                await page.goto(DASHBOARD_URL, wait_until="networkidle")

                if await page.locator(USERNAME_SELECTOR).first.is_visible():
                    await login(page)
                    if not page.url.startswith(DASHBOARD_URL):
                        await page.goto(DASHBOARD_URL, wait_until="networkidle")
                    await context.storage_state(path=str(STATE_FILE))

                selectors = FIG_SELECTORS
                for sel in selectors:
                    await page.locator(sel).first.wait_for(state="visible", timeout=30_000)
                await page.wait_for_timeout(SETTLE_MS)

            for n, sel in enumerate(selectors, start=1):
                await save_figure(page.locator(sel).first, n)
        finally:
            await browser.close()

    status["last_success"] = now_iso()
    status["last_error"] = None
    log.info("Saved %d figure(s) to %s", len(selectors), OUTPUT_DIR)


async def save_figure(element, n: int) -> None:
    png, jpg = fig_file(n, "png"), fig_file(n, "jpg")
    tmp = png.with_suffix(".tmp.png")
    await element.screenshot(path=str(tmp))
    with Image.open(tmp) as img:
        if IMAGE_MAX_WIDTH and img.width > IMAGE_MAX_WIDTH:
            height = round(img.height * IMAGE_MAX_WIDTH / img.width)
            img = img.resize((IMAGE_MAX_WIDTH, height), Image.LANCZOS)
            img.save(tmp)
        tmp_jpg = jpg.with_suffix(".tmp.jpg")
        img.convert("RGB").save(tmp_jpg, "JPEG", quality=90)
    # atomic swaps, never serve a half-written file
    tmp_jpg.replace(jpg)
    tmp.replace(png)


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
# SenseCraft's web dashboard fetches the image via browser JS, which needs CORS
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "HEAD", "OPTIONS"]
)
NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


def figure_numbers() -> list[int]:
    return [n for n in range(1, len(FIG_SELECTORS) + 1) if fig_file(n, "png").exists()]


def image_page(figs: list[int], footer: bool) -> HTMLResponse:
    imgs = "".join(
        f'<img src="/fig/{n}.png?v={int(fig_file(n, "png").stat().st_mtime)}" alt="Figure {n}">'
        for n in figs
    )
    updated = f'<p>Updated: {status["last_success"] or "never"}</p>' if footer else ""
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
  {imgs or "<p>No screenshot yet</p>"}
  {updated}
</body></html>""",
        headers=NO_CACHE,
    )


def serve_image(path: Path, media_type: str):
    if not path.exists():
        return JSONResponse({"error": "no screenshot yet"}, status_code=503)
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": f"public, max-age={CACHE_SECONDS}"},
    )


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    return image_page(figure_numbers(), footer=True)


# .png/.jpg routes must come before /fig/{n}, which would also match "1.png"
@app.api_route("/fig/{n}.png", methods=["GET", "HEAD"])
def figure_png(n: int):
    return serve_image(fig_file(n, "png"), "image/png")


@app.api_route("/fig/{n}.jpg", methods=["GET", "HEAD"])
def figure_jpg(n: int):
    return serve_image(fig_file(n, "jpg"), "image/jpeg")


@app.api_route("/fig/{n}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def figure_page(n: int):
    return image_page([n] if fig_file(n, "png").exists() else [], footer=False)


@app.api_route("/chart.png", methods=["GET", "HEAD"])
def chart_png():
    return serve_image(fig_file(1, "png"), "image/png")


@app.api_route("/chart.jpg", methods=["GET", "HEAD"])
def chart_jpg():
    return serve_image(fig_file(1, "jpg"), "image/jpeg")


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {**status, "figures": len(FIG_SELECTORS)}
