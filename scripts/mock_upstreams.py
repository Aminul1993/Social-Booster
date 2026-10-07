"""Local stand-ins for Ollama Cloud and Buffer - demo and UI-test the full flow offline.

Buffer API clients need a Buffer account and Ollama Cloud needs a paid key, so
this tiny server mimics Buffer's OAuth (with PKCE) and GraphQL endpoints and
the chat completions endpoint the app uses. Nothing is posted anywhere.

    python scripts/mock_upstreams.py --port 8020

Then start the app with (e.g. in .env):

    OLLAMA_ENDPOINT=http://127.0.0.1:8020/v1/chat/completions
    OLLAMA_API_KEY=mock
    BUFFER_CLIENT_ID=mock
    BUFFER_CLIENT_SECRET=mock
    BUFFER_OAUTH_URL=http://127.0.0.1:8020/auth
    BUFFER_TOKEN_URL=http://127.0.0.1:8020/token
    BUFFER_API_URL=http://127.0.0.1:8020/graphql

Received posts are listed at http://127.0.0.1:8020/posts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import secrets
import uuid
from typing import Any
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Mock Ollama + Buffer")
POSTS: list[dict[str, Any]] = []
CODES: dict[str, str] = {}  # authorization code -> PKCE code_challenge
CHANNELS = [
    {"id": "mock-instagram", "service": "instagram", "name": "acme.studio", "avatar": ""},
    {"id": "mock-linkedin", "service": "linkedin", "name": "Acme Studio", "avatar": ""},
    {"id": "mock-x", "service": "twitter", "name": "acme", "avatar": ""},
]


@app.post("/v1/chat/completions")
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    prompt = body["messages"][-1]["content"]
    match = re.search(r"(\[.*?\])", prompt)
    keywords: list[str] = json.loads(match.group(1)) if match else ["photo"]
    subject = keywords[0] if keywords else "this moment"
    tags = [f"#{re.sub(r'[^0-9A-Za-z]', '', k).lower()}" for k in keywords][:4]
    tags += ["#instagood", "#photooftheday", "#weekendvibes"]
    content = json.dumps(
        {"caption": f"Say hello to {subject} - pure good vibes, zero filter.", "hashtags": tags}
    )
    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
    )


@app.get("/auth", response_class=HTMLResponse)
async def authorize(
    redirect_uri: str, state: str, code_challenge: str, client_id: str = ""
) -> HTMLResponse:
    code = secrets.token_urlsafe(16)
    CODES[code] = code_challenge
    allow = f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"
    deny = f"{redirect_uri}?{urlencode({'error': 'access_denied', 'error_description': 'The user denied access', 'state': state})}"
    return HTMLResponse(
        "<!doctype html><title>Mock Buffer</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto'>"
        f"<h1>Mock Buffer</h1><p>App <b>{html.escape(client_id)}</b> wants to post on your behalf.</p>"
        f"<p><a id='allow' href='{html.escape(allow)}'>Authorize</a> &middot; "
        f"<a id='deny' href='{html.escape(deny)}'>Deny</a></p></body>"
    )


@app.post("/token", response_model=None)
async def token(request: Request) -> dict[str, Any] | JSONResponse:
    form = await request.form()
    if form.get("grant_type") == "authorization_code":
        challenge = CODES.pop(str(form.get("code")), None)
        digest = hashlib.sha256(str(form.get("code_verifier", "")).encode()).digest()
        if challenge != base64.urlsafe_b64encode(digest).rstrip(b"=").decode():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    return {
        "access_token": f"mock-access-{secrets.token_hex(4)}",
        "refresh_token": f"mock-refresh-{secrets.token_hex(4)}",
        "token_type": "Bearer",
        "expires_in": 3600,
    }


@app.post("/graphql")
async def graphql(request: Request) -> dict[str, Any]:
    body = await request.json()
    query: str = body["query"]
    variables: dict[str, Any] = body.get("variables") or {}
    if "createPost" in query:
        POSTS.append(variables["input"])
        post = {"id": uuid.uuid4().hex[:12]}
        return {"data": {"createPost": {"__typename": "PostActionSuccess", "post": post}}}
    if "channels(" in query:
        return {"data": {"channels": CHANNELS}}
    return {"data": {"account": {"organizations": [{"id": "mock-org"}]}}}


@app.get("/posts")
async def posts() -> list[dict[str, Any]]:
    return POSTS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
