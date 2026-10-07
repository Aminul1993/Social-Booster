# HTTP API

The UI endpoints are designed for HTMX: they accept form-encoded or multipart
bodies and return **HTML fragments**. Interactive OpenAPI docs are available at
`/docs` and `/redoc` outside production (`ENABLE_API_DOCS=true` forces them on).

## Conventions

| Topic | Rule |
|---|---|
| Session | First `GET /` sets the signed `mab_session` cookie (session id + CSRF token). |
| CSRF | Every `POST`, `PATCH`, `DELETE` must send `X-CSRF-Token: <token>`. The page renders it into `<body hx-headers='{"X-CSRF-Token": "..."}'>`. Missing/invalid -> `403`. |
| HTMX requests | Send `HX-Request: true`. Errors then come back as an empty body with `HX-Trigger: {"app:toast": {"level", "message"}}` and `HX-Reswap: none`. |
| JSON clients | `Accept: application/json` turns error responses into `{"detail": "...", "request_id": "..."}`. |
| Request id | Every response carries `X-Request-ID` (an incoming `X-Request-ID` of 8-64 safe characters is reused). |
| Rate limits | Per client IP and worker; `429` with `Retry-After` when exceeded. Scopes: `upload`, `generate`, `schedule`, `auth`, `default`. |
| Body limit | Bodies above `MAX_REQUEST_BODY_MB` (default: one full upload batch) -> `413`. |
| Toasts | Successful actions also set `HX-Trigger` with a `success`/`warning`/`info` toast. |

Validation errors for the **card forms** (`/generate`, `/buffer/schedule`) are
returned as `422` *with* a re-rendered card containing inline messages; HTMX is
configured to swap `422` responses.

---

## Pages

### `GET /`

Full page: navbar with Buffer status, upload form, and one card per draft of
the current session (newest first). Sets the session cookie on first visit.
`Cache-Control: no-store`.

---

## Drafts

### `POST /upload`

Upload one or more images.

| | |
|---|---|
| Body | `multipart/form-data`, repeated field `files` |
| Limits | `MAX_FILES_PER_UPLOAD` files, `MAX_UPLOAD_SIZE_MB` each, `MAX_IMAGE_PIXELS`, `MAX_DRAFTS_PER_SESSION` |
| Accepted | JPEG, PNG, WebP (declared MIME **and** magic bytes must agree) |
| Rate limit | `RATE_LIMIT_UPLOAD` |

Responses

| Status | Body | Headers |
|---|---|---|
| 200 | one `_card.html` per accepted file | toast `success` ("Uploaded 2 images...") or `warning` listing skipped files with reasons |
| 400 | empty | toast: nothing valid uploaded / too many files / draft limit reached |
| 413 | empty | toast: upload too large |
| 422 | empty | toast: `files` field missing |

```bash
curl -b jar -c jar http://localhost:8000/ -o page.html
TOKEN=$(grep -o '"X-CSRF-Token": "[^"]*"' page.html | cut -d'"' -f4)
curl -b jar -H "X-CSRF-Token: $TOKEN" -H "HX-Request: true" \
     -F "files=@dog.jpg;type=image/jpeg" -F "files=@beach.png;type=image/png" \
     http://localhost:8000/upload
```

### `POST /generate`

Write a caption and hashtags with Ollama Cloud.

| Field | Type | Notes |
|---|---|---|
| `draft_id` | 32 hex chars | required |
| `keywords` | string | comma-separated visual concepts (pre-filled from ResNet-50 labels, editable) |
| `tone` | `friendly` \| `professional` \| `playful` \| `inspirational` \| `luxury` \| `bold` | default `friendly` |

| Status | Meaning |
|---|---|
| 200 | card with caption (<= `CAPTION_MAX_CHARS`) and `HASHTAGS_MIN`-`HASHTAGS_MAX` hashtags |
| 404 | draft does not exist in this session |
| 422 | no usable keywords (card with inline error) / malformed `draft_id` (toast) |
| 502 | AI rejected credentials or returned an unusable answer |
| 503 | AI not configured, or still failing after retries |

### `PATCH /drafts/{draft_id}`

Autosave of the editable copy (the card triggers it 800 ms after typing stops).

| Field | Notes |
|---|---|
| `caption` | max 2,200 characters (Instagram limit); whitespace normalised |
| `hashtags` | free text, spaces/commas; `#` added, duplicates removed, max 30 |

Returns `_save_status.html` (`200` "Saved hh:mm:ss UTC", or `422` "Not saved: ...").

### `DELETE /drafts/{draft_id}`

