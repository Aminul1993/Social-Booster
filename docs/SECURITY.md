# Security

This document describes the threat model, the controls that implement it and
the operational practices expected in production.

## Assets

| Asset | Where it lives |
|---|---|
| Ollama Cloud API key, Buffer client secret | Server environment / Docker secrets only (`SecretStr`) |
| Users' Buffer access tokens | SQLite, **Fernet-encrypted** (AES-128-CBC + HMAC-SHA256), keyed by session id |
| Session (id, CSRF token, OAuth state, flashes) | itsdangerous-**signed** cookie (`HttpOnly`, `SameSite=Lax`, `Secure` in production) |
| Uploaded images | Upload volume; random 128-bit names; EXIF/GPS stripped |
| Drafts (captions, hashtags) | SQLite, scoped to the session id |

## Threats and mitigations

| Threat | Mitigation | Where |
|---|---|---|
| **API keys exposed to the browser** | Ollama/Buffer calls happen server-side only; keys are `SecretStr`, never rendered, logged or serialised. | `app/config.py`, `services/*` |
| **Session tampering** | Starlette `SessionMiddleware` signs the cookie with `SESSION_SECRET` (itsdangerous `TimestampSigner`, `max_age`). Tampered cookies are discarded. Production refuses to start without a 32+ character secret. | `app/main.py`, `app/config.py` |
| **Buffer token leakage** | The token is *not* in the cookie (signed != encrypted). It is encrypted at rest and only decrypted in memory to call Buffer; it is sent as a header, never in a URL. Log redaction scrubs `Bearer ...`, `access_token=...`, `code=...` as defence in depth. | `app/security.py`, `app/repositories.py`, `app/logging_config.py` |
| **CSRF** | Synchronizer token per session, required in `X-CSRF-Token` on every POST/PATCH/DELETE (constant-time comparison). `SameSite=Lax` cookies and `selfRequestsOnly` HTMX add depth. FastAPI's strict content-type checking blocks JSON-without-content-type tricks. | `app/security.py::verify_csrf` |
| **OAuth login CSRF / code injection / replay** | 256-bit random `state`, bound to the session, single-use, 10-minute TTL, constant-time compare. PKCE (S256): the `code_verifier` is an HMAC of the state under the server secret, so it is never stored or sent to the browser. Codes are exchanged server-side with the client secret; never retried. Refresh tokens are single-use, renewed once per session at a time and never retried. | `app/security.py`, `app/routes/buffer.py`, `app/accounts.py` |
| **IDOR (accessing someone else's draft)** | Every query filters by `session_id`; draft ids are 128-bit random; ids are validated by regex. | `app/repositories.py` |
| **Malicious uploads** (polyglots, scripts, wrong types) | Allow-listed declared MIME types; magic-byte sniffing must agree; Pillow full decode; images are **re-encoded** (destroys appended payloads, strips metadata); storage keys generated server-side; files served with `nosniff`. | `services/images.py`, `services/storage.py` |
| **Decompression bombs / huge files** | Per-file byte limit enforced while streaming, explicit pixel budget checked before decoding, Pillow's bomb warning escalated to an error, global request body limit (413). | `app/validation.py`, `services/images.py`, `app/middleware.py` |
| **Path traversal** | Original filenames are only displayed (sanitised, HTML-escaped); storage keys must match `^[0-9a-f]{32}\.(jpg|png|webp)$`. | `services/storage.py` |
| **Privacy leaks via EXIF/GPS** | All metadata removed on re-encode; orientation applied first. | `services/images.py` |
| **Images shared with the vision provider** | With `VISION_BACKEND=ollama` (default) a <=512 px JPEG preview of each upload, already stripped of metadata, is sent to `VISION_ENDPOINT` (Ollama Cloud by default) over HTTPS with the API key server-side; the model's reply is treated as data (JSON-parsed, normalised, escaped, and JSON-quoted when reused in the copy prompt). Set `VISION_BACKEND=disabled` to keep images on the server. | `services/vision.py`, `services/prompts.py` |
| **XSS** | Jinja2 autoescaping; toasts use `textContent`; JSON in attributes via `tojson`; strict **CSP** (`script-src 'self'`, `style-src 'self'`, no inline code, `object-src 'none'`, `frame-ancestors 'none'`); HTMX `allowEval=false`, `allowScriptTags=false`, `attributesToSettle` excludes `style`. | `templates/`, `static/js/app.js`, `app/middleware.py`, `app/templating.py` |
| **Clickjacking** | `X-Frame-Options: DENY` and `frame-ancestors 'none'`. | `app/middleware.py` |
| **Brute force / abuse / cost blow-up** | Token-bucket rate limits per IP for upload, generate, schedule, auth and other writes; bounded files per request, drafts per session; `OLLAMA_MAX_TOKENS` caps AI spend; vision concurrency limited by a semaphore. | `app/rate_limit.py`, `app/config.py` |
| **Denial of service through image analysis** | Vision requests bounded per worker by a semaphore with a per-image time budget; inference happens at the provider, not on the server's CPU. | `services/vision.py` |
| **Duplicate posts** | Posting is only retried when the connection failed before sending; re-sending an already published draft asks for confirmation. | `services/buffer.py`, `templates/_card.html` |
| **Host header attacks** | `ALLOWED_HOSTS` (TrustedHostMiddleware); public URLs come from `PUBLIC_BASE_URL` in production. | `app/main.py`, `app/views.py` |
| **Information leakage in errors** | Generic messages with a request id; stack traces only in logs; `/docs` disabled in production. | `app/error_handlers.py` |
| **Stale data** | Drafts/images purged after `DRAFT_RETENTION_HOURS`; tokens of expired sessions purged. | `app/container.py` |
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
* Rotate `SESSION_SECRET` to invalidate all sessions. Set a separate
  `TOKEN_ENCRYPTION_KEY` if rotating the session secret must not disconnect
  Buffer accounts; rotating `TOKEN_ENCRYPTION_KEY` discards stored tokens
  (users reconnect).
* Protect `/metrics` with `METRICS_TOKEN` or keep it off the public network.

## Known limitations

* There are no user accounts: a "user" is a browser session. Anyone who can
  read the session cookie (e.g. on a shared computer) can use that session.
  Put the app behind your SSO/reverse-proxy authentication for multi-user
  deployments, or add accounts (see ARCHITECTURE.md, Extensibility).
* Uploaded images are public by URL (unguessable 128-bit names) because Buffer
  must be able to fetch them.
* Rate limits are per worker process; use a shared limiter (proxy or Redis)
  for strict global limits.
* SQLite suits a single host. Multi-host deployments need a shared database.

## Reporting a vulnerability

Please report privately to the maintainers instead of opening a public issue.
Include steps to reproduce and the affected version (`GET /health`).
