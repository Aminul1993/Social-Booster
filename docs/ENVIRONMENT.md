# Environment variables

Configuration is loaded by `app/config.py` (Pydantic Settings). Precedence,
highest first:

1. Process environment variables
2. `.env` in the project root (see `.env.example`)
3. Secret files in `/run/secrets/<lower_case_name>` (Docker / Kubernetes secrets)
4. Defaults below

Names are case-insensitive. An **empty** value (`VAR=`) means "use the
default". Secrets are `SecretStr`: they never appear in `repr()`, logs or error
pages. Invalid values stop the application at start-up with a clear message.

## Application

| Variable | Default | Description |
|---|---|---|
| `APP_NAME` | `AI Marketing Post Builder` | Shown in the UI and OpenAPI title. |
| `ENVIRONMENT` | `development` | `development`, `production` or `test`. Production requires `SESSION_SECRET`, enables secure cookies + HSTS and hides API docs. |
| `DEBUG` | `false` | FastAPI debug mode. Never enable in production. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `LOG_FORMAT` | `json` | `json` (one object per line) or `console`. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for `python -m app`. Docker uses `0.0.0.0:8000`. |
| `PUBLIC_BASE_URL` | *(request URL)* | Public origin, e.g. `https://social.example.com`. **Required in production**: Buffer downloads images from `PUBLIC_BASE_URL/uploads/...`. Include any sub-path prefix. |
| `ALLOWED_HOSTS` | `*` | Comma-separated `Host` headers to accept (`localhost`/`127.0.0.1` are always allowed for health checks). |
| `ENABLE_API_DOCS` | on outside production | Serve `/docs`, `/redoc`, `/openapi.json`. |
| `MAX_REQUEST_BODY_MB` | files x size + 1 MB | Hard cap on any request body (413 above it). |

## Security

| Variable | Default | Description |
|---|---|---|
| `SESSION_SECRET` | random per start (dev only) | Signs the session cookie and derives the token-encryption key. **>= 32 random characters, required in production.** `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SESSION_COOKIE_NAME` | `mab_session` | Cookie name. |
| `SESSION_MAX_AGE_SECONDS` | `1209600` (14 days) | Session lifetime; stored tokens older than this are purged. |
| `COOKIE_SECURE` | `true` in production | `Secure` flag on cookies (HTTPS only). Also enables HSTS. |
| `TOKEN_ENCRYPTION_KEY` | derived from `SESSION_SECRET` (HKDF) | Optional Fernet key for OAuth tokens at rest, so `SESSION_SECRET` can rotate without disconnecting users. `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

## Persistence

| Variable | Default | Description |
|---|---|---|
| `DATABASE_PATH` | `data/app.db` | SQLite file (relative paths are relative to the project root). |
| `UPLOAD_DIR` | `uploads` | Directory for sanitised images, served at `/uploads`. |
| `DRAFT_RETENTION_HOURS` | `72` | Drafts untouched this long are deleted with their images (hourly job). `0` disables. |
| `MAX_DRAFTS_PER_SESSION` | `100` | Upper bound per browser session. |

## Uploads

| Variable | Default | Description |
|---|---|---|
| `MAX_UPLOAD_SIZE_MB` | `10` | Per-file size limit (spec: 10 MB). |
| `MAX_FILES_PER_UPLOAD` | `10` | Files per request. |
| `MAX_IMAGE_PIXELS` | `40000000` | Pixel budget of the *original* image (decompression-bomb protection). Also caps upload memory for PNG/WebP: ~4 bytes per pixel while an image is processed (WebP ~16 while decoding). |
| `IMAGE_QUALITY` | `90` | JPEG/WebP re-encode quality (50-100). |
| `IMAGE_MAX_DIMENSION` | `2048` | Larger images are downscaled (aspect ratio kept) so their longer side is at most this many pixels before they are stored and published. JPEGs are decoded at reduced scale, so big photos never sit in memory at full size. `0` keeps the original size. |
| `IMAGE_MAX_CONCURRENCY` | `1` | Images decoded and re-encoded at once per worker, across all requests; the rest of a batch waits on disk. Raise for throughput only if memory allows. |

## Vision (image description)

| Variable | Default | Description |
|---|---|---|
| `VISION_BACKEND` | `ollama` | `ollama`: an online vision model describes each image (sentence + keywords). `disabled`: users type keywords and nothing is sent to the vision model. |
| `VISION_MODEL` | `gemma4:31b` | Vision-capable model (`ollama` backend). On Ollama Cloud's free plan this is the vision model that works today; others return HTTP 402 "not in the Free plan". |
| `VISION_ENDPOINT` | `OLLAMA_ENDPOINT` | Chat endpoint for the vision model: Ollama (`/api/chat`) or any OpenAI-compatible `/chat/completions` that accepts `image_url` parts. |
| `VISION_API_KEY` | `OLLAMA_API_KEY` | Bearer key for `VISION_ENDPOINT`. |
| `VISION_TOP_K` | `5` | Keywords/labels per image. |
| `VISION_MAX_CONCURRENCY` | `2` | Concurrent vision requests per worker (semaphore). |
| `VISION_TIMEOUT_SECONDS` | `30` | Total time budget per image, retries included. |

The ResNet-50 settings (`VISION_WEIGHTS`, `VISION_MIN_CONFIDENCE`,
`VISION_NUM_THREADS`, `VISION_WARMUP`, `TORCH_HOME`) no longer exist and are
ignored; `VISION_BACKEND=resnet50` is rejected at start-up.

## Ollama Cloud

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_API_KEY` | - | Ollama Cloud API key (sent as `Authorization: Bearer`). Not needed for self-hosted Ollama. |
| `OLLAMA_ENDPOINT` | `https://ollama.com/v1/chat/completions` | OpenAI-compatible chat endpoint; a URL ending in `/api/chat` switches to Ollama's native API (e.g. `http://localhost:11434/api/chat`). |
| `OLLAMA_MODEL` | `gpt-oss:120b` | Any model your account can use (e.g. `llama3.2:latest` on self-hosted Ollama). |
| `OLLAMA_TEMPERATURE` | `0.7` | 0-2. |
| `OLLAMA_MAX_TOKENS` | `512` | Raise it for reasoning models that think before answering. |
| `OLLAMA_TIMEOUT_SECONDS` | `30` | Per attempt. |
| `OLLAMA_MAX_RETRIES` | `3` | Total attempts for timeouts, connection errors, 429 and 5xx. |
| `CAPTION_MAX_CHARS` | `150` | Generated caption length (spec: <= 150). Users may edit up to 2,200. |
| `HASHTAGS_MIN` / `HASHTAGS_MAX` | `5` / `8` | Generated hashtag count (spec: 5-8). |

