"""Buffer routes: OAuth connect/callback/disconnect and post scheduling."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.dependencies import Accounts, Container, CsrfProtected, Drafts, PublisherToken, SessionId
from app.errors import OAuthStateError, toast_header
from app.models import PublishRecord, ScheduleForm
from app.rate_limit import rate_limit
from app.security import add_flash, consume_oauth_state, issue_oauth_state
from app.validation import CopyEdit, validate_schedule
from app.views import absolute_url, build_card, is_publicly_reachable, render_card
from services.content import compose_post_text
from services.errors import PublisherError
from services.publishing import PostRequest, PublishMode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/buffer", tags=["buffer"])

_HOME = "/"


def _home(request: Request) -> RedirectResponse:
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(f"{root}{_HOME}", status_code=303)


@router.get(
    "/auth",
    dependencies=[Depends(rate_limit("auth"))],
    summary="Start Buffer OAuth",
    description="Redirects to Buffer's consent screen with a session-bound `state`.",
    response_class=RedirectResponse,
    status_code=303,
)
async def buffer_auth(request: Request, session_id: SessionId, accounts: Accounts) -> Response:
    if not accounts.publisher.configured:
        add_flash(request, "danger", "Buffer is not configured on this server.")
        return _home(request)
    state = issue_oauth_state(request)
    return RedirectResponse(accounts.publisher.authorization_url(state), status_code=303)


@router.get(
    "/callback",
    dependencies=[Depends(rate_limit("auth"))],
    summary="Buffer OAuth callback",
    description=(
        "Validates `state`, exchanges `code` for an access token, stores it encrypted "
        "server-side and redirects to `/` with a flash message."
    ),
    response_class=RedirectResponse,
    status_code=303,
)
async def buffer_callback(
    request: Request,
    session_id: SessionId,
    accounts: Accounts,
    container: Container,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> Response:
    events = container.metrics.oauth_events
    provider = accounts.provider
    try:
        consume_oauth_state(request, state)
    except OAuthStateError as exc:
        logger.warning("OAuth state validation failed", extra={"provider": provider})
        events.labels(provider=provider, outcome="invalid_state").inc()
        add_flash(request, "danger", exc.message)
        return _home(request)

    if error:
        events.labels(provider=provider, outcome="denied").inc()
        detail = (error_description or "").strip()[:200]
        add_flash(
            request,
            "warning",
            f"Buffer authorization was not completed{': ' + detail if detail else '.'}",
        )
        return _home(request)
    if not code:
        events.labels(provider=provider, outcome="missing_code").inc()
        add_flash(request, "danger", "Buffer did not return an authorization code.")
        return _home(request)

    try:
        await accounts.connect(session_id, code)
    except PublisherError as exc:
        events.labels(provider=provider, outcome="exchange_failed").inc()
        logger.warning("OAuth code exchange failed", extra={"error": exc.message})
        add_flash(request, "danger", exc.message)
        return _home(request)

    events.labels(provider=provider, outcome="connected").inc()
    add_flash(request, "success", "Buffer connected. You can now schedule posts.")
    return _home(request)


@router.get(
    "/refresh",
    summary="Refresh Buffer profiles",
    description="Reloads the connected profiles from Buffer, then redirects to `/`.",
    response_class=RedirectResponse,
    status_code=303,
)
async def buffer_refresh(request: Request, session_id: SessionId, accounts: Accounts) -> Response:
    status = await accounts.status(session_id, refresh=True)
    if status.error:
        add_flash(request, "danger", status.error)
    elif status.connected:
        add_flash(request, "info", f"Found {len(status.profiles)} Buffer profile(s).")
    return _home(request)


@router.post(
    "/disconnect",
    dependencies=[CsrfProtected, Depends(rate_limit("auth"))],
    summary="Disconnect Buffer",
    description="Deletes the stored token and asks HTMX to reload the page.",
)
async def buffer_disconnect(
    request: Request, session_id: SessionId, accounts: Accounts
) -> Response:
    await accounts.disconnect(session_id)
    add_flash(request, "info", "Buffer disconnected.")
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.post(
    "/schedule",
    response_class=HTMLResponse,
    dependencies=[CsrfProtected, Depends(rate_limit("schedule"))],
    summary="Schedule a post on Buffer",
    description=(
        "Validates the edited caption/hashtags, profiles and time, then creates the Buffer "
        "update with the image attached. Returns the re-rendered card (422 with inline "
        "errors when validation fails)."
    ),
)
async def buffer_schedule(
    request: Request,
    form: Annotated[ScheduleForm, Form()],
    session_id: SessionId,
    token: PublisherToken,
    drafts: Drafts,
    accounts: Accounts,
    container: Container,
) -> HTMLResponse:
    draft = await drafts.get(session_id, form.draft_id)
    profiles = await accounts.profiles(session_id, token)
    publisher = await accounts.status(session_id)

    validation = validate_schedule(form, allowed_profile_ids={p.id for p in profiles})
    if validation.result is None:
        card = build_card(
            request, container.storage, draft, schedule_form=form, errors=validation.errors
        )
        return render_card(
            request,
            card,
            publisher,
            status_code=422,
            headers=toast_header("warning", "Please fix the highlighted fields."),
        )

    valid = validation.result
    draft = await drafts.apply_copy(draft, CopyEdit(caption=valid.caption, hashtags=valid.hashtags))
    media_url = absolute_url(request, container.settings, drafts.public_path(draft))
    post = PostRequest(
        profile_ids=valid.profile_ids,
        text=compose_post_text(valid.caption, valid.hashtags),
        mode=valid.mode,
        media_url=media_url,
        scheduled_at=valid.scheduled_at,
    )

    publish_metric = container.metrics.publish_requests
    try:
        result = await accounts.publish(session_id, token, post)
    except PublisherError:
        publish_metric.labels(
            provider=accounts.provider, mode=valid.mode.value, outcome="error"
        ).inc()
        raise
    publish_metric.labels(
        provider=accounts.provider, mode=valid.mode.value, outcome="success"
    ).inc()

    names = [p.username for p in profiles if p.id in valid.profile_ids]
    draft = await drafts.record_publish(
        draft,
        PublishRecord(
            provider=accounts.provider,
            mode=valid.mode,
            profile_ids=list(valid.profile_ids),
            profile_names=names,
            scheduled_at=valid.scheduled_at,
            update_ids=list(result.update_ids),
            message=result.message,
        ),
    )

    targets = f"{len(names)} profile{'s' if len(names) != 1 else ''}"
    if valid.mode is PublishMode.SCHEDULE and valid.scheduled_at is not None:
        message = f"Scheduled on {targets} for {valid.scheduled_at:%Y-%m-%d %H:%M} UTC."
    elif valid.mode is PublishMode.QUEUE:
        message = f"Added to the Buffer queue of {targets}."
    else:
        message = f"Shared now on {targets}."
    level: str = "success"
    if not is_publicly_reachable(media_url):
        level = "warning"
        message += (
            " Note: Buffer cannot download images from a local address - set PUBLIC_BASE_URL "
            "to a public URL."
        )

    card = build_card(request, container.storage, draft, notice=message, notice_level=level)
    return render_card(
        request,
        card,
        publisher,
        headers=toast_header("success" if level == "success" else "warning", message),
    )
