"""Shared test doubles, payload builders and an HTMX-aware test client."""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from PIL import Image

from services.vision import VisionLabel

OLLAMA_URL = "https://ollama.test/v1/chat/completions"
BUFFER_OAUTH_URL = "https://buffer.test/oauth2/authorize"
BUFFER_TOKEN_URL = "https://api.buffer.test/1/oauth2/token.json"
BUFFER_PROFILES_URL = "https://api.buffer.test/1/profiles.json"
BUFFER_POST_URL = "https://api.buffer.test/1/updates/create.json"

PROFILES_PAYLOAD: list[dict[str, Any]] = [
    {
        "id": "prof-ig",
        "service": "instagram",
        "formatted_username": "@acme",
        "avatar_https": "https://cdn.buffer.test/a.png",
    },
    {"id": "prof-x", "service": "twitter", "service_username": "acme"},
]

_CSRF_RE = re.compile(r'"X-CSRF-Token": "([^"]+)"')


class FakeClassifier:
    """Deterministic stand-in for ResNet-50."""

    labels: tuple[VisionLabel, ...] = (
        VisionLabel("golden_retriever", 0.8213),
        VisionLabel("tennis ball", 0.0912),
        VisionLabel("Labrador retriever", 0.0411),
    )

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, image: Image.Image, top_k: int) -> list[VisionLabel]:
        self.calls += 1
        return list(self.labels[:top_k])


class FailingClassifier:
    def predict(self, image: Image.Image, top_k: int) -> list[VisionLabel]:
        raise RuntimeError("inference exploded")


def make_image(
    fmt: str = "PNG",
    size: tuple[int, int] = (64, 48),
    color: tuple[int, int, int] = (200, 30, 30),
    **save_kwargs: Any,
) -> bytes:
    buffer = io.BytesIO()
    mode = "RGBA" if fmt == "PNG" and save_kwargs.pop("alpha", False) else "RGB"
    Image.new(mode, size, color).save(buffer, format=fmt, **save_kwargs)
    return buffer.getvalue()


def ollama_reply(content: str, *, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }


GOOD_COPY = json.dumps(
    {
        "caption": "Fetch mode: activated. Pure joy on four paws.",
        "hashtags": ["#dogsofinstagram", "#goldenretriever", "#puppylove", "#doglife", "#fetch"],
    }
)


def form_body(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


def query_params(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


class AppClient:
    """Wraps ``httpx.AsyncClient`` with the CSRF/HTMX headers the UI sends."""

    def __init__(self, http: httpx.AsyncClient, app: FastAPI) -> None:
        self.http = http
        self.app = app
        self.csrf = ""

    @property
    def container(self) -> Any:
        return self.app.state.container

    @property
    def session_id(self) -> str:
        """Server-side session id, read from the (signed, base64 JSON) session cookie."""
        raw = self.http.cookies.get("mab_session")
        assert raw, "no session cookie"
        payload = raw.split(".")[0]
        payload += "=" * (-len(payload) % 4)
        sid: str = json.loads(base64.b64decode(payload))["sid"]
        return sid

    async def open(self) -> httpx.Response:
        response = await self.http.get("/")
        match = _CSRF_RE.search(response.text)
        assert match, "CSRF token not rendered"
        self.csrf = match.group(1)
        return response

    def htmx_headers(self, **extra: str) -> dict[str, str]:
        return {"HX-Request": "true", "X-CSRF-Token": self.csrf, **extra}

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.http.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.htmx_headers(), **kwargs.pop("headers", {})}
        return await self.http.post(url, headers=headers, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.htmx_headers(), **kwargs.pop("headers", {})}
        return await self.http.patch(url, headers=headers, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.htmx_headers(), **kwargs.pop("headers", {})}
        return await self.http.delete(url, headers=headers, **kwargs)

    async def upload(self, *files: tuple[str, bytes, str]) -> httpx.Response:
        return await self.post("/upload", files=[("files", file) for file in files])

    async def upload_one(self, name: str = "dog.png") -> str:
        """Upload one image and return the new draft id."""
        response = await self.upload((name, make_image(), "image/png"))
        assert response.status_code == 200, response.text
        match = re.search(r'id="draft-([0-9a-f]{32})"', response.text)
        assert match
        return match.group(1)

    async def connect_buffer(self, *, code: str = "auth-code") -> httpx.Response:
        """Run the OAuth round trip (token endpoint must be mocked)."""
        start = await self.get("/buffer/auth")
        assert start.status_code == 303
        state = query_params(start.headers["location"])["state"]
        return await self.get("/buffer/callback", params={"code": code, "state": state})


def toast(response: httpx.Response) -> dict[str, str]:
    """Decode the ``HX-Trigger`` toast payload of a response."""
    header = response.headers.get("HX-Trigger")
    assert header, f"no HX-Trigger header (status {response.status_code})"
    payload: dict[str, dict[str, str]] = json.loads(header)
    return payload["app:toast"]


@asynccontextmanager
async def make_client(app: FastAPI) -> AsyncIterator[AppClient]:
    """Run the app's lifespan and open a browser-like session (GET /)."""
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            app_client = AppClient(http, app)
            await app_client.open()
            yield app_client


@asynccontextmanager
async def make_raw_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Lifespan-managed client without the initial page load."""
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
