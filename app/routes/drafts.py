"""Draft routes: upload, AI generation, autosave and delete (HTMX fragments)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path, Request, UploadFile
from fastapi.responses import HTMLResponse

from app.dependencies import Accounts, Container, CsrfProtected, Drafts, SessionId
from app.drafts import KeywordsRequiredError
from app.errors import BadRequestError, toast_header
from app.models import DraftEditForm, GenerateForm
from app.rate_limit import rate_limit
from app.templating import templates
from app.views import build_card, render_card, render_cards

router = APIRouter(tags=["drafts"])

DraftId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$", description="Draft identifier")]


@router.post(
    "/upload",
    response_class=HTMLResponse,
    dependencies=[CsrfProtected, Depends(rate_limit("upload"))],
    summary="Upload images",
    description=(
        "Accepts one or more JPEG/PNG/WebP images (multipart field `files`). Each file is "
        "validated, sanitised, stored and described by the vision model. Returns one card per "
        "accepted image; rejected files are reported in an `HX-Trigger` toast."
    ),
)
async def upload(
    request: Request,
    session_id: SessionId,
    drafts: Drafts,
    accounts: Accounts,
    container: Container,
    files: Annotated[list[UploadFile], File(description="Images to upload")],
) -> HTMLResponse:
    outcome = await drafts.upload(session_id, files)
    skipped = "; ".join(f"{name} ({reason})" for name, reason in outcome.rejected)
    if not outcome.drafts:
        raise BadRequestError(f"No images were uploaded: {skipped}.")

    count = len(outcome.drafts)
    noun = "image" if count == 1 else "images"
    if skipped:
        headers = toast_header("warning", f"Uploaded {count} {noun}. Skipped: {skipped}.")
    else:
        headers = toast_header("success", f"Uploaded {count} {noun}. Generate copy for each card.")
    publisher = await accounts.status(session_id)
    cards = [build_card(request, container.storage, draft) for draft in outcome.drafts]
    return render_cards(request, cards, publisher, headers=headers)


@router.post(
    "/generate",
    response_class=HTMLResponse,
    dependencies=[CsrfProtected, Depends(rate_limit("generate"))],
    summary="Generate caption and hashtags",
    description=(
        "Sends the draft's (editable) keywords to Ollama Cloud and returns the card re-rendered "
        "with the generated caption and hashtags. Returns 422 with inline errors when no "
        "keywords are given."
    ),
)
async def generate(
    request: Request,
    form: Annotated[GenerateForm, Form()],
    session_id: SessionId,
    drafts: Drafts,
    accounts: Accounts,
    container: Container,
) -> HTMLResponse:
    try:
        draft = await drafts.generate(
            session_id, form.draft_id, keywords=form.keywords, tone=form.tone
        )
    except KeywordsRequiredError as exc:
        draft = await drafts.get(session_id, form.draft_id)
        card = build_card(
            request,
            container.storage,
            draft,
            keywords=form.keywords,
            errors={"keywords": exc.message},
        )
        publisher = await accounts.status(session_id)
        return render_card(request, card, publisher, status_code=422)

    publisher = await accounts.status(session_id)
    card = build_card(request, container.storage, draft, focus="caption")
    return render_card(
        request,
        card,
        publisher,
        headers=toast_header("success", "Caption and hashtags are ready. Edit them freely."),
    )


@router.patch(
    "/drafts/{draft_id}",
    response_class=HTMLResponse,
    dependencies=[CsrfProtected, Depends(rate_limit("default"))],
    summary="Save caption/hashtag edits",
    description="Autosave endpoint. Returns a small status fragment (422 when invalid).",
)
async def update_draft(
    request: Request,
    draft_id: DraftId,
    form: Annotated[DraftEditForm, Form()],
    session_id: SessionId,
    drafts: Drafts,
) -> HTMLResponse:
    draft, errors = await drafts.update_copy(
        session_id, draft_id, caption=form.caption, hashtags=form.hashtags
    )
    return templates.TemplateResponse(
        request,
        "partials/_save_status.html",
        {"draft": draft, "errors": errors},
        status_code=422 if errors else 200,
    )


@router.delete(
    "/drafts/{draft_id}",
    response_class=HTMLResponse,
    dependencies=[CsrfProtected, Depends(rate_limit("default"))],
    summary="Delete a draft",
    description="Deletes the draft and its stored image. Returns an empty fragment.",
)
async def delete_draft(draft_id: DraftId, session_id: SessionId, drafts: Drafts) -> HTMLResponse:
    await drafts.delete(session_id, draft_id)
    return HTMLResponse("", headers=toast_header("info", "Draft deleted."))
