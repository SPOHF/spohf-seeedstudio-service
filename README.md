# SPOHF reTerminal Screenshot Service

Shows a chart from the SPOHF digital twin dashboard on a Seeed Studio
[reTerminal E1004](https://wiki.seeedstudio.com/getting_started_with_reterminal_e1004/)
e-paper display.

The dashboard sits behind a Keycloak (OAuth2) login, so SenseCraft HMI can't
load it directly. This service logs in with a headless browser, screenshots the
chart when the panel asks for it, and serves it on a public URL that SenseCraft
HMI renders on the panel.

Capturing is on demand: a request re-captures when the last screenshot is older
than `MAX_AGE_MINUTES`, so nothing runs between panel wake-ups. That lets
Railway's Serverless mode sleep the container, and you only pay while it's awake.

```
SPOHF dashboard ──(Playwright login + screenshot on request)──> this service (Railway)
                                                                     │
reTerminal E1004 <── SenseCraft HMI "Web Content" renders  https://<service>/
```

## Endpoints

| Path            | What                                                                   |
| --------------- | ---------------------------------------------------------------------- |
| `/`             | HTML page with all figures stacked. **Use this in SenseCraft.**        |
| `/fig/{n}`      | HTML page with figure `n` only, for one Web Content widget per figure  |
| `/fig/{n}.png`  | Latest screenshot of figure `n` (also `.jpg`)                          |
| `/chart.png`    | Figure 1 (also `.jpg`)                                                 |
| `/health`       | JSON: last attempt, last success, last error, number of figures        |

Without `DASHBOARD_URL` the service runs in **demo mode** and renders a random
bar chart instead, which is handy for testing the display pipeline.

## Configuration

All settings are environment variables (see `screenshot-service/.env.example`).

| Variable                             | Example / default                                                          |
| ------------------------------------ | -------------------------------------------------------------------------- |
| `DASHBOARD_URL`                      | `https://wp6-red.spohf.fontysvenlo.dev/multi_height/crop-climate`          |
| `LOGIN_USERNAME`                     | account email                                                              |
| `LOGIN_PASSWORD`                     | account password                                                           |
| `FIG1_SELECTOR`                      | `article.card:has(h2:has-text("Risk timeline"))` → `/fig/1`                |
| `FIG2_SELECTOR`                      | `article.card:has(h2:has-text("Climate —"))` → `/fig/2`                    |
| `CHART_SELECTOR`                     | Single figure if no `FIGn_SELECTOR` is set (default `body`)                |
| `MAX_AGE_MINUTES`                    | `5` (re-capture on request if the last capture is older)                   |
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
5. **Settings → Deploy → Serverless**: enable it. The container sleeps after
   10 min without outbound traffic and wakes on the next request. The first
   request after sleeping takes longer (cold start + login + screenshot).
6. Every push to `main` redeploys automatically. Check `/health` for `last_success`.

Current deployment: https://spohf-seeedstudio-service-production.up.railway.app/

## SenseCraft HMI setup

1. SenseCraft HMI portal → **Workspace → My Pages → + Web Content**.
2. URL: `https://spohf-seeedstudio-service-production.up.railway.app/` (all figures),
   or one widget per figure with `/fig/1`, `/fig/2`, ...
3. Set the device **Interval (min)**. The panel re-renders the page on every wake-up.

Notes:

- Use **Web Content**, not the image/Gallery upload. Uploaded images are copied
  once and never refresh.
- The panel can't be forced to refresh remotely. It sleeps between intervals
  (minimum 5 min). Keep `MAX_AGE_MINUTES` shorter than the device interval, so
  every wake-up gets a fresh screenshot.
- Short intervals drain the battery fast. Use USB power for ≤ 30 min.
