# Architecture

Social Booster is a single FastAPI service that renders HTML with
Jinja2 and uses HTMX for in-page updates. There is no separate front-end build:
the browser loads one page, and every interaction (upload, AI generation,
autosave, scheduling) is a small HTTP request that returns an HTML fragment.

## 1. System context

```mermaid
flowchart LR
    user([Marketer's browser<br/>HTMX + Bootstrap 5])
    subgraph host[Application host / container]
        proxy[Reverse proxy<br/>Caddy / NGINX<br/>TLS, body limit]
        app[FastAPI app<br/>Gunicorn + Uvicorn workers]
        db[(SQLite WAL<br/>drafts)]
        files[(Upload storage<br/>/uploads volume)]
    end
    ollama[Ollama Cloud<br/>chat completions:<br/>vision model + copywriter]
    buffer[Buffer API<br/>GraphQL]
    prom[Prometheus]

    user -- HTTPS --> proxy --> app
    app --> db
    app --> files
    app -- "HTTPS, Bearer API key (image previews, prompts)" --> ollama
    app -- "HTTPS, Bearer BUFFER_ACCESS_TOKEN" --> buffer
    buffer -- fetches image --> proxy
    prom -- /metrics --> app
```

Secrets (Ollama key, Buffer personal API key) only ever exist on the server.
The browser holds a signed session cookie containing a random session id and a
CSRF token - nothing else. The Buffer key is server configuration, so every
visitor of the app posts to the same Buffer account; the app has no login of
its own (see [SECURITY.md](SECURITY.md)).

## 2. Code layout and layers

```mermaid
flowchart TB
    subgraph web[app/ - web layer]
        routes[routes/<br/>pages, drafts, buffer, ops]
        deps[dependencies.py<br/>DI providers]
        mw[middleware.py<br/>security headers, request id,<br/>metrics, body limit]
        sec[security.py<br/>session, CSRF, flash]
        err[error_handlers.py<br/>toast / JSON / HTML errors]
        views[views.py + templating.py<br/>view models, Jinja env]
    end
    subgraph appsvc[app/ - application services]
        drafts[drafts.py<br/>DraftService]
        accounts[accounts.py<br/>PublisherAccounts]
        val[validation.py]
        repo[repositories.py + db.py<br/>SQLite access]
        container[container.py<br/>composition root]
    end
    subgraph services[services/ - framework-agnostic adapters]
        vision[vision.py<br/>VisionService + OllamaImageDescriber]
        ollama[ollama.py + prompts.py<br/>OllamaClient, parser]
        buffer[buffer.py + publishing.py<br/>BufferClient implements SocialPublisher]
        storage[storage.py<br/>Storage protocol, LocalFileStorage]
        images[images.py<br/>ImageProcessor]
        content[content.py, retry.py, errors.py]
    end

    routes --> deps --> container
    routes --> drafts
    routes --> accounts
    routes --> views
    drafts --> repo
    drafts --> vision
    drafts --> ollama
    drafts --> storage
    drafts --> images
    accounts --> buffer
    container --> services
```

Dependency rule: `services/` never imports `app/`. Services receive plain
dataclass configs (`VisionConfig`, `OllamaConfig`, `BufferConfig`) and a shared
`httpx.AsyncClient`, so they can be reused from a CLI or worker and are unit
tested without FastAPI.

| Layer | Responsibility | Key types |
|---|---|---|
| Routes | Parse forms, call application services, pick a template | `APIRouter`s in `app/routes/` |
| Dependencies | Inject container, session id, CSRF check, rate limits, "publisher configured" guard | `app/dependencies.py`, `app/rate_limit.py` |
| Application services | Orchestrate the workflow, enforce business rules | `DraftService`, `PublisherAccounts` |
| Repositories | Persistence scoped by session id | `DraftRepository` |
| Service adapters | Talk to Ollama (vision + copy), Buffer, disk | `VisionService`, `OllamaClient`, `BufferClient`, `LocalFileStorage` |
| Composition root | Build everything from `Settings`, own lifecycle | `ServiceContainer`, `create_app()` |

## 3. Request pipeline

```mermaid
flowchart LR
    req[HTTP request] --> sh[SecurityHeaders]
    sh --> rc[RequestContext<br/>X-Request-ID, access log,<br/>metrics, 500 page]
    rc --> bl[BodySizeLimit<br/>413]
    bl --> th[TrustedHost<br/>optional]
    th --> sess[SessionMiddleware<br/>signed cookie]
    sess --> router[FastAPI router]
    router --> d1[Depends: rate_limit]
    d1 --> d2[Depends: verify_csrf]
    d2 --> d3[Depends: session id /<br/>publisher configured]
    d3 --> handler[Route handler]
    handler --> tpl[TemplateResponse<br/>+ HX-Trigger toast]
```

