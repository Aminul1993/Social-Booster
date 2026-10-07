# AI Marketing Post Builder

Turn photos into ready-to-post social content in three steps:

1. **Upload** one or more images - a **ResNet-50** model running on the server
   detects the top-5 visual concepts in each.
2. **Generate** a punchy caption and 5-8 hashtags with **Ollama Cloud**, then
   edit them freely (changes autosave).
3. **Schedule** the post with its image on one or more **Buffer** profiles -
   at a specific time, in the queue, or right now.

One FastAPI service renders Jinja2 pages; **HTMX** swaps small HTML fragments
for every interaction, styled with **Bootstrap 5**. No front-end build step.

![Post builder](docs/images/home-light.jpg)

| Copy editor and scheduling | Dark mode | Mobile |
|---|---|---|
| ![Card](docs/images/card-light.jpg) | ![Dark](docs/images/card-dark.jpg) | ![Mobile](docs/images/mobile.jpg) |

## Highlights

* **Server-side AI only** - API keys and OAuth tokens never reach the browser;
  Buffer tokens are encrypted at rest; the cookie holds just a signed session id.
* **Secure by default** - CSRF tokens, single-use OAuth `state`, strict CSP (no
  inline code, HTMX eval disabled), upload validation with magic-byte sniffing
  and re-encoding (EXIF/GPS stripped), rate limiting, body-size limits,
  security headers.
* **Resilient integrations** - retries with back-off and `Retry-After`,
  structured parsing of model output with a repair turn, graceful degradation
  when the vision model or Buffer is unavailable.
* **Drafts persist** per session, so the OAuth round trip to Buffer never loses
  work.
* **Production tooling** - Pydantic Settings, JSON logs with request ids,
  `/health`, `/health/ready`, Prometheus `/metrics`, multi-stage Docker image
  (non-root, read-only, weights baked in), Gunicorn, Caddy TLS profile, CI,
  pre-commit, Ruff, Black, strict mypy, 375 tests at 98.8 % coverage.

## Quick start

### Docker (recommended)

```bash
cd Social-Booster
cp .env.example .env
#   SESSION_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
#   OLLAMA_API_KEY=...   BUFFER_CLIENT_ID=...   BUFFER_CLIENT_SECRET=...
#   COOKIE_SECURE=false  (only while testing over plain http://localhost)
docker compose up -d --build
open http://localhost:8000
```

### Local Python

```bash
cd Social-Booster
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt                 # CPU-only PyTorch
cp .env.example .env                                # add your keys
python scripts/download_model.py                    # optional: pre-fetch ResNet-50
uvicorn app.main:app --reload --port 8000
```

### No accounts? Use the mocks

```bash
python scripts/mock_upstreams.py --port 8020
```

and point `OLLAMA_*`/`BUFFER_*` at it (see [docs/SETUP.md](docs/SETUP.md#running-without-ollamabuffer-accounts)).
The full flow - including the OAuth consent screen - works offline.

## How it works

```mermaid
sequenceDiagram
    participant U as Browser (HTMX)
    participant A as FastAPI
    participant V as ResNet-50
    participant O as Ollama Cloud
    participant B as Buffer
    U->>A: POST /upload (images)
    A->>V: classify (thread pool)
    A-->>U: cards with detected concepts
    U->>A: POST /generate (keywords, tone)
    A->>O: chat completion (JSON copy)
    A-->>U: card with caption + hashtags
    U->>A: GET /buffer/auth -> consent -> /buffer/callback
    A->>B: exchange code, list profiles
    U->>A: POST /buffer/schedule (profiles, time)
    A->>B: create update (text + image URL + scheduled_at)
    A-->>U: card with publish summary
```

Details and diagrams: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Configuration

All settings are environment variables (or `.env`, or `/run/secrets`). The
essentials:

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `production` enforces secure cookies, HSTS, hidden API docs, a strong `SESSION_SECRET` |
| `SESSION_SECRET` | signs session cookies, derives the token-encryption key |
| `PUBLIC_BASE_URL` | public origin; Buffer downloads images from it |
| `OLLAMA_API_KEY`, `OLLAMA_ENDPOINT`, `OLLAMA_MODEL` | AI copywriting |
| `BUFFER_CLIENT_ID`, `BUFFER_CLIENT_SECRET`, `BUFFER_REDIRECT_URI` | Buffer OAuth app |
| `VISION_BACKEND` | `resnet50` or `disabled` |

Full reference: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the post builder page |
| POST | `/upload` | upload images -> tagged draft cards |
| POST | `/generate` | AI caption + hashtags for a draft |
| PATCH | `/drafts/{id}` | autosave caption/hashtag edits |
| DELETE | `/drafts/{id}` | delete a draft and its image |
| GET | `/buffer/auth`, `/buffer/callback` | Buffer OAuth 2 |
| GET | `/buffer/refresh` | reload Buffer profiles |
| POST | `/buffer/disconnect` | forget the Buffer token |
| POST | `/buffer/schedule` | schedule / queue / share now |
| GET | `/health`, `/health/ready`, `/metrics` | operations |

Request/response details: [docs/API.md](docs/API.md); interactive docs at
`/docs` outside production.

## Development

```bash
pytest                                      # 375 tests, coverage gate 90 %
ruff check . && black --check .             # lint + format
mypy app services tests scripts gunicorn.conf.py   # strict typing
pre-commit run --all-files                  # from the repository root
```

## Documentation

| Guide | |
|---|---|
| [Setup](docs/SETUP.md) | local installation, Buffer app registration, mocks, tooling |
| [Deployment](docs/DEPLOYMENT.md) | Docker, Compose + Caddy TLS, Gunicorn, sizing, backups, CI/CD |
| [Architecture](docs/ARCHITECTURE.md) | layers, HTMX flow, templates, AI/vision/OAuth/scheduling flows |
| [API](docs/API.md) | endpoints, headers, status codes |
| [Environment](docs/ENVIRONMENT.md) | every configuration variable |
| [Security](docs/SECURITY.md) | threat model and controls |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | symptoms -> causes -> fixes |
| [Requirements](docs/REQUIREMENTS.md) | traceability matrix, gap analysis, coverage report |

## Project layout

```
app/        FastAPI app: routes, DI, security, middleware, workflow services, persistence
services/   framework-agnostic adapters: vision, ollama, buffer, storage, images
templates/  Jinja2 pages and HTMX fragments
static/     CSS, JS, vendored Bootstrap 5.3.8 + HTMX 2.0.11
tests/      unit/ and integration/ (upload, OAuth, generation, scheduling)
docs/       guides
scripts/    model pre-download, mock upstreams
deploy/     Caddyfile
```

## Credits

Screenshot photo: "Golden Retriever Dukedestiny01" by Janneke Vreugdenhil
(derivative by Anka Friedrich), Wikimedia Commons, public domain.

## License

MIT - see the repository's `LICENSE`.
