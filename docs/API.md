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
| Rate limits | Per client IP and worker; `429` with `Retry-After` when exceeded. Scopes: `upload`, `generate`, `schedule`, `default`. |
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
| Limits | `MAX_FILES_PER_UPLOAD` files, `MAX_UPLOAD_SIZE_MB` each, `MAX_IMAGE_PIXELS`, `MAX_DRAFTS_PER_SESSION`; images larger than `IMAGE_MAX_DIMENSION` px are downscaled |
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
| `keywords` | string | comma-separated visual concepts (pre-filled from the vision model, editable); the stored image description is added to the prompt as background |
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

The app authenticates to Buffer with one server-wide personal API key
(`BUFFER_ACCESS_TOKEN`), so there is no per-visitor connect step: every
session sees the same Buffer account and shares one profile cache
(`BUFFER_PROFILES_CACHE_SECONDS`). Buffer counts as configured when that
variable is set.

### `GET /buffer/refresh`

Reloads the profiles from Buffer (bypassing the cache), then `303 /` with a
flash message: `info` with the number of profiles found, or `danger` with the
error (for example when Buffer rejects the configured key). Also the target of
the "Retry" buttons shown while Buffer is unavailable. Because it bypasses the
shared cache and spends the Buffer account's API quota, it is rate-limited by
`RATE_LIMIT_DEFAULT` (`429` when exceeded).

### `POST /buffer/schedule`

Publish a draft through Buffer.

| Field | Type | Notes |
|---|---|---|
| `draft_id` | 32 hex chars | required |
| `caption` | string | required, <= 2,200 chars; saved to the draft |
| `hashtags` | string | normalised like the autosave endpoint |
| `profile_ids` | repeated | at least one; must belong to the configured Buffer account |
| `mode` | `schedule` \| `queue` \| `now` | default `schedule` |
| `scheduled_for` | `YYYY-MM-DDTHH:MM` | required for `schedule`; >= 1 minute ahead, <= 365 days |
| `timezone` | IANA name | browser timezone (filled by the page); default `UTC` |

What is sent to Buffer (`BUFFER_API_URL`, GraphQL, `Authorization: Bearer <BUFFER_ACCESS_TOKEN>`), one
`createPost` mutation per selected profile (Buffer calls them channels):

```jsonc
{
  "channelId": "<id>",
  "text": "<caption>\n\n<#tag #tag>",
  "schedulingType": "automatic",
  "mode": "customScheduled",          // schedule; "addToQueue" for queue, "shareNow" for now
  "dueAt": "2026-11-12T15:00:00Z",    // schedule only
  "assets": [{"image": {"url": "<PUBLIC_BASE_URL>/uploads/<key>"}}]
}
```

| Status | Meaning |
|---|---|
| 200 | card with publish summary; toast `success` (or `warning` when the image URL is not publicly reachable, or Buffer refused some of the profiles) |
| 404 | draft not found in this session |
| 422 | card re-rendered with field errors |
| 502 | Buffer refused the post for every profile (its message is shown), or rejected the configured key (HTTP 401/403 or GraphQL `UNAUTHORIZED`/`UNAUTHENTICATED`; toast "Buffer rejected the configured access token. Check BUFFER_ACCESS_TOKEN (Buffer -> Settings -> API).") |
| 503 | `BUFFER_ACCESS_TOKEN` not set (toast "Buffer is not configured on this server."), or Buffer unreachable / 5xx |

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
| `mab_rate_limited_total` | `scope` |

### Static files

* `GET /static/...` - CSS, JS, vendored Bootstrap 5.3.8 and HTMX 2.0.11 (gzip).
* `GET /uploads/<32-hex>.<jpg|png|webp>` - sanitised uploaded images
  (`Cross-Origin-Resource-Policy: cross-origin` so Buffer can fetch them).
