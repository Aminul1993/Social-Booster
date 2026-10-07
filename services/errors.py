"""Exception hierarchy shared by every service adapter.

The web layer maps these to HTTP responses (see ``app.errors``); services never
raise framework exceptions themselves.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for failures raised by a service adapter.

    Attributes:
        message: Human-readable description that is safe to show to end users.
        retryable: ``True`` when repeating the same call may succeed.
        status_code: Upstream HTTP status, when the failure came from an HTTP API.
        retry_after: Seconds the upstream asked us to wait before retrying.
    """

    service: str = "service"

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


class ServiceNotConfiguredError(ServiceError):
    """Raised when a service is called without the credentials it needs."""


class StorageError(ServiceError):
    """Raised when an uploaded file cannot be stored or retrieved."""

    service = "storage"


class VisionError(ServiceError):
    """Raised when the image-tagging model cannot load or analyse an image."""

    service = "vision"


class OllamaError(ServiceError):
    """Raised when the Ollama Cloud chat API fails."""

    service = "ollama"


class OllamaAuthError(OllamaError):
    """Ollama Cloud rejected the API key (HTTP 401/403)."""


class OllamaResponseError(OllamaError):
    """The model answered, but the answer could not be turned into copy."""


class OllamaNotConfiguredError(OllamaError, ServiceNotConfiguredError):
    """No API key / endpoint configured for Ollama Cloud."""


class PublisherError(ServiceError):
    """Raised when a social publishing provider (Buffer, ...) fails."""

    service = "publisher"


class PublisherAuthError(PublisherError):
    """The provider rejected the OAuth credentials or access token."""


class PublisherAPIError(PublisherError):
    """The provider understood the request but refused it (validation, limits...)."""
