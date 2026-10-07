"""Ollama Cloud chat client used to write captions and hashtags.

Features
--------
* Speaks both the OpenAI-compatible endpoint (``/v1/chat/completions``, the
  default) and Ollama's native ``/api/chat`` endpoint; the style is detected
  from the URL, so the same client works for Ollama Cloud and self-hosted
  Ollama.
* Retry policy with exponential back-off for timeouts, connection errors,
  HTTP 429 and 5xx (honouring ``Retry-After``). Authentication and other 4xx
  errors fail fast.
* Structured parsing: strict JSON first, then the ``CAPTION:``/``HASHTAGS:``
  line format, then a free-text heuristic. Reasoning blocks (``<think>``) and
  markdown fences are stripped.
* Error recovery: if nothing usable comes back, the model gets one "repair"
  turn; hashtags are topped up from the detected keywords when the model
  returns too few.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from services.content import (
    keyword_to_hashtag,
    normalize_caption,
    normalize_hashtags,
    truncate_words,
)
from services.errors import (
    OllamaAuthError,
    OllamaError,
    OllamaNotConfiguredError,
    OllamaResponseError,
)
from services.prompts import REPAIR_PROMPT, ChatMessage, CopyRequest, build_copy_messages
from services.retry import (
    RETRYABLE_STATUS_CODES,
    RetryPolicy,
    SleepFunc,
    parse_retry_after,
    retry_async,
)

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://ollama.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-oss:120b"

ParseStrategy = Literal["json", "labeled", "heuristic"]


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    """Connection settings for the chat endpoint."""

    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    api_key: str | None = None
    temperature: float = 0.7
    max_tokens: int = 512
    timeout_seconds: float = 30.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    @property
    def api_style(self) -> Literal["openai", "native"]:
        path = urlparse(self.endpoint).path.rstrip("/")
        return "native" if path.endswith("/api/chat") else "openai"

    @property
    def is_cloud(self) -> bool:
        host = (urlparse(self.endpoint).hostname or "").lower()
        return host == "ollama.com" or host.endswith(".ollama.com")

    @property
    def configured(self) -> bool:
        """Cloud endpoints need an API key; self-hosted Ollama does not."""
        if not self.endpoint or not self.model:
            return False
        return bool(self.api_key) or not self.is_cloud


@dataclass(frozen=True, slots=True)
class ParsedCopy:
    caption: str
    hashtags: list[str]
    strategy: ParseStrategy


@dataclass(frozen=True, slots=True)
class GeneratedCopy:
    """Final, normalised marketing copy."""

    caption: str
    hashtags: tuple[str, ...]
    model: str
    strategy: ParseStrategy
    repaired: bool = False


# --------------------------------------------------------------------------- parsing
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*|```")
_LABEL_RE = {
    "caption": re.compile(
        r"^[\s*_>#-]*caption[\s*_]*[:\-–][\s*_]*(.+)$",  # noqa: RUF001 - en dash on purpose
        re.IGNORECASE | re.MULTILINE,
    ),
    "hashtags": re.compile(
        r"^[\s*_>#-]*hashtags?[\s*_]*[:\-–][\s*_]*(.+)$",  # noqa: RUF001 - en dash on purpose
        re.IGNORECASE | re.MULTILINE,
    ),
}
_HASHTAG_TOKEN_RE = re.compile(r"#[^\s#,;]+", re.UNICODE)
_QUOTES = "\"'“”‘’`"  # noqa: RUF001 - typographic quotes models like to emit


def clean_model_text(raw: str) -> str:
    """Model reply without reasoning blocks and markdown code fences."""
    text = _THINK_RE.sub("", raw)
    return _FENCE_RE.sub("", text).strip()


def _strip_quotes(text: str) -> str:
    return text.strip().strip(_QUOTES).strip()


def _parse_json(text: str) -> ParsedCopy | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    lowered = {str(k).lower(): v for k, v in data.items()}
    caption = lowered.get("caption")
    hashtags = lowered.get("hashtags", lowered.get("tags", []))
    if not isinstance(caption, str) or not caption.strip():
        return None
    if isinstance(hashtags, str):
        tags = normalize_hashtags(hashtags)
    elif isinstance(hashtags, list):
        tags = normalize_hashtags([str(tag) for tag in hashtags])
    else:
        tags = []
    return ParsedCopy(caption=_strip_quotes(caption), hashtags=tags, strategy="json")


def _parse_labeled(text: str) -> ParsedCopy | None:
    caption_match = _LABEL_RE["caption"].search(text)
    if not caption_match:
        return None
    caption = _strip_quotes(caption_match.group(1))
    if not caption:
        return None
    hashtags_match = _LABEL_RE["hashtags"].search(text)
    tags = normalize_hashtags(hashtags_match.group(1)) if hashtags_match else []
    return ParsedCopy(caption=caption, hashtags=tags, strategy="labeled")


def _parse_heuristic(text: str) -> ParsedCopy | None:
    if text.startswith("{") and text.endswith("}"):
        return None  # a JSON object without a usable caption: let the repair round fix it
    tags = normalize_hashtags(_HASHTAG_TOKEN_RE.findall(text))
    without_tags = _HASHTAG_TOKEN_RE.sub("", text)
    for line in without_tags.splitlines():
        candidate = _strip_quotes(line.strip(" -*•"))
        if candidate:
            return ParsedCopy(caption=candidate, hashtags=tags, strategy="heuristic")
    return None


def parse_copy(raw: str) -> ParsedCopy:
    """Extract caption + hashtags from a model reply.

    Raises:
        OllamaResponseError: when no caption can be recovered.
    """
    text = clean_model_text(raw)
    for strategy in (_parse_json, _parse_labeled, _parse_heuristic):
        parsed = strategy(text)
        if parsed is not None:
            return parsed
    raise OllamaResponseError("The AI response did not contain a caption.")


def finalize_copy(
    parsed: ParsedCopy, request: CopyRequest, *, model: str, repaired: bool = False
) -> GeneratedCopy:
    """Apply the length/count contract of :class:`CopyRequest` to parsed output."""
    caption = truncate_words(normalize_caption(parsed.caption), request.caption_max_chars)
    if not caption:
        raise OllamaResponseError("The AI response contained an empty caption.")
    hashtags = list(parsed.hashtags[: request.max_hashtags])
    if len(hashtags) < request.min_hashtags:
        seen = {tag.casefold() for tag in hashtags}
        for keyword in request.keywords:
            tag = keyword_to_hashtag(keyword)
            if tag and tag.casefold() not in seen:
                seen.add(tag.casefold())
                hashtags.append(tag)
            if len(hashtags) >= request.min_hashtags:
                break
    return GeneratedCopy(
        caption=caption,
        hashtags=tuple(hashtags),
        model=model,
        strategy=parsed.strategy,
        repaired=repaired,
    )


# --------------------------------------------------------------------------- client
class OllamaClient:
    """Async client for Ollama Cloud's chat API."""

    def __init__(
        self,
        config: OllamaConfig,
        http_client: httpx.AsyncClient,
        *,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        self.config = config
        self._http = http_client
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return self.config.configured

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _payload(self, messages: list[ChatMessage]) -> dict[str, Any]:
        if self.config.api_style == "native":
            return {
                "model": self.config.model,
                "messages": messages,  # native messages carry ``images`` as-is
                "stream": False,
                "options": {
                    "temperature": self.config.temperature,
                    "num_predict": self.config.max_tokens,
                },
            }
        return {
            "model": self.config.model,
            "messages": [self._openai_message(message) for message in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }

    @staticmethod
    def _openai_message(message: ChatMessage) -> dict[str, Any]:
        """OpenAI-style message: images become ``image_url`` content parts."""
        images = message.get("images")
        if not images:
            return {"role": message["role"], "content": message["content"]}
        parts: list[dict[str, Any]] = [{"type": "text", "text": message["content"]}]
        parts.extend(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}
            for image in images
        )
        return {"role": message["role"], "content": parts}

    async def chat(self, messages: list[ChatMessage]) -> str:
        """Send a chat conversation and return the assistant's text."""
        if not self.configured:
            raise OllamaNotConfiguredError("The AI service is not configured (set OLLAMA_API_KEY).")
        payload = self._payload(messages)

        async def attempt() -> str:
            return await self._send(payload)

        return await retry_async(
            attempt,
            policy=self.config.retry,
            is_retryable=lambda exc: isinstance(exc, OllamaError) and exc.retryable,
            sleep=self._sleep,
            description="Ollama chat request",
        )

    async def _send(self, payload: dict[str, Any]) -> str:
        timeout = httpx.Timeout(
            self.config.timeout_seconds, connect=min(10.0, self.config.timeout_seconds)
        )
        try:
            response = await self._http.post(
                self.config.endpoint, json=payload, headers=self._headers(), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise OllamaError("The AI service timed out.", retryable=True) from exc
        except httpx.TransportError as exc:
            raise OllamaError("Could not reach the AI service.", retryable=True) from exc

        status = response.status_code
        if status in (401, 403):
            raise OllamaAuthError("The AI service rejected the API key.", status_code=status)
        if status == 402:
            raise OllamaError(
                f"The AI model '{self.config.model}' is not included in your plan; "
                "choose another model.",
                status_code=status,
            )
        if status == 404:
            raise OllamaError(
                f"The AI model '{self.config.model}' or endpoint was not found.",
                status_code=status,
            )
        if status in RETRYABLE_STATUS_CODES:
            raise OllamaError(
                f"The AI service is busy (HTTP {status}).",
                retryable=True,
                status_code=status,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if response.is_error:
            raise OllamaError(
                f"The AI service returned an error (HTTP {status}).", status_code=status
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise OllamaResponseError("The AI service returned invalid JSON.") from exc
        return self._extract_content(data)

    def _extract_content(self, data: Any) -> str:
        if not isinstance(data, dict):
            raise OllamaResponseError("Unexpected response from the AI service.")
        if data.get("error"):
            raise OllamaError(f"The AI service reported an error: {data['error']}")

        finish_reason: str | None = None
        content: Any = None
        if isinstance(data.get("choices"), list) and data["choices"]:
            choice = data["choices"][0] or {}
            content = (choice.get("message") or {}).get("content")
            finish_reason = choice.get("finish_reason")
        elif isinstance(data.get("message"), dict):
            content = data["message"].get("content")
            finish_reason = data.get("done_reason")

        if isinstance(content, str) and content.strip():
            return content
        if finish_reason == "length":
            raise OllamaResponseError(
                "The AI model ran out of tokens before answering; increase OLLAMA_MAX_TOKENS."
            )
        raise OllamaResponseError("The AI service returned an empty answer.")

    async def generate_copy(self, request: CopyRequest) -> GeneratedCopy:
        """Generate a caption and hashtags for the given visual concepts."""
        messages = build_copy_messages(request)
        raw = await self.chat(messages)
        try:
            parsed = parse_copy(raw)
        except OllamaResponseError:
            logger.warning("Unparseable AI reply; asking the model to repair it")
            repair: list[ChatMessage] = [
                *messages,
                {"role": "assistant", "content": raw},
                {"role": "user", "content": REPAIR_PROMPT},
            ]
            raw = await self.chat(repair)
            return finalize_copy(parse_copy(raw), request, model=self.config.model, repaired=True)
        return finalize_copy(parsed, request, model=self.config.model)
