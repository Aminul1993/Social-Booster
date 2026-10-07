# Requirements traceability, gap analysis and coverage report

Source of truth: `steps.md` ("production-ready starter kit"), extended by the
build brief (security, production enhancements, testing and documentation
phases). Section numbers (§) refer to `steps.md`.

Legend: **Done** = implemented and verified by the listed tests;
**Changed** = implemented with a documented deviation (see Gap analysis).

## 1. Requirement traceability matrix

### Functional

| ID | Requirement | Source | Implementation | Verification | Status |
|---|---|---|---|---|---|
| FR-01 | Upload **multiple** images in one request | §4 `/upload`, §8 | `app/routes/drafts.py::upload`, `app/drafts.py::DraftService.upload` | `integration/test_upload_workflow.py::test_multiple_images_become_tagged_cards` | Done |
| FR-02 | Store files under `uploads/` with server-generated safe names; serve them publicly | §1, §4 | `services/storage.py::LocalFileStorage`, `/uploads` mount | `unit/test_storage.py`, upload workflow (served headers) | Done |
| FR-03 | Tag every image with a **ResNet-50** (ImageNet), **top-5** concepts | §0, §5 | `services/vision.py::ResNet50Classifier`, `VisionService` (`VISION_TOP_K=5`) | `unit/test_vision.py::TestResNet50` (real graph), Docker smoke test (photo -> "golden retriever") | Done |
| FR-04 | Inference must not block the event loop | §5 | dedicated `ThreadPoolExecutor` + semaphore + timeout | `test_vision.py::test_concurrency_is_bounded`, `test_timeout` | Done |
| FR-05 | Card fragment per image showing detected labels | §4, §8 `_card.html` | `templates/_card.html`, `partials/_cards.html` | upload workflow tests (labels + confidence) | Done |
| FR-06 | Generate caption + hashtags with **Ollama Cloud** from the visual concepts | §4 `/generate`, §6 | `services/ollama.py::OllamaClient.generate_copy`, `app/routes/drafts.py::generate` | `integration/test_generation_workflow.py`, `unit/test_ollama.py` | Changed (D-07, D-11) |
| FR-07 | Caption <= 150 chars; 5-8 hashtags prefixed with `#` | §4 prompt | `services/prompts.py`, `services/ollama.py::finalize_copy` (`CAPTION_MAX_CHARS`, `HASHTAGS_MIN/MAX`) | `test_ollama.py::TestFinalize` | Done |
| FR-08 | OpenAI-compatible chat payload, `temperature 0.7`, bounded `max_tokens` | §6, §11 | `OllamaClient._payload` (+ native `/api/chat`) | `test_ollama.py::test_generate_copy_openai_payload`, `test_native_api_payload_and_response` | Done |
| FR-09 | Re-render the same card with editable caption and hashtags | §8 | `_card.html` (textarea + input), autosave `PATCH /drafts/{id}` | `test_generation_workflow.py::test_autosave_edits`, browser E2E | Done |
| FR-10 | Start Buffer OAuth (redirect to consent page) | §4 `/buffer/auth`, §7 | `app/routes/buffer.py::buffer_auth`, `BufferClient.authorization_url` | `test_oauth_workflow.py::test_full_connect_flow`, `test_buffer.py::TestOAuth` | Done |
| FR-11 | OAuth callback exchanges the code for an access token | §4, §7 | `buffer_callback`, `BufferClient.exchange_code` | OAuth workflow + unit tests | Changed (D-04) |
| FR-12 | Persist the token server-side; it never reaches the browser | §0, §4, §11 | `TokenRepository` (Fernet-encrypted, keyed by session id) | `test_oauth_workflow.py::test_full_connect_flow` (token not in page/cookie, ciphertext only) | Changed (D-05) |
| FR-13 | Retrieve the user's Buffer profiles | §7 | `BufferClient.list_profiles`, `PublisherAccounts.profiles` (cached) | `test_buffer.py::TestProfiles`, `test_accounts.py` | Done |
| FR-14 | Schedule the post on Buffer with text, hashtags, image and date-time | §4 `/buffer/schedule`, §7 | `app/routes/buffer.py::buffer_schedule`, `BufferClient.publish` | `test_scheduling_workflow.py::test_schedule_end_to_end`, browser E2E | Changed (D-04, D-12) |
| FR-15 | Success feedback fragment after scheduling | §4 | card notice + publish summary + `HX-Trigger` toast | scheduling workflow | Done |
| FR-16 | UI shows whether Buffer is connected / offers "Connect Buffer" | §8, §9 | `partials/_buffer_status.html`, schedule panel states | OAuth workflow, `test_routes.py::TestIndex` | Done |
| FR-17 | Drag-and-drop upload zone | §8 | `index.html` dropzone + `app.js` drop handler | browser E2E (file input path), manual | Done |
| FR-18 | Multiple Buffer profiles + profile selection | §12, brief Ph.7 | checkbox list per card, `profile_ids[]`, server-side allow-list | `test_scheduling_workflow.py::test_unknown_profile_rejected`, E2E | Done |
| FR-19 | Image preview | §12, brief Ph.5 | client-side thumbnails before upload, card images, full-size modal | browser E2E (modal) | Done |
| FR-20 | Home page rendering connection state | §4 `index` | `app/routes/pages.py::index` | `test_routes.py::TestIndex` | Done |
| FR-21 | Publishing modes and future extensibility of providers | brief Ph.7 | `PublishMode` (schedule/queue/now), `SocialPublisher` protocol | `test_scheduling_workflow.py::test_queue_and_now_modes`, `test_buffer.py::test_implements_publisher_protocol` | Done |
| FR-22 | Media posting | brief Ph.7 | `media[photo]` + `media[thumbnail]` with absolute `PUBLIC_BASE_URL` | `test_buffer.py::test_schedule_payload`, scheduling workflow | Done |

