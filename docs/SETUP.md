# Local setup

## Requirements

* Python **3.12+** (3.12 and 3.13 are tested in CI)
* ~2 GB disk for PyTorch (CPU build) and the ResNet-50 weights (~100 MB)
* Optional: Docker 24+ for the container workflow
* Credentials: an Ollama Cloud API key and a Buffer OAuth app (or use the
  bundled mock services, see below)

## 1. Install

```bash
cd Social-Booster
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt  # runtime + test/lint tooling
# runtime only:  pip install -r requirements.txt
```

`requirements.txt` points pip at the CPU-only PyTorch index, so Linux does not
download multi-GB CUDA wheels.

Pre-fetch the model weights (otherwise the first start downloads them):

```bash
python scripts/download_model.py --weights IMAGENET1K_V2
```

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
| `BUFFER_CLIENT_ID` / `BUFFER_CLIENT_SECRET` | from your Buffer OAuth client (see below) |
| `BUFFER_REDIRECT_URI` | your app's `https://.../buffer/callback`, exactly as registered with Buffer |
| `PUBLIC_BASE_URL` | leave empty locally; see "Images on Buffer" below |

All variables are documented in [ENVIRONMENT.md](ENVIRONMENT.md).

### Registering the Buffer app

1. Sign in to Buffer and open **Settings -> API**
   ([guide](https://developers.buffer.com/guides/building-apps.md)).
2. Register an OAuth client of type **private** (the app keeps the secret on
   its server) and add `BUFFER_REDIRECT_URI` as a redirect URI. Buffer compares
   it character for character and requires `https`, even on localhost.
3. Copy the client id and secret into `.env`. Leave `BUFFER_OAUTH_URL`,
   `BUFFER_TOKEN_URL` and `BUFFER_API_URL` unset: the defaults are Buffer's
   current endpoints. The old `bufferapp.com` v1 URLs answer these clients with
   `invalid_client`.

The app asks for `account:read posts:write offline_access`, uses PKCE, and
renews Buffer's one-hour access tokens with the refresh token automatically.

### Images on Buffer

Buffer downloads the image from `PUBLIC_BASE_URL/uploads/<file>`. A localhost
URL is not reachable from Buffer's servers; the app still schedules the post
but warns you. For a real local test, expose the app with a tunnel
(`cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`) and set
both `PUBLIC_BASE_URL` and `BUFFER_REDIRECT_URI` to the tunnel's https URL.

## 3. Run

```bash
uvicorn app.main:app --reload --port 8000
# or: python -m app
```

Open http://localhost:8000.

### Running without Ollama/Buffer accounts

`scripts/mock_upstreams.py` imitates Ollama's chat endpoint and Buffer's OAuth
(with PKCE) and GraphQL endpoints so you can try the complete flow offline:

```bash
python scripts/mock_upstreams.py --port 8020
```

```dotenv
OLLAMA_ENDPOINT=http://127.0.0.1:8020/v1/chat/completions
OLLAMA_API_KEY=mock
BUFFER_CLIENT_ID=mock
BUFFER_CLIENT_SECRET=mock
BUFFER_OAUTH_URL=http://127.0.0.1:8020/auth
BUFFER_TOKEN_URL=http://127.0.0.1:8020/token
BUFFER_API_URL=http://127.0.0.1:8020/graphql
```

Posts "sent" to the mock are listed at http://127.0.0.1:8020/posts.

### Without PyTorch

Set `VISION_BACKEND=disabled` to skip the model entirely (users type keywords
for each image). You can then install everything except `torch`/`torchvision`.

## 4. Quality tooling

```bash
pytest                      # unit + integration tests, coverage gate 90 %
pytest -m "not torch"       # skip the real ResNet-50 graph test (faster)
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
│   ├── accounts.py         OAuth tokens + profile cache
│   ├── security.py         session, CSRF, OAuth state, token encryption
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
│   ├── vision.py           ResNet-50 tagging
│   ├── ollama.py           Ollama Cloud client + parser
│   ├── prompts.py          prompt templates
│   ├── buffer.py           Buffer OAuth + posting
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
├── scripts/                download_model.py, mock_upstreams.py
├── deploy/Caddyfile        TLS reverse proxy
├── Dockerfile, docker-compose.yml, gunicorn.conf.py
├── requirements.txt, requirements-dev.txt, pyproject.toml
└── .env.example
```