Exceptions are mapped centrally (`app/error_handlers.py`): HTMX requests get
an empty body with an `HX-Trigger` toast and `HX-Reswap: none` (the page keeps
its state), JSON clients get `{"detail", "request_id"}`, and browsers get the
`errors/error.html` page.

## 4. HTMX interaction model

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser (HTMX)
    participant A as FastAPI
    B->>A: GET /
    A-->>B: index.html (cards for existing drafts,<br/>CSRF token in hx-headers)
    B->>A: POST /upload (multipart, X-CSRF-Token)
    A-->>B: N x _card.html, HX-Trigger toast
    Note over B: hx-swap="afterbegin" into #drafts
    B->>A: POST /generate (draft_id, keywords, tone)
    A-->>B: _card.html with caption + hashtags
    Note over B: hx-swap="outerHTML" replaces #draft-ID
    B->>A: PATCH /drafts/ID (debounced 800 ms)
    A-->>B: _save_status.html ("Saved 12:01:03")
    B->>A: POST /buffer/schedule (profiles, mode, time, tz)
    A-->>B: _card.html with publish summary (or 422 + inline errors)
```

HTMX runtime configuration (rendered into `<meta name="htmx-config">`):

* `allowEval: false`, `allowScriptTags: false`, `includeIndicatorStyles: false`
  so the strict CSP (`script-src 'self'; style-src 'self'`) holds.
* `responseHandling` swaps **422** responses, so validation errors re-render
  the card inline; other 4xx/5xx are never swapped and surface as toasts.
* `selfRequestsOnly: true` - HTMX can only talk to this origin.

`static/js/app.js` adds only UI glue: toasts from `HX-Trigger`, drag & drop,
client-side pre-validation and previews (object URLs), upload progress,
character/hashtag counters, browser-timezone detection, local time display,
the image modal and the light/dark theme toggle.

## 5. Template architecture

```mermaid
flowchart TB
    base[base.html<br/>head, CSP-safe assets,<br/>hx-headers CSRF, toasts]
    index[index.html<br/>navbar, hero, upload, drafts grid,<br/>image modal, flash data]
    error[errors/error.html]
    card[_card.html<br/>one draft: image, labels, keywords,<br/>tone, generate, caption, hashtags,<br/>schedule panel, publish summary]
    cards[partials/_cards.html<br/>loop of cards - upload response]
    status[partials/_buffer_status.html<br/>not configured / profiles / retry]
    save[partials/_save_status.html]
    icons[partials/_icons.html<br/>inline SVG macro]

    base --> index
    base --> error
    index --> status
    index --> card
    cards --> card
    card --> icons
    index --> icons
    status --> icons
```

Fragments are rendered with `TemplateResponse` so a single context processor
(`app/templating.py`) supplies `csrf_token`, `static_url()`, `app_name` and the
HTMX config everywhere. `_card.html` is driven by a `CardView` view model
(`app/views.py`) whose form values fall back to the stored draft but are
overridden by submitted values after a validation error - nothing the user
typed is lost.

## 6. Image processing flow

```mermaid
flowchart TB
    up[UploadFile] --> lim{size <= MAX_UPLOAD_SIZE_MB?<br/>streamed check}
    lim -- no --> rej[reject: larger than limit]
    lim -- yes --> mime{declared MIME allowed?}
    mime -- no --> rej2[reject: unsupported type]
    mime -- yes --> magic{magic bytes = JPEG/PNG/WebP<br/>and match declared type?}
    magic -- no --> rej3[reject: content mismatch]
    magic -- yes --> pil[Pillow verify + pixel budget<br/>decompression-bomb guard]
    pil -- error --> rej4[reject: corrupted]
    pil --> clean[apply EXIF orientation,<br/>downscale to IMAGE_MAX_DIMENSION,<br/>re-encode, strip EXIF/XMP/GPS]
    clean --> store[Storage.save -> random key<br/>atomic write]
    store --> tag[VisionService.analyze<br/>online vision model,<br/>semaphore + timeout]
    tag -- VisionError --> degrade[draft without keywords<br/>user types them]
    tag --> labels[description + top-k keywords]
    labels --> draft[(Draft row)]
    degrade --> draft