### UI / UX (brief Phase 5)

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| UI-01 | Jinja2 + HTMX + Bootstrap 5, no placeholder HTML | `templates/`, Bootstrap 5.3.8, HTMX 2.0.11 (vendored) | browser E2E | Done |
| UI-02 | Upload interface, previews, progress | dropzone, `upload-previews`, progress bar on `htmx:xhr:progress` | E2E | Done |
| UI-03 | AI generation workflow with loading states | spinner + busy label, shimmer overlay, fieldset disabled during requests | E2E | Done |
| UI-04 | Caption / hashtag editing | textarea + input, counters, debounced autosave | E2E, generation workflow | Done |
| UI-05 | Buffer connection flow | navbar widget + per-card connect link, drafts survive the redirect | E2E ("draft + edits survived OAuth redirect") | Done |
| UI-06 | Scheduling interface | profiles, mode switch (CSS-only), local datetime + timezone | E2E, scheduling workflow | Done |
| UI-07 | Success / error feedback | `HX-Trigger` toasts, flash messages, inline 422 errors | route + workflow tests, E2E | Done |
| UI-08 | Mobile responsiveness | responsive grid, no horizontal scroll at 390 px | E2E | Done |
| UI-09 | Accessibility | labels, `aria-live` regions, skip link, focus styles, keyboard-usable dropzone, reduced motion | manual review | Done |

