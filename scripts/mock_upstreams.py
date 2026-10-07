"""Local stand-ins for Ollama Cloud and Buffer - demo and UI-test the full flow offline.

Buffer no longer hands out API apps to everyone and Ollama Cloud needs a paid
key, so this tiny server mimics the four Buffer endpoints and the chat
completions endpoint the app uses. Nothing is posted anywhere.

    python scripts/mock_upstreams.py --port 8020

Then start the app with (e.g. in .env):

    OLLAMA_ENDPOINT=http://127.0.0.1:8020/v1/chat/completions
    OLLAMA_API_KEY=mock
    BUFFER_CLIENT_ID=mock
    BUFFER_CLIENT_SECRET=mock
    BUFFER_OAUTH_URL=http://127.0.0.1:8020/oauth2/authorize
    BUFFER_TOKEN_URL=http://127.0.0.1:8020/1/oauth2/token.json
    BUFFER_PROFILES_URL=http://127.0.0.1:8020/1/profiles.json
    BUFFER_POST_URL=http://127.0.0.1:8020/1/updates/create.json

Received posts are listed at http://127.0.0.1:8020/posts.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import uuid
from typing import Any
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Mock Ollama + Buffer")
POSTS: list[dict[str, Any]] = []
PROFILES = [
    {
        "id": "mock-instagram",
        "service": "instagram",
        "formatted_username": "@acme.studio",
    },
    {"id": "mock-linkedin", "service": "linkedin", "formatted_username": "Acme Studio"},
    {"id": "mock-x", "service": "twitter", "formatted_username": "@acme"},
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


@app.get("/oauth2/authorize", response_class=HTMLResponse)
async def authorize(redirect_uri: str, state: str, client_id: str = "") -> HTMLResponse:
    allow = f"{redirect_uri}?{urlencode({'code': 'mock-code', 'state': state})}"
    deny = f"{redirect_uri}?{urlencode({'error': 'access_denied', 'error_description': 'The user denied access', 'state': state})}"
    return HTMLResponse(
        "<!doctype html><title>Mock Buffer</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto'>"
        f"<h1>Mock Buffer</h1><p>App <b>{html.escape(client_id)}</b> wants to post on your behalf.</p>"
        f"<p><a id='allow' href='{html.escape(allow)}'>Authorize</a> &middot; "
        f"<a id='deny' href='{html.escape(deny)}'>Deny</a></p></body>"
    )


@app.post("/1/oauth2/token.json")
async def token() -> dict[str, str]:
    return {"access_token": "1/mock-access-token", "token_type": "bearer"}


@app.get("/1/profiles.json")
async def profiles() -> list[dict[str, str]]:
    return PROFILES


@app.post("/1/updates/create.json")
async def create_update(request: Request) -> dict[str, Any]:
    form = await request.form()
    profile_ids = form.getlist("profile_ids[]")
    post = {key: form.getlist(key) for key in form}
    POSTS.append(post)
    return {
        "success": True,
        "buffer_count": len(POSTS),
        "message": "One more post in your Buffer.",
        "updates": [{"id": uuid.uuid4().hex[:12], "profile_id": pid} for pid in profile_ids],
    }


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