```

Decoded images cost ~4 bytes per pixel, so each worker decodes at most
`IMAGE_MAX_CONCURRENCY` (default 1) images at a time across all requests; the
rest of a batch waits in the multipart spool files on disk. Images whose longer
side exceeds `IMAGE_MAX_DIMENSION` (default 2048 px) are downscaled before
storing; JPEGs are decoded straight at 1/2, 1/4 or 1/8 scale by libjpeg
(`Image.draft`), so a large photo never exists in memory at full size. EXIF
orientation is applied in place, and the vision backend gets a <=512 px
preview made while the pixels are decoded instead of decoding the stored file
again.

The preview is sent as a base64 JPEG to a vision model (`VISION_MODEL`,
default `gemma4:31b`, free on Ollama Cloud) through the same `OllamaClient` as
copywriting, sharing `OLLAMA_ENDPOINT`/`OLLAMA_API_KEY` unless
`VISION_ENDPOINT`/`VISION_API_KEY` are set. It returns JSON with a one-sentence
`description` and `keywords`. The description is shown on the card, used as alt
text and passed to the caption prompt as background. Nothing heavy runs in the
worker (~50 MB RSS). `VISION_BACKEND=disabled` skips the call and users type
the keywords.

A semaphore (`VISION_MAX_CONCURRENCY`) bounds analyses per worker and
`VISION_TIMEOUT_SECONDS` caps each one; failures leave the draft without
keywords and the user types them. Pillow work runs through `asyncio.to_thread`.

## 7. AI generation flow

```mermaid
sequenceDiagram
    autonumber
    participant R as POST /generate
    participant D as DraftService
    participant O as OllamaClient
    participant C as Ollama Cloud
    R->>D: generate(session, draft_id, keywords, tone)
    D->>D: normalize_keywords (empty -> 422 inline error)
    D->>O: generate_copy(CopyRequest)
    O->>O: build_copy_messages (system + JSON-only user prompt)
    loop retry policy (timeouts, 429, 5xx; honours Retry-After)
        O->>C: POST /v1/chat/completions (Bearer key)
        C-->>O: choices[0].message.content
    end
    O->>O: parse_copy: JSON -> "CAPTION:/HASHTAGS:" -> heuristic
    alt unparseable
        O->>C: repair turn ("reply with ONLY the JSON object")
        C-->>O: corrected answer
    end
    O->>O: finalize: caption <= CAPTION_MAX_CHARS,<br/>HASHTAGS_MIN..MAX (top up from keywords)
    O-->>D: GeneratedCopy
    D->>D: save draft (status = generated)
    D-->>R: Draft -> _card.html
```

Failure handling: 401/403 -> "check OLLAMA_API_KEY" (no retry); 404 -> model
not found; exhausted retries -> 503 "AI service busy"; reasoning models that
spend all tokens -> explicit "increase OLLAMA_MAX_TOKENS". The card is never
replaced on failure (`HX-Reswap: none`), so edits are preserved.

## 8. Buffer authentication and profiles

The app authenticates to Buffer with one personal API key created in Buffer
(*Settings -> API -> Create API key*) and configured as `BUFFER_ACCESS_TOKEN`.
The key acts on behalf of that one Buffer account, reaches all of its
organizations and channels, has no scopes and does not expire until it is
revoked - so there is no consent screen, code exchange or token refresh.

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant A as App
    participant BF as Buffer
    B->>A: GET /
    alt BUFFER_ACCESS_TOKEN not set
        A-->>B: navbar badge "Buffer not configured"
    else configured
        A->>BF: POST api.buffer.com (Bearer BUFFER_ACCESS_TOKEN, GraphQL)<br/>account.organizations, then channels - cached 5 min
        BF-->>A: channels
        A-->>B: page with profiles in navbar and schedule forms
    end
    B->>A: GET /buffer/refresh
    A->>BF: same queries, bypassing the cache
    A-->>B: 303 -> / + flash (profile count or error)
```

`PublisherAccounts` (`app/accounts.py`) wraps the `SocialPublisher`
implementation (`BufferClient`) and keeps a single, server-wide profile cache
(`BUFFER_PROFILES_CACHE_SECONDS`); every session sees the same account and
profiles. Its `status()` never raises: it reports *not configured*,
*connected* (with profiles, or with an outage message) or *rejected*.

If Buffer rejects the key (HTTP 401/403 or a GraphQL `UNAUTHORIZED` /
`UNAUTHENTICATED` error) the cache is dropped and the UI shows "Buffer rejected
the configured access token. Check BUFFER_ACCESS_TOKEN (Buffer -> Settings ->
API)." with a "Buffer unavailable · Retry" button (navbar) and a "Retry Buffer"
button (schedule panel), both linking to `/buffer/refresh`. Nothing is deleted;
the error persists until the key is fixed on the server.

## 9. Scheduling flow

