# Deployment

The application ships as one container: Gunicorn managing Uvicorn workers,
SQLite and the upload directory on volumes, and an optional Caddy reverse
proxy for HTTPS. Images are described by an online vision model, so there is
no ML runtime in the image.

## 1. Checklist

- [ ] `ENVIRONMENT=production`
- [ ] `SESSION_SECRET` - 32+ random characters (the app refuses to start otherwise)
- [ ] `PUBLIC_BASE_URL=https://your-domain` - Buffer fetches images from it
- [ ] `BUFFER_REDIRECT_URI=https://your-domain/buffer/callback` (also registered in the Buffer app)
- [ ] `ALLOWED_HOSTS=your-domain`
- [ ] TLS terminated in front of the app; `COOKIE_SECURE` left at its default (`true`)
- [ ] `OLLAMA_API_KEY`, `BUFFER_CLIENT_ID`, `BUFFER_CLIENT_SECRET` set (env or `/run/secrets`)
- [ ] Persistent volumes for `/app/uploads` and `/app/data`; backups scheduled
- [ ] `/metrics` protected (`METRICS_TOKEN`) or not exposed publicly
- [ ] `FORWARDED_ALLOW_IPS` set to the proxy's address (client IPs for rate limiting)

## 2. Docker Compose (single host)

```bash
cd Social-Booster
cp .env.example .env        # fill in the checklist values
docker compose up -d --build
docker compose ps           # wait for "healthy"
```

With automatic HTTPS (Let's Encrypt via Caddy, ports 80/443 open, DNS pointing
to the host):

```bash
DOMAIN=social.example.com docker compose --profile tls up -d --build
```

The compose file runs the app with a read-only root filesystem, `/tmp` on
tmpfs, all Linux capabilities dropped, `no-new-privileges`, memory/CPU limits
and log rotation.

## 3. Plain Docker

```bash
docker build -t marketing-ai-builder:1.0.0 .
docker run -d --name mab -p 8000:8000 \
  --env-file .env -e ENVIRONMENT=production \
  -v mab-uploads:/app/uploads -v mab-data:/app/data \
  --read-only --tmpfs /tmp \
  marketing-ai-builder:1.0.0
```

Build argument: `PYTHON_VERSION` (default `3.12`).

The image runs as uid `10001`, exposes `8000`, declares a `HEALTHCHECK` on
`/health`, and starts `gunicorn -c gunicorn.conf.py app.main:app`.

## 4. Without containers

```bash
pip install -r requirements.txt
export ENVIRONMENT=production SESSION_SECRET=... PUBLIC_BASE_URL=https://...
gunicorn -c gunicorn.conf.py app.main:app
```

Put NGINX/Caddy in front. Example NGINX location:

```nginx
client_max_body_size 110m;
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Request-ID $request_id;
    proxy_read_timeout 120s;
}
```

and run Gunicorn with `FORWARDED_ALLOW_IPS=127.0.0.1`.

On Windows servers use `uvicorn app.main:app --host 0.0.0.0 --port 8000
--workers 2 --proxy-headers` (Gunicorn is POSIX-only).

## 5. Sizing

| Resource | Guidance |
|---|---|
| Memory | ~110 MB for the whole container idle (2 workers; measured), ~210 MB peak while uploading a batch of large JPEGs. Uploads add memory per worker while an image is processed (`IMAGE_MAX_CONCURRENCY` images at a time): ~+90 MB for JPEGs of any size (decoded at reduced scale down to `IMAGE_MAX_DIMENSION`), ~4-8 bytes per pixel for PNG (~+360 MB for 40 MP with transparency) and ~16 for WebP (~+620 MB for 40 MP). Size `MAX_IMAGE_PIXELS` to fit. |
| CPU | Light: image description runs at the vision provider (~2 s per image on Ollama Cloud); locally only Pillow decoding/re-encoding. |
| Network | Each upload sends a <=512 px JPEG preview (~50-100 kB) to the vision provider; its free tier has rate limits. |
| Disk | Image ~320 MB. Uploads: up to `MAX_UPLOAD_SIZE_MB` per draft, purged after `DRAFT_RETENTION_HOURS`. |
| Start-up | Instant: there is no model to load; `/health/ready` reports `vision: ok` once an API key is configured. |

## 6. Health, metrics, logs

* Liveness: `GET /health` - use for container/Kubernetes liveness probes.
* Readiness: `GET /health/ready` - `503` if the database or storage is down.
* Metrics: `GET /metrics` (Prometheus). With several workers, metrics are
  aggregated through `PROMETHEUS_MULTIPROC_DIR` (set in the image).
* Logs: one JSON object per line on stdout, each with `request_id`; the
  response header `X-Request-ID` lets you correlate user reports with logs.

Kubernetes probe example:

```yaml
livenessProbe:
  httpGet: {path: /health, port: 8000}
  periodSeconds: 15
readinessProbe:
  httpGet: {path: /health/ready, port: 8000}
  initialDelaySeconds: 5
  periodSeconds: 10
```

## 7. Data and backups

| Path | Content | Backup |
|---|---|---|
| `/app/data/app.db` (+ `-wal`, `-shm`) | drafts, encrypted tokens | `sqlite3 app.db ".backup 'backup.db'"` (safe while running) |
| `/app/uploads` | sanitised images | file-level copy / volume snapshot |

Drafts are working data with a retention window, so backups are mainly to
survive host loss during that window.

## 8. Scaling out

The default deployment is single-host. To run several hosts behind a load
balancer:

1. Replace SQLite with a shared database in `app/repositories.py` / `app/db.py`.
2. Implement the `Storage` protocol for object storage (S3/GCS) and serve
   images from it (`public_path` returns the public object URL).
3. Use a shared rate limiter (Redis, or the load balancer).
4. Keep `SESSION_SECRET` (and `TOKEN_ENCRYPTION_KEY`) identical on all hosts.

## 9. CI/CD

`.github/workflows/ci.yml` (repository root) runs on every push/PR touching
the project:

1. **quality** - ruff, black, strict mypy
2. **test** - pytest with the 90 % coverage gate on Python 3.12 and 3.13
3. **docker** - builds the image (GitHub Actions cache), runs it read-only and
   smoke-tests `/health`, `/health/ready` and `/`

Extend the `docker` job with a registry login and `push: true` (e.g. on tags)
to publish images.

## 10. Upgrades

1. Build the new image; check the changelog/environment docs for new variables.
2. `docker compose up -d --build` replaces the container; the schema is created
   idempotently on start, volumes are preserved.
3. Roll back by re-deploying the previous image tag.