### Security (§11 checklist + brief Phase 4)

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| SEC-01 | API keys only on the server | `SecretStr` settings, server-side calls | `test_config.py::test_secrets_are_hidden` | Done |
| SEC-02 | Signed cookies (itsdangerous) | Starlette `SessionMiddleware` | `test_routes.py::test_session_cookie_is_signed_httponly_lax` | Done |
| SEC-03 | CSRF protection (`X-CSRF-Token`) | `security.verify_csrf`, `hx-headers` | `test_security.py::TestCsrf`, `test_routes.py::TestCsrf` | Done |
| SEC-04 | OAuth state validation | `issue_oauth_state` / `consume_oauth_state` (single-use, TTL) | `test_security.py::TestOAuthState`, OAuth workflow (forged, replay, missing) | Done |
| SEC-05 | File upload validation, 10 MB per image | `read_upload`, `ImageProcessor` | `test_images.py`, `test_validation.py::TestReadUpload` | Done |
| SEC-06 | MIME validation | declared MIME allow-list + magic bytes + Pillow format | `test_images.py::TestProcessing` | Done |
| SEC-07 | Safe filenames / storage abstraction | random keys, key regex, `Storage` protocol | `test_storage.py` | Done |
| SEC-08 | Rate limiting | token buckets per scope/IP | `test_rate_limit.py`, `test_routes.py::TestRateLimiting`, OAuth rate-limit test | Done |
| SEC-09 | Secure secrets handling | env / `.env` (ignored) / `/run/secrets`, production secret check | `test_config.py::TestProduction` | Done |
| SEC-10 | No dynamic code evaluation | no eval in Python; HTMX `allowEval=false`; strict CSP | E2E "no CSP violations" | Done |
| SEC-11 | Token never echoed | encrypted storage, header-only transport, log redaction | OAuth workflow, `test_logging.py::test_redact` | Done |
| SEC-12 | DoS: bound concurrent inference | semaphore + executor + timeout, body limit, pixel budget | vision + middleware tests | Done |
| SEC-13 | Bound Ollama cost | `OLLAMA_MAX_TOKENS`, `OLLAMA_TEMPERATURE`, generate rate limit | `test_ollama.py` payload assertions | Done |

### Backend / platform (brief Phases 4, 6, 8)

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| BE-01 | Async routes | all routes `async def`; CPU work off-loop | type-checked, tests | Done |
| BE-02 | Validation | Pydantic form models, `app/validation.py` | `test_validation.py` | Done |
| BE-03 | Error handling | `app/errors.py`, `app/error_handlers.py`, last-resort 500 renderer | `test_routes.py::TestErrors`, `test_views.py::test_map_service_error` | Done |
| BE-04 | Logging (structured) | `app/logging_config.py` JSON/console + request id | `test_logging.py` | Done |
| BE-05 | Dependency injection | `app/dependencies.py`, `ServiceContainer` | route tests use real DI | Done |
| BE-06 | Configuration management (Pydantic Settings) | `app/config.py` | `test_config.py` (incl. `.env.example` validity) | Done |
| BE-07 | Ollama retry policy, timeouts, structured parsing, error recovery, prompt templates | `services/retry.py`, `services/ollama.py`, `services/prompts.py` | `test_ollama.py`, `test_retry.py`, `test_prompts.py` | Done |
| BE-08 | Vision: model initialisation, label extraction, async, CPU optimisation, failure handling | `services/vision.py` | `test_vision.py` | Done |
| BE-09 | Health endpoint | `/health`, `/health/ready` | `test_routes.py::TestOps` | Done |
| BE-10 | Metrics endpoint | `/metrics` (Prometheus, multiprocess-aware) | `test_routes.py::TestOps` | Done |

### Deployment & tooling (§10 + brief Phase 8)

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| DEP-01 | `uvicorn app.main:app --reload` works | lazy `app` attribute in `app/main.py` | `test_routes.py::TestModuleEntryPoints` | Done |
| DEP-02 | Gunicorn + Uvicorn workers behind a reverse proxy with TLS | `gunicorn.conf.py` (`uvicorn_worker`), `deploy/Caddyfile` | container run (gunicorn 26, 2 workers) | Done |
| DEP-03 | Docker support | multi-stage `Dockerfile`, `docker-compose.yml` | image built and smoke-tested read-only | Done |
| DEP-04 | Environment configuration (`.env`, never committed) | `.env.example`, `.gitignore`, `.dockerignore` | `test_env_example_is_valid` | Done |
| DEP-05 | CI/CD | `.github/workflows/ci.yml` (quality, tests 3.12/3.13, docker smoke) | YAML validated | Done |
| DEP-06 | Pre-commit, Ruff, Black, type checking | `.pre-commit-config.yaml`, `pyproject.toml` | all hooks pass | Done |
| DEP-07 | Pytest, > 90 % coverage | `tests/` (unit + integration) | 375 tests, 98.75 % | Done |
| DEP-08 | Python 3.12+ | `requires-python >=3.12`, PEP 695 generics | CI matrix | Done |