```mermaid
sequenceDiagram
    autonumber
    participant R as POST /buffer/schedule
    participant V as validate_schedule
    participant P as PublisherAccounts
    participant BF as Buffer
    R->>P: publisher configured? (503 toast if BUFFER_ACCESS_TOKEN unset)
    R->>P: profiles() (cached, shared by all sessions)
    R->>V: caption, hashtags, profile_ids, mode, scheduled_for, timezone
    alt invalid
        V-->>R: field errors
        R-->>R: 422 + card with inline errors and user input
    else valid
        R->>R: save edited copy, build absolute media URL (PUBLIC_BASE_URL)
        R->>P: publish(PostRequest)
        loop each selected channel
            P->>BF: GraphQL createPost(channelId, text, assets.image.url,<br/>mode customScheduled + dueAt (UTC) | shareNow | addToQueue)
            BF-->>P: PostActionSuccess { post { id } } | MutationError { message }
        end
        R->>R: record PublishRecord, status = published
        R-->>R: card with publish summary + success toast
    end
```

Only connection failures that happened *before* the request was sent are
retried when posting, so a retry can never create a duplicate post. Channels
are independent: if Buffer refuses some of them, the others are still sent and
the toast names the refused ones; only when every channel fails is it an error.
A rejected key aborts the request with a `502` toast and clears the profile
cache.

## 10. Security model

```mermaid
flowchart LR
    subgraph browser[Browser - untrusted]
        cookie[signed session cookie<br/>sid + CSRF token + flash only<br/>HttpOnly, SameSite=Lax, Secure in prod]
    end
    subgraph server[Server - trusted]
        csrf[CSRF check on every<br/>POST/PATCH/DELETE]
        secrets[Env / Docker secrets<br/>SecretStr: OLLAMA_API_KEY,<br/>BUFFER_ACCESS_TOKEN]
        publish[POST /buffer/schedule]
        uploads[Upload pipeline<br/>validate + re-encode]
        rl[Rate limiter]
        csp[CSP + security headers]
    end
    cookie --> csrf
    csrf --> publish
    secrets --> publish
```

The app has no user accounts: anyone who can reach it can publish with the
configured Buffer key, so public deployments belong behind access control at
the proxy (basic auth, SSO, IP allow-list).

See [SECURITY.md](SECURITY.md) for the full threat model and controls.

## 11. Persistence

| Table | Key | Content |
|---|---|---|
| `drafts` | `id`, indexed by `(session_id, created_at)` | Draft JSON document (labels, keywords, caption, hashtags, publish record) |

A background task (hourly) deletes drafts untouched for
`DRAFT_RETENTION_HOURS` together with their images. The `oauth_tokens` table
used by older versions (per-session Buffer OAuth tokens) is dropped on startup
so their refresh tokens do not linger.

## 12. Extensibility

| Extension | How |
|---|---|
| Another vision model or provider | Set `VISION_MODEL` / `VISION_ENDPOINT` / `VISION_API_KEY` (any OpenAI-compatible endpoint that accepts images), or implement the `ImageDescriber` protocol and pass it to `VisionService` |
| Another LLM provider (OpenAI, Gemini, self-hosted Ollama) | Point `OLLAMA_ENDPOINT` at any OpenAI-compatible endpoint, or Ollama's native `/api/chat` |
| Another publishing provider | Implement `SocialPublisher` (`services/publishing.py`) and wire it in `app/container.py` |
| Object storage (S3, GCS) | Implement the `Storage` protocol; return public URLs from `public_path()` |
| Multi-host deployment | Swap SQLite for Postgres in the repositories and the in-process rate limiter for Redis; the interfaces stay the same |

## 13. Key design decisions

1. **Server-side drafts.** Uploaded images and generated copy are persisted per
   session, so a reload or a Buffer outage never loses work and
   every request references a `draft_id` instead of trusting client-supplied
   image URLs or labels.
2. **One server-wide Buffer key.** The original design kept a per-visitor Buffer
   token in a signed cookie (signed is not encrypted); a later version used
   per-session OAuth tokens encrypted at rest. Buffer now authenticates with a
   single personal API key held in server configuration, so no Buffer
   credential is ever stored per session or sent to the browser, and the cookie
   carries only an opaque session id. The trade-off: every visitor shares that
   Buffer account, so access to the app must be controlled in front of it.
3. **Vendored front-end assets.** Bootstrap and HTMX are served from `/static`
   so the CSP can forbid every third-party script and the app works offline.
4. **Graceful degradation.** Vision failures fall back to manual keywords, Buffer
   outages are reported in the UI, AI errors keep the card intact.
5. **No eval anywhere.** HTMX eval is disabled; all behaviour is declarative
   attributes plus one small static script.
