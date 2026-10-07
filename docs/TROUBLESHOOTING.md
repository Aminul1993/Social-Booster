# Troubleshooting

Every response carries an `X-Request-ID`; search the logs for it first. Error
pages and toasts for unexpected errors show the same id as "Reference".

## Start-up

| Symptom | Cause | Fix |
|---|---|---|
| `SESSION_SECRET must be set ... in production` | Production without a strong secret | Set a 32+ character `SESSION_SECRET`. |
| `Invalid rate limit 'x'` | Malformed `RATE_LIMIT_*` | Use `<count>/<second|minute|hour|day>`, e.g. `20/minute`. |
| `PUBLIC_BASE_URL must be an absolute http(s) URL` | Missing scheme | Use `https://social.example.com`. |
| Readiness shows `vision: error` / banner "Automatic image description is unavailable" | No `OLLAMA_API_KEY` (or `VISION_API_KEY`) | Set the key (the vision model shares Ollama's by default). The app keeps working with manual keywords. Set `VISION_BACKEND=disabled` to silence it. |
| `Input should be 'ollama' or 'disabled'` for `vision_backend` | `VISION_BACKEND=resnet50` left over from an older `.env` / host settings; ResNet-50 was removed | Set `VISION_BACKEND=ollama` (or delete the variable). |
| `Control server error: Read-only file system` (Gunicorn) | Custom command without `gunicorn.conf.py` | Start with `gunicorn -c gunicorn.conf.py app.main:app` (puts the control socket on `/tmp`). |
| Health check fails with HTTP 400 | Host not in `ALLOWED_HOSTS` | Add your domain; `localhost`/`127.0.0.1` are always allowed. |

## Session and security

| Symptom | Cause | Fix |
|---|---|---|
| Toast "Your session could not be verified" (403) | CSRF token missing/stale - usually the server restarted with an ephemeral dev secret, or cookies are blocked | Reload the page. Set a fixed `SESSION_SECRET` in development. |
| Logged out / Buffer disconnected after every restart | No `SESSION_SECRET` in development | Set one in `.env`. |
| Cookies not stored on plain `http://` in production | `COOKIE_SECURE` defaults to `true` in production | Serve over HTTPS (recommended) or set `COOKIE_SECURE=false` for an internal test only. |
| `429 Too many requests` | Rate limit reached | Wait `Retry-After` seconds or raise `RATE_LIMIT_*`. Behind a proxy, set `FORWARDED_ALLOW_IPS` so clients are not all seen as the proxy's IP. |
| Browser console shows CSP errors | A browser extension or custom template code adds inline styles/scripts | The app itself uses no inline code. Move custom code into `static/`. |

## Uploads

| Symptom | Cause | Fix |
|---|---|---|
| "Only JPEG, PNG and WebP images are supported." | HEIC, GIF, SVG... | Export as JPEG/PNG/WebP. |
| "The file content does not match its declared image type." | Renamed file (e.g. PNG named `.jpg`) | Re-export the image properly. |
| "larger than 10 MB" / 413 | `MAX_UPLOAD_SIZE_MB` / proxy body limit | Raise the setting **and** the proxy's limit (`client_max_body_size`, Caddy `request_body max_size`). |
| "The image is too large (W x H pixels)" | Above `MAX_IMAGE_PIXELS` | Downscale or raise the limit (memory grows with pixels). |
| Worker memory jumps during uploads / OOM kills | Large PNG/WebP files must be decoded at full size (40 MP PNG ~+360 MB, 40 MP WebP ~+620 MB; JPEGs stay under ~+90 MB) | Lower `MAX_IMAGE_PIXELS` (e.g. `16000000`), keep `IMAGE_MAX_CONCURRENCY=1` and `IMAGE_MAX_DIMENSION` set. Outside Docker, also set `MALLOC_MMAP_THRESHOLD_=1048576` so freed images go back to the OS. |
| Cards say "couldn't be analysed automatically" | The vision model failed; the reason is in the log (`Image analysis failed`). HTTP 402 "not in your plan": `VISION_MODEL` needs a paid Ollama plan (use `gemma4:31b`). 429/timeouts: free-tier rate limits | Type keywords, retry later, or lower `VISION_MAX_CONCURRENCY`. |
| Strange keywords | The vision model misread the image | Edit the keywords before generating; they steer the AI. |

## AI generation (Ollama Cloud)

| Toast / log | Cause | Fix |
|---|---|---|
| "AI copywriting is not configured" banner | `OLLAMA_API_KEY` empty with the cloud endpoint | Set the key. |
| "The AI service rejected the configured API key" | 401/403 | Check the key and that it belongs to the account owning the model. |
| "The AI model 'x' or endpoint was not found" | 404: wrong model name or URL | Use a model available to your account (`OLLAMA_MODEL`); the endpoint must end in `/v1/chat/completions` or `/api/chat`. |
| "The AI service is busy ... Please try again." | 429/5xx after `OLLAMA_MAX_RETRIES` | Retry later or raise retries/timeouts. |
| "ran out of tokens before answering; increase OLLAMA_MAX_TOKENS" | Reasoning model used the budget thinking | Raise `OLLAMA_MAX_TOKENS` (e.g. 1024) or choose a non-reasoning model. |
| "The AI service timed out." | Slow model | Raise `OLLAMA_TIMEOUT_SECONDS`. |
| Captions get cut with "…" | Longer than `CAPTION_MAX_CHARS` (150 by default) | Raise it or edit the caption (up to 2,200 characters). |

## Buffer

| Symptom | Cause | Fix |
|---|---|---|
| `invalid_client` when connecting Buffer | The client was registered under Buffer's Settings -> API, but `BUFFER_OAUTH_URL`/`BUFFER_TOKEN_URL` still point at the retired `bufferapp.com` v1 endpoints (the app logs a warning at start-up), or the id/secret are wrong | Unset `BUFFER_OAUTH_URL`, `BUFFER_TOKEN_URL`, `BUFFER_PROFILES_URL`, `BUFFER_POST_URL` (defaults: `auth.buffer.com`, `api.buffer.com`) and re-check `BUFFER_CLIENT_ID`/`BUFFER_CLIENT_SECRET`. |
| Buffer shows "redirect_uri mismatch" | `BUFFER_REDIRECT_URI` differs from the app registration | Make them identical, including scheme, host, port and path. |
| "The authorization response could not be verified" | Callback opened in another browser, after > 10 minutes, or twice | Click "Connect Buffer" again in the same browser. |
| "Buffer rejected the authorization code" | Code already used/expired, wrong client secret | Reconnect; check `BUFFER_CLIENT_SECRET`. |
| "Buffer rejected the access token. Please connect Buffer again." | Token revoked in Buffer | The token was removed automatically; reconnect. |
| Post scheduled but without the image / Buffer error about media | Buffer cannot download `localhost` URLs | Set `PUBLIC_BASE_URL` to a public https origin (tunnel locally). The UI warns when the URL is not public. |
| "No social profiles found" | The Buffer account has no connected channels | Add channels in Buffer, then use "Refresh profiles" in the navbar menu. |
| Buffer API errors on every call | Your Buffer plan/app has no access to the publish API, or the endpoints moved | Check the Buffer developer settings; every endpoint URL is configurable (`BUFFER_*_URL`). Use `scripts/mock_upstreams.py` to verify the app side independently. |
| Wrong scheduled time | Times are entered in the browser's timezone (shown next to the field) and converted to UTC | Verify the timezone label; the card shows the scheduled time in your local time. |

## Storage and database

| Symptom | Cause | Fix |
|---|---|---|
| `database is locked` | Another process holds a long write transaction | WAL mode and a 5 s busy timeout handle normal load; avoid opening the DB with other tools while writing. |
| Readiness `storage: error` | Upload directory not writable | Fix volume ownership: `chown -R 10001:10001` on the host path. |
| Drafts disappear | Retention purge | Raise `DRAFT_RETENTION_HOURS` or set `0`. |

## Collecting diagnostics

```bash
curl -s localhost:8000/health/ready | python -m json.tool
docker compose logs app --since 10m | grep <request-id>
curl -s localhost:8000/metrics | grep -E "mab_(ai|publish|uploads|vision)"
```

Set `LOG_LEVEL=DEBUG` temporarily for access logs of static files and probes.
