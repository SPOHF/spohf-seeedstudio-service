# SPOHF reTerminal Screenshot Service

Shows a chart from the SPOHF digital twin dashboard on a Seeed Studio
[reTerminal E1004](https://wiki.seeedstudio.com/getting_started_with_reterminal_e1004/)
e-paper display.

The dashboard sits behind a Keycloak (OAuth2) login, so SenseCraft HMI can't
load it directly. This service logs in with a headless browser, screenshots the
chart on a timer, and serves it on a public URL that SenseCraft HMI renders on
the panel.

```
SPOHF dashboard ──(Playwright login + screenshot every N min)──> this service (Railway)
                                                                      │
reTerminal E1004 <── SenseCraft HMI "Web Content" renders  https://<service>/
```

## Endpoints

| Path         | What                                                             |
| ------------ | ---------------------------------------------------------------- |
| `/`          | Minimal HTML page showing the chart. **Use this in SenseCraft.** |
| `/chart.png` | Latest screenshot (PNG)                                          |
| `/chart.jpg` | Latest screenshot (JPEG)                                         |
| `/health`    | JSON: last attempt, last success, last error                     |

Without `DASHBOARD_URL` the service runs in **demo mode** and renders a random
bar chart instead, which is handy for testing the display pipeline.

## Configuration

All settings are environment variables (see `screenshot-service/.env.example`).

| Variable                             | Example / default                                                          |
| ------------------------------------ | -------------------------------------------------------------------------- |
| `DASHBOARD_URL`                      | `https://wp6-blue.spohf.fontysvenlo.dev/gdd`                               |
| `LOGIN_USERNAME`                     | account email                                                              |
| `LOGIN_PASSWORD`                     | account password                                                           |
| `CHART_SELECTOR`                     | `.js-plotly-plot` (CSS selector of the element to capture; default `body`) |
| `INTERVAL_MINUTES`                   | `30`                                                                       |
| `VIEWPORT_WIDTH` / `VIEWPORT_HEIGHT` | `1600` / `1200`                                                            |
| `SETTLE_MS`                          | `3000` (wait for chart animations)                                         |
| `IMAGE_MAX_WIDTH`                    | `0` (no resize)                                                            |
| `CACHE_SECONDS`                      | `60`                                                                       |

The login account must sign in with email + password directly on the Keycloak
form. SSO via "Fontys Venlo staff" (MFA) can't be automated. Never commit
credentials: `.env` is git-ignored, and on Railway they live in service variables.

## Deployment (Railway)

1. New Project → **Deploy from GitHub repo** → this repo.
2. Service **Settings → Root Directory**: `/screenshot-service` (Railway builds the `Dockerfile` there).
3. **Settings → Networking → Generate Domain.**
4. **Variables**: set the ones from the table above.
5. Every push to `main` redeploys automatically. Check `/health` for `last_success`.

Current deployment: https://spohf-seeedstudio-service-production.up.railway.app/

## SenseCraft HMI setup

1. SenseCraft HMI portal → **Workspace → My Pages → + Web Content**.
2. URL: `https://spohf-seeedstudio-service-production.up.railway.app/`
3. Set the device **Interval (min)**. The panel re-renders the page on every wake-up.

Notes:

- Use **Web Content**, not the image/Gallery upload. Uploaded images are copied
  once and never refresh.
- The panel can't be forced to refresh remotely. It sleeps between intervals
  (minimum 5 min). Keep `INTERVAL_MINUTES` a bit shorter than the device interval.
- Short intervals drain the battery fast. Use USB power for ≤ 30 min.
