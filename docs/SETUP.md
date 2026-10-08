# Local setup

## Requirements

* Python **3.12+** (3.12 and 3.13 are tested in CI)
* ~250 MB disk for the dependencies
* Optional: Docker 24+ for the container workflow
* Credentials: an Ollama Cloud API key and a Buffer personal API key (or use
  the bundled mock services, see below)

## 1. Install

```bash
cd Social-Booster
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt  # runtime + test/lint tooling
# runtime only:  pip install -r requirements.txt
```

Images are described by an online vision model (`VISION_MODEL`, default
`gemma4:31b` on Ollama Cloud's free plan) using the same `OLLAMA_API_KEY` as
copywriting, so there is no model to download.

## 2. Configure

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # -> SESSION_SECRET
```

Edit `.env`:

| Variable | Value |
|---|---|
| `SESSION_SECRET` | the generated string (optional in development: a random one is generated per start, which logs you out on every restart) |
| `OLLAMA_API_KEY` | your Ollama Cloud key (https://ollama.com/settings/keys) |
| `OLLAMA_MODEL` | a model your account can use, e.g. `gpt-oss:120b` |
| `BUFFER_ACCESS_TOKEN` | your Buffer API key (see below) |
| `PUBLIC_BASE_URL` | leave empty locally; see "Images on Buffer" below |

All variables are documented in [ENVIRONMENT.md](ENVIRONMENT.md).

### Creating the Buffer API key

1. Sign in to Buffer and open **Settings -> API**
   (<https://publish.buffer.com/settings/api>).
2. Click **Create API key** and copy the key.
3. Put it in `.env` as `BUFFER_ACCESS_TOKEN=...`. Leave `BUFFER_API_URL`
   unset: the default is Buffer's current
   GraphQL endpoint; the old `bufferapp.com` v1 API does not accept these keys.
4. Restart the app. The navbar shows "Buffer connected · N profiles"; until the
   variable is set it shows "Buffer not configured".

The key acts on behalf of that one Buffer account, can reach all of its
organizations and channels, has no scopes and does not expire until you revoke
it in Buffer. There is no per-visitor "connect" step: **everyone who can open
the app posts with this key**, so do not expose the app publicly without access
control (see [SECURITY.md](SECURITY.md)).

### Images on Buffer

Buffer downloads the image from `PUBLIC_BASE_URL/uploads/<file>`. A localhost
URL is not reachable from Buffer's servers; the app still schedules the post
but warns you. For a real local test, expose the app with a tunnel
(`cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`) and set
`PUBLIC_BASE_URL` to the tunnel's https URL. Anyone with the tunnel URL can
post with your Buffer key, so keep it private and close it after testing.

## 3. Run

```bash
uvicorn app.main:app --reload --port 8000
# or: python -m app
```

Open http://localhost:8000.

### Running without Ollama/Buffer accounts

`scripts/mock_upstreams.py` imitates Ollama's chat endpoint and Buffer's
GraphQL endpoint so you can try the complete flow offline (the mock answers
`401` to requests without a `Bearer` header, so `BUFFER_ACCESS_TOKEN` must be
set to any value):

```bash
python scripts/mock_upstreams.py --port 8020
```

```dotenv
OLLAMA_ENDPOINT=http://127.0.0.1:8020/v1/chat/completions
OLLAMA_API_KEY=mock
BUFFER_ACCESS_TOKEN=mock
BUFFER_API_URL=http://127.0.0.1:8020/graphql
```

Posts "sent" to the mock are listed at http://127.0.0.1:8020/posts.

### Without image description

Set `VISION_BACKEND=disabled` to skip image analysis entirely (users type
keywords for each image); nothing is sent to the vision provider.

## 4. Quality tooling

```bash
pytest                      # unit + integration tests, coverage gate 90 %
pytest -m "not integration"   # unit tests only (faster)
ruff check .                # lint (incl. bandit security rules)
black --check .             # formatting
mypy app services tests scripts gunicorn.conf.py   # strict type checking
```

Pre-commit hooks (the config lives at the repository root):

```bash
cd ..                       # repository root
pre-commit install
pre-commit run --all-files
```

The `mypy` hook runs `python -m mypy` inside `Social-Booster/`, so
activate the virtual environment before committing.

## 5. Project layout

```
Social-Booster/
├── app/                    FastAPI application (web + application layer)
│   ├── main.py             create_app(), middleware, routers, lifespan
│   ├── config.py           Pydantic Settings
│   ├── container.py        composition root (builds all services)
│   ├── dependencies.py     DI providers
│   ├── routes/             pages, drafts, buffer, ops
│   ├── drafts.py           upload -> tag -> generate -> edit -> publish workflow
│   ├── accounts.py         Buffer connection status + shared profile cache
│   ├── security.py         session, CSRF, flash messages
│   ├── middleware.py       security headers, request id, metrics, body limit
│   ├── rate_limit.py       token-bucket limiter
│   ├── error_handlers.py   exception -> toast / JSON / HTML
│   ├── models.py           Pydantic schemas
│   ├── validation.py       form/upload validation
│   ├── db.py, repositories.py   SQLite persistence
│   ├── templating.py, views.py  Jinja2 env + view models
│   ├── logging_config.py   structured logging
│   └── metrics.py          Prometheus metrics
├── services/               framework-agnostic adapters
│   ├── vision.py           image description (online vision model)
│   ├── ollama.py           Ollama Cloud client + parser
│   ├── prompts.py          prompt templates
│   ├── buffer.py           Buffer API key auth, channels + posting
│   ├── publishing.py       provider-neutral contracts
│   ├── storage.py          storage abstraction
│   ├── images.py           upload validation/sanitising
│   ├── content.py          caption/hashtag helpers
│   ├── retry.py, errors.py
├── templates/              Jinja2 (base, index, _card, partials, errors)
├── static/                 css, js, img, vendor (Bootstrap 5.3.8, HTMX 2.0.11)
├── uploads/                runtime: sanitised images (git-ignored)
├── data/                   runtime: SQLite database (git-ignored)
├── tests/                  unit/ and integration/
├── docs/                   this documentation
├── scripts/                mock_upstreams.py
├── deploy/Caddyfile        TLS reverse proxy
├── Dockerfile, docker-compose.yml, gunicorn.conf.py
├── requirements.txt, requirements-dev.txt, pyproject.toml
└── .env.example
```
