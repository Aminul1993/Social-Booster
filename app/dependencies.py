"""FastAPI dependency providers (dependency injection).

Routes declare what they need with the ``Annotated`` aliases below; tests can
replace any provider through ``app.dependency_overrides``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.accounts import PublisherAccounts
from app.config import Settings
from app.container import ServiceContainer
from app.drafts import DraftService
from app.errors import NotConnectedError
from app.security import ensure_session, verify_csrf
from services.publishing import OAuthToken


def get_container(request: Request) -> ServiceContainer:
    container: ServiceContainer = request.app.state.container
    return container


def get_settings(container: Annotated[ServiceContainer, Depends(get_container)]) -> Settings:
    return container.settings


def get_drafts(container: Annotated[ServiceContainer, Depends(get_container)]) -> DraftService:
    return container.drafts


def get_accounts(
    container: Annotated[ServiceContainer, Depends(get_container)],
) -> PublisherAccounts:
    return container.accounts


def get_session_id(request: Request) -> str:
    return ensure_session(request)


Container = Annotated[ServiceContainer, Depends(get_container)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Drafts = Annotated[DraftService, Depends(get_drafts)]
Accounts = Annotated[PublisherAccounts, Depends(get_accounts)]
SessionId = Annotated[str, Depends(get_session_id)]


async def require_token(accounts: Accounts, session_id: SessionId) -> OAuthToken:
    """The session's publishing token, or a 401 asking the user to connect."""
    if not accounts.publisher.configured:
        raise NotConnectedError("Buffer is not configured on this server.")
    token = await accounts.token(session_id)
    if token is None:
        raise NotConnectedError()
    return token


PublisherToken = Annotated[OAuthToken, Depends(require_token)]
CsrfProtected = Depends(verify_csrf)