### Documentation (brief Phase 10)

| Document | File |
|---|---|
| README | [`README.md`](../README.md) |
| Setup guide | [`SETUP.md`](SETUP.md) |
| Deployment guide | [`DEPLOYMENT.md`](DEPLOYMENT.md) |
| Architecture (Mermaid) | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| API documentation | [`API.md`](API.md) + `/docs` |
| Environment variables | [`ENVIRONMENT.md`](ENVIRONMENT.md) |
| Security | [`SECURITY.md`](SECURITY.md) |
| Troubleshooting | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) |

## 2. Gap analysis

### Deviations from `steps.md` (deliberate, with rationale)

| ID | `steps.md` | Implemented | Why |
|---|---|---|---|
| D-01 | `services/`, `templates/`, `static/` inside `app/` | top-level `services/`, `templates/`, `static/` | Required layout of the build brief; `steps.md`'s own `main.py` already resolved templates from the project root. |
| D-02 | `OLLAMA_ENDPOINT` default `https://api.ollama.com/v1/chat/completions`, model hard-coded `llama3.2:latest` | default `https://ollama.com/v1/chat/completions`, new `OLLAMA_MODEL` (default `gpt-oss:120b`) | `ollama.com` is Ollama Cloud's documented API host and `llama3.2` is not a cloud model. Both remain configurable; native `/api/chat` also supported for self-hosted Ollama. |
| D-03 | Buffer URLs on `buffer.com` / `api.buffer.com`; profiles URL hard-coded | `bufferapp.com` / `api.bufferapp.com` defaults; same variable names plus `BUFFER_PROFILES_URL` | Buffer's v1 publish API is served from `api.bufferapp.com`; every URL is overridable. |
| D-04 | Token exchange and update creation sent as JSON; `media.picture` | form-encoded bodies; `media[photo]` + `media[thumbnail]`; `profile_ids[]` | Buffer's v1 API expects form encoding; image updates require photo + thumbnail. |
| D-05 | Access token stored in a signed cookie | Fernet-encrypted server-side storage keyed by an opaque session id | A signed cookie is readable by the browser; the stated goal ("the token never hits the browser") required server-side storage. |
| D-06 | `torch.hub.load(..., pretrained=True)` + downloaded `imagenet_classes.txt` | `torchvision.models.resnet50(weights=ResNet50_Weights.IMAGENET1K_V2)`; class names from weight metadata | `pretrained=` is removed in current torchvision; metadata removes the extra download. Weights are baked into the Docker image. |
| D-07 | Prompt asks for two lines `CAPTION:` / `HASHTAGS:` | prompt asks for JSON; parser accepts JSON, the original line format and free text, plus one repair turn | Structured parsing and error recovery requirements. |
| D-08 | HTMX 1.9.12 and Bootstrap 5.3.3 from CDNs | HTMX 2.0.11 and Bootstrap 5.3.8 vendored in `static/vendor/` | Strict CSP without third-party script origins; current maintained versions; works offline. |
| D-09 | Old dependency pins (FastAPI 0.111, torch 2.3...) | current pins (FastAPI 0.142, torch 2.14 CPU...) | Python 3.12+ target and security fixes. |
| D-10 | One schedule form at the bottom that uses the *first* card | scheduling per card, multiple profiles, three modes | Each image is its own post; spec §12 lists profile selection as the next step. |
| D-11 | `/generate` receives `image_url` + `labels` from the client | receives `draft_id` + editable `keywords` | Prevents arbitrary media-URL injection; drafts are server-side so they survive the OAuth redirect. |
| D-12 | `datetime-local` labelled "UTC" | interpreted in the browser's IANA timezone and converted to UTC | `datetime-local` has no timezone; using the user's zone avoids off-by-hours schedules. |
| D-13 | Upload limit "10 MB" mentioned only | enforced while streaming + pixel budget + request body cap | Security checklist made concrete. |
| D-14 | Concurrency suggestion `asyncio.Semaphore(2)` | `VISION_MAX_CONCURRENCY` (default 2) semaphore + bounded executor | Same intent, configurable. |

