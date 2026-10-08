"""Shared test doubles, payload builders and an HTMX-aware test client."""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from PIL import Image

from services.vision import ImageAnalysis

OLLAMA_URL = "https://ollama.test/v1/chat/completions"
BUFFER_API_URL = "https://api.buffer.test/graphql"
BUFFER_ACCESS_TOKEN = "buffer-personal-key"

CHANNELS_PAYLOAD: list[dict[str, Any]] = [
    {
        "id": "prof-ig",
        "service": "instagram",
        "name": "acme_ig",
        "displayName": "@acme",
        "avatar": "https://cdn.buffer.test/a.png",
        "isDisconnected": False,
        "isLocked": False,
    },
    {
        "id": "prof-x",
        "service": "twitter",
        "name": "acme",
        "displayName": None,
        "avatar": "",
        "isDisconnected": False,
        "isLocked": False,
    },
]


class FakeBufferAPI:
    """Stand-in for Buffer's GraphQL endpoint, used as a respx side effect."""

    def __init__(
        self,
        channels: list[Any] | None = None,  # Any: tests also feed malformed entries
        organizations: tuple[str, ...] = ("org-1",),
    ) -> None:
        self.channels = CHANNELS_PAYLOAD if channels is None else channels
        self.organizations = organizations
        self.posts: list[dict[str, Any]] = []  # createPost inputs that succeeded
        self.refusals: dict[str, str] = {}  # channelId -> MutationError message
        self.requests: list[httpx.Request] = []
        self.post_attempts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        query: str = body["query"]
        variables: dict[str, Any] = body.get("variables") or {}
        if "createPost" in query:
            self.post_attempts += 1
            values = variables["input"]
            if message := self.refusals.get(values["channelId"]):
                result: dict[str, Any] = {"__typename": "InvalidInputError", "message": message}
            else:
                self.posts.append(values)
                post = {"id": f"post-{len(self.posts)}"}
                result = {"__typename": "PostActionSuccess", "post": post}
            return httpx.Response(200, json={"data": {"createPost": result}})
        if "channels(" in query:
            return httpx.Response(200, json={"data": {"channels": self.channels}})
        if "organizations" in query:
            orgs = [{"id": org} for org in self.organizations]
            return httpx.Response(200, json={"data": {"account": {"organizations": orgs}}})
        return httpx.Response(200, json={"errors": [{"message": f"Unknown query: {query}"}]})


_CSRF_RE = re.compile(r'"X-CSRF-Token": "([^"]+)"')


class FakeDescriber:
    """Deterministic stand-in for the online vision model."""

    configured = True
    model = "fake-vision"

    def __init__(self, analysis: ImageAnalysis | None = None) -> None:
        self.analysis = analysis or ImageAnalysis(
            keywords=["golden retriever", "tennis ball", "Labrador retriever"],
            description="A golden retriever chases a tennis ball across a sunny lawn.",
        )
        self.calls: list[tuple[tuple[int, int], int]] = []

    async def describe(self, image: Image.Image, max_keywords: int) -> ImageAnalysis:
        self.calls.append((image.size, max_keywords))
        return self.analysis


class FailingDescriber(FakeDescriber):
    async def describe(self, image: Image.Image, max_keywords: int) -> ImageAnalysis:
        raise RuntimeError("model exploded")


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
