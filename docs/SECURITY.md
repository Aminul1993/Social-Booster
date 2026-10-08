# Security

This document describes the threat model, the controls that implement it and
the operational practices expected in production.

## Assets

| Asset | Where it lives |
|---|---|
| Ollama Cloud API key, Buffer personal API key (`BUFFER_ACCESS_TOKEN`) | Server environment / Docker secrets only (`SecretStr`) |
| The Buffer account itself | Reachable by **anyone who can open the app**: the one server-wide key can post to every channel of that account and does not expire until revoked |
| Session (id, CSRF token, flashes) | itsdangerous-**signed** cookie (`HttpOnly`, `SameSite=Lax`, `Secure` in production) |
| Uploaded images | Upload volume; random 128-bit names; EXIF/GPS stripped |
| Drafts (captions, hashtags) | SQLite, scoped to the session id |

## Threats and mitigations

| Threat | Mitigation | Where |
|---|---|---|
| **API keys exposed to the browser** | Ollama/Buffer calls happen server-side only; keys are `SecretStr`, never rendered, logged or serialised. | `app/config.py`, `services/*` |
| **Session tampering** | Starlette `SessionMiddleware` signs the cookie with `SESSION_SECRET` (itsdangerous `TimestampSigner`, `max_age`). Tampered cookies are discarded. Production refuses to start without a 32+ character secret. | `app/main.py`, `app/config.py` |
| **Buffer key leakage** | The key is server configuration only: never in the cookie (signed != encrypted), never rendered into a page and never stored in the database. It is sent as an `Authorization` header, never in a URL. Log redaction scrubs `Bearer ...`, `access_token=...`, `token=...` as defence in depth. | `app/config.py`, `services/buffer.py`, `app/logging_config.py` |
| **Unauthorised publishing** (anyone who reaches the app posts to the Buffer account) | The app has **no login of its own**: every visitor shares the account behind `BUFFER_ACCESS_TOKEN`. Deploy it on a private network or behind access control at the proxy - Caddy `basic_auth`, an SSO/forward-auth proxy or an IP allow-list - keeping `/uploads/*` reachable for Buffer. CSRF tokens and the `schedule` rate limit stop cross-site and scripted abuse, not a visitor who can open the page. Revoke the key in Buffer (Settings -> API) if it may have leaked. | `deploy/Caddyfile`, [DEPLOYMENT.md](DEPLOYMENT.md#access-control) |
| **CSRF** | Synchronizer token per session, required in `X-CSRF-Token` on every POST/PATCH/DELETE (constant-time comparison). `SameSite=Lax` cookies and `selfRequestsOnly` HTMX add depth. FastAPI's strict content-type checking blocks JSON-without-content-type tricks. | `app/security.py::verify_csrf` |
| **IDOR (accessing someone else's draft)** | Every query filters by `session_id`; draft ids are 128-bit random; ids are validated by regex. | `app/repositories.py` |
| **Malicious uploads** (polyglots, scripts, wrong types) | Allow-listed declared MIME types; magic-byte sniffing must agree; Pillow full decode; images are **re-encoded** (destroys appended payloads, strips metadata); storage keys generated server-side; files served with `nosniff`. | `services/images.py`, `services/storage.py` |
| **Decompression bombs / huge files** | Per-file byte limit enforced while streaming, explicit pixel budget checked before decoding, Pillow's bomb warning escalated to an error, global request body limit (413). | `app/validation.py`, `services/images.py`, `app/middleware.py` |
| **Path traversal** | Original filenames are only displayed (sanitised, HTML-escaped); storage keys must match `^[0-9a-f]{32}\.(jpg|png|webp)$`. | `services/storage.py` |
| **Privacy leaks via EXIF/GPS** | All metadata removed on re-encode; orientation applied first. | `services/images.py` |
| **Images shared with the vision provider** | With `VISION_BACKEND=ollama` (default) a <=512 px JPEG preview of each upload, already stripped of metadata, is sent to `VISION_ENDPOINT` (Ollama Cloud by default) over HTTPS with the API key server-side; the model's reply is treated as data (JSON-parsed, normalised, escaped, and JSON-quoted when reused in the copy prompt). Set `VISION_BACKEND=disabled` to keep images on the server. | `services/vision.py`, `services/prompts.py` |
| **XSS** | Jinja2 autoescaping; toasts use `textContent`; JSON in attributes via `tojson`; strict **CSP** (`script-src 'self'`, `style-src 'self'`, no inline code, `object-src 'none'`, `frame-ancestors 'none'`); HTMX `allowEval=false`, `allowScriptTags=false`, `attributesToSettle` excludes `style`. | `templates/`, `static/js/app.js`, `app/middleware.py`, `app/templating.py` |
| **Clickjacking** | `X-Frame-Options: DENY` and `frame-ancestors 'none'`. | `app/middleware.py` |
| **Brute force / abuse / cost blow-up** | Token-bucket rate limits per IP for upload, generate, schedule, other writes and Buffer profile refreshes; bounded files per request, drafts per session; `OLLAMA_MAX_TOKENS` caps AI spend; vision concurrency limited by a semaphore. | `app/rate_limit.py`, `app/config.py` |
| **Denial of service through image analysis** | Vision requests bounded per worker by a semaphore with a per-image time budget; inference happens at the provider, not on the server's CPU. | `services/vision.py` |
| **Duplicate posts** | Posting is only retried when the connection failed before sending; re-sending an already published draft asks for confirmation. | `services/buffer.py`, `templates/_card.html` |
| **Host header attacks** | `ALLOWED_HOSTS` (TrustedHostMiddleware); public URLs come from `PUBLIC_BASE_URL` in production. | `app/main.py`, `app/views.py` |
| **Information leakage in errors** | Generic messages with a request id; stack traces only in logs; `/docs` disabled in production. | `app/error_handlers.py` |
| **Stale data** | Drafts/images purged after `DRAFT_RETENTION_HOURS`; the `oauth_tokens` table left by older (OAuth-based) versions is dropped on start-up so their refresh tokens do not linger. | `app/container.py`, `app/db.py` |
| **Supply chain** | Exact dependency pins, no ML runtime (small dependency set), front-end assets vendored (no CDN at runtime), CI on every change. | `requirements*.txt`, `static/vendor/`, `.github/workflows/ci.yml` |
| **Container escape / privilege** | Non-root user (uid 10001), read-only root filesystem, `no-new-privileges`, all capabilities dropped. | `Dockerfile`, `docker-compose.yml` |

## Response headers

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self';
  img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self';
  form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-site   (cross-origin for /uploads/*)
Strict-Transport-Security: max-age=63072000; includeSubDomains   (when COOKIE_SECURE)
X-Request-ID: <id>
```

`img-src https:` allows Buffer profile avatars; everything else is same-origin.

## Secrets handling

* Provide secrets through the environment, a `.env` file with `0600`
  permissions, or Docker/Kubernetes secrets mounted at `/run/secrets/<name>`
  (e.g. `/run/secrets/session_secret`). `.env` is git- and docker-ignored.
* Rotate `SESSION_SECRET` to invalidate all sessions; Buffer is unaffected.
* Rotate the Buffer key by creating a new one in Buffer (Settings -> API),
  updating `BUFFER_ACCESS_TOKEN`, restarting, then revoking the old key. Buffer
  API keys have no scopes and do not expire on their own, so revoke any key
  that may have leaked.
* Protect `/metrics` with `METRICS_TOKEN` or keep it off the public network.

## Known limitations

* There are no user accounts: a "user" is a browser session. Anyone who can
  read the session cookie (e.g. on a shared computer) can use that session.
* Everyone who can open the app publishes to the same Buffer account
  (`BUFFER_ACCESS_TOKEN`). Put a public deployment behind access control -
  Caddy `basic_auth`, your SSO/reverse-proxy authentication or an IP
  allow-list - or add accounts (see ARCHITECTURE.md, Extensibility).
* Uploaded images are public by URL (unguessable 128-bit names) because Buffer
  must be able to fetch them.
* Rate limits are per worker process; use a shared limiter (proxy or Redis)
  for strict global limits.
* SQLite suits a single host. Multi-host deployments need a shared database.

## Reporting a vulnerability

Please report privately to the maintainers instead of opening a public issue.
Include steps to reproduce and the affected version (`GET /health`).
