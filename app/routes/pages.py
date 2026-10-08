"""Full-page routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.dependencies import Accounts, Container, Drafts, SessionId
from app.security import pop_flashes
from app.templating import templates
from app.views import build_card

router = APIRouter(tags=["pages"])


@router.get(
    "/",
    response_class=HTMLResponse,
    summary="Post builder UI",
    description="Renders the single-page UI with the session's drafts and Buffer status.",
)
async def index(
    request: Request,
    session_id: SessionId,
    drafts: Drafts,
    accounts: Accounts,
    container: Container,
) -> HTMLResponse:
    items = await drafts.list_for_session(session_id)
    publisher = await accounts.status()
    settings = container.settings
    cards = [build_card(request, container.storage, draft) for draft in items]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "cards": cards,
            "publisher": publisher,
            "flashes": pop_flashes(request),
            "ollama_configured": container.ollama.configured,
            "vision": container.vision.health(),
            "limits": {
                "max_files": settings.max_files_per_upload,
                "max_bytes": settings.max_upload_bytes,
                "max_mb": round(settings.max_upload_size_mb, 1),
                "max_drafts": settings.max_drafts_per_session,
            },
        },
        # The page embeds the session's CSRF token: never cache it.
        headers={"Cache-Control": "no-store"},
    )