Deletes the draft and its stored image. `200` with an empty body (HTMX removes
the card) and an `info` toast; `404` if the draft is unknown.

---

## Buffer

### `GET /buffer/auth`

Starts OAuth 2. Stores a single-use `state` (10-minute TTL) in the signed
session and answers `303` to `BUFFER_OAUTH_URL?client_id&redirect_uri&response_type=code&state[&scope]`.
If Buffer is not configured: `303 /` with an error flash.

### `GET /buffer/callback`

| Query | Notes |
|---|---|
| `state` | must match the stored state (constant-time compare, single use) |
| `code` | authorization code (exchanged server-side, form-encoded) |
| `error`, `error_description` | set by Buffer when the user cancels |

Always answers `303 /` with a flash message (`success`, `warning` or
`danger`). On success the token is encrypted and stored for the session.

### `GET /buffer/refresh`

Reloads the connected profiles from Buffer (bypassing the cache), `303 /`.

### `POST /buffer/disconnect`

Deletes the stored token. `200` with `HX-Refresh: true`.

### `POST /buffer/schedule`

Publish a draft through Buffer.

| Field | Type | Notes |
|---|---|---|
| `draft_id` | 32 hex chars | required |
| `caption` | string | required, <= 2,200 chars; saved to the draft |
| `hashtags` | string | normalised like the autosave endpoint |
| `profile_ids` | repeated | at least one; must belong to the connected account |
| `mode` | `schedule` \| `queue` \| `now` | default `schedule` |
| `scheduled_for` | `YYYY-MM-DDTHH:MM` | required for `schedule`; >= 1 minute ahead, <= 365 days |
| `timezone` | IANA name | browser timezone (filled by the page); default `UTC` |

What is sent to Buffer (`BUFFER_POST_URL`, form-encoded, `Authorization: Bearer`):

```
profile_ids[]=<id>&profile_ids[]=<id>
text=<caption>\n\n<#tag #tag>
media[photo]=<PUBLIC_BASE_URL>/uploads/<key>
media[thumbnail]=<PUBLIC_BASE_URL>/uploads/<key>
scheduled_at=2026-11-12T15:00:00Z      (mode=schedule)
now=true                               (mode=now)
```

| Status | Meaning |
|---|---|
| 200 | card with publish summary; toast `success` (or `warning` when the image URL is not publicly reachable) |
| 401 | Buffer not connected/configured (toast), or token revoked (`HX-Refresh` + flash) |
| 404 | draft not found in this session |
| 422 | card re-rendered with field errors |
| 502 | Buffer refused the post (its message is shown) |
| 503 | Buffer unreachable / 5xx |

---

## Operations

### `GET /health`

Liveness. `200 {"status": "ok", "version": "1.0.0", "environment": "production", "uptime_seconds": 12.3}`.

### `GET /health/ready`

Readiness. Checks the database and upload storage (required) and reports the
vision model and integrations.

```json
{
  "status": "ok",
  "checks": {
    "database": {"status": "ok", "detail": null},
    "storage":  {"status": "ok", "detail": null},
    "vision":   {"status": "ok", "detail": null},
    "ollama":   {"status": "configured", "detail": null},
    "buffer":   {"status": "configured", "detail": null}
  }
}
```

`status` is `ok`, `degraded` (vision model failed to load - the app still works
with manual keywords) or `unavailable` (HTTP `503`). Vision can also be
`loading` or `disabled`.

### `GET /metrics`

Prometheus exposition format. Requires `Authorization: Bearer $METRICS_TOKEN`
when that variable is set; `404` when `METRICS_ENABLED=false`.

| Metric | Labels |
|---|---|
| `mab_http_requests_total` | `method`, `route` (template), `status` |
| `mab_http_request_duration_seconds` | `method`, `route` |
| `mab_uploads_total` | `outcome` = accepted / rejected |
| `mab_vision_inference_seconds`, `mab_vision_failures_total`, `mab_vision_model_ready` | |
| `mab_ai_generations_total` | `outcome`; plus `mab_ai_generation_seconds` |
| `mab_publish_requests_total` | `provider`, `mode`, `outcome` |
| `mab_oauth_events_total` | `provider`, `outcome` (connected, denied, invalid_state, ...) |
| `mab_rate_limited_total` | `scope` |

### Static files

* `GET /static/...` - CSS, JS, vendored Bootstrap 5.3.8 and HTMX 2.0.11 (gzip).
* `GET /uploads/<32-hex>.<jpg|png|webp>` - sanitised uploaded images
  (`Cross-Origin-Resource-Policy: cross-origin` so Buffer can fetch them).
