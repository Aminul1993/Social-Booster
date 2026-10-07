"""Operational endpoints: liveness, readiness and Prometheus metrics."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import __version__
from app.dependencies import Container
from app.errors import NotFoundError
from app.models import ComponentHealth, HealthResponse, ReadinessResponse
from services.errors import StorageError

router = APIRouter(tags=["ops"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Cheap check that the process is serving requests.",
)
async def health(container: Container) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=container.settings.environment.value,
        uptime_seconds=container.uptime_seconds,
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    description=(
        "Checks the database and upload storage (required) and reports the vision model, "
        "Ollama and Buffer configuration (informational). Returns 503 when a required "
        "component is down."
    ),
    responses={503: {"model": ReadinessResponse, "description": "Not ready"}},
)
async def ready(container: Container) -> JSONResponse:
    checks: dict[str, ComponentHealth] = {}
    required_ok = True

    try:
        await container.database.ping()
        checks["database"] = ComponentHealth(status="ok")
    except Exception as exc:
        required_ok = False
        checks["database"] = ComponentHealth(status="error", detail=type(exc).__name__)

    try:
        await container.storage.check_health()
        checks["storage"] = ComponentHealth(status="ok")
    except StorageError as exc:
        required_ok = False
        checks["storage"] = ComponentHealth(status="error", detail=exc.message)

    vision = container.vision.health()
    checks["vision"] = ComponentHealth(
        status=str(vision["status"]), detail=str(vision.get("error") or "") or None
    )
    container.metrics.vision_ready.set(1 if container.vision.ready else 0)
    checks["ollama"] = ComponentHealth(
        status="configured" if container.ollama.configured else "not_configured"
    )
    checks["buffer"] = ComponentHealth(
        status="configured" if container.accounts.publisher.configured else "not_configured"
    )

    if not required_ok:
        overall = "unavailable"
    elif vision["status"] == "error":
        overall = "degraded"
    else:
        overall = "ok"
    body = ReadinessResponse(status=overall, checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if required_ok else 503)


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    description=(
        "Prometheus exposition format. When `METRICS_TOKEN` is set, requires "
        "`Authorization: Bearer <token>`."
    ),
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
async def metrics(request: Request, container: Container) -> Response:
    settings = container.settings
    if not settings.metrics_enabled:
        raise NotFoundError("Metrics are disabled.")
    expected = settings.secret_value(settings.metrics_token)
    if expected:
        provided = request.headers.get("authorization", "")
        if not secrets.compare_digest(provided.encode(), f"Bearer {expected}".encode()):
            return Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
    payload, content_type = container.metrics.render()
    return Response(payload, media_type=content_type, headers={"Cache-Control": "no-store"})