### Items from `steps.md` intentionally not implemented

| Item | Reason / alternative |
|---|---|
| §13 single-file `demo_app.py` | Superseded by the full project; `scripts/mock_upstreams.py` + `VISION_BACKEND` give an even quicker local demo. |
| §12 swap to CLIP / Google Vision | Not required; `ImageClassifier` protocol makes it a drop-in. |
| §12 OpenAI / Gemini instead of Ollama | Any OpenAI-compatible endpoint works via `OLLAMA_ENDPOINT`. |
| §12 server-side APScheduler auto-posting | Buffer performs the scheduling (core requirement); not duplicated. |
| §12 user accounts (FastAPI-Users) | Out of scope; sessions + server-side token store are the extension point (see SECURITY.md "Known limitations"). |
| §12 cropper / carousel | Optional ideas; previews and a full-size modal are provided instead. |

### Gaps found during review and fixed

| Finding | Fix |
|---|---|
| Model replies like `**CAPTION:** text` kept the markdown | Label regex now strips markdown after the colon (`test_labeled_lines_like_the_original_prompt`). |
| JSON reply with an empty caption fell through to the free-text heuristic | Heuristic skips JSON objects so the repair turn runs. |
| Unconfigured Ollama returned 502 | Dedicated `OllamaNotConfiguredError` -> 503 "not configured". |
| Size messages printed "larger than 0 MB" for sub-MB limits | `format_megabytes()` helper. |
| Gunicorn 26 control socket failed on the read-only container filesystem | `control_socket` moved to `/tmp`. |
| Uvicorn's ANSI `color_message` leaked into JSON logs | Filtered as a reserved attribute. |
| Toasts covered the navbar controls | Toast container moved to the bottom-right. |
| HTMX settle could copy inline `style` attributes (CSP violations) | `attributesToSettle` excludes `style`. |
| Re-sending an already published draft took one click | Confirmation dialog on re-send. |
| Empty values in `.env.example` failed validation | `env_ignore_empty=True` + regression test. |

## 3. Final coverage report

Validation run on Windows 11 / Python 3.12.0 and in the Linux container
(Python 3.12-slim, Gunicorn 26.2, 2 workers).

| Check | Result |
|---|---|
| Unit tests | 334 passed |
| Integration tests (upload, OAuth, AI generation, scheduling workflows) | 41 passed |
| **Total** | **375 passed, 0 failed** |
| Line + branch coverage (`app`, `services`) | **98.75 %** (gate: 90 %) |
| Ruff (incl. bandit security rules) | 0 findings |
| Black | 68 files unchanged |
| Mypy `--strict` (app, services, tests, scripts, gunicorn.conf.py) | 0 errors in 68 files |
| Pre-commit (11 hooks) | all passed |
| Docker image build | success (1.8 GB, weights baked in) |
| Container smoke test (read-only rootfs, production mode) | `/health` 200, `/health/ready` ok, vision model ready in ~2.8 s |
| Real ResNet-50 inference in the container | golden-retriever photo -> top-1 "golden retriever", 145 ms request |
| Browser E2E (Chromium, container + mock Ollama/Buffer) | 24/24 checks pass (the only console message is Chrome logging the intentional 422 validation response): upload, labels, generate, autofocus, autosave, counters, OAuth round trip with drafts preserved, 422 inline errors, mode switch, scheduling to 2 profiles with image URL and UTC time, modal, dark mode, 390 px mobile layout, delete, **no CSP violations** |

Per-module coverage (lowest first): `services/images.py` 94 %,
`app/drafts.py` 96 %, `app/dependencies.py` / `logging_config.py` /
`middleware.py` 97 %, all other modules 98-100 %.

**Requirements coverage: 62 / 62 traced requirements implemented and
verified, plus all 8 documentation deliverables.** Four requirement rows are
marked *Changed* (14 documented deviations D-01..D-14 overall); six optional
§12/§13 ideas are intentionally deferred with alternatives.