## Buffer

| Variable | Default | Description |
|---|---|---|
| `BUFFER_CLIENT_ID` | - | OAuth client id (Buffer -> Settings -> API, private client). |
| `BUFFER_CLIENT_SECRET` | - | OAuth client secret. |
| `BUFFER_REDIRECT_URI` | `http://localhost:8000/buffer/callback` | Must exactly match a redirect URI registered with Buffer, which requires `https` (also on localhost). |
| `BUFFER_OAUTH_URL` | `https://auth.buffer.com/auth` | Consent screen (authorization code + PKCE). |
| `BUFFER_TOKEN_URL` | `https://auth.buffer.com/token` | Code -> token exchange and token refresh. |
| `BUFFER_API_URL` | `https://api.buffer.com` | GraphQL API: organizations, channels, `createPost`. |
| `BUFFER_SCOPE` | `account:read posts:write offline_access` | Scopes requested on the consent screen; `offline_access` gives the refresh token that renews the one-hour access tokens. |
| `BUFFER_TIMEOUT_SECONDS` | `20` | Per request. |
| `BUFFER_MAX_RETRIES` | `3` | Attempts for channel listing; posting only retries connection failures; token requests are never retried. |
| `BUFFER_PROFILES_CACHE_SECONDS` | `300` | Channel cache per session (`0` disables). |

The legacy v1 URLs (`bufferapp.com/oauth2/authorize`, `api.bufferapp.com/1/...`)
reject clients created under Buffer's Settings -> API with `invalid_client`;
the app logs a warning at start-up if any `BUFFER_*_URL` still points there.
`BUFFER_PROFILES_URL` and `BUFFER_POST_URL` no longer exist and are ignored.

## Rate limiting

Format `<count>/<second|minute|hour|day>`, per client IP and per worker
process.

| Variable | Default | Applies to |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | All limits. |
| `RATE_LIMIT_UPLOAD` | `20/minute` | `POST /upload` |
| `RATE_LIMIT_GENERATE` | `30/minute` | `POST /generate` |
| `RATE_LIMIT_SCHEDULE` | `20/minute` | `POST /buffer/schedule` |
| `RATE_LIMIT_AUTH` | `10/minute` | `/buffer/auth`, `/buffer/callback`, `/buffer/disconnect` |
| `RATE_LIMIT_DEFAULT` | `120/minute` | Autosave and delete |

## Observability

| Variable | Default | Description |
|---|---|---|
| `METRICS_ENABLED` | `true` | Serve `/metrics`. |
| `METRICS_TOKEN` | - | If set, `/metrics` requires `Authorization: Bearer <token>`. |
| `PROMETHEUS_MULTIPROC_DIR` | `/tmp/prometheus` in Docker | Aggregates metrics across Gunicorn workers. Must be an empty, dedicated directory. |

## Process manager (Gunicorn, `gunicorn.conf.py`)

| Variable | Default | Description |
|---|---|---|
| `WEB_CONCURRENCY` | `2` | Worker processes (~50 MB RSS each). |
| `BIND` | `0.0.0.0:$PORT` | Listen address. |
| `GUNICORN_TIMEOUT` | `120` | Worker timeout. |
| `GUNICORN_GRACEFUL_TIMEOUT` | `30` | Graceful shutdown. |
| `GUNICORN_KEEPALIVE` | `5` | Keep-alive seconds. |
| `GUNICORN_MAX_REQUESTS` / `_JITTER` | `0` | Recycle workers after N requests (0 = never). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted (client IP for rate limiting, scheme for URLs). |
