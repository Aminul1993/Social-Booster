"""Buffer routes: profile refresh and post scheduling."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.dependencies import (
    Accounts,
    Container,
    CsrfProtected,
    Drafts,
    PublisherConfigured,
    SessionId,
)
from app.errors import toast_header
from app.models import PublishRecord, ScheduleForm
from app.rate_limit import rate_limit
from app.security import add_flash
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
    "/refresh",
    # Bypasses the shared profile cache, so it spends the Buffer account's API quota.
    dependencies=[Depends(rate_limit("default"))],
    summary="Refresh Buffer profiles",
    description="Reloads the connected profiles from Buffer, then redirects to `/`.",
    response_class=RedirectResponse,
    status_code=303,
)
async def buffer_refresh(request: Request, accounts: Accounts) -> Response:
    status = await accounts.status(refresh=True)
    if status.error:
        add_flash(request, "danger", status.error)
    elif status.connected:
        add_flash(request, "info", f"Found {len(status.profiles)} Buffer profile(s).")
    return _home(request)


@router.post(
    "/schedule",
    response_class=HTMLResponse,
    dependencies=[CsrfProtected, PublisherConfigured, Depends(rate_limit("schedule"))],
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
    drafts: Drafts,
    accounts: Accounts,
    container: Container,
) -> HTMLResponse:
    draft = await drafts.get(session_id, form.draft_id)
    profiles = await accounts.profiles()
    publisher = await accounts.status()

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
        result = await accounts.publish(post)
    except PublisherError:
        publish_metric.labels(
            provider=accounts.provider, mode=valid.mode.value, outcome="error"
        ).inc()
        raise
    publish_metric.labels(
        provider=accounts.provider, mode=valid.mode.value, outcome="success"
    ).inc()

    refused = dict(result.failures)
    sent = [p for p in profiles if p.id in valid.profile_ids and p.id not in refused]
    names = [p.username for p in sent]
    draft = await drafts.record_publish(
        draft,
        PublishRecord(
            provider=accounts.provider,
            mode=valid.mode,
            profile_ids=[p.id for p in sent],
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
    if refused:
        level = "warning"
        by_id = {p.id: p.username for p in profiles}
        reasons = "; ".join(f"{by_id.get(pid, pid)}: {reason}" for pid, reason in refused.items())
        message += f" Not sent to {reasons}"
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
